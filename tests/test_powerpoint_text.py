from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from pptx import Presentation

from slideguard.adapters.powerpoint import session
from slideguard.adapters.powerpoint.copies import SavedPresentationCopy
from slideguard.adapters.powerpoint.errors import PowerPointBusyError
from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
from slideguard.adapters.powerpoint.snapshot import extract_snapshot
from slideguard.domain.models import Box, TextRun


class ComError(Exception):
    def __init__(self, hresult):
        self.hresult = hresult
        super().__init__("COM measurement failure")


class Properties(SimpleNamespace):
    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if isinstance(value, BaseException):
            raise value
        return value


class Collection:
    def __init__(self, items):
        self.items = items
        self.Count = len(items)

    def Item(self, index):
        assert 1 <= index <= self.Count
        return self.items[index - 1]


def color(rgb=0x332211, color_type=1):
    return Properties(Type=color_type, RGB=rgb)


def fill(*, visible=-1, fill_type=1, transparency=0, rgb=0xFFFFFF, color_type=1):
    return Properties(
        Visible=visible, Type=fill_type, Transparency=transparency, ForeColor=color(rgb, color_type)
    )


def run(text, *, size=24.0, bold=0, rgb=0x332211, color_type=1):
    color_value = color(rgb, color_type)
    return Properties(
        Text=text,
        Font=Properties(
            Size=size,
            Bold=bold,
            Color=color_value,
            Fill=Properties(Visible=-1, Type=1, Transparency=0.0, ForeColor=color_value),
        ),
    )


def opaque_run(text, size, rgb, bold):
    return TextRun(text, size, rgb, bold, rgb, 0.0, "solid", True)


class TextRange(Properties):
    def __init__(self, runs, *, text=None):
        super().__init__(
            Text="".join(item.Text for item in runs) if text is None else text,
            BoundLeft=12.5,
            BoundTop=16.25,
            BoundWidth=125.5,
            BoundHeight=35.0,
            Font=AssertionError("Do not collapse mixed formatting into a whole-range font"),
        )
        self.items = runs
        self.count = len(runs)
        self.calls = []
        self.runs_failure = None

    def Runs(self, start=None, length=None):
        if self.runs_failure is not None:
            raise self.runs_failure
        if start is None:
            return Properties(Count=self.count)
        self.calls.append((start, length))
        assert length == 1 and start >= 1
        if not self.items:
            raise AttributeError("Runs unavailable")
        start = min(start, len(self.items))
        self.items[start - 1].Start = 1 + sum(
            len(item.Text.encode("utf-16-le")) // 2 for item in self.items[: start - 1]
        )
        return self.items[start - 1]


def shape(runs=None, *, shape_type=17, text=None, shape_id=7):
    text_range = TextRange([run("발표")] if runs is None else runs, text=text)
    return Properties(
        Id=shape_id,
        Name="제목",
        Type=shape_type,
        HasTextFrame=-1,
        Left=0.0,
        Top=0.0,
        Width=300.0,
        Height=200.0,
        TextFrame=Properties(Orientation=1, TextRange=text_range),
        TextFrame2=Properties(Orientation=1, TextRange=text_range),
        Rotation=0.0,
        HorizontalFlip=0,
        VerticalFlip=0,
        Visible=-1,
        AutoShapeType=1,
        PlaceholderFormat=Properties(ContainedType=14),
        ThreeD=Properties(Visible=0),
        Fill=fill(),
        Line=Properties(Visible=0),
        ZOrderPosition=1,
    )


def make_slide(shapes):
    for position, item in enumerate(shapes, 1):
        item.ZOrderPosition = position
    return Properties(
        Shapes=Collection(shapes),
        Background=Properties(Fill=fill(rgb=0xAA8866)),
        DisplayMasterShapes=0,
        FollowMasterBackground=0,
        CustomLayout=Properties(Shapes=Collection([])),
        HeadersFooters=Properties(
            DateAndTime=Properties(Visible=0), Footer=Properties(Visible=0), SlideNumber=Properties(Visible=0)
        ),
    )


