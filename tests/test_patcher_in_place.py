"""원본에 적용(백업 후 저장)·되돌리기 테스트. PowerPoint 대신 가짜 문서를 사용합니다."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from slideguard.adapters.powerpoint.patcher import (
    ApplyRejected,
    PowerPointPatcher,
    default_backup_dir,
    owner_file_of,
    restore_backup,
    sha256_of,
)
from slideguard.domain.models import AnalysisReport, DeckSnapshot
from slideguard.domain.models import ChangeProposal as TeamProposal
from slideguard.planning import ChangeProposal, Property
from tests.fakes import FakePolicy
from tests.test_patcher import Doc, DocRun, DocShape

ORIGINAL_BYTES = b"original pptx bytes"
SAVED_BYTES = b"patched pptx bytes"


class FileDoc(Doc):
    """저장하면 실제 파일 내용이 바뀌는 가짜 문서 (원본 저장 확인용)."""

    def __init__(self, shape, path, *, was_open=False):
        super().__init__(shape)
        self.path = Path(path)
        self.was_open = was_open

    def save(self):
        super().save()
        self.path.write_bytes(SAVED_BYTES)


def opener(doc, opened=None):
    @contextmanager
    def open_original(path):
        if opened is not None:
            opened.append(path)
        yield doc

    return open_original


@pytest.fixture
def original(tmp_path):
    path = tmp_path / "deck.pptx"
    path.write_bytes(ORIGINAL_BYTES)
    return path


def size_proposal(before=10.0, after=14.0):
    return ChangeProposal(1, 5, Property.FONT_SIZE, before, after, 0.1, "test")


def patcher_for(doc, opened=None):
    return PowerPointPatcher(open_original=opener(doc, opened))


def test_backup_is_made_then_original_is_saved(original, tmp_path):
    run = DocRun(10.0, "000000")
    doc = FileDoc(DocShape(runs=[run]), original)
    backup_dir = tmp_path / "backups"

    result = patcher_for(doc).apply_in_place(
        str(original), sha256_of(str(original)), [size_proposal()], FakePolicy(), str(backup_dir)
    )

    assert result.in_place and result.output_path == str(original)
    assert run.size == 14.0 and doc.saved
    assert original.read_bytes() == SAVED_BYTES
    backup = Path(result.backup_path)
    assert backup.parent == backup_dir and backup.read_bytes() == ORIGINAL_BYTES
    assert backup.name.startswith("deck_") and backup.suffix == ".pptx"


def test_changed_original_is_rejected_before_backup(original, tmp_path):
    doc = FileDoc(DocShape(runs=[DocRun(10.0, "000000")]), original)
    with pytest.raises(ApplyRejected, match="변경"):
        patcher_for(doc).apply_in_place(
            str(original), "0" * 64, [size_proposal()], FakePolicy(), str(tmp_path / "b")
        )
    assert not (tmp_path / "b").exists()
    assert not doc.saved


def test_nothing_applied_keeps_original_and_removes_backup(original, tmp_path):
    run = DocRun(20.0, "000000")  # 이미 기준 이상 → 값이 달라 거절
    doc = FileDoc(DocShape(runs=[run]), original)
    backup_dir = tmp_path / "backups"

    result = patcher_for(doc).apply_in_place(
        str(original), sha256_of(str(original)), [size_proposal()], FakePolicy(), str(backup_dir)
    )

    assert result.output_path is None and result.backup_path is None
    assert not doc.saved and original.read_bytes() == ORIGINAL_BYTES
    assert list(backup_dir.iterdir()) == []


def test_locked_proposals_do_not_even_open_or_back_up(original, tmp_path):
    doc = FileDoc(DocShape(runs=[DocRun(10.0, "000000")]), original)
    opened = []
    policy = FakePolicy(locked_shapes=frozenset({(1, 5)}))

    result = patcher_for(doc, opened).apply_in_place(
        str(original), sha256_of(str(original)), [size_proposal()], policy, str(tmp_path / "b")
    )

    assert opened == [] and result.output_path is None
    assert not (tmp_path / "b").exists()


def test_failure_before_any_change_removes_backup(original, tmp_path):
    @contextmanager
    def broken(path):
        raise RuntimeError("PowerPoint를 열 수 없음")
        yield

    backup_dir = tmp_path / "backups"
    with pytest.raises(RuntimeError):
        PowerPointPatcher(open_original=broken).apply_in_place(
            str(original), sha256_of(str(original)), [size_proposal()], FakePolicy(), str(backup_dir)
        )
    assert list(backup_dir.iterdir()) == []
    assert original.read_bytes() == ORIGINAL_BYTES


def test_open_document_flag_is_reported(original, tmp_path):
    doc = FileDoc(DocShape(runs=[DocRun(10.0, "000000")]), original, was_open=True)
    result = patcher_for(doc).apply_in_place(
        str(original), sha256_of(str(original)), [size_proposal()], FakePolicy(), str(tmp_path / "b")
    )
    assert result.was_open


def test_apply_to_original_team_interface_returns_backup(original, tmp_path):
    run = DocRun(10.0, "000000")
    doc = FileDoc(DocShape(runs=[run]), original)
    team = TeamProposal(
        id="font.size@1:5",
        finding_id="typography:1:5:1",
        shape_key="1:5",
        property=Property.FONT_SIZE,
        before=10.0,
        after=14.0,
        cost=0.4,
    )
    deck = DeckSnapshot(str(original), sha256_of(str(original)), 1, 720.0, 540.0)
    report = AnalysisReport(deck=deck, proposals=[team])
    patcher = patcher_for(doc)

    backup = patcher.apply_to_original(report, [team.id], tmp_path / "b", FakePolicy())

    assert backup.read_bytes() == ORIGINAL_BYTES
    assert patcher.last_result.applied and run.size == 14.0


def test_restore_backup_brings_original_back(original, tmp_path):
    backup = tmp_path / "backup.pptx"
    backup.write_bytes(ORIGINAL_BYTES)
    original.write_bytes(SAVED_BYTES)

    restore_backup(backup, original)

    assert original.read_bytes() == ORIGINAL_BYTES


def test_restore_is_refused_while_powerpoint_has_the_file_open(original, tmp_path):
    backup = tmp_path / "backup.pptx"
    backup.write_bytes(ORIGINAL_BYTES)
    original.write_bytes(SAVED_BYTES)
    Path(owner_file_of(str(original))).write_bytes(b"")

    with pytest.raises(ApplyRejected, match="열려"):
        restore_backup(backup, original)
    assert original.read_bytes() == SAVED_BYTES


# ---------------------------------------------------------------- 열린 문서 찾기 (가짜 PowerPoint)


class FakePres:
    def __init__(self, full_name, saved=-1, read_only=0):
        self.FullName = full_name
        self.Saved = saved
        self.ReadOnly = read_only


class FakePresentations:
    def __init__(self, items):
        self.items = items
        self.opened = []

    @property
    def Count(self):  # noqa: N802 (COM 이름)
        return len(self.items)

    def Item(self, index):  # noqa: N802
        return self.items[index - 1]

    def Open(self, path, *flags):  # noqa: N802
        self.opened.append(path)
        return FakePres(path)


class FakeApp:
    def __init__(self, items):
        self.Presentations = FakePresentations(items)


def session_open(path, items):
    from slideguard.adapters.powerpoint.patcher import OriginalDocumentSession

    session = OriginalDocumentSession(str(path))
    app = FakeApp(items)
    return session, app, session._open(app)


def test_open_presentation_is_reused_without_opening_again(original):
    session, app, pres = session_open(original, [FakePres(str(original))])
    assert session._attached and app.Presentations.opened == []
    assert pres.FullName == str(original)


def test_closed_file_is_opened_without_window(original, tmp_path):
    session, app, _pres = session_open(original, [FakePres(str(tmp_path / "other.pptx"))])
    assert not session._attached and app.Presentations.opened == [str(original)]


def test_unsaved_changes_in_powerpoint_are_refused(original):
    with pytest.raises(ApplyRejected, match="저장하지 않은"):
        session_open(original, [FakePres(str(original), saved=0)])


def test_read_only_presentation_is_refused(original):
    with pytest.raises(ApplyRejected, match="읽기 전용"):
        session_open(original, [FakePres(str(original), read_only=-1)])


def test_default_backup_dir_is_outside_work_folders(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert default_backup_dir() == tmp_path / "appdata" / "SlideGuard" / "backups"
