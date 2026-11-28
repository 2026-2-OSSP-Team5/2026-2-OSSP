import gc
import queue
from hashlib import sha256

import pytest

from slideguard.adapters.powerpoint.properties import indexed_property
from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
from slideguard.adapters.powerpoint.session import PowerPointReadSession
from slideguard.bootstrap import build_snapshot_collection
from tests.fixtures.expanded import create_expanded_deck

pytestmark = [pytest.mark.powerpoint, pytest.mark.usefixtures("powerpoint_environment")]


@pytest.fixture(scope="module")
def expanded_source(powerpoint_environment, tmp_path_factory):
    import pythoncom
    import win32com.client

    folder = tmp_path_factory.mktemp("확장 객체")
    path = folder / "그룹 표 차트 스마트아트.pptx"
    create_expanded_deck(path)
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    app = document = None
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        # SmartArtLayouts requires an active test presentation window on this Office build.
        document = app.Presentations.Open(str(path), ReadOnly=0, Untitled=0, WithWindow=-1)
        slide = document.Slides.Item(1)
        chart = slide.Shapes.Item(3).Chart
        chart.HasLegend = True
        chart.HasTitle = True
        chart.ChartTitle.Text = "차트 제목"
        chart.Axes(1, 1).HasTitle = True
        chart.Axes(1, 1).AxisTitle.Text = "분류 축"
        chart.SeriesCollection(1).ApplyDataLabels()
        chart.SeriesCollection(1).Points(1).DataLabel.Text = "사용자 레이블"
        smart = slide.Shapes.AddSmartArt(app.SmartArtLayouts.Item(1), 50, 400, 800, 200)
        smart.Name = "node-diagram"
        for index in range(1, smart.SmartArt.AllNodes.Count + 1):
            smart.SmartArt.AllNodes.Item(index).TextFrame2.TextRange.Text = f"노드 {index}"
        slide = document.Slides.Item(2)
        slide.Shapes.Item(1).TextFrame2.TextRange.Font.Fill.Transparency = 0.9
        hidden = slide.Shapes.AddTextbox(1, 10, 300, 200, 30)
        hidden.Name = "hidden-text"
        hidden.TextFrame.TextRange.Text = "Hidden"
        hidden.Visible = 0
        text_range = slide.Shapes.Item(2).TextFrame2.TextRange
        indexed_property(text_range, "Characters", 3, 2).Font.Fill.Transparency = 0.5
        pie = document.Slides.Item(3).Shapes.Item(1).Chart
        pie.HasTitle = False
        pie.SeriesCollection(1).ApplyDataLabels()
        pie.SeriesCollection(1).Points(2).HasDataLabel = False
        probe = document.Slides.Item(4).Shapes.Item(1).GroupItems.Item(1)
        probe.Shadow.Visible = -1
        probe.Shadow.OffsetX = 25
        document.Save()
    finally:
        if document is not None:
            document.Saved = -1
            document.Close()
        document = app = None
        gc.collect()
        pythoncom.CoUninitialize()
    return path


@pytest.fixture(scope="module")
def expanded_deck(expanded_source):
    before = expanded_source.read_bytes()
    result = PowerPointDeckReader(PowerPointReadSession()).read(expanded_source)
    assert expanded_source.read_bytes() == before
    assert result.source_sha256 == sha256(before).hexdigest()
    assert len({shape.key for shape in result.shapes}) == len(result.shapes)
    return result


def test_actual_group_view_keeps_ancestor_address_and_coordinates(expanded_deck):
    value = next(item for item in expanded_deck.shapes if item.name == "group-text")
    assert value.text == "그룹 내부 텍스트" and value.supported
    # Office may flatten identity-transform inner groups when loading the saved
    # package. The locator addresses the actual COM view used by the patcher.
    assert len(value.locator.shape_path) >= 2 and value.locator.kind == "shape"
    assert value.locator.shape_path[-1] == value.shape_id
    assert value.box.x == pytest.approx(100) and value.box.y == pytest.approx(70)
    assert value.runs[0].font_size_pt == 24 and value.rendered_box is not None


def test_actual_merged_cells_are_unique_and_have_text_style(expanded_deck):
    cells = [item for item in expanded_deck.shapes if item.locator.kind == "table_cell"]
    assert {item.text for item in cells} == {"병합 셀", "아래 왼쪽", "아래 오른쪽"}
    assert len(cells) == 3
    assert {item.locator.indices for item in cells} == {(1, 1), (2, 1), (2, 2)}
    assert all(item.box is not None and item.runs and item.runs[0].font_size_pt > 0 for item in cells)


def test_actual_chart_title_axis_title_and_data_labels(expanded_deck):
    elements = {item.locator.kind: item for item in expanded_deck.shapes if item.slide_number == 1}
    assert elements["chart_title"].text == "차트 제목"
    assert elements["axis_title"].text == "분류 축"
    labels = [
        item for item in expanded_deck.shapes if item.slide_number == 1 and item.locator.kind == "data_label"
    ]
    assert {item.text for item in labels} == {"사용자 레이블", "2"}
    for value in [elements["chart_title"], elements["axis_title"], *labels]:
        assert value.box is not None and value.box.x >= 480
        assert value.runs and value.runs[0].font_size_pt > 0
        assert value.rendered_box is None and value.background is None


