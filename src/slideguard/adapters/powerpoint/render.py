"""Sample the rendered neighborhood of text as review evidence."""

from dataclasses import replace
from itertools import product

from slideguard.domain.models import Measurement

from .pixel_bounds import export_slide, measure_pixels


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


def render_evidence(presentation, snapshot, target):
    by_slide = {}
    for shape in snapshot.shapes:
        if shape.effective_visible is True and shape.locator and shape.locator.kind == "shape":
            by_slide.setdefault(shape.slide_number, []).append(shape)
    updated = {}
    height = min(2048, max(1, round(1280 * snapshot.height_pt / snapshot.width_pt)))
    for number, shapes in by_slide.items():
        slide = presentation.Slides.Item(number)

        def render(probe):
            return export_slide(probe, target, 1280, height)

        image = render(slide)
        if image is None:
            continue
        sx, sy = image.width / snapshot.width_pt, image.height / snapshot.height_pt
        for shape in shapes:
            measured = measure_pixels(shape, slide, image, render, sx, sy)
            updated[shape.key] = _sample(measured, image, sx, sy)
    return replace(snapshot, shapes=tuple(updated.get(shape.key, shape) for shape in snapshot.shapes))
