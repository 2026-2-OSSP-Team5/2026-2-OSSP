"""Extract saved object contents without activating any embedded application."""

from io import BytesIO
from zipfile import BadZipFile, ZipFile

from lxml import etree

from slideguard.domain.models import ObjectEvidence

from .geometry import saved_geometry

_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_MATH = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_CAPTION = "{http://schemas.microsoft.com/office/powerpoint/2017/3/main}"
_LIMIT = 2_000_000


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml(blob):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    return etree.fromstring(blob, parser)


def _office_package(blob):
    """Read bounded authored text/cached values from an embedded ZIP Office document."""
    result = []
    budget = _LIMIT
    with ZipFile(BytesIO(blob)) as archive:
        for part in archive.infolist():
            path = part.filename
            if not (
                path == "word/document.xml"
                or path == "xl/sharedStrings.xml"
                or (path.startswith("xl/worksheets/sheet") and path.endswith(".xml"))
            ):
                continue
            if part.file_size > budget:
                return tuple(result), True
            budget -= part.file_size
            root = _xml(archive.read(part))
            if path.startswith("xl/worksheets/"):
                for node in root.xpath(".//*[local-name()='c']"):
                    value = "".join(
                        child.text or ""
                        for child in node.xpath(
                            "./*[local-name()='v'] | ./*[local-name()='is']//*[local-name()='t']"
                        )
                    )
                    if value:
                        result.append(
                            ObjectEvidence(
                                "embedded_cell",
                                f"{path}#{node.get('r', '')}",
                                "saved",
                                value,
                                properties=(("cell_type", node.get("t", "n")),),
                            )
                        )
                        if len(result) >= 1024:
                            return tuple(result), True
            else:
                item_tag = "si" if path.startswith("xl/") else "p"
                for index, node in enumerate(root.xpath(f".//*[local-name()='{item_tag}']"), 1):
                    text = "".join(t.text or "" for t in node.xpath(".//*[local-name()='t']"))
                    if text:
                        result.append(ObjectEvidence("embedded_text", f"{path}#{index}", "saved", text))
                        if len(result) >= 1024:
                            return tuple(result), True
    return tuple(result), False


def _relation_evidence(source, node, kind, index):
    relationship = next(
        (node.get(_REL + key) for key in ("link", "embed", "id") if node.get(_REL + key)), None
    )
    attrs = tuple((local(key), value) for key, value in node.attrib.items() if not key.startswith(_REL))
    origin = f"{kind}:{index}"
    rel = source.part.rels.get(relationship) if relationship else None
    if rel is None:
        return ObjectEvidence(kind, origin, "unavailable", properties=attrs)
    if rel.is_external:
        return ObjectEvidence(kind, origin, "external", properties=attrs)
    part = rel.target_part
    blob = part.blob
    properties = (
        *attrs,
        ("part", str(part.partname)),
        ("content_type", part.content_type),
        ("bytes", len(blob)),
    )
    if kind == "caption_track" and len(blob) <= _LIMIT:
        try:
            text = blob.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = ""
        if text.startswith("WEBVTT"):
            return ObjectEvidence(kind, origin, "saved", text, properties=properties)
    if kind == "ole" and len(blob) <= _LIMIT and blob.startswith(b"PK"):
        try:
            children, truncated = _office_package(blob)
        except (BadZipFile, etree.XMLSyntaxError, RuntimeError, ValueError, OSError):
            children, truncated = (), False
        if children:
            return ObjectEvidence(
                kind, origin, "partial" if truncated else "saved", properties=properties, children=children
            )
    return ObjectEvidence(kind, origin, "partial", properties=properties)


def object_contents(source):
    records = []
    walker = etree.iterwalk(source.element, events=("start",))
    for index, (_, node) in enumerate(walker, 1):
        tag = local(node.tag)
        if tag in {"sp", "pic", "grpSp", "graphicFrame", "cxnSp"} and node is not source.element:
            # An OLE object's fallback picture is handled through its owner.
            walker.skip_subtree()
            continue
        if node.tag == _MATH + "oMath":
            text = "".join(child.text or "" for child in node.iter(_MATH + "t"))
            records.append(
                ObjectEvidence(
                    "equation",
                    f"math:{index}",
                    "saved",
                    text,
                    properties=(("omml", etree.tostring(node, encoding="unicode")),),
                )
            )
        elif node.tag == _CAPTION + "track":
            records.append(_relation_evidence(source, node, "caption_track", index))
        elif tag == "oleObj":
            records.append(_relation_evidence(source, node, "ole", index))
        elif tag in {"videoFile", "audioFile", "media"} and any(key.startswith(_REL) for key in node.attrib):
            records.append(_relation_evidence(source, node, "media", index))
    return tuple(records)


