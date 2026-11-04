"""Read line and paragraph ranges whose PowerPoint geometry can be verified."""

from slideguard.domain.models import Box, TextSpan

from .measurements import optional


def text_spans(item, shape):
    if not shape.text or shape.rendered_box is None:
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
            values = tuple(
                optional(lambda name=name, entry=entry: getattr(entry, name), name, [])
                for name in ("BoundLeft", "BoundTop", "BoundWidth", "BoundHeight")
            )
            box = Box(*values) if all(type(value) in (float, int) for value in values) else None
            if isinstance(text, str):
                spans.append(TextSpan(kind, index, text, box))
    return tuple(spans)
