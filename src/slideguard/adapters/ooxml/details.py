"""Supplement COM observations with saved-file metadata without opening external links."""

from dataclasses import replace
from math import isfinite

from pptx import Presentation

from slideguard.domain.models import Measurement, ObjectEvidence, SlideDetails

from .geometry import saved_geometry
from .objects import chart_contents, inherited_objects, object_contents, timeline

# Keep the XML vocabulary in the adapter. These are saved settings, not effective
# render measurements or an inference about the author's intent.
_METADATA = frozenset(
    """
 cNvPr ph xfrm off ext chOff chExt prstGeom custGeom ln solidFill gradFill gs pattFill t fld
 blipFill blip srcRect alpha srgbClr schemeClr latin ea cs bodyPr normAutofit spAutoFit
 noAutofit pPr rPr endParaRPr buChar buAutoNum spcPct spcPts br hlinkClick
 tblPr gridCol tr tc tcPr rowSpan gridSpan hMerge vMerge cNvGraphicFramePr bg bgPr bgRef
 audioFile videoFile oleObj media relIds graphicData timing transition cTn spTgt
 cMediaNode sndTgt anim animEffect animMotion set seq par bldP custShow
 catAx valAx dateAx serAx scaling min max majorUnit minorUnit numFmt
 tickLblPos majorTickMark minorTickMark majorGridlines minorGridlines
 dispUnits dispUnitsLbl legend legendPos legendEntry delete overlay layout manualLayout
 plotArea dTable ser idx order marker trendline errBars dLbls dLbl spPr txPr
 cat val xVal yVal numCache strCache multiLvlStrCache pt ptCount lvl v tx
 ptLst cxnLst cxn prSet presOf ptType
""".split()
)
_MAX_FIELDS = 1024


def _name(qname):
    return qname.rsplit("}", 1)[-1]


def facts(root, prefix, *, omit=()):
    """Flatten selected paths with sibling indices so attributes retain ownership."""
    result = []
    stack = [(root, prefix)]
    while stack and len(result) < _MAX_FIELDS:
        node, path = stack.pop()
        tag = _name(node.tag) if isinstance(node.tag, str) else ""
        if tag in omit:
            continue
        if tag in _METADATA:
            result.append((path, tag))
            result.extend((f"{path}/@{_name(key)}", value) for key, value in node.attrib.items())
            if node.text and node.text.strip():
                result.append((f"{path}/#text", node.text.strip()))
        stack.extend(
            (child, f"{path}/{index}:{_name(child.tag)}")
            for index, child in reversed(list(enumerate(node, 1)))
            if isinstance(child.tag, str)
        )
    if stack:
        result.append((f"{prefix}/truncated", True))
    return tuple(result)


def _shape_index(shapes, path=()):
    for index, shape in enumerate(shapes, 1):
        address = (*path, shape.shape_id)
        yield address, (shape, index)
        children = getattr(shape, "shapes", None)
        if children is not None:
            yield from _shape_index(children, address)


def _augment(shape, source, order):
    element = source.element
    nonvisual = next(iter(element.xpath("./*/p:cNvPr")), None)
    props = list(shape.properties)
    props.append(("saved_order", order))
    if nonvisual is not None:
        for attr in ("descr", "title"):
            value = nonvisual.get(attr)
            if value is not None:
                props.append((f"alt_{attr}", value))
        for node in nonvisual.iter():
            if _name(node.tag) == "decorative":
                props.append(("decorative", node.get("val") in {"1", "true"}))
    if source.is_placeholder:
        props.append(("placeholder_role", str(source.placeholder_format.type)))
    for index, child in enumerate(element, 1):
        props.extend(facts(child, f"shape/{index}", omit=("sp", "grpSp", "pic", "graphicFrame", "cxnSp")))
    if getattr(type(source), "image", None) is not None:
        try:
            width, height = source.image.size
        except (AttributeError, KeyError, ValueError, OSError):
            pass
        else:
            props.extend((("image_width_px", width), ("image_height_px", height)))
    if source.has_chart:
        props.extend(facts(source.chart._element, "chart"))
    # These relationships are read only when they already exist inside the package.
    for ref in element.xpath(".//a:graphicData/*"):
        if _name(ref.tag) != "relIds":
            continue
        for key, relationship_id in ref.attrib.items():
            rel = source.part.rels.get(relationship_id)
            if rel is not None and not rel.is_external and hasattr(rel.target_part, "_element"):
                props.extend(facts(rel.target_part._element, f"diagram/{_name(key)}"))
    box, corners = saved_geometry(source)
    evidence = object_contents(source)
    if box is not None:
        evidence = (
            *evidence,
            ObjectEvidence("saved_geometry", "drawingml", "saved", box=box, corners=corners),
        )
    if source.has_chart:
        evidence = (*evidence, *chart_contents(source))
    return replace(shape, properties=tuple(props), evidence=(*shape.evidence, *evidence))