def extract(slide):
    presentation = Properties(
        PageSetup=Properties(SlideWidth=960.0, SlideHeight=540.0), Slides=Collection([slide])
    )
    saved = SavedPresentationCopy(Path("original.pptx"), Path("copy.pptx"), "saved-version")
    return extract_snapshot(presentation, saved)


def measure(item, **slide_properties):
    slide = make_slide([item])
    for name, value in slide_properties.items():
        setattr(slide, name, value)
    return extract(slide).shapes[0]


def test_mixed_runs_preserve_text_sizes_bold_and_resolved_colors():
    item = shape(
        [
            run("작은 ", size=12.0, bold=0, rgb=0x0000FF),
            run("큰\r", size=32.0, bold=-1, rgb=0x00FF00, color_type=2),
            run("한글😀", size=18.0, bold=0, rgb=0xFF0000),
        ]
    )
    result = measure(item)
    assert result.text == "작은 큰\r한글😀"
    assert result.runs == (
        opaque_run("작은 ", 12.0, (255, 0, 0), False),
        opaque_run("큰\r", 32.0, (0, 255, 0), True),
        opaque_run("한글😀", 18.0, (0, 0, 255), False),
    )
    assert item.TextFrame.TextRange.calls == [(1, 1), (2, 1), (3, 1)]
    assert result.box == Box(0.0, 0.0, 300.0, 200.0)
    assert result.rendered_box == Box(12.5, 16.25, 125.5, 35.0)
    assert result.background == (255, 255, 255)
    assert result.supported


def test_equal_measured_attributes_do_not_merge_com_runs():
    result = measure(shape([run("A"), run("B")]))
    assert [item.text for item in result.runs] == ["A", "B"]


def test_inherited_theme_format_uses_the_values_resolved_by_com():
    result = measure(shape([run("상속", size=28.5, bold=-1, color_type=2, rgb=0xCC8844)]))
    assert result.runs == (opaque_run("상속", 28.5, (68, 136, 204), True),)


@pytest.mark.parametrize("size", [0, -2, -1, None, float("nan"), float("inf"), "24"])
def test_unknown_font_size_keeps_color_bold_and_the_next_run(size):
    result = measure(shape([run("A", size=size, bold=-1), run("B", size=36)]))
    assert result.runs[0] == opaque_run("A", None, (17, 34, 51), True)
    assert result.runs[1].font_size_pt == 36.0
    assert any("font size" in note for note in result.notes)
    assert result.rendered_box is not None and result.background is not None


@pytest.mark.parametrize("failure", [AttributeError(), NotImplementedError(), ComError(0x80004001)])
def test_unavailable_individual_font_property_does_not_erase_other_measurements(failure):
    result = measure(shape([run("A", size=failure), run("B", bold=-1)]))
    assert result.runs[0].font_size_pt is None
    assert result.runs[0].color == (17, 34, 51)
    assert result.runs[0].bold is False
    assert result.runs[1].bold is True
    assert result.supported and result.background == (255, 255, 255)


def test_unavailable_font_keeps_the_run_text_and_rendered_bounds():
    item = run("A")
    item.Font = AttributeError("Font unavailable")
    result = measure(shape([item]))
    assert result.runs == (TextRun("A", fill_kind="unknown"),)
    assert result.rendered_box is not None
    assert result.supported


@pytest.mark.parametrize("bold", [-2, 1, 2, None, True])
def test_mixed_or_unknown_bold_is_none_without_losing_other_values(bold):
    result = measure(shape([run("A", bold=bold)]))
    assert result.runs[0] == opaque_run("A", 24.0, (17, 34, 51), None)


