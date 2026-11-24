import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from slideguard.adapters.ooxml.details import enrich_snapshot
from slideguard.domain.models import DeckSnapshot

from .autofit import refresh_shrunk_text
from .copies import saved_pptx_copy
from .image_text import read_image_text
from .render import render_evidence
from .session import PowerPointReadSession
from .snapshot import extract_snapshot

LOGGER = logging.getLogger("slideguard.reader")

# 같은 파일 내용(SHA256)을 이미 PowerPoint로 읽었으면 그 스냅샷을 다시 쓴다 (앱이 켜져 있는 동안만).
# 설정(색약 맞춤·청중·기준값 등)을 바꾸면 모든 파일을 다시 검사하는데, 바뀐 건 '판정 방법'이지
# 파일 내용이 아니라서 PowerPoint로 다시 열고 그리고 문자 인식할 필요가 없다 (몇 분 → 몇 초).
# 그림 속 글자 검사를 새로 켠 경우만 예외: 이전 스냅샷에는 문자 인식 결과가 없어서 다시 읽음.
_SNAPSHOT_CACHE: OrderedDict[tuple, tuple[DeckSnapshot, bool]] = OrderedDict()
_SNAPSHOT_CACHE_SIZE = 40
_SNAPSHOT_CACHE_LOCK = threading.Lock()


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def clear_snapshot_cache() -> None:
    with _SNAPSHOT_CACHE_LOCK:
        _SNAPSHOT_CACHE.clear()


class PowerPointDeckReader:
    def __init__(
        self,
        session: PowerPointReadSession,
        *,
        render: bool = False,
        wants_image_text: Callable[[Path], bool] | None = None,
    ):
        self._session = session
        self._render = render
        # 파일마다 '그림 속 글자 검사'를 할지 (설정에서 끄면 문자 인식 단계를 아예 건너뜀)
        self._wants_image_text = wants_image_text

    def read(self, path: Path) -> DeckSnapshot:
        """같은 내용을 이미 읽었으면 그 스냅샷, 아니면 PowerPoint로 읽는다."""
        wants_ocr = self._render and self._image_text_on(path)
        sha = _file_sha256(path)
        key = (str(Path(path).resolve()), sha, self._render)
        if sha is not None:
            with _SNAPSHOT_CACHE_LOCK:
                cached = _SNAPSHOT_CACHE.get(key)
            if cached is not None and (cached[1] or not wants_ocr):
                LOGGER.info(
                    "SNAPSHOT_REUSED | file=%s | 같은 내용을 이미 읽음 (설정만 바뀜)", Path(path).name
                )
                return cached[0]
        snapshot = self._read_with_powerpoint(path)
        if snapshot.source_sha256 == sha:
            with _SNAPSHOT_CACHE_LOCK:
                _SNAPSHOT_CACHE[key] = (snapshot, wants_ocr)
                _SNAPSHOT_CACHE.move_to_end(key)
                while len(_SNAPSHOT_CACHE) > _SNAPSHOT_CACHE_SIZE:
                    _SNAPSHOT_CACHE.popitem(last=False)
        return snapshot

    def _read_with_powerpoint(self, path: Path) -> DeckSnapshot:
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
                    # 그림 속 글자 (Windows 문자 인식): 그림이 있는 슬라이드만
                    step = perf_counter()
                    if self._image_text_on(path):
                        snapshot = read_image_text(presentation, snapshot, saved.copy_path.parent)
                    timing["ocr"] = perf_counter() - step
                step = perf_counter()
                result = enrich_snapshot(snapshot, saved.copy_path)
                timing["enrich"] = perf_counter() - step
                return result

            if self._render:
                snapshot = self._session.read_copy(saved.copy_path, extract, discard_probe_edits=True)
            else:
                snapshot = self._session.read_copy(saved.copy_path, extract)
        total = perf_counter() - started
        inside = sum(timing.get(key, 0.0) for key in ("extract", "render", "ocr", "enrich"))
        LOGGER.info(
            "READ_TIMING | file=%s | 복사=%.2fs PowerPoint열기·닫기=%.2fs 도형읽기=%.2fs "
            "그리기=%.2fs(슬라이드 %s장, 내보내기 %s번, 표칸 측정 %s개) 그림글자=%.2fs 파일보강=%.2fs 합계=%.2fs",
            Path(path).name,
            timing.get("copy", 0.0),
            max(total - timing.get("copy", 0.0) - inside, 0.0),
            timing.get("extract", 0.0),
            timing.get("render", 0.0),
            stats.get("slides", 0),
            stats.get("exports", 0),
            stats.get("cells", 0),
            timing.get("ocr", 0.0),
            timing.get("enrich", 0.0),
            total,
        )
        return snapshot

    def _image_text_on(self, path: Path) -> bool:
        if self._wants_image_text is None:
            return True
        try:
            return bool(self._wants_image_text(path))
        except Exception:  # noqa: BLE001 - 설정을 못 읽으면 기본(검사함)
            return True