def _compound_metadata(shape, source):
    locator = shape.locator
    if locator.kind == "table_cell" and source.has_table:
        row, column = locator.indices
        if row <= len(source.table.rows) and column <= len(source.table.columns):
            cell = source.table.cell(row - 1, column - 1)
            return replace(
                shape,
                properties=(
                    *shape.properties,
                    ("row_span", int(cell.span_height)),
                    ("column_span", int(cell.span_width)),
                    ("header_row", row == 1 and bool(source.table.first_row)),
                    *facts(cell._tc, "cell"),
                ),
            )
    if locator.kind == "chart_legend" and source.has_chart:
        # Keep saved labels and legend settings separate from the COM display order.
        chart = source.chart
        plots = chart._element.xpath(".//*[local-name()='plotArea']/*")
        candidate_labels = []
        for plot in plots:
            if _name(plot.tag) in {"pieChart", "pie3DChart", "ofPieChart", "doughnutChart"}:
                values = plot.xpath(
                    "./*[local-name()='ser'][1]/*[local-name()='cat']"
                    "//*[local-name()='pt']/*[local-name()='v']"
                )
                candidate_labels.extend(("category", str(node.text)) for node in values if node.text)
        if not candidate_labels:
            candidate_labels.extend(("series", str(series.name)) for series in chart.series if series.name)
        legend_nodes = chart._element.xpath(".//*[local-name()='chart']/*[local-name()='legend']")
        records = tuple(
            ObjectEvidence("chart_legend_saved_label", f"ooxml:{kind}:{index}", "saved", text)
            for index, (kind, text) in enumerate(candidate_labels[:256], 1)
        )
        if len(candidate_labels) > 256:
            records = (*records, ObjectEvidence("chart_legend_saved_label", "ooxml:truncated", "partial"))
        return replace(
            shape,
            evidence=(*shape.evidence, *records),
            properties=(
                *shape.properties,
                *(facts(legend_nodes[0], "saved_legend") if legend_nodes else ()),
                *(
                    (f"saved_series_name_{i}", str(series.name))
                    for i, series in enumerate(source.chart.series, 1)
                    if isinstance(series.name, str)
                ),
            ),
        )
    return shape


def _measurements(shape):
    kind = shape.locator.kind if shape.locator else "shape"
    geometry_state = "approximate" if kind.startswith(("chart_", "axis_", "smartart_")) else "measured"
    visible_state = {True: "measured", False: "hidden", None: "unavailable"}[shape.effective_visible]
    text_state = (
        "measured"
        if shape.text
        else "absent"
        if shape.supported and not shape.notes
        else ("unavailable" if shape.supported else "unsupported")
    )
    return replace(
        shape,
        measurements=(
            *shape.measurements,
            Measurement("visibility", visible_state, "Object and ancestor visibility"),
            Measurement(
                "geometry", geometry_state if shape.box else "unavailable", "Slide-space object bounds"
            ),
            Measurement("text", text_state, "PowerPoint text extraction"),
            Measurement(
                "font_size",
                "measured"
                if shape.runs
                and all(run.font_size_pt is not None and isfinite(run.font_size_pt) for run in shape.runs)
                else "unavailable",
                "Effective font sizes for all extracted runs",
            ),
            Measurement(
                "background",
                "measured" if shape.background is not None else "unavailable",
                "Confirmed uniform background under text",
            ),
            Measurement(
                "text_bounds", "measured" if shape.rendered_box else "unavailable", "Text range bounds"
            ),
            Measurement(
                "content_box", "measured" if shape.content_box else "unavailable", "Text frame interior"
            ),
        ),
    )


def enrich_snapshot(snapshot, path):
    presentation = Presentation(str(path))
    existing = {slide.number: slide for slide in snapshot.slides}
    details = []
    indexed = {}
    for number, slide in enumerate(presentation.slides, 1):
        index = dict(_shape_index(slide.shapes))
        indexed[number] = index
        title = slide.shapes.title
        previous = existing.get(number, SlideDetails(number))
        layout = slide.slide_layout
        properties = (
            ("layout", layout.name),
            ("layout_shape_count", len(layout.shapes)),
            ("master_shape_count", len(layout.slide_master.shapes)),
            ("show_master_shapes", slide.element.get("showMasterSp", "1") not in {"0", "false"}),
            *facts(layout.element, "layout", omit=("extLst",)),
            *facts(layout.slide_master.element, "master", omit=("extLst",)),
            *facts(slide.element, "slide", omit=("spTree", "extLst")),
        )
        details.append(
            replace(
                previous,
                hidden=slide.element.get("show", "1") in {"0", "false"},
                title=title.text if title is not None else None,
                objects=(*inherited_objects(slide), *timeline(slide)),
                properties=(*previous.properties, *properties),
            )
        )
    shapes = []
    for shape in snapshot.shapes:
        path = shape.locator.shape_path if shape.locator else (shape.shape_id,)
        item = indexed.get(shape.slide_number, {}).get(path)
        if item and (shape.locator is None or shape.locator.kind == "shape"):
            shape = _augment(shape, *item)
        elif item:
            shape = _compound_metadata(shape, item[0])
        shapes.append(_measurements(shape))
    return replace(snapshot, shapes=tuple(shapes), slides=tuple(details))