@pytest.mark.parametrize("color_type", [-2, 0, 3, None])
def test_mixed_or_unknown_color_type_is_not_reported_as_black(color_type):
    result = measure(shape([run("A", rgb=0, color_type=color_type)]))
    assert result.runs[0].color is None
    assert result.runs[0].font_size_pt == 24.0
    assert result.runs[0].bold is False


@pytest.mark.parametrize("rgb", [-2, -2147483648, 0x1000000, None, "#000000", AttributeError()])
def test_unresolved_color_has_no_default_rgb(rgb):
    result = measure(shape([run("A", rgb=rgb)]))
    assert result.runs[0].color is None
    assert result.runs[0].font_size_pt == 24.0
    assert any("color" in note for note in result.notes)


@pytest.mark.parametrize(
    "rgb,expected", [(0, (0, 0, 0)), (0xFFFFFF, (255, 255, 255)), (0xABCDEF, (239, 205, 171))]
)
def test_confirmed_rgb_values_are_preserved(rgb, expected):
    assert measure(shape([run("A", rgb=rgb)])).runs[0].color == expected


def test_structurally_unavailable_runs_preserve_plain_text_and_other_measurements():
    item = shape()
    item.TextFrame.TextRange.runs_failure = AttributeError("Runs unavailable")
    result = measure(item)
    assert result.text == "발표" and result.runs == ()
    assert result.rendered_box is not None and result.background is not None
    assert any("segmentation" in note for note in result.notes)


@pytest.mark.parametrize("mode", ["missing-text", "coverage", "count"])
def test_unverifiable_segmentation_does_not_create_misleading_offsets(mode):
    first, second = run("A"), run("B")
    item = shape([first, second])
    if mode == "missing-text":
        second.Text = AttributeError("Run text unavailable")
    elif mode == "coverage":
        item.TextFrame.TextRange.Text = "ABC"
    else:
        item.TextFrame.TextRange.runs_failure = AttributeError("Run segmentation unavailable")
    result = measure(item)
    assert result.runs == ()
    assert result.text in ("AB", "ABC")
    assert result.rendered_box is not None
    assert result.supported and result.notes


def test_empty_text_does_not_infer_style_from_the_insertion_point():
    item = shape([], text="")
    item.TextFrame.TextRange.runs_failure = AssertionError("Empty text must not request runs")
    item.TextFrame.TextRange.BoundLeft = AssertionError("Empty text has no measured glyph bounds")
    result = measure(item)
    assert result.text == "" and result.runs == ()
    assert result.background is None and result.rendered_box is None
    assert result.supported and any("Empty text" in note for note in result.notes)


@pytest.mark.parametrize(
    "property_name,value",
    [
        ("Rotation", 90),
        ("Rotation", 270),
        ("Rotation", None),
        ("HorizontalFlip", -1),
        ("VerticalFlip", -1),
        ("Visible", 0),
    ],
)
def test_unverified_shape_transform_does_not_discard_font_measurements(property_name, value):
    item = shape()
    setattr(item, property_name, value)
    result = measure(item)
    assert result.rendered_box is None and result.background is None
    assert result.runs[0].font_size_pt == 24.0
    assert result.supported and any("Rendered bounds" in note for note in result.notes)


@pytest.mark.parametrize("orientation", [2, 3, 4, -2, None])
def test_vertical_or_unknown_text_orientation_keeps_bounds_unknown(orientation):
    item = shape()
    item.TextFrame.Orientation = orientation
    result = measure(item)
    assert result.rendered_box is None
    assert result.runs[0].color == (17, 34, 51)


@pytest.mark.parametrize(
    "name,value",
    [("BoundWidth", -1), ("BoundHeight", float("nan")), ("BoundLeft", AttributeError()), ("BoundTop", None)],
)
def test_incomplete_or_invalid_text_bounds_do_not_fall_back_to_shape_box(name, value):
    item = shape()
    setattr(item.TextFrame.TextRange, name, value)
    result = measure(item)
    assert result.rendered_box is None and result.box == Box(0, 0, 300, 200)
    assert result.runs[0].font_size_pt == 24.0


