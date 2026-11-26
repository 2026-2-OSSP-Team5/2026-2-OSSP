import logging
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from pptx import Presentation

from slideguard.adapters.ooxml.details import enrich_snapshot
from slideguard.application.ports import AnalysisProgress, ReadInterrupted
from slideguard.domain.models import DeckSnapshot

from .autofit import refresh_shrunk_text
from .copies import saved_pptx_copy
from .incremental import measurement_key, slide_fingerprints
from .render import render_evidence
from .session import PowerPointReadSession, _plain_snapshot
from .snapshot import extract_snapshot

LOGGER = logging.getLogger("slideguard.reader")


class PowerPointDeckReader:
    def __init__(self, session: PowerPointReadSession, *, render: bool = False, baseline_provider=None):
        self._session = session
        self._render = render
        self._baseline_provider = baseline_provider
        self._slide_cache = OrderedDict()

    def read_incremental(self, path, *, should_yield=lambda: False, on_progress=None, slide_numbers=None):
        """Read changed slides; yield only between complete, detached slide measurements."""
        started = perf_counter()
        with saved_pptx_copy(path) as saved:
            key = measurement_key(self._render)
            fingerprints = slide_fingerprints(saved.copy_path)
            total = len(fingerprints)
            selected = tuple(range(1, total + 1) if slide_numbers is None else slide_numbers)
            if any(not 1 <= number <= total for number in selected):
                raise ValueError("Requested slide is outside the presentation")
            if self._baseline_provider is not None:
                baseline = self._baseline_provider(saved.source_path)
                if (
                    baseline is not None
                    and baseline.measurement_key == key
                    and len(baseline.slide_fingerprints) == baseline.slide_count
                ):
                    for number, (slide_id, digest) in enumerate(baseline.slide_fingerprints, 1):
                        if digest:
                            self._remember(
                                (str(saved.source_path), slide_id, digest, key),
                                replace(
                                    baseline,
                                    shapes=tuple(s for s in baseline.shapes if s.slide_number == number),
                                    slides=tuple(s for s in baseline.slides if s.number == number),
                                ),
                            )
            parts = {}
            changed = []
            reused = 0

            def progress(stage, completed=0):
                if on_progress is not None:
                    on_progress(AnalysisProgress(saved.source_path, stage, completed, len(selected), reused))

            def checkpoint():
                if should_yield():
                    raise ReadInterrupted()

            checkpoint()
            for number in selected:
                slide_id, digest = fingerprints[number - 1]
                cache_key = (str(saved.source_path), slide_id, digest, key)
                cached = self._slide_cache.get(cache_key) if digest else None
                if cached is None:
                    changed.append(number)
                else:
                    parts[number] = replace(
                        cached,
                        shapes=tuple(replace(s, slide_number=number) for s in cached.shapes),
                        slides=tuple(replace(s, number=number) for s in cached.slides),
                    )
                    reused += 1
            progress("compare", reused)

            def extract(presentation):
                metadata = Presentation(saved.copy_path)
                for number in changed:
                    checkpoint()
                    progress(f"read:{number}", len(parts))
                    part = extract_snapshot(presentation, saved, slide_numbers=(number,))
                    if self._render:
                        part = refresh_shrunk_text(presentation, part)
                        progress(f"render:{number}", len(parts))
                        part = render_evidence(presentation, part, saved.copy_path.with_suffix(".png"))
                    part = enrich_snapshot(
                        part, saved.copy_path, slide_numbers=(number,), presentation=metadata
                    )
                    part = _plain_snapshot(part)
                    parts[number] = part
                    slide_id, digest = fingerprints[number - 1]
                    if digest:
                        self._remember((str(saved.source_path), slide_id, digest, key), part)
                    progress("read", len(parts))
                    checkpoint()
                return combine()

            def combine():
                template = parts[selected[0]]
                return replace(
                    template,
                    source_path=str(saved.source_path),
                    source_sha256=saved.source_sha256,
                    slide_count=total,
                    shapes=tuple(s for n in selected for s in parts[n].shapes),
                    slides=tuple(s for n in selected for s in parts[n].slides),
                    slide_fingerprints=fingerprints if slide_numbers is None else (),
                    measurement_key=key,
                )

            if changed or not parts:
                if not parts and not changed:
                    return self.read(path)
                if self._render:
                    snapshot = self._session.read_copy(saved.copy_path, extract, discard_probe_edits=True)
                else:
                    snapshot = self._session.read_copy(saved.copy_path, extract)
            else:
                snapshot = combine()
            checkpoint()
            progress("check", len(selected))
        LOGGER.info(
            "INCREMENTAL_READ | file=%s | changed=%d reused=%d total=%d | duration=%.2fs",
            Path(path).name,
            len(changed),
            reused,
            len(selected),
            perf_counter() - started,
        )
        return snapshot

    def _remember(self, key, snapshot):
        self._slide_cache[key] = snapshot
        self._slide_cache.move_to_end(key)
        while len(self._slide_cache) > 128:
            self._slide_cache.popitem(last=False)

    def read(self, path: Path) -> DeckSnapshot:
        """읽기 단계별 시간을 slideguard.log에 READ_TIMING으로 남긴다 (어디가 느린지 찾기 위해)."""
        started = perf_counter()
        timing: dict[str, float] = {}
        stats: dict[str, int] = {}
        with saved_pptx_copy(path) as saved:
            timing["copy"] = perf_counter() - started

            def extract(presentation):
                step = perf_counter()
                snapshot = extract_snapshot(presentation, saved)
                if self._render:
                    # 자동 축소 상자는 파일에 저장된 축소 비율이 화면과 다를 수 있어 직접 계산한다
                    # (사본을 잠깐 고치므로 버리는 사본 모드에서만)
                    snapshot = refresh_shrunk_text(presentation, snapshot)
                timing["extract"] = perf_counter() - step
                if self._render:
                    step = perf_counter()
                    snapshot = render_evidence(
                        presentation, snapshot, saved.copy_path.with_suffix(".png"), stats=stats
                    )
                    timing["render"] = perf_counter() - step
                step = perf_counter()
                result = enrich_snapshot(snapshot, saved.copy_path)
                timing["enrich"] = perf_counter() - step
                return result

            if self._render:
                snapshot = self._session.read_copy(saved.copy_path, extract, discard_probe_edits=True)
            else:
                snapshot = self._session.read_copy(saved.copy_path, extract)
        total = perf_counter() - started
        inside = sum(timing.get(key, 0.0) for key in ("extract", "render", "enrich"))
        LOGGER.info(
            "READ_TIMING | file=%s | 복사=%.2fs PowerPoint열기·닫기=%.2fs 도형읽기=%.2fs "
            "그리기=%.2fs(슬라이드 %s장, 내보내기 %s번, 표칸 측정 %s개) 파일보강=%.2fs 합계=%.2fs",
            Path(path).name,
            timing.get("copy", 0.0),
            max(total - timing.get("copy", 0.0) - inside, 0.0),
            timing.get("extract", 0.0),
            timing.get("render", 0.0),
            stats.get("slides", 0),
            stats.get("exports", 0),
            stats.get("cells", 0),
            timing.get("enrich", 0.0),
            total,
        )
        return snapshot
