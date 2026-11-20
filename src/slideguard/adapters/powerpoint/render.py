"""Sample the rendered neighborhood of text as review evidence."""

from dataclasses import replace
from itertools import product

from slideguard.domain.models import Measurement

from .pixel_bounds import export_slide, measure_table_cells


def _with_cell_box(shape, text_box):
    measurement = Measurement(
        "text_bounds",
        "measured" if text_box else "unavailable",
        "Slide PNG difference after clearing cells one by one on a probe slide (fixed table geometry)",
    )
    return replace(
        shape,
        rendered_box=text_box,
        visible_text_box=text_box,
        measurements=tuple(item for item in shape.measurements if item.field != "text_bounds")
        + (measurement,),
    )


def _needs_pixels(shape) -> bool:
    """이 객체 때문에 슬라이드를 그려야 하는지: 표 칸 글자, 또는 배경색을 모르는 글상자."""
    if shape.locator.kind == "table_cell":
        return bool(shape.text.strip())
    return shape.background is None and shape.rendered_box is not None


def _sample(shape, image, sx, sy):
    box = shape.rendered_box
    if shape.background is not None or not box or shape.effective_visible is not True:
        return shape
    left, right = round(box.x * sx), round((box.x + box.width) * sx)
    top, bottom = round(box.y * sy), round((box.y + box.height) * sy)
    if right <= left or bottom <= top:
        return shape
    points = {
        *product(range(left, right + 1, max(1, (right - left) // 12)), (top - 2, bottom + 2)),
        *product((left - 2, right + 2), range(top, bottom + 1, max(1, (bottom - top) // 12))),
    }
    samples = tuple(
        (x / sx, y / sy, *image.getpixel((x, y)))
        for x, y in sorted(points)
        if 0 <= x < image.width and 0 <= y < image.height
    )
    if len(samples) < 4:
        return shape
    return replace(
        shape,
        background_samples=samples,
        measurements=(
            *shape.measurements,
            Measurement(
                "background_samples", "approximate", "Pixels sampled outside the rendered text bounds"
            ),
        ),
    )


def render_evidence(presentation, snapshot, target, stats: dict | None = None):
    """슬라이드마다 PNG를 한 번 그려 글자 주변 배경 픽셀을 모으고, 표 칸 글자 위치를 픽셀로 잰다.

    성능: 슬라이드 PNG 내보내기는 한 번에 0.1~0.3초 걸린다. 예전에는 객체마다
    (객체 숨겨 그리기 1번 + 슬라이드 복제 후 글자 지우고 그리기 2번) = 3번씩 그렸는데,
    그 결과(visible_object_box, 일반 도형의 visible_text_box)는 어떤 검사도 쓰지 않았다.
    이제 픽셀 측정은 그 값이 꼭 필요한 표 칸(셀 좌표를 Office가 셀 기준으로만 주기 때문)에만 한다.
    배경 픽셀은 배경색을 이미 아는 글상자는 건너뛰고, 그런 글상자뿐인 슬라이드는 그리지도 않는다.
    """
    stats = {} if stats is None else stats
    stats.update(slides=0, exports=0, cells=0)
    by_slide = {}
    for shape in snapshot.shapes:
        if (
            shape.effective_visible is True
            and shape.locator
            and shape.locator.kind in {"shape", "table_cell"}
        ):
            by_slide.setdefault(shape.slide_number, []).append(shape)
    updated = {}
    height = min(2048, max(1, round(1280 * snapshot.height_pt / snapshot.width_pt)))
    for number, shapes in by_slide.items():
        if not any(_needs_pixels(shape) for shape in shapes):
            continue  # 배경색을 다 알고 표 칸도 없으면 그릴 필요가 없음
        slide = presentation.Slides.Item(number)

        def render(probe):
            stats["exports"] += 1
            return export_slide(probe, target, 1280, height)

        stats["slides"] += 1
        image = render(slide)
        if image is None:
            continue
        sx, sy = image.width / snapshot.width_pt, image.height / snapshot.height_pt
        cell_boxes = {}
        tables: dict[tuple, list] = {}
        for shape in shapes:
            if shape.locator.kind == "table_cell" and shape.box is not None and shape.text.strip():
                tables.setdefault(shape.locator.shape_path, []).append(shape)
        for cells in tables.values():  # 표마다 한 번 복제 (칸마다 복제하지 않음)
            stats["cells"] += len(cells)
            cell_boxes.update(measure_table_cells(cells, slide, render, sx, sy))
        for shape in shapes:
            measured = shape
            if shape.key in cell_boxes:
                measured = _with_cell_box(shape, cell_boxes[shape.key])
            updated[shape.key] = _sample(measured, image, sx, sy)
    return replace(snapshot, shapes=tuple(updated.get(shape.key, shape) for shape in snapshot.shapes))
