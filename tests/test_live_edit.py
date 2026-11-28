"""렌즈 체크박스의 실시간 적용(열린 문서에 바로 적용·되돌리기) 테스트. PowerPoint 대신 가짜 문서."""

from contextlib import contextmanager

import pytest

from slideguard.adapters.powerpoint.live_edit import LiveEditor, LiveEditUnavailable
from slideguard.domain.models import AnalysisReport, ChangeProposal, DeckSnapshot
from tests.test_patcher import Doc, DocRun, DocShape


def _report(*proposals):
    deck = DeckSnapshot("C:/deck.pptx", "abc", 1, 960, 540)
    return AnalysisReport(deck=deck, findings=[], proposals=list(proposals), status="analyzed")


def _size(before=11.0, after=14.0, pid="font.size@1:5"):
    return ChangeProposal(pid, "typography:1:5:1", "1:5", "font.size", before, after, 0.1)


def _editor(doc):
    opened = []

    @contextmanager
    def session(path):
        opened.append(path)
        yield doc

    editor = LiveEditor(session)
    editor.opened = opened
    return editor


def test_check_applies_and_uncheck_restores_without_saving():
    runs = [DocRun(11.0, "000000"), DocRun(11.0, "000000")]
    doc = Doc(DocShape(runs))
    editor = _editor(doc)
    report = _report(_size())
    result = editor.apply(report, ["font.size@1:5"])
    assert result.applied == ["font.size@1:5"] and [run.size for run in runs] == [14.0, 14.0]
    assert editor.applied_ids == {"font.size@1:5"}
    assert editor.apply(report, ["font.size@1:5"]).applied == []  # 이미 적용됨: 다시 하지 않음
    result = editor.revert(["font.size@1:5"])
    assert result.applied == ["font.size@1:5"] and result.skipped == 0
    assert [run.size for run in runs] == [11.0, 11.0]
    assert editor.applied_ids == set()
    assert doc.saved is False  # 실시간 적용은 저장하지 않는다


def test_revert_leaves_values_the_user_changed_afterwards():
    runs = [DocRun(11.0, "000000"), DocRun(11.0, "000000")]
    editor = _editor(Doc(DocShape(runs)))
    editor.apply(_report(_size()), ["font.size@1:5"])
    runs[1].size = 20.0  # 적용 뒤 사용자가 PowerPoint에서 직접 바꿈
    result = editor.revert(["font.size@1:5"])
    assert result.skipped == 1
    assert [run.size for run in runs] == [11.0, 20.0]


def test_stale_proposal_is_rejected_and_nothing_is_remembered():
    shape = DocShape(geometry=(50, 50, 400, 75))  # 검사 뒤 사용자가 이미 높이를 바꿈
    proposal = ChangeProposal("shape.height@1:5", "f", "1:5", "shape.height", 60, 90, 0.1)
    editor = _editor(Doc(shape))
    result = editor.apply(_report(proposal), [proposal.id])
    assert result.applied == [] and "현재 높이" in result.rejected[0][1]
    assert shape.geometry[3] == 75 and editor.applied_ids == set()


def test_bundle_failure_rolls_back_the_part_that_was_applied():
    shape = DocShape(geometry=(50, 100, 400, 60))
    height = ChangeProposal("shape.height@1:5#overflow:1:5", "f", "1:5", "shape.height", 60, 90, 0.1)
    push = ChangeProposal(
        "shape.position@1:7#overflow:1:5", "f", "1:7", "shape.position", (50, 170), (50, 190), 0.1
    )
    editor = _editor(Doc(shape))  # 객체 7이 없어 내리기는 실패
    result = editor.apply(_report(height, push), [height.id, push.id])
    assert result.applied == [height.id] and result.rejected[0][0] == push.id
    editor.revert([height.id])
    assert shape.geometry[3] == 60


def test_fill_scrim_is_removed_again_on_revert():
    shape = DocShape()
    shape.cleared = False

    def clear_fill():
        shape.fill = None

    shape.clear_fill = clear_fill
    after = {"color": "000000", "transparency": 0.4}
    proposal = ChangeProposal("shape.fill@1:5", "f", "1:5", "shape.fill", None, after, 0.1)
    editor = _editor(Doc(shape))
    editor.apply(_report(proposal), [proposal.id])
    assert shape.fill == ("000000", 0.4)
    editor.revert([proposal.id])
    assert shape.fill is None


def test_another_document_resets_memory_and_unavailable_is_raised():
    editor = _editor(Doc(DocShape([DocRun(11.0, "000000")])))
    editor.apply(_report(_size()), ["font.size@1:5"])
    editor.forget()
    assert editor.applied_ids == set()

    @contextmanager
    def closed(_path):
        raise LiveEditUnavailable("PowerPoint에서 이 문서가 열려 있을 때만 바로 고칠 수 있습니다.")
        yield

    with pytest.raises(LiveEditUnavailable):
        LiveEditor(closed).apply(_report(_size()), ["font.size@1:5"])


def test_uncheck_autofit_change_also_restores_the_box_height():
    """자동 맞춤을 바꾸면 PowerPoint가 상자 높이를 바꾸므로, 해제할 때 원래 높이까지 되돌린다."""
    shape = DocShape([DocRun(24.0, "000000")], geometry=(50, 50, 400, 100), autofit="shrink")
    editor = _editor(Doc(shape))
    change = ChangeProposal(
        "text.autofit@1:5", "layout.overflow:1:5", "1:5", "text.autofit", "shrink", "grow", 0.1
    )
    assert editor.apply(_report(change), ["text.autofit@1:5"]).applied == ["text.autofit@1:5"]
    shape.geometry[3] = 180  # PowerPoint가 '텍스트에 맞춤'으로 상자를 늘린 것처럼
    editor.revert(["text.autofit@1:5"])
    assert shape.autofit == "shrink" and shape.geometry[3] == 100


def test_table_rows_are_restored_after_the_text_size():
    """표 글자를 키우며 늘린 행 높이도 되돌린다. 글자를 먼저 줄인 뒤에 행을 줄인다."""
    order = []

    class TableShape(DocShape):
        def __init__(self, runs):
            super().__init__(runs)
            self.rows = [30.0, 30.0]

        def fit_table_rows(self):
            before = list(self.rows)
            self.rows = [50.0, 50.0]
            return before

        def set_row_heights(self, heights):
            order.append(("rows", [run.size for run in self._runs]))
            self.rows = list(heights)

    from slideguard.adapters.powerpoint.live_edit import _RecordingShape, _undo

    runs = [DocRun(10.0, "000000")]
    shape = TableShape(runs)
    log = []
    recording = _RecordingShape(shape, 1, 5, log)
    recording.runs()[0].set_size(18.0)  # patcher가 표 글자를 키운 뒤
    recording.fit_table_rows()  # 행 높이를 맞추는 순서 그대로
    assert shape.rows == [50.0, 50.0]
    _undo(Doc(shape), log)
    assert shape.rows == [30.0, 30.0] and runs[0].size == 10.0
    assert order == [("rows", [10.0])]  # 행은 글자를 10pt로 되돌린 뒤에


def test_bold_change_is_undone():
    from slideguard.adapters.powerpoint.live_edit import _RecordingShape, _undo

    runs = [DocRun(20.0, "D62728")]
    shape = DocShape(runs)
    log = []
    _RecordingShape(shape, 1, 5, log).runs()[0].set_bold(True)
    assert runs[0].bold is True
    _undo(Doc(shape), log)
    assert runs[0].bold is False