def inherited_objects(slide):
    layout = slide.slide_layout
    if slide.element.get("showMasterSp", "1") in {"0", "false"}:
        return ()
    parents = [("layout", layout.shapes)]
    if layout.element.get("showMasterSp", "1") not in {"0", "false"}:
        parents.insert(0, ("master", layout.slide_master.shapes))
    result = []
    for origin, shapes in parents:
        for shape in shapes:
            if shape.is_placeholder:
                continue
            nonvisual = shape.element.xpath("./*/p:cNvPr")
            if nonvisual and nonvisual[0].get("hidden") in {"true", "1"}:
                continue
            box, corners = saved_geometry(shape)
            result.append(
                ObjectEvidence(
                    "inherited_shape",
                    f"{origin}:{shape.shape_id}",
                    "partial",
                    shape.text if shape.has_text_frame else "",
                    box,
                    (("name", shape.name), ("type", str(shape.shape_type))),
                    corners=corners,
                )
            )
    return tuple(result)


def timeline(slide):
    timings = slide.element.xpath("./p:timing")
    if not timings:
        return ()
    root = timings[0]
    nodes = root.xpath(".//*[local-name()='cTn']")
    records = []
    for node in nodes[:1024]:
        parent = next((ancestor for ancestor in node.iterancestors() if local(ancestor.tag) == "cTn"), None)
        settings = [(local(key), value) for key, value in node.attrib.items()]
        if parent is not None:
            settings.append(("parent_id", parent.get("id", "")))
        for index, member in enumerate(node, 1):
            if local(member.tag) in {"stCondLst", "endCondLst"}:
                for offset, condition in enumerate(member, 1):
                    settings.extend(
                        (f"{local(member.tag)}:{offset}:{local(key)}", value)
                        for key, value in condition.attrib.items()
                    )
            settings.append((f"child:{index}", local(member.tag)))
        # Behavior targets usually sit beside cTn inside a cBhvr/cMediaNode.
        owner = node.getparent()
        for target in owner.xpath("./*[local-name()='tgtEl']/*[local-name()='spTgt']"):
            if target.get("spid") is not None:
                settings.append(("target_shape_id", target.get("spid")))
        records.append(
            ObjectEvidence("time_node", f"timing:{len(records) + 1}", "saved", properties=tuple(settings))
        )
    if len(nodes) > 1024:
        records.append(ObjectEvidence("time_node", "timing:truncated", "partial"))
    return tuple(records)


def chart_contents(source):
    result = []
    chart = source.chart._element
    nodes = chart.xpath(".//*[local-name()='plotArea']/*/*[local-name()='ser']")
    for number, series in enumerate(nodes[:256], 1):
        properties = [("plot_type", local(series.getparent().tag))]
        for index, axis in enumerate(series.getparent().xpath("./*[local-name()='axId']"), 1):
            if axis.get("val") is not None:
                properties.append((f"axis_id:{index}", axis.get("val")))
        for member in series:
            if local(member.tag) in {"spPr", "marker", "trendline", "errBars", "dPt", "dLbls"}:
                properties.append((f"saved_{local(member.tag)}", etree.tostring(member, encoding="unicode")))
        for key in ("idx", "order"):
            matches = series.xpath(f"./*[local-name()='{key}']")
            if matches and matches[0].get("val") is not None:
                properties.append((key, matches[0].get("val")))
        children = []
        for field in series:
            if local(field.tag) not in {"tx", "cat", "val", "xVal", "yVal", "bubbleSize"}:
                continue
            points = field.xpath(".//*[local-name()='pt']")
            values = points or field.xpath(".//*[local-name()='v']")
            for position, point in enumerate(values[:2048]):
                value = "".join(point.itertext())
                if value:
                    children.append(
                        ObjectEvidence(
                            "chart_value",
                            f"{local(field.tag)}:{position}",
                            "saved",
                            value,
                            properties=(("index", point.get("idx", str(position))),),
                        )
                    )
            if len(values) > 2048:
                children.append(ObjectEvidence("chart_value", f"{local(field.tag)}:truncated", "partial"))
        result.append(
            ObjectEvidence(
                "chart_series",
                f"series:{number}",
                "saved",
                properties=tuple(properties),
                children=tuple(children),
            )
        )
    if len(nodes) > 256:
        result.append(ObjectEvidence("chart_series", "series:truncated", "partial"))
    return tuple(result)
