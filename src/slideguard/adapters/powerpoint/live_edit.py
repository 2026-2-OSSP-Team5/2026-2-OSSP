"""열려 있는 PowerPoint 문서에 수정 후보를 바로 켰다 껐다 하는 실시간 적용.

슬라이드 렌즈의 체크박스에서 쓴다.
  체크   → apply(): 수정 후보를 화면의 문서에 바로 적용 (저장하지 않음)
  체크 해제 → revert(): 그때 바꾼 값을 하나하나 원래 값으로 되돌림
적용을 유지하려면 사용자가 PowerPoint에서 저장하면 된다 (저장하면 재검사가 다시 돈다).

안전장치
  - 사용자가 PowerPoint에서 열어 둔 문서에만 쓴다. 파일을 새로 열거나 저장·닫지 않는다.
  - 적용할 때는 patcher와 같은 규칙으로 "현재 값이 후보를 만들 때 값과 같은지" 확인한다.
  - 되돌릴 때는 "지금 값이 우리가 넣은 값 그대로인지" 확인하고, 사용자가 그 뒤에 직접 고친 값은 건드리지 않는다.
  - 되돌리기 정보는 COM 객체가 아니라 (슬라이드, 객체 ID, 몇 번째 글자 묶음, 이전 값, 새 값)으로 저장한다.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from slideguard.planning.bindings import TeamBindings

from .patcher import TOLERANCE, ApplyResult, Document, _ComDocument, apply_proposal

MSO_TRUE = -1


class LiveEditUnavailable(Exception):
    """열린 문서를 찾지 못했거나 고칠 수 없는 상태 (사용자에게 보여 줄 문장)."""


@dataclass(frozen=True)
class UndoStep:
    slide: int
    shape_id: int
    kind: str  # run.size / run.color / run.font / paragraph.spacing / shape.height / shape.position / shape.zorder / shape.fill / shape.autofit
    index: int  # 몇 번째 글자 묶음·문단인지 (도형 단위 변경은 0)
    before: object
    after: object


@dataclass
class LiveResult:
    applied: list[str] = field(default_factory=list)  # 적용(또는 되돌림)한 제안 ID
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (제안 ID, 이유)
    skipped: int = 0  # 되돌릴 때 사용자가 이미 바꿔 둬서 건드리지 않은 값 수


class LiveEditor:
    """제안 ID별로 무엇을 바꿨는지 기억해 두고 되돌린다."""

    def __init__(self, session_factory: Callable[[str], AbstractContextManager] | None = None):
        self.session_factory = session_factory or OpenDocumentSession
        self.bindings = TeamBindings()
        self._steps: dict[str, list[UndoStep]] = {}
        self._path: str | None = None

    @property
    def applied_ids(self) -> set[str]:
        return set(self._steps)

    def forget(self) -> None:
        """검사 결과가 바뀌었거나 사용자가 저장했을 때: 기억만 지운다 (문서는 그대로)."""
        self._steps.clear()
        self._path = None

    def apply(self, report: object, proposal_ids: Iterable[str]) -> LiveResult:
        path = str(report.deck.source_path)
        if self._path is not None and not _same(self._path, path):
            self.forget()
        wanted = [
            item for item in report.proposals if item.id in set(proposal_ids) and item.id not in self._steps
        ]
        result = LiveResult()
        if not wanted:
            return result
        with self.session_factory(path) as document:
            for proposal in wanted:
                log: list[UndoStep] = []
                internal = self.bindings.from_team_proposal(proposal)
                outcome = ApplyResult(path)
                apply_proposal(_RecordingDocument(document, log), internal, outcome)
                if outcome.applied:
                    self._steps[proposal.id] = log
                    result.applied.append(proposal.id)
                else:
                    _undo(document, log)  # 일부만 바뀌었으면 되돌려 둔다
                    reason = outcome.rejected[0][1] if outcome.rejected else "적용하지 못했습니다."
                    result.rejected.append((proposal.id, reason))
        self._path = path
        return result

    def revert(self, proposal_ids: Iterable[str]) -> LiveResult:
        targets = [item for item in proposal_ids if item in self._steps]
        result = LiveResult()
        if not targets or self._path is None:
            return result
        with self.session_factory(self._path) as document:
            for proposal_id in reversed(targets):
                result.skipped += _undo(document, self._steps.pop(proposal_id))
                result.applied.append(proposal_id)
        return result


# ---------------------------------------------------------------- 기록하면서 바꾸기


class _RecordingDocument:
    def __init__(self, document: Document, log: list[UndoStep]):
        self._document = document
        self._log = log

    def find_shape(self, slide_index: int, shape_id: int):
        shape = self._document.find_shape(slide_index, shape_id)
        return None if shape is None else _RecordingShape(shape, slide_index, shape_id, self._log)

    def save(self) -> None:  # 실시간 적용은 저장하지 않는다
        raise LiveEditUnavailable("실시간 적용은 저장하지 않습니다.")


class _RecordingShape:
    def __init__(self, shape, slide: int, shape_id: int, log: list[UndoStep]):
        self._shape, self._slide, self._id, self._log = shape, slide, shape_id, log

    def _record(self, kind: str, index: int, before, after) -> None:
        self._log.append(UndoStep(self._slide, self._id, kind, index, before, after))

    def runs(self):
        return [_RecordingRun(run, index, self) for index, run in enumerate(self._shape.runs())]

    def paragraphs(self):
        return [_RecordingParagraph(item, index, self) for index, item in enumerate(self._shape.paragraphs())]

    def text_overflows(self):
        return self._shape.text_overflows()

    def get_geometry(self):
        return self._shape.get_geometry()

    def set_height(self, height: float) -> None:
        self._record("shape.height", 0, self._shape.get_geometry()[3], float(height))
        self._shape.set_height(height)

    def fit_table_rows(self):
        """표 행 높이 맞추기. 되돌릴 때 원래 행 높이로 돌리려고 기억한다."""
        fit = getattr(self._shape, "fit_table_rows", None)
        before = fit() if fit is not None else None
        if before is not None:
            self._record("table.rows", 0, tuple(before), None)
        return before

    def set_width(self, width: float) -> None:
        self._record("shape.width", 0, self._shape.get_geometry()[2], float(width))
        self._shape.set_width(width)

    def set_position(self, left: float, top: float) -> None:
        old_left, old_top, _w, _h = self._shape.get_geometry()
        self._record("shape.position", 0, (old_left, old_top), (float(left), float(top)))
        self._shape.set_position(left, top)

    def bring_to_front(self) -> None:
        self._record("shape.zorder", 0, self._shape.get_zorder(), None)
        self._shape.bring_to_front()

    def get_fill(self):
        return self._shape.get_fill()

    def set_fill(self, hex_color: str, transparency: float) -> None:
        self._record("shape.fill", 0, self._shape.get_fill(), (str(hex_color).upper(), float(transparency)))
        self._shape.set_fill(hex_color, transparency)

    def get_autofit(self):
        return self._shape.get_autofit()

    def set_autofit(self, mode: str) -> None:
        # '도형을 텍스트에 맞춤'으로 바꾸면 PowerPoint가 상자 높이를 저절로 바꾸므로 원래 높이도 기억한다
        height = self._shape.get_geometry()[3]
        self._record("shape.autofit", 0, (self._shape.get_autofit(), height), mode)
        self._shape.set_autofit(mode)


class _RecordingRun:
    def __init__(self, run, index: int, owner: _RecordingShape):
        self._run, self._index, self._owner = run, index, owner

    def get_size(self):
        return self._run.get_size()

    def set_size(self, size_pt: float) -> None:
        self._owner._record("run.size", self._index, self._run.get_size(), float(size_pt))
        self._run.set_size(size_pt)

    def get_color(self):
        return self._run.get_color()

    def set_color(self, hex_color: str) -> None:
        self._owner._record("run.color", self._index, self._run.get_color(), str(hex_color).upper())
        self._run.set_color(hex_color)

    def get_font_names(self):
        return self._run.get_font_names()

    def set_font_name(self, name: str) -> None:
        names = self._run.get_font_names()
        self._owner._record("run.font", self._index, names[0] if names else None, name)
        self._run.set_font_name(name)


class _RecordingParagraph:
    def __init__(self, paragraph, index: int, owner: _RecordingShape):
        self._paragraph, self._index, self._owner = paragraph, index, owner

    def get_line_spacing(self):
        return self._paragraph.get_line_spacing()

    def set_line_spacing(self, multiple: float) -> None:
        self._owner._record(
            "paragraph.spacing", self._index, self._paragraph.get_line_spacing(), float(multiple)
        )
        self._paragraph.set_line_spacing(multiple)


# ---------------------------------------------------------------- 되돌리기


def _undo(document: Document, steps: list[UndoStep]) -> int:
    """기록을 거꾸로 되돌린다. 사용자가 그 사이 바꾼 값은 건드리지 않고, 그런 값의 수를 돌려준다."""
    skipped = 0
    # 표 행 높이는 글자 크기를 되돌린 '뒤에' 돌린다 (글자가 큰 동안에는 행을 줄일 수 없음)
    others = [step for step in steps if step.kind != "table.rows"]
    rows = [step for step in steps if step.kind == "table.rows"]
    for step in list(reversed(others)) + list(reversed(rows)):
        shape = document.find_shape(step.slide, step.shape_id)
        if shape is None or not _restore(shape, step):
            skipped += 1
    return skipped


def _close(left, right) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= TOLERANCE
    return left == right


def _restore(shape, step: UndoStep) -> bool:
    kind = step.kind
    if kind.startswith("run."):
        runs = shape.runs()
        if step.index >= len(runs):
            return False
        run = runs[step.index]
        if kind == "run.size" and step.before is not None and _close(run.get_size(), step.after):
            run.set_size(float(step.before))
            return True
        if (
            kind == "run.color"
            and step.before is not None
            and str(run.get_color() or "").upper() == step.after
        ):
            run.set_color(str(step.before))
            return True
        if kind == "run.font" and step.before is not None and step.after in run.get_font_names():
            run.set_font_name(str(step.before))
            return True
        return False
    if kind == "table.rows":
        set_rows = getattr(shape, "set_row_heights", None)
        if set_rows is None or step.before is None:
            return False
        set_rows(list(step.before))
        return True
    if kind == "paragraph.spacing":
        paragraphs = shape.paragraphs()
        if step.index >= len(paragraphs) or step.before is None:
            return False
        paragraph = paragraphs[step.index]
        if not _close(paragraph.get_line_spacing(), step.after):
            return False
        paragraph.set_line_spacing(float(step.before))
        return True
    left, top, width, height = shape.get_geometry()
    if kind == "shape.height" and _close(height, step.after):
        shape.set_height(float(step.before))
        return True
    if kind == "shape.width" and _close(width, step.after):
        shape.set_width(float(step.before))
        return True
    if kind == "shape.position" and _close(left, step.after[0]) and _close(top, step.after[1]):
        shape.set_position(float(step.before[0]), float(step.before[1]))
        return True
    if kind == "shape.zorder" and step.before is not None:
        shape.set_zorder(int(step.before))
        return True
    if kind == "shape.fill":
        current = shape.get_fill()
        if current is None or current[0].upper() != step.after[0]:
            return False
        if step.before is None:
            shape.clear_fill()
        else:
            shape.set_fill(step.before[0], step.before[1])
        return True
    if kind == "shape.autofit" and shape.get_autofit() == step.after and step.before is not None:
        mode, old_height = step.before if isinstance(step.before, tuple) else (step.before, None)
        if mode is None:
            return False
        shape.set_autofit(str(mode))
        if old_height is not None:
            shape.set_height(float(old_height))  # 늘어난 상자를 원래 높이로
        return True
    return False


# ---------------------------------------------------------------- 열린 문서 연결


class OpenDocumentSession(AbstractContextManager):
    """with OpenDocumentSession(path) as doc: ...  사용자가 PowerPoint에서 연 문서에만 연결한다.

    새로 열거나 저장하거나 닫지 않는다. 문서가 열려 있지 않으면 LiveEditUnavailable.
    """

    def __init__(self, path: str):
        self._path = os.path.abspath(path)
        self._document: _ComDocument | None = None

    def __enter__(self) -> _ComDocument:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            application = win32com.client.GetActiveObject("PowerPoint.Application")
            presentations = application.Presentations
            for index in range(1, presentations.Count + 1):
                presentation = presentations.Item(index)
                if not _same(str(presentation.FullName), self._path):
                    continue
                if presentation.ReadOnly == MSO_TRUE:
                    raise LiveEditUnavailable("문서가 읽기 전용으로 열려 있어 바로 고칠 수 없습니다.")
                self._document = _ComDocument(presentation, was_open=True)
                return self._document
            raise LiveEditUnavailable("PowerPoint에서 이 문서가 열려 있을 때만 바로 고칠 수 있습니다.")
        except LiveEditUnavailable:
            self._release()
            raise
        except Exception as error:
            self._release()
            raise LiveEditUnavailable(f"PowerPoint에 연결하지 못했습니다: {error}") from error

    def __exit__(self, exc_type, exc, tb) -> None:
        self._release()

    def _release(self) -> None:
        import gc

        import pythoncom

        if self._document is not None:
            self._document._pres = None
        self._document = None
        gc.collect()
        pythoncom.CoUninitialize()


def _same(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
