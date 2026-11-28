"""Measure rendered pixel differences on the private PowerPoint copy."""

from dataclasses import replace

from PIL import Image, ImageChops

from slideguard.domain.models import Box, Measurement

from .measurements import optional


def export_slide(slide, target, width, height):
    notes = []
    try:
        optional(lambda: slide.Export(str(target), "PNG", width, height), "Slide PNG export", notes)
        if notes or not target.is_file():
            return None
        with Image.open(target) as source:
            return source.convert("RGB")
    finally:
        target.unlink(missing_ok=True)


def _pixel_box(before, after, sx, sy):
    if after is None or before.size != after.size:
        return None
    bounds = ImageChops.difference(before, after).getbbox()
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    return Box(left / sx, top / sy, (right - left) / sx, (bottom - top) / sy)


def _positions(slide, path):
    collection = slide.Shapes
    result = []
    for shape_id in path:
        for index in range(1, collection.Count + 1):
            item = collection.Item(index)
            if item.Id == shape_id:
                result.append(index)
                if len(result) < len(path):
                    collection = item.GroupItems
                break
        else:
            return ()
    return tuple(result)


def _item(slide, positions):
    item = slide.Shapes.Item(positions[0])
    for position in positions[1:]:
        item = item.GroupItems.Item(position)
    return item


def _geometry(item):
    return item.Left, item.Top, item.Width, item.Height


def _text_pixels(slide, positions, render, sx, sy, *, cell_indices=()):
    notes = []
    duplicate = optional(lambda: slide.Duplicate().Item(1), "Temporary probe slide", notes)
    if duplicate is None:
        return None
    try:
        item = _item(duplicate, positions)
        container = item
        if cell_indices:
            row, column = cell_indices
            item = container.Table.Cell(row, column).Shape
        frame = optional(lambda: item.TextFrame, "Probe text frame", notes)
        # Clearing text is safe as a comparison only with a fixed shape size.
        if optional(lambda: frame.AutoSize, "Probe text frame AutoSize", notes) != 0:
            return None

        def geometry():
            if cell_indices:
                table = container.Table
                return (
                    _geometry(item),
                    _geometry(container),
                    tuple(table.Rows.Item(index).Height for index in range(1, table.Rows.Count + 1)),
                    tuple(table.Columns.Item(index).Width for index in range(1, table.Columns.Count + 1)),
                )
            return _geometry(item)

        before_geometry = geometry()
        before = render(duplicate)
        if before is None:
            return None
        optional(lambda: setattr(frame.TextRange, "Text", ""), "Clear text in probe slide", notes)
        if notes or geometry() != before_geometry:
            return None
        after = render(duplicate)
        if geometry() != before_geometry:
            return None
        return _pixel_box(before, after, sx, sy)
    finally:
        duplicate.Delete()


def measure_pixels(shape, slide, before, render, sx, sy):
    if (
        shape.effective_visible is not True
        or not shape.locator
        or shape.locator.kind not in {"shape", "table_cell"}
    ):
        return shape
    positions = _positions(slide, shape.locator.shape_path)
    if not positions:
        return shape
    if shape.locator.kind == "table_cell":
        if shape.box is None or not shape.text.strip():
            return shape
        text_box = _text_pixels(slide, positions, render, sx, sy, cell_indices=shape.locator.indices)
        measurement = Measurement(
            "text_bounds",
            "measured" if text_box else "unavailable",
            "Slide PNG difference after clearing only this cell on a fixed-geometry probe table",
        )
        return replace(
            shape,
            rendered_box=text_box,
            visible_text_box=text_box,
            measurements=tuple(item for item in shape.measurements if item.field != "text_bounds")
            + (measurement,),
        )
    item = _item(slide, positions)
    probe_notes = []
    if optional(lambda: item.Visible, "Probe shape visibility", probe_notes) != -1:
        return shape
    try:
        optional(lambda: setattr(item, "Visible", 0), "Hide probe shape", probe_notes)
        object_box = None if probe_notes else _pixel_box(before, render(slide), sx, sy)
    finally:
        if item.Visible != -1:
            item.Visible = -1
    text_box = _text_pixels(slide, positions, render, sx, sy) if shape.text else None
    notes = [
        Measurement(
            "visible_object_box",
            "measured" if object_box else "unavailable",
            "Slide PNG difference with object hidden; visible pixels including effects",
        ),
    ]
    if shape.text:
        notes.append(
            Measurement(
                "visible_text_box",
                "measured" if text_box else "unavailable",
                "Probe slide PNG difference after removing text from a fixed-size frame",
            )
        )
    return replace(
        shape,
        visible_object_box=object_box,
        visible_text_box=text_box,
        measurements=(*shape.measurements, *notes),
    )