def test_actual_smartart_nodes_have_unique_addresses_and_formatting(expanded_deck):
    nodes = [item for item in expanded_deck.shapes if item.locator.kind == "smartart_node"]
    assert len(nodes) >= 2 and nodes[0].text == "노드 1"
    assert all(item.box is not None and item.runs and item.runs[0].font_size_pt > 0 for item in nodes)
    assert len({item.locator.indices for item in nodes}) == len(nodes)


def test_actual_pie_chart_without_axes_keeps_individual_data_label(expanded_deck):
    elements = [item for item in expanded_deck.shapes if item.slide_number == 3]
    labels = [item for item in elements if item.locator.kind == "data_label"]
    assert len(labels) == 1 and labels[0].text == "1"
    assert labels[0].locator.indices == (1, 1)
    assert not any(item.locator.kind in ("chart_title", "axis_title") for item in elements)


def test_actual_transparency_is_preserved_without_false_opaque_rgb(expanded_deck):
    value = next(item for item in expanded_deck.shapes if item.name == "transparent")
    assert value.runs and value.runs[0].transparency == pytest.approx(0.9)
    assert value.runs[0].source_color == (0, 0, 0) and value.runs[0].color is None
    assert value.runs[0].font_size_pt == 24


def test_actual_unicode_mixed_opacity_is_not_collapsed(expanded_deck):
    value = next(item for item in expanded_deck.shapes if item.name == "mixed-alpha")
    assert "".join(run.text for run in value.runs) == "😀AB"
    assert any(run.transparency == 0 and run.color == (0, 0, 0) for run in value.runs)
    assert any(run.transparency == pytest.approx(0.5) and run.color is None for run in value.runs)


def test_read_on_initialized_thread_preserves_callers_com_apartment(expanded_source):
    import pythoncom
    import win32com.client

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    app = None
    try:
        PowerPointDeckReader(PowerPointReadSession()).read(expanded_source)
        app = win32com.client.DispatchEx("PowerPoint.Application")
        assert app.Version
    finally:
        app = None
        gc.collect()
        pythoncom.CoUninitialize()


def test_full_pipeline_delivers_expanded_contents(expanded_source):
    output, failures = queue.Queue(), queue.Queue()
    pipeline = build_snapshot_collection(expanded_source.parent, output.put, on_failure=failures.put)
    pipeline.start()
    try:
        result = output.get(timeout=30)
        assert pipeline.collector.wait_idle(30)
        assert result.source_sha256 == sha256(expanded_source.read_bytes()).hexdigest()
        assert {"table_cell", "chart_title", "smartart_node"} <= {item.locator.kind for item in result.shapes}
        probe = next(item for item in result.shapes if item.name == "rotated-probe-text")
        assert probe.visible_object_box and probe.visible_text_box
        legend = next(item for item in result.shapes if item.locator.kind == "chart_legend")
        entry = next(item for item in legend.evidence if item.kind == "chart_legend_entry")
        assert dict(entry.properties)["font_size_pt"] > 0
        key = next(item for item in entry.children if item.kind == "chart_legend_key")
        assert key.properties
        assert any(
            item.kind == "chart_legend_saved_label" and item.text == "계열" for item in legend.evidence
        )
        assert failures.empty() and pipeline.is_running
    finally:
        pipeline.stop()


def test_actual_measurements_capture_hidden_text_interior_and_chart_groups(expanded_deck):
    hidden = next(item for item in expanded_deck.shapes if item.name == "hidden-text")
    assert hidden.visible is False and hidden.effective_visible is False
    assert any(m.field == "visibility" and m.state == "hidden" for m in hidden.measurements)
    text = next(item for item in expanded_deck.shapes if item.name == "mixed-alpha")
    assert text.content_box is not None and text.content_box.width < text.box.width
    groups = [item for item in expanded_deck.shapes if item.locator.kind.startswith("chart_")]
    assert any(item.locator.kind == "chart_tick_labels" and item.runs for item in groups)
    assert any(item.locator.kind == "chart_legend" and item.box for item in groups)


def test_actual_rendered_evidence_is_approximate(expanded_source):
    before = expanded_source.read_bytes()
    deck = PowerPointDeckReader(PowerPointReadSession(), render=True).read(expanded_source)
    assert expanded_source.read_bytes() == before
    probe = next(item for item in deck.shapes if item.name == "rotated-probe-text")
    assert probe.rendered_box is None
    assert probe.visible_object_box is not None
    assert probe.visible_text_box is not None
    assert probe.visible_text_box.width < probe.visible_object_box.width
    assert probe.visible_text_box.height < probe.visible_object_box.height
    hidden = next(item for item in deck.shapes if item.name == "hidden-text")
    assert hidden.visible_object_box is None and hidden.visible_text_box is None
    estimated = [
        item
        for item in deck.shapes
        if any(m.field == "background_samples" and m.state == "approximate" for m in item.measurements)
    ]
    assert estimated
    assert all(item.background is None for item in estimated)
    assert all(len(item.background_samples) >= 4 for item in estimated)
