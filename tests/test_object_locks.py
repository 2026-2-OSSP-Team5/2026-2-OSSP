import pytest
from PySide6.QtWidgets import QApplication

from slideguard.adapters.ooxml.reader import OoxmlDeckReader
from slideguard.adapters.storage.sqlite import SqliteWorkspaceSettingsRepository
from slideguard.domain.policy import AnalysisPolicy
from slideguard.ui import overlay
from slideguard.ui.object_locks import ObjectLocksEditor
from tests.fixtures.presentations import make_deck


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "presentation.pptx"
    path.write_bytes(make_deck().data)
    return path


def test_adds_current_powerpoint_selection_and_displays_names_and_content(application, document):
    shapes = OoxmlDeckReader().read(document).shapes[:2]
    editor = ObjectLocksEditor(selection_provider=lambda: (document, shapes))
    editor.set_document(document, ())
    try:
        assert editor.objects.count() == 0
        editor.add_button.click()
        assert editor.keys == {shape.key for shape in shapes}
        assert editor.objects.count() == 2
        label = editor.objects.item(0).text()
        assert "1번 슬라이드" in label and shapes[0].name in label
        assert "기본 텍스트" in label
        assert shapes[0].key not in label
        editor.add_button.click()
        assert editor.objects.count() == 2
    finally:
        editor.close()


def test_reveals_selected_object_and_can_remove_its_lock(application, document):
    shapes = OoxmlDeckReader().read(document).shapes[:2]
    revealed = []
    editor = ObjectLocksEditor(revealer=lambda *args: revealed.append(args))
    editor.set_document(document, {shape.key for shape in shapes})
    try:
        assert not editor.remove_button.isEnabled()
        editor.objects.setCurrentRow(0)
        editor.reveal_button.click()
        assert revealed == [(document, shapes[0].key)]
        editor.remove_button.click()
        assert editor.keys == {shapes[1].key}
    finally:
        editor.close()


def test_selection_from_another_document_does_not_change_locks(application, document):
    shapes = OoxmlDeckReader().read(document).shapes[:1]
    editor = ObjectLocksEditor(selection_provider=lambda: (document.with_name("other.pptx"), shapes))
    editor.set_document(document, ())
    try:
        editor.add_button.click()
        assert not editor.keys
        assert document.name in editor.status.text()
    finally:
        editor.close()


def test_selection_errors_are_displayed_without_losing_existing_locks(application, document):
    def unavailable():
        raise ValueError("PowerPoint에서 객체를 선택하세요.")

    editor = ObjectLocksEditor(selection_provider=unavailable)
    editor.set_document(document, {"1:2"})
    try:
        editor.add_button.click()
        assert editor.keys == {"1:2"}
        assert "객체를 선택" in editor.status.text()
    finally:
        editor.close()


def test_unavailable_objects_can_still_be_unlocked(application, tmp_path):
    editor = ObjectLocksEditor()
    editor.set_document(tmp_path / "removed.pptx", {"5:3"})
    try:
        assert editor.objects.item(0).text() == "5번 슬라이드 · 저장된 고정 객체"
        editor.objects.setCurrentRow(0)
        editor.remove_button.click()
        assert not editor.keys
    finally:
        editor.close()


def test_settings_save_and_cancel_use_the_object_list(application, document, tmp_path, monkeypatch):
    repository = SqliteWorkspaceSettingsRepository(tmp_path / "state.sqlite3")
    shapes = OoxmlDeckReader().read(document).shapes[:2]
    repository.save(
        tmp_path, False, policy=AnalysisPolicy(locked_shapes=frozenset({shapes[0].key})), document=document
    )
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: repository)
    monkeypatch.setattr(overlay.startup, "is_enabled", lambda: False)
    dialog = overlay.SettingsDialog(path_provider=lambda: document)
    try:
        dialog.load()
        assert dialog.locked_objects.keys == {shapes[0].key}
        dialog.locked_objects.selection_provider = lambda: (document, shapes)
        dialog.locked_objects.add_button.click()
        dialog.reject()
        assert repository.load().policy_for(document).locked_shapes == {shapes[0].key}
        dialog.load(lock_shape=shapes[1].key)
        assert dialog.locked_objects.keys == {shape.key for shape in shapes}
        dialog.save()
        assert repository.load().policy_for(document).locked_shapes == {shape.key for shape in shapes}
    finally:
        dialog.close()
