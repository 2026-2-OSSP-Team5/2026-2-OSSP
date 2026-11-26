"""Select and lock actual PowerPoint objects without entering object IDs."""

import gc

import pytest
from PySide6.QtWidgets import QApplication

from slideguard.ui.object_locks import ObjectLocksEditor
from tests.fixtures.presentations import make_deck

pytestmark = pytest.mark.powerpoint


def test_native_selection_can_be_added_revealed_and_unlocked(powerpoint_environment, tmp_path):
    import pythoncom
    import win32com.client

    path = tmp_path / "object-selection.pptx"
    original = make_deck().data
    path.write_bytes(original)
    qt = QApplication.instance() or QApplication([])
    pythoncom.CoInitialize()
    application = document = window = shape = None
    editor = ObjectLocksEditor()
    try:
        application = win32com.client.DispatchEx("PowerPoint.Application")
        document = application.Presentations.Open(str(path), ReadOnly=0, Untitled=0, WithWindow=-1)
        window = document.Windows.Item(1)
        window.Activate()
        window.View.GotoSlide(1)
        shape = document.Slides.Item(1).Shapes.Item(1)
        first_id = int(shape.Id)
        shape.Select()
        editor.set_document(path, ())
        editor.add_button.click()
        assert editor.keys == {f"1:{first_id}"}, editor.status.text()
        assert "기본 텍스트" in editor.objects.item(0).text()

        shape = document.Slides.Item(1).Shapes.Item(2)
        shape.Select()
        second_id = int(shape.Id)
        editor.add_button.click()
        assert editor.keys == {f"1:{first_id}", f"1:{second_id}"}
        editor.objects.setCurrentRow(0)
        editor.reveal_button.click()
        assert window.Selection.ShapeRange.Item(1).Id == first_id
        editor.remove_button.click()
        assert editor.keys == {f"1:{second_id}"}
        assert document.Saved == -1
        assert path.read_bytes() == original
        assert qt is QApplication.instance()
    finally:
        editor.close()
        shape = window = None
        if document is not None:
            document.Saved = -1
            document.Close()
        document = application = None
        gc.collect()
        pythoncom.CoUninitialize()