def measure_table_cells(cells, slide, render, sx, sy):
    """한 표의 글자 있는 칸들을 한꺼번에 잰다. {shape.key: Box 또는 None}.

    예전에는 칸마다 슬라이드를 복제하고 두 번 그렸다 (칸 n개 → 복제 n번 + 내보내기 2n번).
    이제는 표마다 한 번 복제해서 한 번 그린 뒤, 칸 글자를 하나씩 지우며 바로 앞 그림과 비교한다
    (복제 1번 + 내보내기 n+1번). 지운 칸 하나만 달라지므로 그 차이가 그 칸 글자의 위치다.
    지우는 사이 표 크기·줄 높이가 바뀌면 그 칸은 측정하지 않는다 (위치가 함께 바뀌어 비교가 틀어짐).
    """
    result = {shape.key: None for shape in cells}
    if not cells:
        return result
    positions = _positions(slide, cells[0].locator.shape_path)
    if not positions:
        return result
    notes = []
    duplicate = optional(lambda: slide.Duplicate().Item(1), "Temporary probe slide", notes)
    if duplicate is None:
        return result
    try:
        container = _item(duplicate, positions)
        table = container.Table

        def geometry():
            return (
                _geometry(container),
                tuple(table.Rows.Item(index).Height for index in range(1, table.Rows.Count + 1)),
                tuple(table.Columns.Item(index).Width for index in range(1, table.Columns.Count + 1)),
            )

        previous = render(duplicate)
        if previous is None:
            return result
        for shape in cells:
            row, column = shape.locator.indices
            item = table.Cell(row, column).Shape
            frame = optional(lambda item=item: item.TextFrame, "Probe text frame", notes)
            if (
                frame is None
                or optional(lambda frame=frame: frame.AutoSize, "Probe text frame AutoSize", notes) != 0
            ):
                continue
            before = geometry()
            optional(
                lambda frame=frame: setattr(frame.TextRange, "Text", ""), "Clear text in probe slide", notes
            )
            after = render(duplicate)
            if after is None:
                break
            if geometry() == before:
                result[shape.key] = _inside_cell(_pixel_box(previous, after, sx, sy), shape.box)
            previous = after  # 다음 칸은 이번 그림과 비교 (지운 칸은 계속 비어 있음)
        return result
    finally:
        duplicate.Delete()


def _inside_cell(text_box, cell_box):
    """칸 글자를 지웠을 때 달라진 픽셀이 그 칸 근처에 있어야 그 칸의 글자다.

    지우는 사이 PowerPoint가 다른 곳(맞춤법 표시, 다시 그려진 표 테두리 등)까지 다르게 그리면
    차이가 표 전체로 잡혀 '655pt 넘침' 같은 오판정이 난다 (함정 PPT 7쪽). 그런 값은 버린다(None).
    칸 밖으로 글자 한 줄 높이 정도 나간 것까지는 실제 위치로 인정한다.
    """
    if text_box is None or cell_box is None:
        return text_box
    slack = max(cell_box.height, 20.0)
    inside = (
        text_box.x >= cell_box.x - slack
        and text_box.y >= cell_box.y - slack
        and text_box.x + text_box.width <= cell_box.x + cell_box.width + slack
        and text_box.y + text_box.height <= cell_box.y + cell_box.height + slack
    )
    return text_box if inside else None