def test_negative_coordinates_are_preserved_without_unit_conversion():
    item = shape()
    item.Left = -50.0
    item.TextFrame.TextRange.BoundLeft = -12.5
    result = measure(item)
    assert result.box.x == -50.0 and result.rendered_box.x == -12.5
    assert result.background is not None


def test_3d_text_keeps_formatting_but_not_unverified_bounds():
    item = shape()
    item.ThreeD.Visible = -1
    result = measure(item)
    assert result.rendered_box is None and result.background is None
    assert result.runs[0].font_size_pt == 24.0
    assert any("3D" in note for note in result.notes)


@pytest.mark.parametrize("color_type", [1, 2])
def test_solid_opaque_shape_background_uses_resolved_rgb(color_type):
    item = shape()
    item.Fill = fill(rgb=0x765432, color_type=color_type)
    assert measure(item).background == (50, 84, 118)


@pytest.mark.parametrize(
    "fill_value",
    [
        fill(fill_type=3),
        fill(fill_type=6),
        fill(fill_type=2),
        fill(fill_type=4),
        fill(fill_type=-2),
        fill(transparency=0.5),
        fill(transparency=1),
        fill(transparency=None),
        fill(visible=-2),
    ],
)
def test_ambiguous_backgrounds_preserve_font_and_bounds_without_inventing_color(fill_value):
    item = shape()
    item.Fill = fill_value
    result = measure(item)
    assert result.background is None
    assert result.rendered_box is not None and result.runs[0].font_size_pt == 24.0
    assert result.notes


def test_unfilled_single_shape_can_use_an_explicit_plain_slide_background():
    item = shape()
    item.Fill.Visible = 0
    result = measure(item)
    assert result.background == (102, 136, 170)


@pytest.mark.parametrize("mode", ["inherited", "master", "layout", "footer", "date", "page", "gradient"])
def test_unverified_slide_background_layers_are_not_assumed_white(mode):
    item = shape()
    item.Fill.Visible = 0
    slide = make_slide([item])
    if mode == "inherited":
        slide.FollowMasterBackground = -1
    elif mode == "master":
        slide.DisplayMasterShapes = -1
    elif mode == "layout":
        # 레이아웃 장식은 배경 그래픽이 표시될 때만 슬라이드에 그려짐
        slide.DisplayMasterShapes = -1
        slide.CustomLayout.Shapes = Collection([object()])
    elif mode in {"footer", "date", "page"}:
        name = {"footer": "Footer", "date": "DateAndTime", "page": "SlideNumber"}[mode]
        getattr(slide.HeadersFooters, name).Visible = -1
    else:
        slide.Background.Fill.Type = 3
    result = extract(slide).shapes[0]
    assert result.background is None
    assert result.runs[0].color == (17, 34, 51)


@pytest.mark.parametrize("mode", ["ellipse", "line", "outside"])
def test_unconfirmed_background_coverage_is_none(mode):
    item = shape(shape_type=1)
    if mode == "ellipse":
        item.AutoShapeType = 9
    elif mode == "line":
        item.Line.Visible = -1
    else:
        item.TextFrame.TextRange.BoundWidth = 500.0
    result = measure(item)
    assert result.background is None
    assert result.rendered_box is not None and result.runs[0].font_size_pt == 24.0


def test_higher_slide_objects_make_background_unknown_without_a_layout_judgment():
    target, higher = shape(), shape(shape_id=99)
    result = extract(make_slide([target, higher]))
    assert result.shapes[0].background is None
    assert result.shapes[1].background == (255, 255, 255)
    assert result.shapes[0].supported
    assert any("higher slide objects" in note for note in result.shapes[0].notes)


def test_frontmost_opaque_rectangle_covers_lower_picture_layers():
    picture, target = shape(shape_type=13, shape_id=99), shape()
    picture.TextFrame = AssertionError("Do not read unsupported objects")
    result = extract(make_slide([picture, target]))
    assert not result.shapes[0].supported
    assert result.shapes[1].background == (255, 255, 255)


