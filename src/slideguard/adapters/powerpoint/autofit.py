"""'넘치면 글자 자동 축소' 상자의 실제 글자 크기를 직접 계산한다.

문제 (함정 PPT 4쪽: 화면에는 20pt인데 13pt로 판정)
  PPT 파일에는 자동 축소 비율(예: 55%)이 저장되어 있고, PowerPoint의 글자 크기 값도 그 비율로 줄어든
  크기(13.2pt)를 돌려준다. 그런데 이 비율은 실제와 다를 수 있다 (다른 프로그램이 만든 파일, 축소 뒤
  상자 크기를 바꾼 파일 등). PowerPoint는 화면에 그릴 때 비율을 다시 계산하지만(→ 20pt),
  창 없이 연 검사용 사본은 다시 계산하지 않아 저장된 값을 그대로 돌려준다.

해결: 저장된 비율을 믿지 않고 PowerPoint가 하는 일을 검사용 사본에서 직접 해 본다.
  1) 자동 축소를 끄고 원래 글자 크기(24pt)를 읽는다
  2) 글자를 100%, 90%, 85% ... 로 줄여 가며 PowerPoint가 잰 글자 높이(BoundHeight)가
     상자 안쪽 높이에 들어가는 가장 큰 비율을 찾는다
     (PowerPoint는 글자와 함께 줄 간격도 최대 20%까지 줄이므로 그만큼 여유를 준다)
  3) 원래 크기 × 그 비율을 실제 글자 크기로 쓰고, 사본을 원래대로 되돌린다
  사본은 저장하지 않고 버리는 파일이라 원본에는 아무 영향이 없다.
  계산 결과는 slideguard.log에 AUTOFIT_FIT으로 남긴다.
"""

import logging
from dataclasses import replace

from .patcher import _ComShape
from .pixel_bounds import _item, _positions

LOGGER = logging.getLogger("slideguard.reader")

SHRINK = 2  # msoAutoSizeTextToFitShape (넘치면 글자 자동 축소)
NONE = 0  # msoAutoSizeNone
SCALES = (1.0, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.45, 0.4, 0.35, 0.3, 0.25)
LINE_REDUCTION = 0.8  # PowerPoint가 줄 간격을 최대 20% 줄여 맞추는 몫
CHANGE_RATIO = 0.03  # 3% 넘게 달라져야 고친다 (반올림 차이 무시)


def _fit_scale(item, runs, bases) -> float | None:
    """원래 크기(bases)에서 몇 %로 줄여야 상자에 들어가는지. 재지 못하면 None."""
    frame = item.TextFrame2
    inner = float(item.Height) - float(frame.MarginTop) - float(frame.MarginBottom)
    if inner <= 0:
        return None
    for scale in SCALES:
        for run, base in zip(runs, bases, strict=True):
            if base:
                run.set_size(round(base * scale, 1))
        needed = float(frame.TextRange.BoundHeight)
        if needed * LINE_REDUCTION <= inner:
            return scale
    return SCALES[-1]


def _measure(item, slide_height):
    """(저장된 크기 목록, 원래 크기 목록, 맞는 비율). 무엇이든 실패하면 None. 사본은 원래대로 되돌린다."""
    shape = _ComShape(item, slide_height)
    frame = item.TextFrame2
    stored = [run.get_size() for run in shape.runs()]
    frame.AutoSize = NONE  # 축소를 끄면 PowerPoint가 원래 크기로 돌려준다
    try:
        runs = shape.runs()
        bases = [run.get_size() for run in runs]
        if len(bases) != len(stored) or not any(bases):
            return None
        try:
            scale = _fit_scale(item, runs, bases)
        finally:
            for run, base in zip(runs, bases, strict=True):
                if base:
                    run.set_size(base)
        return stored, bases, scale
    finally:
        frame.AutoSize = SHRINK


def refresh_shrunk_text(presentation, snapshot):
    """자동 축소 상자의 글자 크기를 직접 계산한 값으로 맞춘 스냅샷을 돌려준다.

    검사용 사본을 잠깐 고치므로, 저장하지 않고 버리는 사본(render 모드)에서만 부른다.
    """
    slide_height = float(snapshot.height_pt)
    updated = {}
    for shape in snapshot.shapes:
        if not shape.text.strip() or (shape.locator is not None and shape.locator.kind != "shape"):
            continue
        if not any(run.font_size_pt for run in shape.runs):
            continue
        path = shape.locator.shape_path if shape.locator is not None else (shape.shape_id,)
        try:
            slide = presentation.Slides.Item(shape.slide_number)
            positions = _positions(slide, path)
            if not positions:
                continue
            item = _item(slide, positions)
            if int(item.TextFrame2.AutoSize) != SHRINK:
                continue
            measured = _measure(item, slide_height)
        except Exception as error:  # noqa: BLE001 - COM 오류면 원래 값 그대로
            LOGGER.info("AUTOFIT_FIT | shape=%s | 계산 실패: %s", shape.key, error)
            continue
        if measured is None:
            continue
        stored, bases, scale = measured
        effective = [round(base * scale, 1) if base else None for base in bases]
        LOGGER.info(
            "AUTOFIT_FIT | shape=%s | 파일에 저장된 크기 %s → 원래 %s × %d%% = %s",
            shape.key,
            stored,
            bases,
            round(scale * 100),
            effective,
        )
        first_stored = next((size for size in stored if size), None)
        first_effective = next((size for size in effective if size), None)
        if not first_stored or not first_effective:
            continue
        if abs(first_effective - first_stored) <= first_stored * CHANGE_RATIO:
            continue
        ratio = first_effective / first_stored
        runs = tuple(
            replace(run, font_size_pt=round(run.font_size_pt * ratio, 1)) if run.font_size_pt else run
            for run in shape.runs
        )
        updated[shape.key] = replace(shape, runs=runs)
    if not updated:
        return snapshot
    return replace(snapshot, shapes=tuple(updated.get(shape.key, shape) for shape in snapshot.shapes))
