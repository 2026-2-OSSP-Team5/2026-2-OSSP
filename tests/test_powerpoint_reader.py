import io
import threading
import zipfile
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from pptx import Presentation

from slideguard.adapters.powerpoint import copies, session
from slideguard.adapters.powerpoint.errors import (
    PowerPointBusyError,
    PowerPointCleanupError,
    PowerPointExtractionError,
    PowerPointOpenError,
    PowerPointSourceChangedError,
)
from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
from slideguard.domain.models import Box


class ComError(Exception):
    def __init__(self, hresult):
        self.hresult = hresult
        super().__init__("COM property read failed")


class Collection:
    def __init__(self, items):
        self.items = items
        self.Count = len(items)

    def Item(self, number):
        assert 1 <= number <= self.Count
        return self.items[number - 1]


class ShapeProxy:
    def __init__(
        self, shape_id=7, *, shape_type=17, text="발표\r둘째 줄", name="제목", frame=-1, failures=None
    ):
        self._failures = failures or {}
        self.Id = shape_id
        self.Name = name
        self.Type = shape_type
        self.Left, self.Top, self.Width, self.Height = -12.5, 24.0, 320.5, 72.0
        self.HasTextFrame = frame
        self.TextFrame = SimpleNamespace(TextRange=SimpleNamespace(Text=text))
        self.PlaceholderFormat = SimpleNamespace(ContainedType=14)

    def __getattribute__(self, name):
        if name != "_failures":
            failure = object.__getattribute__(self, "_failures").get(name)
            if failure is not None:
                raise failure
        return object.__getattribute__(self, name)

    def __getattr__(self, name):
        if name in {"GroupItems", "Table", "Chart", "SmartArt", "Font", "Fill"}:
            raise AssertionError(f"Out-of-scope content was traversed: {name}")
        raise AttributeError(name)


class PresentationProxy:
    def __init__(self, runtime, slides):
        self.runtime = runtime
        self.Slides = Collection([SimpleNamespace(Shapes=Collection(shapes)) for shapes in slides])
        self.PageSetup = SimpleNamespace(SlideWidth=960.0, SlideHeight=540.0)

    def Close(self):
        assert self.runtime.copy_path.is_file(), "Delete the copy only after the COM document is closed"
        self.runtime.events.append("close")
        if self.runtime.close_failure:
            raise self.runtime.close_failure


class Runtime:
    com_error = ComError

    def __init__(self):
        self.events = []
        self.copy_path = None
        self.copy_bytes = None
        self.on_open = None
        self.open_failure = None
        self.close_failure = None
        self.presentation = PresentationProxy(self, [[ShapeProxy()], []])

    def initialize(self):
        self.events.append("initialize")

    def connect(self):
        return SimpleNamespace(Presentations=SimpleNamespace(Open=self.open))

    def open(self, path, **options):
        assert options == {"ReadOnly": -1, "Untitled": -1, "WithWindow": 0}
        self.copy_path = Path(path)
        self.copy_bytes = self.copy_path.read_bytes()
        self.events.append("open")
        if self.open_failure:
            raise self.open_failure
        if self.on_open:
            self.on_open()
        return self.presentation

    def pump_messages(self):
        pass

    def uninitialize(self):
        assert self.copy_path.is_file(), "Keep the temporary copy until COM cleanup finishes"
        self.events.append("uninitialize")


def pptx_bytes(text="original"):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(0, 0, 914400, 914400).text = text
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "한글 발표.PPTX"
    source.write_bytes(pptx_bytes())
    return source


@pytest.fixture
def runtime(monkeypatch):
    runtime = Runtime()
    monkeypatch.setattr(session, "_load_runtime", lambda: runtime)
    return runtime


@pytest.fixture
def reader(runtime):
    return PowerPointDeckReader(session.PowerPointReadSession(retry_delay_seconds=0))


@pytest.fixture
def temporary_directories(monkeypatch, tmp_path):
    original = copies.TemporaryDirectory
    directories = []

    def make_directory(**options):
        directory = original(dir=tmp_path, **options)
        directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(copies, "TemporaryDirectory", make_directory)
    return directories


