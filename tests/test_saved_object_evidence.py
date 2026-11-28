from io import BytesIO
from zipfile import ZipFile

import pytest
from pptx import Presentation
from pptx.chart.data import ChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.opc.package import Part
from pptx.opc.packuri import PackURI
from pptx.oxml import parse_xml
from pptx.util import Pt

from slideguard.adapters.ooxml.geometry import saved_geometry
from slideguard.adapters.ooxml.objects import chart_contents, inherited_objects, object_contents, timeline

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def test_group_transform_composes_rotation_and_flip():
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    child = group.shapes.add_textbox(Pt(100), Pt(40), Pt(40), Pt(20))
    group.rotation = 90
    xfrm = group.element.xpath("./p:grpSpPr/a:xfrm")[0]
    xfrm.set("flipH", "1")
    box, corners = saved_geometry(child)
    assert (box.x, box.y, box.width, box.height) == pytest.approx((110, 30, 20, 40))
    assert corners[0] == pytest.approx((130, 70))


def test_equation_caption_embedded_text_and_link_are_separate():
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    shape = slide.shapes.add_textbox(0, 0, Pt(40), Pt(20))
    package = slide.part.package
    track = Part(
        PackURI("/ppt/media/captions.vtt"), "text/vtt", package, b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello"
    )
    track_id = slide.part.relate_to(track, "http://schemas.microsoft.com/office/2017/04/relationships/track")
    linked_id = slide.part.relate_to("https://example.invalid/file.mp4", R + "/video", is_external=True)
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("word/document.xml", "<document><p><r><t>embedded</t></r></p></document>")
    embedded = Part(
        PackURI("/ppt/embeddings/example.docx"), "application/octet-stream", package, data.getvalue()
    )
    embedded_id = slide.part.relate_to(embedded, R + "/package")
    shape.element.append(
        parse_xml(
            f'<p:extLst xmlns:p="{P}" xmlns:r="{R}" '
            'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
            'xmlns:p173="http://schemas.microsoft.com/office/powerpoint/2017/3/main">'
            "<m:oMath><m:r><m:t>x+y</m:t></m:r></m:oMath>"
            f'<p173:track lang="en" r:embed="{track_id}"/>'
            f'<p:videoFile r:link="{linked_id}"/>'
            f'<p:oleObj progId="Word.Document.12" r:id="{embedded_id}"/></p:extLst>'
        )
    )
    equation, caption, media, ole = object_contents(shape)
    assert equation.kind == "equation" and equation.text == "x+y"
    assert caption.kind == "caption_track" and caption.text.endswith("hello")
    assert media.state == "external" and not media.text
    assert ole.children[0].text == "embedded"


def test_timeline_inherited_shape_and_chart_cache_are_saved_evidence():
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.element.append(
        parse_xml(
            f'<p:timing xmlns:p="{P}"><p:tnLst><p:par><p:cTn id="1">'
            '<p:childTnLst><p:set><p:cBhvr><p:cTn id="2" dur="100"/>'
            '<p:tgtEl><p:spTgt spid="3"/></p:tgtEl></p:cBhvr></p:set></p:childTnLst>'
            "</p:cTn></p:par></p:tnLst></p:timing>"
        )
    )
    source = slide.shapes.add_textbox(0, 0, Pt(40), Pt(20))
    source.text = "master logo"
    slide.slide_layout.slide_master.shapes._spTree.append(parse_xml(source.element.xml))
    assert any(
        record.text == "master logo" and record.state == "partial" for record in inherited_objects(slide)
    )
    slide.element.set("showMasterSp", "0")
    assert inherited_objects(slide) == ()
    assert ("target_shape_id", "3") in timeline(slide)[1].properties
    assert ("parent_id", "1") in timeline(slide)[1].properties
    data = ChartData()
    data.categories = ["category"]
    data.add_series("series", [42])
    chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, 0, 0, Pt(100), Pt(100), data)
    result = chart_contents(chart)
    assert len(result) == 1
    assert {value.text for value in result[0].children} == {"category", "series", "42"}
