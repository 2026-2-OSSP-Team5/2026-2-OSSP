"""Read line and paragraph ranges whose PowerPoint geometry can be verified."""

from slideguard.domain.models import Box, TextSpan

from .measurements import optional


def range_properties(entry):
    values = {}
    for name in ("Start", "Length"):
        value = optional(lambda name=name: getattr(entry, name), name, [])
        if type(value) in (int, float) and value >= 0:
            values[name] = value
    return values


def paragraph_properties(entry):
    values = range_properties(entry)
    indent = optional(lambda: entry.IndentLevel, "IndentLevel", [])
    if type(indent) in (int, float) and indent >= 0:
        values["IndentLevel"] = indent
    paragraph = optional(lambda: entry.ParagraphFormat, "Paragraph format", [])
    for name in ("Alignment", "SpaceWithin", "SpaceBefore", "SpaceAfter", "LineRuleWithin"):
        value = optional(lambda name=name: getattr(paragraph, name), name, [])
        if type(value) in (int, float, bool):
            values[name] = value
    bullet = optional(lambda: paragraph.Bullet.Visible, "Bullet visibility", [])
    if bullet in (-1, 0):
        values["bullet"] = bullet == -1
    bullet_type = optional(lambda: paragraph.Bullet.Type, "Bullet type", [])
    if type(bullet_type) is int and bullet_type >= 0:
        values["BulletType"] = bullet_type
    if bullet_type == 2:
        for name in ("Number", "Style"):
            value = optional(lambda name=name: getattr(paragraph.Bullet, name), name, [])
            if type(value) is int and value >= 0:
                values["Bullet" + name] = value
    for name in ("Size", "Bold"):
        value = optional(lambda name=name: getattr(entry.Font, name), name, [])
        if type(value) in (int, float) and (name == "Bold" or value > 0):
            values[name] = value
    return tuple(sorted(values.items()))


def text_spans(item, shape, *, measure_bounds: bool = True):
    if not shape.text or (measure_bounds and shape.rendered_box is None):
        return ()
    source = optional(lambda: item.TextFrame.TextRange, "Text range", [])
    spans = []
    for kind, member in (("line", "Lines"), ("paragraph", "Paragraphs")):
        collection = optional(lambda member=member: getattr(source, member)(), member, [])
        count = optional(lambda collection=collection: collection.Count, f"{member} count", [])
        if type(count) is not int or count < 1:
            continue
        for index in range(1, min(count, 1024) + 1):
            entry = optional(lambda member=member, index=index: getattr(source, member)(index, 1), member, [])
            text = optional(lambda entry=entry: entry.Text, f"{member} text", [])
            box = None
            if measure_bounds:
                values = tuple(
                    optional(lambda name=name, entry=entry: getattr(entry, name), name, [])
                    for name in ("BoundLeft", "BoundTop", "BoundWidth", "BoundHeight")
                )
                box = Box(*values) if all(type(value) in (float, int) for value in values) else None
            if isinstance(text, str):
                details = (
                    paragraph_properties(entry)
                    if kind == "paragraph"
                    else tuple(sorted(range_properties(entry).items()))
                )
                spans.append(TextSpan(kind, index, text, box, details))
    return tuple(spans)