def test_read_returns_saved_revision_metadata_and_preserves_source(reader, runtime, source):
    original = source.read_bytes()
    result = reader.read(source)

    assert result.source_path == str(source.resolve())
    assert result.source_sha256 == sha256(runtime.copy_bytes).hexdigest() == sha256(original).hexdigest()
    assert result.slide_count == 2
    assert (result.width_pt, result.height_pt) == (960.0, 540.0)
    assert len(result.shapes) == 1
    shape = result.shapes[0]
    assert (shape.slide_number, shape.shape_id, shape.name, shape.key) == (1, 7, "제목", "1:7")
    assert shape.box == Box(-12.5, 24.0, 320.5, 72.0)
    assert shape.text == "발표\r둘째 줄"
    assert shape.supported
    assert shape.runs == ()
    assert shape.background is None and shape.rendered_box is None
    assert result.limitations
    assert asdict(result)["shapes"][0]["text"] == "발표\r둘째 줄"
    assert source.read_bytes() == original
    assert runtime.copy_path != source
    assert runtime.events == ["initialize", "open", "close", "uninitialize"]
    assert not runtime.copy_path.parent.exists()


def test_relative_path_is_normalized(reader, source, monkeypatch):
    monkeypatch.chdir(source.parent)
    assert reader.read(Path(source.name)).source_path == str(source.resolve())


def test_different_ctime_from_handle_does_not_reject_unchanged_bytes(reader, source, monkeypatch):
    real_fstat = copies.os.fstat

    def handle_stat(descriptor):
        info = real_fstat(descriptor)
        return SimpleNamespace(
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns,
            st_ctime_ns=info.st_ctime_ns + 1000000,
        )

    monkeypatch.setattr(copies.os, "fstat", handle_stat)
    assert reader.read(source).source_sha256 == sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize("slides", [[], [[]], [[], []]])
def test_empty_deck_and_empty_slides_are_preserved(reader, runtime, source, slides):
    runtime.presentation = PresentationProxy(runtime, slides)
    result = reader.read(source)
    assert result.slide_count == len(slides)
    assert result.shapes == ()


def test_shape_ids_are_preserved_and_scoped_to_a_slide(reader, runtime, source):
    runtime.presentation = PresentationProxy(runtime, [[ShapeProxy(40), ShapeProxy(2)], [ShapeProxy(40)]])
    result = reader.read(source)
    assert [shape.key for shape in result.shapes] == ["1:40", "1:2", "2:40"]


@pytest.mark.parametrize(
    "shape_type,frame,text", [(1, 0, ""), (1, -1, "도형"), (17, -1, ""), (14, -1, "제목")]
)
def test_primary_shape_types_include_empty_text_and_basic_shapes(
    reader, runtime, source, shape_type, frame, text
):
    shape = ShapeProxy(shape_type=shape_type, frame=frame, text=text)
    if frame == 0:
        shape._failures["TextFrame"] = AssertionError("A shape without a text frame must not access it")
    runtime.presentation = PresentationProxy(runtime, [[shape]])
    result = reader.read(source).shapes[0]
    assert result.supported
    assert result.text == text


@pytest.mark.parametrize(
    "shape_type,label",
    [
        (21, "diagram"),
        (13, "picture"),
        (999, "999"),
    ],
)
def test_unsupported_shapes_keep_metadata_without_traversing_content(
    reader, runtime, source, shape_type, label
):
    shape = ShapeProxy(
        shape_type=shape_type,
        failures={
            "HasTextFrame": AssertionError("Do not inspect unsupported text"),
            "TextFrame": AssertionError("Do not read container content"),
        },
    )
    runtime.presentation = PresentationProxy(runtime, [[shape]])
    result = reader.read(source).shapes[0]
    assert (result.shape_id, result.name, result.box) == (7, "제목", Box(-12.5, 24.0, 320.5, 72.0))
    assert not result.supported
    assert result.text == ""
    assert any(label in note for note in result.notes)


@pytest.mark.parametrize("contained_type", [13, 21])
def test_non_text_placeholder_content_is_not_marked_supported(reader, runtime, source, contained_type):
    shape = ShapeProxy(shape_type=14, failures={"TextFrame": AssertionError("Unsupported content")})
    shape.PlaceholderFormat.ContainedType = contained_type
    runtime.presentation = PresentationProxy(runtime, [[shape]])
    result = reader.read(source).shapes[0]
    assert not result.supported
    assert any("placeholder content" in note for note in result.notes)