def test_frontmost_unfilled_text_with_other_objects_has_unknown_background():
    target = shape()
    target.Fill.Visible = 0
    result = extract(make_slide([shape(shape_id=99), target]))
    assert result.shapes[1].background is None


def test_supported_type_is_preserved_when_text_frame_presence_is_unknown():
    item = shape(shape_type=14)
    item.HasTextFrame = AttributeError()
    result = measure(item)
    assert result.supported
    assert result.runs == () and result.rendered_box is None
    assert any("HasTextFrame" in note for note in result.notes)


def test_notes_are_local_to_the_object_and_limitations_describe_the_reader():
    first, second = shape(), shape(shape_id=99)
    first.TextFrame.TextRange.items[0].Font.Size = None
    result = extract(make_slide([first, second]))
    assert any("font size" in note for note in result.shapes[0].notes)
    assert not any("font size" in note for note in result.shapes[1].notes)
    assert any("COM measurements" in note for note in result.limitations)
    assert all("not measured" not in note for note in result.limitations)


@pytest.mark.parametrize("hresult", [0x8001010A, 0x80010001, 0x80010108])
@pytest.mark.parametrize("property_name", ["size", "rgb", "bounds", "background"])
def test_unknown_com_failures_are_not_converted_to_partial_success(property_name, hresult):
    item = shape()
    failure = ComError(hresult)
    if property_name == "size":
        item.TextFrame.TextRange.items[0].Font.Size = failure
    elif property_name == "rgb":
        item.TextFrame.TextRange.items[0].Font.Color.RGB = failure
    elif property_name == "bounds":
        item.TextFrame.TextRange.BoundLeft = failure
    else:
        item.Fill.Transparency = failure
    with pytest.raises(ComError):
        measure(item)


@pytest.mark.parametrize("fails", [False, True])
def test_reader_session_integration_returns_plain_measurements_and_cleans_up(tmp_path, monkeypatch, fails):
    source = tmp_path / "서식 테스트.PPTX"
    Presentation().save(source)
    original = source.read_bytes()
    item = shape([run("통합", bold=-1, size=30.0)])
    if fails:
        item.TextFrame.TextRange.items[0].Font.Size = ComError(0x8001010A)
    slide = make_slide([item])
    events, copies = [], []

    def close():
        assert copies[0].is_file()
        events.append("close")

    presentation = Properties(
        PageSetup=Properties(SlideWidth=960.0, SlideHeight=540.0), Slides=Collection([slide]), Close=close
    )

    def open_copy(path, **options):
        copies.append(Path(path))
        assert copies[0] != source and copies[0].read_bytes() == original
        return presentation

    runtime = SimpleNamespace(
        com_error=ComError,
        initialize=lambda: events.append("initialize"),
        connect=lambda: Properties(Presentations=Properties(Open=open_copy)),
        pump_messages=lambda: None,
        uninitialize=lambda: events.append("uninitialize"),
    )
    monkeypatch.setattr(session, "_load_runtime", lambda: runtime)
    reader = PowerPointDeckReader(session.PowerPointReadSession(retry_delay_seconds=0))
    if fails:
        with pytest.raises(PowerPointBusyError):
            reader.read(source)
    else:
        result = reader.read(source)
        assert result.source_sha256 == sha256(original).hexdigest()
        assert asdict(result)["shapes"][0]["runs"][0] == {
            "text": "통합",
            "font_size_pt": 30.0,
            "color": (17, 34, 51),
            "bold": True,
            "source_color": (17, 34, 51),
            "transparency": 0.0,
            "fill_kind": "solid",
            "fill_visible": True,
        }
    assert source.read_bytes() == original
    assert events == ["initialize", "close", "uninitialize"]
    assert not copies[0].parent.exists()