def test_placeholder_without_text_frame_is_not_marked_supported(reader, runtime, source):
    runtime.presentation = PresentationProxy(runtime, [[ShapeProxy(shape_type=14, frame=0)]])
    assert not reader.read(source).shapes[0].supported


@pytest.mark.parametrize(
    "failure", [AttributeError("missing"), NotImplementedError(), ComError(0x80004001), ComError(-2147352573)]
)
def test_optional_unavailable_text_keeps_other_objects_and_marks_the_limitation(
    reader, runtime, source, failure
):
    first = ShapeProxy(failures={"TextFrame": failure})
    runtime.presentation = PresentationProxy(runtime, [[first, ShapeProxy(20, text="다른 도형")]])
    result = reader.read(source)
    assert result.shapes[0].supported  # supported describes the type, not measurement completeness
    assert result.shapes[0].text == ""
    assert any("Shape text" in note for note in result.shapes[0].notes)
    assert result.shapes[1].text == "다른 도형"


def test_missing_optional_name_or_type_is_reported(reader, runtime, source):
    shape = ShapeProxy(failures={"Name": AttributeError(), "Type": AttributeError()})
    runtime.presentation = PresentationProxy(runtime, [[shape]])
    result = reader.read(source).shapes[0]
    assert result.name == ""
    assert result.shape_id == 7
    assert not result.supported
    assert any("Shape.Name" in note for note in result.notes)
    assert any("Shape.Type" in note for note in result.notes)


@pytest.mark.parametrize("hresult", [0x8001010A, 0x80010001, 0x80010108])
def test_busy_and_disconnected_com_errors_do_not_become_partial_success(reader, runtime, source, hresult):
    runtime.presentation = PresentationProxy(
        runtime, [[ShapeProxy(failures={"TextFrame": ComError(hresult)})]]
    )
    expected = PowerPointBusyError if hresult in {0x8001010A, 0x80010001} else PowerPointExtractionError
    with pytest.raises(expected):
        reader.read(source)
    assert runtime.events[-2:] == ["close", "uninitialize"]
    assert not runtime.copy_path.parent.exists()


@pytest.mark.parametrize("property_name", ["Id", "Left", "Top", "Width", "Height"])
def test_unavailable_identity_or_geometry_fails_the_document(reader, runtime, source, property_name):
    runtime.presentation = PresentationProxy(
        runtime, [[ShapeProxy(failures={property_name: AttributeError()})]]
    )
    with pytest.raises(PowerPointExtractionError):
        reader.read(source)
    assert runtime.events[-2:] == ["close", "uninitialize"]
    assert not runtime.copy_path.parent.exists()


@pytest.mark.parametrize(
    "property_name,value", [("Left", float("nan")), ("Width", -1), ("Height", float("inf")), ("Id", -1)]
)
def test_invalid_required_measurements_are_not_inferred(reader, runtime, source, property_name, value):
    shape = ShapeProxy()
    setattr(shape, property_name, value)
    runtime.presentation = PresentationProxy(runtime, [[shape]])
    with pytest.raises(PowerPointExtractionError):
        reader.read(source)


def test_duplicate_shape_ids_fail_instead_of_producing_ambiguous_keys(reader, runtime, source):
    runtime.presentation = PresentationProxy(runtime, [[ShapeProxy(7), ShapeProxy(7)]])
    with pytest.raises(PowerPointExtractionError, match="Duplicate shape ID"):
        reader.read(source)


@pytest.mark.parametrize("width,height", [(0, 540), (960, -1), (float("nan"), 540)])
def test_invalid_slide_dimensions_fail_the_document(reader, runtime, source, width, height):
    runtime.presentation.PageSetup = SimpleNamespace(SlideWidth=width, SlideHeight=height)
    with pytest.raises(PowerPointExtractionError):
        reader.read(source)


@pytest.mark.parametrize("name", ["deck.ppt", "deck.pptm", "~$lock.pptx", "~scratch.pptx", ".hidden.pptx"])
def test_non_saved_pptx_inputs_are_rejected_before_com(reader, runtime, source, name):
    path = source.with_name(name)
    path.write_bytes(source.read_bytes())
    with pytest.raises(PowerPointOpenError):
        reader.read(path)
    assert runtime.events == []


def test_missing_source_is_rejected_before_com(reader, runtime, tmp_path):
    with pytest.raises(FileNotFoundError):
        reader.read(tmp_path / "missing.pptx")
    assert runtime.events == []


def test_directory_with_pptx_extension_is_rejected(reader, runtime, tmp_path):
    path = tmp_path / "directory.pptx"
    path.mkdir()
    with pytest.raises(PowerPointOpenError):
        reader.read(path)
    assert runtime.events == []


def minimal_package():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("_rels/.rels", "<Relationships/>")
        package.writestr("ppt/presentation.xml", "<presentation-unique-content/>")
    return output.getvalue()


@pytest.mark.parametrize("invalid_kind", ["partial", "missing-part", "crc", "duplicate"])
def test_invalid_packages_fail_before_com_and_remove_the_copy(
    reader, runtime, source, temporary_directories, invalid_kind
):
    data = minimal_package()
    if invalid_kind == "partial":
        data = data[: len(data) // 2]
    elif invalid_kind == "crc":
        data = data.replace(b"presentation-unique", b"Presentation-unique", 1)
    elif invalid_kind == "missing-part":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as package:
            package.writestr("ppt/presentation.xml", "<p/>")
        data = output.getvalue()
    else:
        output = io.BytesIO(data)
        with zipfile.ZipFile(output, "a") as package, pytest.warns(UserWarning, match="Duplicate name"):
            package.writestr("ppt/presentation.xml", "duplicate")
        data = output.getvalue()
    source.write_bytes(data)
    with pytest.raises(PowerPointOpenError, match="Invalid or incomplete PPTX"):
        reader.read(source)
    assert runtime.events == []
    assert temporary_directories and all(not path.exists() for path in temporary_directories)
    assert source.read_bytes() == data


def test_change_during_copy_is_retryable_and_never_opens_com(
    reader, runtime, source, monkeypatch, temporary_directories
):
    original_copy = copies.shutil.copyfileobj
    changed_bytes = pptx_bytes("resaved during copying")

    def copy_then_change(original, destination, **options):
        original_copy(original, destination, **options)
        source.write_bytes(changed_bytes)

    monkeypatch.setattr(copies.shutil, "copyfileobj", copy_then_change)
    with pytest.raises(PowerPointSourceChangedError) as caught:
        reader.read(source)
    assert caught.value.retryable
    assert runtime.events == []
    assert source.read_bytes() == changed_bytes
    assert all(not path.exists() for path in temporary_directories)


def test_hash_comparison_detects_a_change_even_when_metadata_looks_unchanged(
    reader, runtime, source, monkeypatch
):
    original_copy = copies.shutil.copyfileobj
    changed_bytes = pptx_bytes("different saved revision")

    def copy_then_change(original, destination, **options):
        original_copy(original, destination, **options)
        source.write_bytes(changed_bytes)

    monkeypatch.setattr(copies.shutil, "copyfileobj", copy_then_change)
    monkeypatch.setattr(copies, "_signature", lambda _: (1,))
    with pytest.raises(PowerPointSourceChangedError, match="Copied bytes differ"):
        reader.read(source)
    assert runtime.events == []


@pytest.mark.parametrize("action", ["replace", "remove"])
def test_source_replacement_or_removal_during_preparation_is_retryable(
    reader, runtime, source, monkeypatch, action
):
    real_hash = copies._sha256
    replacement = source.with_name("replacement.pptx")
    replacement.write_bytes(pptx_bytes("replacement"))

    def hash_and_change(path):
        digest = real_hash(path)
        if path != source:
            if action == "replace":
                replacement.replace(source)
            else:
                source.unlink()
        return digest

    monkeypatch.setattr(copies, "_sha256", hash_and_change)
    with pytest.raises(PowerPointSourceChangedError):
        reader.read(source)
    assert runtime.events == []


def test_replacement_between_stat_and_open_is_detected(reader, runtime, source, monkeypatch):
    real_open = Path.open
    replacement = source.with_name("replacement.pptx")
    replacement.write_bytes(pptx_bytes("replacement"))

    def open_after_replacement(path, *args, **options):
        if path == source and args == ("rb",):
            replacement.replace(source)
        return real_open(path, *args, **options)

    monkeypatch.setattr(Path, "open", open_after_replacement)
    with pytest.raises(PowerPointSourceChangedError, match="replaced before copying"):
        reader.read(source)
    assert runtime.events == []


def test_partial_copy_io_failure_removes_temporary_files(
    reader, runtime, source, monkeypatch, temporary_directories
):
    def interrupted_copy(original, destination, **options):
        destination.write(original.read(10))
        raise OSError("copy interrupted")

    monkeypatch.setattr(copies.shutil, "copyfileobj", interrupted_copy)
    with pytest.raises(OSError, match="copy interrupted"):
        reader.read(source)
    assert runtime.events == []
    assert temporary_directories and all(not path.exists() for path in temporary_directories)


def test_resave_during_com_read_keeps_the_hash_of_the_copy(reader, runtime, source):
    original = source.read_bytes()
    changed = pptx_bytes("newer save during COM read")
    runtime.on_open = lambda: source.write_bytes(changed)
    result = reader.read(source)
    assert result.source_sha256 == sha256(original).hexdigest()
    assert result.source_sha256 != sha256(source.read_bytes()).hexdigest()
    assert source.read_bytes() == changed
    assert not runtime.copy_path.exists()


def test_modified_temporary_copy_does_not_return_a_mislabeled_snapshot(reader, runtime, source):
    original = source.read_bytes()
    runtime.on_open = lambda: runtime.copy_path.write_bytes(pptx_bytes("unexpected change"))
    with pytest.raises(PowerPointOpenError, match="temporary copy changed"):
        reader.read(source)
    assert source.read_bytes() == original
    assert runtime.events[-2:] == ["close", "uninitialize"]
    assert not runtime.copy_path.exists()


@pytest.mark.parametrize("failure_stage", ["open", "extract", "close"])
def test_session_failures_remove_copy_only_after_com_cleanup(reader, runtime, source, failure_stage):
    original = source.read_bytes()
    if failure_stage == "open":
        runtime.open_failure = ComError(0x80004005)
        expected = PowerPointOpenError
    elif failure_stage == "extract":
        runtime.presentation = PresentationProxy(
            runtime, [[ShapeProxy(failures={"Id": ValueError("bad ID")})]]
        )
        expected = PowerPointExtractionError
    else:
        runtime.close_failure = ComError(0x80004005)
        expected = PowerPointCleanupError
    with pytest.raises(expected):
        reader.read(source)
    assert runtime.events[-1] == "uninitialize"
    assert not runtime.copy_path.parent.exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize("extraction_fails", [False, True])
def test_copy_cleanup_failure_is_reported_without_losing_the_primary_error(
    reader, runtime, source, monkeypatch, extraction_fails
):
    original_factory = copies.TemporaryDirectory

    def make_directory(**options):
        directory = original_factory(**options)
        original_cleanup = directory.cleanup

        def cleanup():
            original_cleanup()
            raise PermissionError("temporary copy cleanup failed")

        directory.cleanup = cleanup
        return directory

    monkeypatch.setattr(copies, "TemporaryDirectory", make_directory)
    if extraction_fails:
        runtime.presentation = PresentationProxy(
            runtime, [[ShapeProxy(failures={"Id": ValueError("bad ID")})]]
        )
    expected = PowerPointExtractionError if extraction_fails else PowerPointCleanupError
    with pytest.raises(expected) as caught:
        reader.read(source)
    if extraction_fails:
        assert caught.value.cleanup_errors[-1].operation == "remove-copy"
        assert "bad ID" in str(caught.value)
    else:
        assert caught.value.operation == "remove-copy"
    assert not runtime.copy_path.parent.exists()


def test_reader_can_run_in_a_worker_without_returning_com_objects(reader, runtime, source):
    result = []
    worker = threading.Thread(target=lambda: result.append(reader.read(source)))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(result) == 1
    assert result[0].shapes[0].box == Box(-12.5, 24.0, 320.5, 72.0)
    assert not runtime.copy_path.exists()


def test_a_second_read_succeeds_after_the_source_stabilizes(reader, runtime, source, monkeypatch):
    original_copy = copies.shutil.copyfileobj

    def copy_then_change(original, destination, **options):
        original_copy(original, destination, **options)
        source.write_bytes(pptx_bytes("resaved"))

    with monkeypatch.context() as scope:
        scope.setattr(copies.shutil, "copyfileobj", copy_then_change)
        with pytest.raises(PowerPointSourceChangedError):
            reader.read(source)
    result = reader.read(source)
    assert result.source_sha256 == sha256(source.read_bytes()).hexdigest()
