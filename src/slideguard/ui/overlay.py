from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from time import monotonic

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from slideguard.adapters import startup
from slideguard.adapters.powerpoint.live_edit import LiveEditor, LiveEditUnavailable
from slideguard.adapters.watching.paths import accepts_path, presentation_paths
from slideguard.application.ports import FileRemoved
from slideguard.bootstrap import (
    PolicyAnalysisHashes,
    build_analysis_policy,
    build_analyzer,
    build_powerpoint_reader,
    build_presentation_patcher,
    build_report_repository,
    build_snapshot_collection,
    build_workspace_settings_repository,
)
from slideguard.domain.models import AnalysisReport, Box, ChangeProposal, Finding
from slideguard.domain.policy import (
    AnalysisPolicy,
    ContrastGoal,
    FixTargets,
    FontGoal,
    Purpose,
    Strength,
    ThresholdOverrides,
)
from slideguard.planning.bindings import TeamBindings, is_recommended
from slideguard.ui.analysis_tasks import SplitProposalWorker, progress_text
from slideguard.ui.finding_details import FindingDetailsDialog, TechnicalDetailsDialog, describe_finding
from slideguard.ui.object_locks import ObjectLocksEditor
from slideguard.ui.preview import render_slide_previews
from slideguard.ui.slide_lens import InputActivity, SlideLens, SlideView, SlideViewProbe, build_marks
from slideguard.ui.user_text import change_text, friendly_error, friendly_reason, readability_label

LOGGER = logging.getLogger("slideguard.overlay")

OVERLAY_STYLE = """
QWidget { font-family: "Malgun Gothic"; color: #202938; font-size: 11px; }
QFrame#panel, QDialog { background: #ffffff; border: 1px solid #dfe5ee; }
QScrollArea#policyScroll, QWidget#policyPage { background: #ffffff; }
QLabel#brand { font-size: 15px; font-weight: 700; color: #172033; }
QLabel#title { font-size: 14px; font-weight: 700; }
QLabel#muted { color: #758195; }
QLabel#explanation { color: #344054; }
QLabel#status { color: #2468d8; font-weight: 700; }
QPushButton { min-height: 27px; padding: 0 10px; border: 1px solid #d7dee8;
              border-radius: 4px; background: #ffffff; }
QPushButton:hover { background: #f5f8fc; }
QPushButton:pressed { background: #dfe7f3; }
QPushButton#proposalSelection[selected="true"] {
    background: #e7f0ff; border-color: #2468d8; color: #1756b2; font-weight: 700;
}
QPushButton#proposalSelection:disabled { background: #f3f5f8; color: #8792a3; }
QPushButton#primary { color: white; background: #2468d8; border-color: #2468d8; font-weight: 700; }
QPushButton#primary:disabled { color: #98a2b3; background: #e8ecf1; border-color: #d9dfe8; }
QPushButton#exit { color: #b42318; border-color: #efc4c0; }
QPushButton#exit:hover { color: #912018; background: #fff1f0; border-color: #e8aaa4; }
QTableWidget { border: 1px solid #e1e6ed; background: #ffffff; gridline-color: #e8ecf2;
               selection-background-color: #e7f0ff; selection-color: #1f2937; outline: 0; }
QTableWidget::item { padding: 5px; border: 0; }
QTableWidget::item:selected { background: #e7f0ff; color: #1f2937; border: 0; }
QHeaderView::section { min-height: 28px; padding: 0 6px; border: 0;
                       border-bottom: 1px solid #e1e6ed; background: #f7f8fa;
                       color: #667085; font-weight: 600; }
QFrame#detail, QFrame#proposal { background: #fbfcfe; border: 1px solid #e3e8ef; border-radius: 5px; }
QLineEdit, QComboBox, QListWidget { min-height: 28px; border: 1px solid #d7dee8;
                                   border-radius: 4px; background: #ffffff; }
QListWidget#proposalList::item { min-height: 21px; padding: 2px 5px; border-bottom: 1px solid #edf0f5; }
QListWidget#proposalList::item:hover { background: #f0f5fc; }
QListWidget#proposalList::item:selected { background: #dceaff; color: #174f9c; }
QCheckBox#proposal { spacing: 7px; padding: 3px; }
QTabWidget, QTabBar { background: #ffffff; }
QTabWidget::pane { border: 1px solid #d7e3f4; background: #ffffff; }
QTabBar::tab { min-height: 28px; padding: 0 14px; border: 1px solid #d7dee8;
               border-bottom: 0; background: #f7f8fa; color: #667085; }
QTabBar::tab:selected { background: #e7f0ff; color: #2468d8;
                        border-color: #b9cdf2; font-weight: 700; }
"""


SHIELD_SIZE = 58
PANEL_HEIGHT = 180
PANEL_MAX_WIDTH = 1280
PANEL_SIDE_PADDING = 16


class ShieldButton(QPushButton):
    dragged = Signal(QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("shield")
        self.setFixedSize(SHIELD_SIZE, SHIELD_SIZE)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName("SlideGuard 열기")
        self._press_position: QPoint | None = None
        self._press_offset = QPoint()
        self._dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.globalPosition().toPoint()
            self._press_offset = event.position().toPoint()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_position is None or not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        position = event.globalPosition().toPoint()
        if not self._dragging:
            if (position - self._press_position).manhattanLength() < QApplication.startDragDistance():
                return
            self._dragging = True
            self.setDown(False)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.dragged.emit(position - self._press_offset)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = None
            if self._dragging:
                self._dragging = False
                self.setCursor(Qt.CursorShape.OpenHandCursor)
                self.setDown(False)
                super().mouseReleaseEvent(event)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        painter.setPen(QPen(QColor("#2468d8" if self.hasFocus() else "#b9cdf2"), 2))
        fill = "#dfe7f3" if self.isDown() else "#f5f8fc" if self.underMouse() else "#ffffff"
        painter.setBrush(QColor(fill))
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        x, y = rect.center().x(), rect.center().y()
        shield = QPainterPath()
        shield.moveTo(x, y - 10)
        shield.lineTo(x + 8, y - 7)
        shield.lineTo(x + 7, y + 1)
        shield.cubicTo(x + 6, y + 6, x + 2, y + 9, x, y + 10)
        shield.cubicTo(x - 2, y + 9, x - 6, y + 6, x - 7, y + 1)
        shield.lineTo(x - 8, y - 7)
        shield.closeSubpath()
        painter.setPen(QPen(QColor("#2468d8"), 1.8))
        painter.setBrush(QColor("#e7f0ff"))
        painter.drawPath(shield)


@dataclass(frozen=True)
class PowerPointWindow:
    hwnd: int
    rect: QRect
    title: str
    monitor_rect: tuple[int, int, int, int] | None = None
    screen_rect: QRect | None = None
    available_rect: QRect | None = None
    foreground: bool = False
    topmost: bool = False


@dataclass(frozen=True)
class PresentationState:
    path: Path | None
    saved: bool


@dataclass(frozen=True)
class AnalysisOutcome:
    report: AnalysisReport
    skip_reasons: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CopyApplyOutcome:
    output_path: Path
    selected_ids: tuple[str, ...]
    applied_ids: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    slide_changes: dict[int, tuple[ChangeProposal, ...]]
    original_previews: dict[int, bytes] = field(default_factory=dict)
    copy_previews: dict[int, bytes] = field(default_factory=dict)
    preview_error: str | None = None
    reinspection: AnalysisReport | None = None
    reinspection_error: str | None = None


class LiveEditTask(QThread):
    """렌즈 체크에 따라 열린 문서에 수정 후보를 적용하거나 되돌린다 (COM은 UI 스레드 밖에서)."""

    done = Signal(object)  # LiveResult 또는 사용자에게 보여 줄 문장

    def __init__(
        self, editor: LiveEditor, report: AnalysisReport, proposal_ids: tuple[str, ...], checked: bool
    ):
        super().__init__()
        self.editor, self.report, self.proposal_ids, self.checked = editor, report, proposal_ids, checked
        self.key = ""  # 결과 안내를 붙일 배지

    def run(self):
        try:
            if self.checked:
                result = self.editor.apply(self.report, self.proposal_ids)
            else:
                result = self.editor.revert(self.proposal_ids)
            self.done.emit(result)
        except LiveEditUnavailable as error:
            self.done.emit(str(error))
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("LIVE_EDIT_FAILED")
            self.done.emit(f"바로 적용하지 못했습니다: {type(error).__name__}")


class PowerPointTracker:
    """Track the foreground PowerPoint frame without installing another hook."""

    def __init__(self):
        self._hwnd: int | None = None

    def poll(self, owned_hwnds: set[int]) -> PowerPointWindow | None:
        try:
            import win32api
            import win32con
            import win32gui
        except ImportError:
            return None

        foreground = win32gui.GetForegroundWindow()
        try:
            foreground_class = win32gui.GetClassName(foreground)
        except Exception:
            foreground_class = ""
        if foreground_class == "PPTFrameClass":
            self._hwnd = foreground
        elif not self._owns_foreground(foreground, owned_hwnds, win32gui, win32con.GW_OWNER):
            return None

        hwnd = self._hwnd
        if hwnd is None or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right <= left or bottom <= top:
            return None
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        monitor_info = win32api.GetMonitorInfo(monitor)
        qt_rect = _qt_screen_rect(monitor_info["Device"], monitor_info["Monitor"])
        available_rect = _to_qt_rect(monitor_info["Work"], monitor_info["Monitor"], qt_rect)
        rect = _to_qt_rect((left, top, right, bottom), monitor_info["Monitor"], qt_rect)
        return PowerPointWindow(
            hwnd=hwnd,
            rect=rect,
            title=win32gui.GetWindowText(hwnd),
            monitor_rect=monitor_info["Monitor"],
            screen_rect=qt_rect,
            available_rect=available_rect,
            foreground=foreground == hwnd,
            topmost=bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST),
        )

    @staticmethod
    def _owns_foreground(hwnd, owned_hwnds, windows, owner_flag):
        # Native save and confirmation dialogs may have their own foreground HWND.
        while hwnd and windows.IsWindow(hwnd):
            if hwnd in owned_hwnds:
                return True
            hwnd = windows.GetWindow(hwnd, owner_flag)
        return False


def active_presentation_path() -> Path | None:
    """Return the saved active presentation path from the running PowerPoint instance."""
    return active_presentation_state().path


def active_presentation_state() -> PresentationState:
    """Return active document identity and whether PowerPoint has unsaved edits."""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        app = presentation = None
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
            presentation = app.ActivePresentation
            full_name = str(presentation.FullName) if presentation is not None else ""
            saved = bool(presentation.Saved) if presentation is not None else False
        finally:
            presentation = app = None
            pythoncom.CoUninitialize()
    except Exception:
        return PresentationState(None, False)
    path = Path(full_name) if full_name else None
    if path is None or not path.exists():
        return PresentationState(None, saved)
    return PresentationState(path.resolve(), saved)


def inspect_saved_copy(path: Path, policy: AnalysisPolicy, before: AnalysisReport) -> AnalysisReport:
    report = build_analyzer(build_powerpoint_reader(render=True)).execute(path, policy)
    if _sha256(path) != report.deck.source_sha256:
        raise ValueError("사본이 재검사 중 변경되었습니다. 다시 검사해 주세요.")
    if report.deck.slide_count == before.deck.slide_count:
        report.compare_with(before)
    build_report_repository().save(report)
    return report


class CopyApplyWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        report: AnalysisReport,
        proposal_ids: tuple[str, ...],
        output_path: Path,
        policy,
        *,
        patcher_builder=build_presentation_patcher,
        preview_renderer=render_slide_previews,
        copy_inspector=inspect_saved_copy,
    ):
        super().__init__()
        self.report = report
        self.proposal_ids = proposal_ids
        self.output_path = output_path
        self.policy = policy
        self.patcher_builder = patcher_builder
        self.preview_renderer = preview_renderer
        self.copy_inspector = copy_inspector

    def run(self):
        try:
            source = Path(self.report.deck.source_path)
            source_hash = _sha256(source)
            if source_hash != self.report.deck.source_sha256:
                raise ValueError("원본이 검사 후 변경되었습니다. 저장 후 다시 검사해 주세요.")

            patcher = self.patcher_builder()
            patcher.apply_to_copy(
                self.report,
                list(self.proposal_ids),
                self.output_path,
                self.policy,
            )
            result = patcher.last_result
            if result is None or result.output_path is None or not self.output_path.is_file():
                raise RuntimeError("패처가 적용 결과를 확인할 수 있는 사본을 만들지 못했습니다.")
            if _sha256(source) != source_hash:
                raise RuntimeError("사본 적용 중 원본 파일이 변경되었습니다.")

            bindings = TeamBindings()
            applied_ids = tuple(bindings.to_team_proposal(item).id for item in result.applied)
            rejected = tuple((bindings.to_team_proposal(item).id, reason) for item, reason in result.rejected)
            applied = [proposal for proposal in self.report.proposals if proposal.id in set(applied_ids)]
            slide_changes: dict[int, list[ChangeProposal]] = {}
            for proposal in applied:
                slide = _slide_number(proposal.shape_key)
                slide_changes.setdefault(slide, []).append(proposal)

            reinspection = None
            reinspection_error = None
            try:
                reinspection = self.copy_inspector(self.output_path, self.policy, self.report)
            except Exception as error:
                reinspection_error = f"{type(error).__name__}: {error}"

            self.succeeded.emit(
                CopyApplyOutcome(
                    output_path=self.output_path,
                    selected_ids=self.proposal_ids,
                    applied_ids=applied_ids,
                    rejected=rejected,
                    warnings=tuple(result.warnings),
                    slide_changes={slide: tuple(proposals) for slide, proposals in slide_changes.items()},
                    reinspection=reinspection,
                    reinspection_error=reinspection_error,
                )
            )
        except Exception as error:
            LOGGER.exception("COPY_APPLY_FAILED | file=%s", Path(self.report.deck.source_path).name)
            self.failed.emit(f"{type(error).__name__}: {error}")


@dataclass(frozen=True)
class OriginalApplyOutcome:
    original_path: Path
    backup_path: Path
    applied_ids: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    was_open: bool
    source_sha256_before: str | None = None


class OriginalApplyWorker(QThread):
    """선택한 제안을 원본에 적용 (C의 apply_to_original: 백업 → 원본에 적용 후 저장)."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        report: AnalysisReport,
        proposal_ids: tuple[str, ...],
        backup_dir: Path,
        policy,
        *,
        patcher_builder=build_presentation_patcher,
    ):
        super().__init__()
        self.report = report
        self.proposal_ids = proposal_ids
        self.backup_dir = backup_dir
        self.policy = policy
        self.patcher_builder = patcher_builder

    def run(self):
        try:
            patcher = self.patcher_builder()
            backup = patcher.apply_to_original(
                self.report, list(self.proposal_ids), self.backup_dir, self.policy
            )
            result = patcher.last_result
            bindings = TeamBindings()
            self.succeeded.emit(
                OriginalApplyOutcome(
                    original_path=Path(self.report.deck.source_path),
                    backup_path=Path(backup),
                    applied_ids=tuple(bindings.to_team_proposal(item).id for item in result.applied),
                    rejected=tuple(
                        (bindings.to_team_proposal(item).id, reason) for item, reason in result.rejected
                    ),
                    warnings=tuple(result.warnings),
                    was_open=bool(result.was_open),
                    source_sha256_before=self.report.deck.source_sha256,
                )
            )
        except Exception as error:
            LOGGER.exception("ORIGINAL_APPLY_FAILED | file=%s", Path(self.report.deck.source_path).name)
            self.failed.emit(f"{type(error).__name__}: {error}")


class ApplyResultDialog(QDialog):
    def __init__(self, outcome: CopyApplyOutcome, parent=None):
        super().__init__(parent)
        self.outcome = outcome
        self.setWindowTitle("SlideGuard 사본 적용 결과")
        self.resize(1060, 680)
        self.setStyleSheet(OVERLAY_STYLE)

        layout = QVBoxLayout(self)
        applied = len(outcome.applied_ids)
        failed = len(outcome.rejected)
        title = QLabel(f"사본 적용 결과 · 성공 {applied}건 · 실패 {failed}건", objectName="title")
        layout.addWidget(title)
        layout.addWidget(QLabel(str(outcome.output_path), objectName="muted"))

        body = QHBoxLayout()
        self.slide_list = QListWidget()
        for number in sorted(outcome.slide_changes):
            item = QListWidgetItem(f"{number}번 슬라이드")
            item.setData(Qt.ItemDataRole.UserRole, number)
            self.slide_list.addItem(item)
        self.slide_list.currentItemChanged.connect(self._show_slide)
        body.addWidget(self.slide_list, 2)

        comparison = QVBoxLayout()
        images = QHBoxLayout()
        before_column = QVBoxLayout()
        before_column.addWidget(QLabel("원본", objectName="title"))
        self.before_image = QLabel("미리보기를 만들 수 없습니다.", objectName="muted")
        self.before_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.before_image.setMinimumSize(360, 240)
        before_column.addWidget(self.before_image, 1)
        images.addLayout(before_column, 1)

        after_column = QVBoxLayout()
        after_column.addWidget(QLabel("적용 후 사본", objectName="title"))
        self.after_image = QLabel("미리보기를 만들 수 없습니다.", objectName="muted")
        self.after_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.after_image.setMinimumSize(360, 240)
        after_column.addWidget(self.after_image, 1)
        images.addLayout(after_column, 1)
        comparison.addLayout(images, 1)

        self.change_text = QLabel()
        self.change_text.setWordWrap(True)
        comparison.addWidget(self.change_text)
        body.addLayout(comparison, 8)
        layout.addLayout(body, 1)

        details = []
        if outcome.rejected:
            details.append(
                "실패: " + "; ".join(f"{proposal_id}: {reason}" for proposal_id, reason in outcome.rejected)
            )
        if outcome.warnings:
            details.append("경고: " + "; ".join(outcome.warnings))
        if outcome.reinspection is not None:
            details.append(comparison_text(outcome.reinspection))
        if outcome.reinspection_error:
            details.append("사본 재검사 실패 · " + outcome.reinspection_error)
        if outcome.preview_error:
            details.append("미리보기: " + outcome.preview_error)
        self.details_label = QLabel("\n".join(details), objectName="muted")
        self.details_label.setWordWrap(True)
        layout.addWidget(self.details_label)

        actions = QHBoxLayout()
        actions.addStretch()
        open_copy = QPushButton("사본 열기", objectName="primary")
        open_copy.clicked.connect(self._open_copy)
        actions.addWidget(open_copy)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)

        if self.slide_list.count():
            self.slide_list.setCurrentRow(0)

    def _show_slide(self, current: QListWidgetItem | None, _previous=None):
        if current is None:
            return
        number = current.data(Qt.ItemDataRole.UserRole)
        self._set_preview(self.before_image, self.outcome.original_previews.get(number))
        self._set_preview(self.after_image, self.outcome.copy_previews.get(number))
        changes = self.outcome.slide_changes.get(number, ())
        self.change_text.setText("\n".join(proposal_change_text(item) for item in changes))

    @staticmethod
    def _set_preview(label: QLabel, payload: bytes | None):
        if not payload:
            label.setPixmap(QPixmap())
            label.setText("미리보기를 만들 수 없습니다. '사본 열기'로 확인해 주세요.")
            return
        pixmap = QPixmap()
        pixmap.loadFromData(payload, "PNG")
        label.setText("")
        label.setPixmap(
            pixmap.scaled(
                label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _open_copy(self):
        try:
            os.startfile(self.outcome.output_path)
        except OSError as error:
            QMessageBox.warning(self, "사본 열기 실패", str(error))


class PanelHeader(QFrame):
    dragged = Signal(QPoint)

    def __init__(self):
        super().__init__()
        self._offset = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("상단을 드래그해 툴바 이동")

    def eventFilter(self, _watched, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            self.mousePressEvent(event)
        elif event.type() == QEvent.Type.MouseMove:
            self.mouseMoveEvent(event)
        elif event.type() == QEvent.Type.MouseButtonRelease:
            self.mouseReleaseEvent(event)
        return False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._offset = event.globalPosition().toPoint() - self.window().pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.dragged.emit(event.globalPosition().toPoint() - self._offset)

    def mouseReleaseEvent(self, event):
        self._offset = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)


class ToolbarLayout(QVBoxLayout):
    """Do not let wrapped evidence text resize the native floating window."""

    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, _width):
        return -1


class OverlayWidget(QWidget):
    geometry_changed = Signal()
    manual_requested = Signal()
    settings_requested = Signal()
    apply_requested = Signal()
    exit_requested = Signal()
    workspace_requested = Signal()
    locate_requested = Signal(object)
    slide_requested = Signal(object)
    lock_requested = Signal(str)
    fixes_requested = Signal(object)
    view_state_changed = Signal()
    lens_toggled = Signal(bool)
    # 수정 후보 목록에서 사용자가 직접 체크/해제함: (같은 묶음 후보 ID들, 체크 여부)
    proposal_user_toggled = Signal(object, bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SlideGuard Overlay")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(OVERLAY_STYLE)
        self.collapsed = True
        self._target: PowerPointWindow | None = None
        self._shield_offset: QPoint | None = None
        self._panel_offset: QPoint | None = None
        self._split_busy = False
        self._report: AnalysisReport | None = None
        self._skip_reasons: dict[str, str] = {}
        self.proposal_items: dict[str, QListWidgetItem] = {}
        self._proposal_ids_by_severity: dict[str, set[str]] = {}
        self._syncing_proposals = False
        self._selection_snapshot: set[str] = set()
        self._analysis_busy = False
        self._apply_busy = False
        self._report_stale = False
        self._finding: Finding | None = None

        outer = ToolbarLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.stack.setMinimumSize(0, 0)
        outer.addWidget(self.stack)
        self.collapsed_page = self._build_collapsed()
        self.expanded_page = self._build_expanded()
        self.stack.addWidget(self.collapsed_page)
        self.stack.addWidget(self.expanded_page)
        self.set_collapsed(True)

    def _build_collapsed(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.shield_button = ShieldButton()
        self.shield_button.setToolTip("클릭해서 열기 · 드래그해서 이동")
        self.shield_button.clicked.connect(lambda: self.set_collapsed(False))
        self.shield_button.dragged.connect(self._move_shield)
        layout.addWidget(self.shield_button)
        self.count_badge = QLabel("", self.shield_button)
        self.count_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_badge.setStyleSheet(
            "background:#d92d20;color:white;border-radius:8px;font-weight:700;font-size:9px;"
        )
        self.count_badge.hide()
        return page

    def _build_expanded(self) -> QWidget:
        panel = QFrame(objectName="panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        self.drag_header = PanelHeader()
        self.drag_header.dragged.connect(self._move_panel)
        header = QHBoxLayout(self.drag_header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(QLabel("🛡 SlideGuard", objectName="brand"))
        self.document_label = QLabel("PowerPoint 문서를 선택하세요", objectName="muted")
        self.document_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.document_label, 1)
        self.status_label = QLabel("대기 중", objectName="status")
        self.status_label.setMaximumWidth(240)
        header.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedSize(80, 8)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        header.addWidget(self.progress_bar)
        self.manual_button = QPushButton("검사 실행", objectName="primary")
        self.manual_button.clicked.connect(self.manual_requested)
        header.addWidget(self.manual_button)
        self.settings_button = QPushButton("⚙")
        self.settings_button.setToolTip("설정")
        self.settings_button.clicked.connect(self.settings_requested)
        header.addWidget(self.settings_button)
        workspace = QPushButton("작업 폴더·이력")
        workspace.clicked.connect(self.workspace_requested)
        header.addWidget(workspace)
        self.lens_check = QCheckBox("슬라이드 위 표시")
        self.lens_check.setToolTip(
            "문제가 있는 개체 위에 배지와 테두리를 겹쳐 보여 줍니다. 클릭은 PowerPoint로 그대로 전달됩니다."
        )
        self.lens_check.setChecked(True)
        self.lens_check.toggled.connect(self.lens_toggled)
        header.addWidget(self.lens_check)
        self.collapse_button = QPushButton("접기")
        self.collapse_button.clicked.connect(lambda: self.set_collapsed(True))
        header.addWidget(self.collapse_button)
        header.addSpacing(6)
        self.exit_button = QPushButton("종료", objectName="exit")
        self.exit_button.setToolTip("SlideGuard 종료")
        self.exit_button.clicked.connect(self.exit_requested)
        header.addWidget(self.exit_button)
        for label in self.drag_header.findChildren(QLabel):
            label.installEventFilter(self.drag_header)
        layout.addWidget(self.drag_header)

        body = QHBoxLayout()
        body.setSpacing(6)
        self.findings_table = QTableWidget(0, 3)
        self.findings_table.setHorizontalHeaderLabels(["쪽", "검사 항목", "판정"])
        self.findings_table.horizontalHeaderItem(0).setToolTip("슬라이드 번호")
        self.findings_table.setMinimumWidth(210)
        self.findings_table.setMaximumWidth(240)
        self.findings_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.findings_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.findings_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.findings_table.setShowGrid(False)
        self.findings_table.verticalHeader().setVisible(False)
        self.findings_table.horizontalHeader().setMinimumSectionSize(28)
        self.findings_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.findings_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.findings_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.findings_table.horizontalHeader().resizeSection(2, 68)
        self.findings_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.findings_table.setWordWrap(False)
        self.findings_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.findings_table.currentCellChanged.connect(self._show_finding)
        self.findings_table.cellClicked.connect(self._request_finding_slide)
        body.addWidget(self.findings_table, 2)

        detail = QFrame(objectName="detail")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(6, 4, 6, 4)
        detail_layout.setSpacing(3)
        self.detail_title = QLabel("검사 결과를 선택하세요", objectName="title")
        self.detail_title.setWordWrap(True)
        self.detail_title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.detail_evidence = QLabel("판정 근거가 여기에 표시됩니다.", objectName="explanation")
        self.detail_evidence.setWordWrap(True)
        self.detail_evidence.setTextFormat(Qt.TextFormat.RichText)
        self.detail_evidence.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.detail_evidence.linkActivated.connect(self._show_measurement_details)
        detail_content = QWidget()
        detail_content.setStyleSheet("background: #fbfcfe;")
        detail_content_layout = QVBoxLayout(detail_content)
        detail_content_layout.setContentsMargins(0, 0, 0, 0)
        detail_content_layout.setSpacing(4)
        detail_content_layout.addWidget(self.detail_title)
        detail_content_layout.addWidget(self.detail_evidence)
        detail_content_layout.addStretch()
        evidence_scroll = self.detail_scroll = QScrollArea()
        evidence_scroll.setWidgetResizable(True)
        evidence_scroll.setFrameShape(QFrame.Shape.NoFrame)
        evidence_scroll.setMinimumSize(0, 0)
        evidence_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        evidence_scroll.setWidget(detail_content)
        self.detail_evidence.setStyleSheet("background: #fbfcfe;")
        detail_layout.addWidget(evidence_scroll, 1)
        self.location_label = QLabel("", objectName="muted")
        self.location_label.setWordWrap(True)
        self.location_label.hide()
        detail_layout.addWidget(self.location_label)
        finding_actions = QHBoxLayout()
        self.locate_button = QPushButton("위치 보기")
        self.locate_button.clicked.connect(lambda: self.locate_requested.emit(self._finding))
        self.locate_button.setEnabled(False)
        finding_actions.addWidget(self.locate_button)
        self.lock_button = QPushButton("객체 고정")
        self.lock_button.clicked.connect(lambda: self.lock_requested.emit(self._finding.shape_key))
        self.lock_button.setEnabled(False)
        finding_actions.addWidget(self.lock_button)
        self.view_fixes_button = QPushButton("수정안 보기")
        self.view_fixes_button.setEnabled(False)
        self.view_fixes_button.clicked.connect(lambda: self.fixes_requested.emit(self._finding))
        finding_actions.addWidget(self.view_fixes_button)
        detail_layout.addLayout(finding_actions)
        body.addWidget(detail, 6)

        proposal = QFrame(objectName="proposal")
        proposal_layout = QVBoxLayout(proposal)
        proposal_layout.setContentsMargins(6, 4, 6, 4)
        proposal_layout.setSpacing(3)
        proposal_header = QHBoxLayout()
        proposal_header.setSpacing(4)
        proposal_header.addWidget(QLabel("수정 제안", objectName="title"))
        self.proposal_details_link = QLabel('<a href="details" style="color:#1756b2">제안 설명</a>')
        self.proposal_details_link.linkActivated.connect(self._show_proposal_details)
        proposal_header.addWidget(self.proposal_details_link)
        self.select_warning_button = QPushButton("문제 발견 선택", objectName="proposalSelection")
        self.select_warning_button.setCheckable(True)
        self.select_warning_button.setToolTip(
            "문제 발견 항목의 최소 수정안을 선택합니다. 다시 누르면 해제합니다."
        )
        self.select_warning_button.clicked.connect(lambda: self.select_severity("warning"))
        proposal_header.addWidget(self.select_warning_button)
        self.select_review_button = QPushButton("검토 필요 선택", objectName="proposalSelection")
        self.select_review_button.setCheckable(True)
        self.select_review_button.setToolTip(
            "검토 필요 항목의 최소 수정안을 선택합니다. 다시 누르면 해제합니다."
        )
        self.select_review_button.clicked.connect(lambda: self.select_severity("review"))
        proposal_header.addWidget(self.select_review_button)
        proposal_header.addStretch()
        self.selected_count_label = QLabel("선택 0건", objectName="status")
        proposal_header.addWidget(self.selected_count_label)
        proposal_layout.addLayout(proposal_header)

        self.proposals_list = QListWidget(objectName="proposalList")
        self.proposals_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        # Keep the panel compact; longer proposal lists scroll within the available height.
        self.proposals_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.proposals_list.setMinimumHeight(0)
        self.proposals_list.setStyleSheet("QListWidget { min-height: 80px; }")
        self.proposals_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.proposals_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.proposals_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.proposals_list.setUniformItemSizes(True)
        self.proposals_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.proposals_list.itemSelectionChanged.connect(self._proposal_selection_changed)
        self.proposals_list.currentItemChanged.connect(self._show_proposal_explanation)
        proposal_body = QHBoxLayout()
        proposal_body.setSpacing(5)
        proposal_body.addWidget(self.proposals_list, 1)
        self.proposal_text = QLabel("검사 결과를 선택하면 제안 여부와 이유를 표시합니다.", objectName="muted")
        self.proposal_text.setWordWrap(True)
        self.proposal_text.setMaximumHeight(28)
        self.proposal_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.proposal_text.setParent(proposal)
        self.proposal_text.hide()
        direct_actions = QVBoxLayout()
        direct_actions.setSpacing(3)
        self.apply_button = QPushButton("선택 적용", objectName="primary")
        self.apply_button.setToolTip("선택한 수정 제안을 새 PPTX 사본에 저장합니다.")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_requested)
        direct_actions.addWidget(self.apply_button)
        self.apply_all_button = QPushButton("최소안 적용")
        self.apply_all_button.setToolTip("현재 문서 전체의 최소 수정안을 새 PPTX 사본에 저장합니다.")
        self.apply_all_button.clicked.connect(lambda: self._apply_all_proposals(recommended=False))
        self.apply_all_button.setEnabled(False)
        direct_actions.addWidget(self.apply_all_button)
        self.apply_all_recommended_button = QPushButton("권장안 적용")
        self.apply_all_recommended_button.setToolTip(
            "현재 문서 전체에 설정의 권장 목표(명암비·글자 크기)를 적용해 새 PPTX 사본에 저장합니다."
        )
        self.apply_all_recommended_button.clicked.connect(lambda: self._apply_all_proposals(recommended=True))
        self.apply_all_recommended_button.setEnabled(False)
        direct_actions.addWidget(self.apply_all_recommended_button)
        proposal_body.addLayout(direct_actions)
        proposal_layout.addLayout(proposal_body, 1)
        body.addWidget(proposal, 7)

        layout.addLayout(body, 1)
        return panel

    def set_collapsed(self, collapsed: bool):
        was_visible = self.isVisible()
        if was_visible:
            # Changing a native window mask while it is visible can leave a stale
            # backing-store strip on Windows, which looks like a second overlay.
            self.hide()
        self.collapsed = collapsed
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, collapsed)
        self.stack.setCurrentWidget(self.collapsed_page if collapsed else self.expanded_page)
        self.setMinimumSize(0, 0)
        maximum = SHIELD_SIZE if collapsed else 16777215
        self.setMaximumSize(maximum, maximum if collapsed else PANEL_HEIGHT)
        if collapsed:
            self.setMask(QRegion(0, 0, SHIELD_SIZE, SHIELD_SIZE, QRegion.RegionType.Ellipse))
        else:
            self.clearMask()
        count = len(self._report.proposals) if self._report is not None and not self._report_stale else 0
        self.count_badge.setVisible(collapsed and count > 0)
        self._apply_geometry()
        self.stack.updateGeometry()
        if was_visible:
            self.show()
            self.update()
            # Windows publishes the layout-derived native minimum size only
            # after the window is shown. Recalculate once with that real size
            # so it cannot shift the overlay above the available screen.
            QTimer.singleShot(0, self._apply_geometry)
        self.view_state_changed.emit()

    def set_target(self, target: PowerPointWindow | None):
        previous = self._target
        self._target = target
        if target is None:
            self.hide()
            if previous is not None:
                _restore_normal_stacking(self)
                self.view_state_changed.emit()
            return
        if previous is not None and previous.hwnd != target.hwnd:
            _restore_normal_stacking(self)
        self.document_label.setText(_presentation_name(target.title))
        self._apply_geometry()
        if not self.isVisible():
            self.show()
            self.view_state_changed.emit()
        _raise_over_powerpoint(self, target)

    def _move_shield(self, position: QPoint):
        if not self.collapsed or self._target is None:
            return
        position = self._bounded_shield_position(position)
        self._shield_offset = position - self._target.rect.topLeft()
        self._apply_geometry()

    def _bounded_shield_position(self, position: QPoint) -> QPoint:
        bounds = self._target.rect
        screen = self._target.screen_rect
        if screen is not None:
            visible = bounds.intersected(screen)
            if not visible.isEmpty():
                bounds = visible
        right = max(bounds.left(), bounds.right() - SHIELD_SIZE + 1)
        bottom = max(bounds.top(), bounds.bottom() - SHIELD_SIZE + 1)
        return QPoint(
            min(max(position.x(), bounds.left()), right),
            min(max(position.y(), bounds.top()), bottom),
        )

    def _apply_geometry(self):
        if self._target is None:
            return
        target = self._target.rect
        if self.collapsed:
            offset = (
                self._shield_offset if self._shield_offset is not None else QPoint(target.width() - 79, 58)
            )
            position = self._bounded_shield_position(target.topLeft() + offset)
            geometry = QRect(position.x(), position.y(), SHIELD_SIZE, SHIELD_SIZE)
            badge_width = max(
                17, self.count_badge.fontMetrics().horizontalAdvance(self.count_badge.text()) + 6
            )
            self.count_badge.setGeometry(max(0, 49 - badge_width), 7, badge_width, 17)
            if self.geometry() != geometry:
                self.setGeometry(geometry)
        else:
            self.layout().activate()
            self.expanded_page.layout().activate()
            available = self._target.available_rect or self._target.screen_rect or target
            bounds = target.intersected(available)
            if bounds.isEmpty():
                bounds = available
            bounds = bounds.adjusted(PANEL_SIDE_PADDING, 32, -PANEL_SIDE_PADDING, -8)
            width = min(PANEL_MAX_WIDTH, max(600, target.width() - 80), bounds.width())
            height = min(PANEL_HEIGHT, bounds.height())
            position = (
                target.topLeft() + self._panel_offset if self._panel_offset is not None else bounds.topLeft()
            )
            left = min(max(position.x(), bounds.left()), bounds.right() - width + 1)
            top = min(max(position.y(), bounds.top()), bounds.bottom() - height + 1)
            geometry = QRect(left, top, width, height)
            if self.geometry() != geometry:
                self.setGeometry(geometry)

    def _move_panel(self, position: QPoint):
        if self.collapsed or self._target is None:
            return
        self._panel_offset = position - self._target.rect.topLeft()
        self._apply_geometry()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.geometry_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.geometry_changed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        self.geometry_changed.emit()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.geometry_changed.emit()

    def show_progress(self, progress):
        text = progress_text(progress)
        if progress.total:
            stage = progress.stage.partition(":")[0]
            label = {"compare": "변경 비교", "read": "읽기", "render": "화면 측정", "check": "판정"}.get(
                stage, "검사"
            )
            self.status_label.setText(
                f"{label} · {progress.completed}/{progress.total}장 · 재사용 {progress.reused}장"
            )
        else:
            self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self.progress_bar.setToolTip(text)
        self.progress_bar.setRange(0, progress.total if progress.total else 0)
        if progress.total:
            self.progress_bar.setValue(progress.completed)
        self.progress_bar.show()

    def show_detected(self, path: Path):
        self.status_label.setText(f"검사 중 · {path.name}")
        self._report_stale = True
        self.count_badge.hide()
        self.set_analysis_busy(True)
        self.view_state_changed.emit()

    def show_failure(self, message: str):
        self._last_failure = message
        self._finding = None
        self.status_label.setText("검사 실패")
        self.detail_title.setText("검사에 실패했습니다")
        self.detail_evidence.setText(
            escape(friendly_error(message))
            + '<br><a href="error-details" style="color:#1756b2">오류 상세 보기</a>'
        )
        self.locate_button.setEnabled(False)
        self.lock_button.setEnabled(False)
        self.location_label.clear()
        self.set_analysis_busy(False)

    def mark_report_stale(self, message: str):
        self._report_stale = True
        self.count_badge.hide()
        self._analysis_busy = False
        self.status_label.setText("재검사 필요")
        self.proposal_text.setText(message)
        self.manual_button.setEnabled(not self._apply_busy)
        self._update_apply_button()
        self.view_state_changed.emit()

    def clear_report(self):
        self._report = self._finding = None
        self._report_stale = False
        self._skip_reasons.clear()
        self.count_badge.clear()
        self.count_badge.hide()
        self.findings_table.setRowCount(0)
        self.proposals_list.clear()
        self.proposal_items.clear()
        self._proposal_ids_by_severity.clear()
        self.status_label.setText("대기 중")
        self.detail_title.setText("검사 결과를 선택하세요")
        self.detail_evidence.clear()
        self.location_label.clear()
        self.proposal_text.clear()
        self.set_analysis_busy(False)
        self.view_state_changed.emit()

    def show_report(self, report: AnalysisReport, skip_reasons: dict[str, str] | None = None):
        self._report = report
        self._finding = None
        self._skip_reasons = dict(skip_reasons or {})
        self._report_stale = False
        self.set_analysis_busy(False)
        count = len(report.findings)
        self.status_label.setText(f"검사 완료 · {count}건")
        self.manual_button.setEnabled(True)
        proposal_count = len(report.proposals)
        self.count_badge.setText(str(proposal_count) if proposal_count else "")
        self.count_badge.setVisible(self.collapsed and proposal_count > 0)
        self._apply_geometry()
        with QSignalBlocker(self.findings_table):
            self.findings_table.setRowCount(count)
            for row, finding in enumerate(report.findings):
                values = [
                    finding.shape_key.partition(":")[0],
                    _rule_name(finding.rule_id),
                    _severity(finding.severity),
                ]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(value)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, finding)
                    self.findings_table.setItem(row, column, item)
            if count:
                self.findings_table.selectRow(0)
        self._populate_proposals(report)
        if count:
            self._show_finding(0, 0, -1, -1)
        else:
            self.detail_title.setText("발견된 문제가 없습니다")
            self.detail_evidence.setText("실행된 규칙에서 보고된 항목이 없습니다.")
            self.location_label.clear()
            self._update_apply_button()

    def _request_finding_slide(self, row: int, _column: int):
        item = self.findings_table.item(row, 0)
        if item is not None:
            self.slide_requested.emit(item.data(Qt.ItemDataRole.UserRole))

    def _show_finding(self, row: int, *_args):
        if row < 0:
            return
        item = self.findings_table.item(row, 0)
        finding = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if finding is None:
            return
        self._finding = finding
        self._update_apply_button()
        slide = finding.shape_key.partition(":")[0]
        explanation = describe_finding(finding)
        self.detail_title.setText(f"{slide} 슬라이드 · {explanation.title}")
        self.detail_title.setToolTip(self.detail_title.text())
        self.detail_evidence.setText(explanation.html(compact=True))
        self.detail_scroll.verticalScrollBar().setValue(0)
        matching = [
            item
            for item in self._report.proposals
            if item.finding_id == finding.id
            or item.property == "slide.split"
            and item.shape_key.split(":")[0] == slide
            and finding.rule_id in ("layout.density", "typography")
        ]
        if matching:
            structural = next((p for p in matching if p.property == "slide.split"), None)
            self.proposal_text.setText(
                structural.explanation
                if structural
                else f"이 결과에 적용 가능한 제안 {len(matching)}건이 있습니다. "
                "리스트의 행을 눌러 적용할 제안을 선택하세요. 다시 누르면 해제됩니다."
            )
        else:
            reason = self._skip_reasons.get(finding.id)
            if reason is None:
                if finding.severity == "unknown":
                    reason = "판단에 필요한 정보를 확인하지 못했습니다. 해당 위치를 직접 확인해 주세요."
                elif finding.severity == "review":
                    reason = "의도한 디자인일 수 있어 자동으로 바꾸지 않았습니다. 해당 위치를 확인해 주세요."
                else:
                    reason = "원래 내용을 유지하면서 적용할 수정안을 찾지 못했습니다. 해당 위치에서 직접 조정해 주세요."
            self.proposal_text.setText(friendly_reason(reason))
        split_note = self._report.proposal_notes.get(finding.id)
        if split_note:
            self.proposal_text.setText(
                self.proposal_text.text() + "\n분할하지 않은 이유: " + friendly_reason(split_note)
            )
        self.proposal_text.setToolTip(self.proposal_text.text())
        if finding_location_box(self._report, finding) is None:
            self.location_label.setText("행을 클릭하면 PowerPoint에서 해당 슬라이드로 이동합니다.")
        else:
            self.location_label.setText("행 클릭: 슬라이드 이동 · 위치 보기: 해당 부분 표시")

        self.view_state_changed.emit()

    def _show_measurement_details(self, link):
        if link == "error-details" and getattr(self, "_last_failure", None):
            self._measurement_dialog = TechnicalDetailsDialog(
                "작업을 완료하지 못한 이유", self._last_failure, self
            )
            self._measurement_dialog.show()
            return
        if link != "measurement-details" or self._finding is None:
            return
        notes = self._skip_reasons.get(self._finding.id, "")
        split_note = self._report.proposal_notes.get(self._finding.id, "") if self._report else ""
        self._measurement_dialog = FindingDetailsDialog(
            self._finding, self, notes="\n".join(dict.fromkeys(filter(None, (notes, split_note))))
        )
        self._measurement_dialog.show()

    def _show_proposal_details(self, _link):
        self._proposal_dialog = TechnicalDetailsDialog("수정 제안 설명", self.proposal_text.text(), self)
        self._proposal_dialog.show()

    def _populate_proposals(self, report: AnalysisReport):
        self.proposals_list.blockSignals(True)
        self.proposals_list.clear()
        self.proposal_items.clear()
        self._proposal_ids_by_severity.clear()
        findings = {item.id: item for item in report.findings}
        has_recommended = {_variant_target(item) for item in report.proposals if is_recommended(item.id)}
        try:
            for proposal in report.proposals:
                finding = findings.get(proposal.finding_id)
                severity = _severity(finding.severity) if finding is not None else "검사 결과"
                if finding is not None:
                    self._proposal_ids_by_severity.setdefault(finding.severity, set()).add(proposal.id)
                slide = _slide_number(proposal.shape_key)
                variant = ""
                if is_recommended(proposal.id):
                    variant = " · 권장 수정"
                elif _variant_target(proposal) in has_recommended:
                    variant = " · 최소 수정"
                label = f"{slide}번 · {severity}{variant} · {proposal_change_text(proposal)}"
                item = QListWidgetItem(label)
                item.setSizeHint(QSize(1, 28))
                item.setToolTip(
                    label + ("\n" + friendly_reason(proposal.explanation) if proposal.explanation else "")
                )
                item.setData(Qt.ItemDataRole.UserRole, proposal)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                self.proposals_list.addItem(item)
                self.proposal_items[proposal.id] = item
        finally:
            self.proposals_list.blockSignals(False)
        self._update_apply_button()

    def _proposal_selection_changed(self):
        if self._syncing_proposals or self._report is None:
            return
        selected = set(self.selected_proposal_ids())
        changed = selected ^ self._selection_snapshot
        handled = set()
        # A native row selection can cover several items at once (keyboard/range).
        # Process a bundle once; additions take precedence over removals in it.
        proposals = sorted(self._report.proposals, key=lambda p: p.id not in selected)
        for proposal in proposals:
            group = _proposal_group(proposal) or proposal.id
            if proposal.id in changed and group not in handled:
                handled.add(group)
                self._proposal_toggled(proposal.id)

    def _show_proposal_explanation(self, item, _previous=None):
        if item is not None:
            proposal = item.data(Qt.ItemDataRole.UserRole)
            if proposal is not None and proposal.explanation:
                self.proposal_text.setText(friendly_reason(proposal.explanation))

    def _proposal_toggled(self, proposal_id: str):
        if self._syncing_proposals or self._report is None:
            return
        proposal = next((item for item in self._report.proposals if item.id == proposal_id), None)
        if proposal is None:
            return
        group = _proposal_group(proposal)
        checked = self.proposal_items[proposal_id].isSelected()
        members = [proposal.id]
        released = []  # 이 체크 때문에 해제된 반대쪽 변형 (최소 ↔ 권장)
        self._syncing_proposals = True
        try:
            if group is not None:
                members = [item.id for item in self._report.proposals if _proposal_group(item) == group]
                for member in members:
                    self.proposal_items[member].setSelected(checked)
            if checked:
                # 최소 수정과 권장 수정은 같은 대상의 다른 값이라 하나만 고를 수 있다
                for other in _counterparts(self._report, proposal):
                    if self.proposal_items[other.id].isSelected():
                        released.append(other.id)
                    self.proposal_items[other.id].setSelected(False)
        finally:
            self._syncing_proposals = False
        self._update_apply_button()
        self.proposals_list.viewport().update()
        self.view_state_changed.emit()
        # 열린 문서에 바로 적용하고 그 슬라이드로 이동 (컨트롤러가 처리). 반대쪽 변형은 먼저 되돌린다
        if released:
            ordinary_released = tuple(pid for pid in released if not pid.startswith("slide.split@"))
            if ordinary_released:
                self.proposal_user_toggled.emit(ordinary_released, False)
        if proposal.property != "slide.split":
            self.proposal_user_toggled.emit(tuple(members), checked)

    def set_proposals_checked(self, proposal_ids, checked: bool):
        """렌즈 배지에서 적용·해제한 후보를 행 선택에도 맞춘다 (신호는 내지 않음)."""
        self._syncing_proposals = True
        try:
            for proposal_id in proposal_ids:
                item = self.proposal_items.get(proposal_id)
                if item is not None and item.isSelected() != checked:
                    if checked and self._report is not None:
                        proposal = item.data(Qt.ItemDataRole.UserRole)
                        for other in _counterparts(self._report, proposal):
                            self.proposal_items[other.id].setSelected(False)
                    item.setSelected(checked)
        finally:
            self._syncing_proposals = False
        self._update_apply_button()
        self.proposals_list.viewport().update()

    def select_severity(self, severity: str):
        # 최소 수정을 고른다 (권장 수정은 목록에서 직접 고르거나 '권장안으로 적용하기')
        proposal_ids = {
            item
            for item in self._proposal_ids_by_severity.get(severity, set())
            if not is_recommended(item) and not item.startswith("slide.split@")
        }
        if self._report is None or not proposal_ids:
            return
        checked = not proposal_ids.issubset(self.selected_proposal_ids())
        self._syncing_proposals = True
        try:
            selected_groups = {
                _proposal_group(proposal)
                for proposal in self._report.proposals
                if proposal.id in proposal_ids and _proposal_group(proposal) is not None
            }
            selected_slides = {
                p.shape_key.split(":")[0] for p in self._report.proposals if p.id in proposal_ids
            }
            for proposal in self._report.proposals:
                if (
                    checked
                    and proposal.property == "slide.split"
                    and proposal.shape_key.split(":")[0] in selected_slides
                ):
                    self.proposal_items[proposal.id].setSelected(False)
                if proposal.id in proposal_ids or _proposal_group(proposal) in selected_groups:
                    self.proposal_items[proposal.id].setSelected(checked)
                    if checked:
                        for other in _counterparts(self._report, proposal):
                            self.proposal_items[other.id].setSelected(False)
        finally:
            self._syncing_proposals = False
        self._update_apply_button()
        self.view_state_changed.emit()

    def _apply_all_proposals(self, recommended: bool = False):
        """모든 대상에 최소 수정(또는 권장 수정이 있으면 권장 수정)을 하나씩 골라 적용한다."""
        chosen = set(_pick_variant(self._report, recommended=recommended)) if self._report else set()
        self.proposals_list.blockSignals(True)
        try:
            for proposal_id, item in self.proposal_items.items():
                item.setSelected(proposal_id in chosen)
        finally:
            self.proposals_list.blockSignals(False)
        self._update_apply_button()
        self.view_state_changed.emit()
        self.apply_requested.emit()

    def clear_proposals(self):
        self._syncing_proposals = True
        try:
            for item in self.proposal_items.values():
                item.setSelected(False)
        finally:
            self._syncing_proposals = False
        self._update_apply_button()
        self.view_state_changed.emit()

    def selected_proposal_ids(self) -> tuple[str, ...]:
        return tuple(proposal_id for proposal_id, item in self.proposal_items.items() if item.isSelected())

    def set_apply_busy(self, busy: bool):
        self._apply_busy = busy
        self.manual_button.setEnabled(not busy and not self._analysis_busy)
        self._update_apply_button()

    def set_analysis_busy(self, busy: bool):
        self._analysis_busy = busy
        self.progress_bar.setVisible(busy)
        if busy:
            self.progress_bar.setRange(0, 0)
        self.manual_button.setEnabled(not busy and not self._apply_busy)
        self._update_apply_button()

    def _update_apply_button(self):
        selected = set(self.selected_proposal_ids())
        self._selection_snapshot = selected
        count = len(selected)
        self.selected_count_label.setText(f"선택 {count}건")
        self.apply_button.setText("적용 중…" if self._apply_busy else "선택 적용")
        ready = self._report is not None and not (
            self._analysis_busy or self._apply_busy or self._report_stale or self._split_busy
        )
        self.view_fixes_button.setEnabled(ready and self._finding is not None)
        for severity, button in (
            ("warning", self.select_warning_button),
            ("review", self.select_review_button),
        ):
            proposal_ids = {
                pid
                for pid in self._proposal_ids_by_severity.get(severity, ())
                if not is_recommended(pid) and not pid.startswith("slide.split@")
            }
            button.setEnabled(ready and bool(proposal_ids))
            pressed = bool(proposal_ids and proposal_ids <= selected)
            button.setChecked(pressed)
            if button.property("selected") != pressed:
                button.setProperty("selected", pressed)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()
        self.apply_button.setEnabled(ready and count > 0)
        self.apply_all_button.setEnabled(
            ready and any(not pid.startswith("slide.split@") for pid in self.proposal_items)
        )
        self.apply_all_recommended_button.setEnabled(
            ready
            and any(
                is_recommended(proposal_id) or proposal_id.startswith("slide.split@")
                for proposal_id in self.proposal_items
            )
        )
        self.locate_button.setEnabled(
            ready
            and self._finding is not None
            and finding_location_box(self._report, self._finding) is not None
        )
        self.lock_button.setEnabled(
            ready
            and self._finding is not None
            and re.fullmatch(r"\d+:\d+", self._finding.shape_key) is not None
        )


class SettingsDialog(QDialog):
    settings_saved = Signal()

    def __init__(self, parent=None, *, path_provider=active_presentation_path):
        super().__init__(parent)
        self.path_provider = path_provider
        self.document: Path | None = None
        self.setWindowTitle("SlideGuard 설정")
        self.resize(590, 490)
        self.setStyleSheet(OVERLAY_STYLE)
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs)
        workspace = QWidget()
        tabs.addTab(workspace, "작업 폴더")
        layout = QVBoxLayout(workspace)

        layout.addWidget(QLabel("작업 폴더", objectName="title"))
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        folder_row.addWidget(self.folder_edit, 1)
        browse = QPushButton("폴더 선택")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        option_row = QHBoxLayout()
        self.recursive = QCheckBox("하위 폴더 포함")
        option_row.addWidget(self.recursive)
        option_row.addStretch()
        refresh = QPushButton("파일 목록 새로고침")
        refresh.clicked.connect(self.refresh_files)
        option_row.addWidget(refresh)
        layout.addLayout(option_row)

        self.files = QListWidget()
        layout.addWidget(self.files, 1)

        self.start_on_login = QCheckBox("Windows 로그인 시 자동 실행")
        self.start_on_login.setEnabled(sys.platform == "win32")
        layout.addWidget(self.start_on_login)
        startup_hint = QLabel(
            "창 없이 대기하다 추적 폴더의 PPTX를 열면 우상단에 방패를 표시합니다.\n"
            "창을 닫아도 대기하며, 완전히 끝내려면 트레이 메뉴에서 종료하세요.",
            objectName="muted",
        )
        startup_hint.setWordWrap(True)
        layout.addWidget(startup_hint)

        policy_page = QWidget(objectName="policyPage")
        self.tabs = tabs
        policy_scroll = QScrollArea()
        policy_scroll.setObjectName("policyScroll")
        self.policy_scroll = policy_scroll
        policy_scroll.setWidgetResizable(True)
        policy_scroll.setFrameShape(QFrame.Shape.NoFrame)
        policy_scroll.setWidget(policy_page)
        tabs.addTab(policy_scroll, "검사 기준·수정 범위")
        policy_form = QFormLayout(policy_page)
        self.purpose = QComboBox()
        self.purpose.addItem("발표용", Purpose.PRESENTATION.value)
        self.purpose.addItem("강의용", Purpose.LECTURE.value)
        self.purpose.addItem("보고서용", Purpose.REPORT.value)
        policy_form.addRow("검사 목적", self.purpose)
        self.strength = QComboBox()
        self.strength.addItem("약하게", Strength.LIGHT.value)
        self.strength.addItem("표준", Strength.STANDARD.value)
        self.strength.addItem("강하게", Strength.STRONG.value)
        policy_form.addRow("수정 강도", self.strength)
        # 수정 목표: 검사 기준을 넘은 뒤 얼마나 여유 있게 고칠지 (검사 결과에는 영향 없음)
        contrast_row = QHBoxLayout()
        self.contrast_goal = QComboBox()
        self.contrast_goal.addItem("기준선만 넘기기 (원래 색에 가장 가깝게)", ContrastGoal.MINIMUM.value)
        self.contrast_goal.addItem("여유 있게 (모든 글자 4.5:1 이상)", ContrastGoal.COMFORTABLE.value)
        self.contrast_goal.addItem("매우 선명하게 (WCAG AAA 7:1)", ContrastGoal.ENHANCED.value)
        self.contrast_goal.addItem("직접 입력", ContrastGoal.CUSTOM.value)
        self.contrast_ratio_edit = QLineEdit(placeholderText="예 6")
        self.contrast_ratio_edit.setMaximumWidth(70)
        self.contrast_goal.currentIndexChanged.connect(self._update_contrast_goal)
        contrast_row.addWidget(self.contrast_goal, 1)
        contrast_row.addWidget(self.contrast_ratio_edit)
        policy_form.addRow("명암비 수정 목표", contrast_row)
        self.font_goal = QComboBox()
        self.font_goal.addItem("최소 크기까지", FontGoal.MINIMUM.value)
        self.font_goal.addItem("권장 크기까지", FontGoal.RECOMMENDED.value)
        policy_form.addRow("글자 크기 수정 목표", self.font_goal)
        self.line_spacing_edit = QLineEdit(placeholderText="기본 1.2 · 1~3 사이")
        policy_form.addRow("권장 줄 간격 (배)", self.line_spacing_edit)
        self.slides_edit = QLineEdit(placeholderText="비우면 전체 · 예 1, 3-5")
        policy_form.addRow("검사 슬라이드", self.slides_edit)
        locks = QHBoxLayout()
        self.property_locks = {}
        for key, label in (
            ("font_size", "글자 크기"),
            ("font_color", "색상"),
            ("font_family", "글꼴"),
            ("position", "위치"),
            ("size", "도형 크기"),
        ):
            checkbox = QCheckBox(label)
            self.property_locks[key] = checkbox
            locks.addWidget(checkbox)
        policy_form.addRow("고정할 속성", locks)
        self.document_label = QLabel()
        self.document_label.setWordWrap(True)
        self.locked_objects = ObjectLocksEditor()
        policy_form.addRow("고정할 객체", self.locked_objects)
        policy_form.addRow("", self.document_label)

        threshold_page = QWidget()
        tabs.addTab(threshold_page, "사용자 기준값")
        threshold_form = QFormLayout(threshold_page)
        threshold_form.addRow(
            QLabel("입력하지 않은 값은 목적별 기본값을 사용합니다. 제목 위계는 1.25배입니다.")
        )
        self.threshold_fields = {}
        for key, label in (
            ("minimum_font", "최소 글자 크기 (pt)"),
            ("recommended_font", "권장 글자 크기 (pt)"),
            ("max_chars", "최대 글자 수"),
            ("max_lines", "최대 줄 수"),
            ("contrast_normal", "일반 글자 명암비 (:1)"),
            ("contrast_large", "대형 글자 명암비 (:1)"),
            ("margin_percent", "안전 여백 (%)"),
        ):
            field = QLineEdit()
            self.threshold_fields[key] = field
            threshold_form.addRow(label, field)
        self.purpose.currentIndexChanged.connect(self._update_threshold_placeholders)
        self.recursive.toggled.connect(self.refresh_files)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("저장", objectName="primary")
        save.clicked.connect(self.save)
        actions.addWidget(save)
        outer.addLayout(actions)
        self.load(capture_document=False)

    def load(self, lock_shape: str | None = None, *, capture_document: bool = True):
        self.document = self.path_provider() if capture_document else None
        self.start_on_login.setEnabled(sys.platform == "win32")
        self.start_on_login.setToolTip("")
        try:
            self._startup_enabled = startup.is_enabled()
        except OSError:
            LOGGER.warning("STARTUP_SETTINGS_UNAVAILABLE", exc_info=True)
            self._startup_enabled = False
            self.start_on_login.setEnabled(False)
            self.start_on_login.setToolTip("Windows 자동 실행 설정을 읽을 수 없습니다.")
        self.start_on_login.setChecked(self._startup_enabled)
        settings = build_workspace_settings_repository().load()
        if settings is not None:
            self.folder_edit.setText(str(settings.workspace))
            self.recursive.setChecked(settings.recursive)
        policy = settings.policy_for(self.document) if settings is not None else AnalysisPolicy()
        self.purpose.setCurrentIndex(self.purpose.findData(policy.purpose.value))
        self.strength.setCurrentIndex(self.strength.findData(policy.strength.value))
        self.contrast_goal.setCurrentIndex(self.contrast_goal.findData(policy.fix.contrast_goal.value))
        self.contrast_ratio_edit.setText(
            "" if policy.fix.contrast_ratio is None else f"{policy.fix.contrast_ratio:g}"
        )
        self.font_goal.setCurrentIndex(self.font_goal.findData(policy.fix.font_goal.value))
        self.line_spacing_edit.setText(
            "" if policy.fix.line_spacing is None else f"{policy.fix.line_spacing:g}"
        )
        self._update_contrast_goal()
        self.slides_edit.setText(", ".join(map(str, policy.slides)))
        for key, checkbox in self.property_locks.items():
            checkbox.setChecked(key in policy.locked_properties)
        for key, editor in self.threshold_fields.items():
            value = getattr(policy.thresholds, key)
            editor.setText("" if value is None else str(value))
        self.document_label.setText(
            f"{self.document.name}에만 적용"
            if self.document is not None
            else "저장된 PowerPoint를 열면 객체를 고정할 수 있습니다."
        )
        keys = set(policy.locked_shapes)
        if lock_shape is not None and self.document is not None:
            keys.add(lock_shape)
            self.tabs.setCurrentIndex(1)
        self.locked_objects.set_document(self.document, keys)
        if lock_shape is not None:
            QTimer.singleShot(0, lambda: self.policy_scroll.ensureWidgetVisible(self.locked_objects))
        self._update_threshold_placeholders()
        self.refresh_files()

    def _update_threshold_placeholders(self):
        policy = AnalysisPolicy(purpose=Purpose(self.purpose.currentData()))
        defaults = (
            policy.font_limits.minimum_pt,
            policy.font_limits.recommended_pt,
            policy.density_limits.max_chars,
            policy.density_limits.max_lines,
            policy.normal_contrast,
            policy.large_contrast,
            policy.margin_ratio * 100,
        )
        for editor, value in zip(self.threshold_fields.values(), defaults, strict=True):
            editor.setPlaceholderText(f"기본 {value:g}")

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "작업 폴더 선택", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)
            self.refresh_files()

    def _update_contrast_goal(self, *_args):
        self.contrast_ratio_edit.setEnabled(self.contrast_goal.currentData() == ContrastGoal.CUSTOM.value)

    def _fix_targets(self) -> FixTargets:
        goal = ContrastGoal(self.contrast_goal.currentData())
        ratio = None
        if goal == ContrastGoal.CUSTOM:
            try:
                ratio = float(self.contrast_ratio_edit.text().strip())
            except ValueError:
                raise ValueError("명암비 수정 목표를 숫자로 입력하세요 (예 6).") from None
        return FixTargets(
            contrast_goal=goal,
            contrast_ratio=ratio,
            font_goal=FontGoal(self.font_goal.currentData()),
            line_spacing=float(self.line_spacing_edit.text())
            if self.line_spacing_edit.text().strip()
            else None,
        )

    def refresh_files(self):
        self.files.clear()
        value = self.folder_edit.text().strip()
        folder = Path(value)
        if not value or not folder.is_dir():
            return
        paths = presentation_paths(folder, recursive=self.recursive.isChecked())
        self.files.addItems(str(path.relative_to(folder)) for path in sorted(paths))

    def save(self):
        value = self.folder_edit.text().strip()
        folder = Path(value)
        if not value or not folder.is_dir():
            QMessageBox.warning(self, "설정 저장 실패", "접근 가능한 작업 폴더를 선택하세요.")
            return
        try:
            shapes = self.locked_objects.keys
            overrides = {}
            for key, editor in self.threshold_fields.items():
                raw = editor.text().strip()
                if raw:
                    try:
                        overrides[key] = int(raw) if key in {"max_lines", "max_chars"} else float(raw)
                    except ValueError:
                        raise ValueError(
                            f"{self.threshold_fields[key].placeholderText()} 형식에 맞춰 숫자를 입력하세요."
                        ) from None
            policy = AnalysisPolicy(
                purpose=Purpose(self.purpose.currentData()),
                strength=Strength(self.strength.currentData()),
                slides=_parse_slides(self.slides_edit.text()),
                locked_properties=frozenset(
                    key for key, item in self.property_locks.items() if item.isChecked()
                ),
                locked_shapes=shapes,
                thresholds=ThresholdOverrides(**overrides),
                fix=self._fix_targets(),
            )
            build_workspace_settings_repository().save(
                folder.resolve(), self.recursive.isChecked(), policy=policy, document=self.document
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "설정 저장 실패", str(error))
            return
        self.settings_saved.emit()
        if self.start_on_login.isChecked() or self._startup_enabled:
            try:
                startup.set_enabled(self.start_on_login.isChecked())
            except (OSError, ValueError) as error:
                QMessageBox.warning(
                    self, "자동 실행 설정 실패", f"작업 폴더와 검사 기준은 저장했습니다.\n{error}"
                )
                return
            self._startup_enabled = self.start_on_login.isChecked()
        self.accept()


def _parse_slides(text: str) -> tuple[int, ...]:
    if not text.strip():
        return ()
    slides = set()
    for part in text.split(","):
        match = re.fullmatch(r"\s*([1-9]\d*)\s*(?:-\s*([1-9]\d*)\s*)?", part)
        if match is None:
            raise ValueError("검사 슬라이드는 1, 3-5 형식으로 입력하세요.")
        start = int(match[1])
        end = int(match[2]) if match[2] else start
        if not start <= end <= 10000:
            raise ValueError("슬라이드 범위는 1~10000 사이에서 오름차순으로 입력하세요.")
        slides.update(range(start, end + 1))
    return tuple(sorted(slides))


class FindingOutline(QWidget):
    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setPen(QPen(QColor("#d92d20"), 3))
        painter.drawRect(self.rect().adjusted(2, 2, -3, -3))


class OverlayController(QObject):
    detected = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    collection_failed = Signal(object)
    file_activity = Signal(object, str)
    progress = Signal(object)
    workspace_requested = Signal()

    def __init__(
        self,
        *,
        tracker: PowerPointTracker | None = None,
        path_provider=active_presentation_path,
        state_provider=active_presentation_state,
        collection_builder=build_snapshot_collection,
        patcher_builder=build_presentation_patcher,
        preview_renderer=render_slide_previews,
        copy_inspector=inspect_saved_copy,
        outline_locator=None,
        slide_navigator=None,
        slide_view_reader=None,
        live_editor=None,
        input_activity=None,
    ):
        super().__init__()
        self.overlay = OverlayWidget()
        self.settings_dialog = SettingsDialog(self.overlay, path_provider=path_provider)
        self._settings_suspended_for: int | None = None
        self.outline = FindingOutline()
        self.outline_locator = outline_locator or _powerpoint_finding_rect
        self.slide_navigator = slide_navigator or _navigate_powerpoint_slide
        self._highlight: tuple[Path, int, Box] | None = None
        # 슬라이드 렌즈: 지금 보이는 슬라이드 위에 검사 결과 배지·변경 표시를 겹쳐 그림
        self.lens = SlideLens()
        self.lens_enabled = True
        self.slide_view_reader = slide_view_reader
        self._lens_probe: SlideViewProbe | None = None
        self._lens_view: SlideView | None = None
        self._lens_target: PowerPointWindow | None = None
        self._lens_checked = 0.0
        self._lens_window: tuple[int, QRect] | None = None  # 마지막으로 위치를 물은 PowerPoint 창
        self._lens_force = True  # 입력이 없어도 다음 번에 한 번 묻기
        # 렌즈 배지의 체크박스: 열린 문서에 바로 적용 / 되돌리기
        self.live_editor = live_editor or LiveEditor()
        self._live_task: LiveEditTask | None = None
        self._live_hash: str | None = None  # 적용할 때의 검사 결과 (저장 후 재검사되면 기억을 지움)
        self.lens.option_toggled.connect(self.toggle_live_edit)
        self._live_queue: list[tuple[tuple[str, ...], bool]] = []  # 적용 중에 들어온 체크 (차례로 처리)
        # PowerPoint에 COM으로 묻는 일은 사용자 입력 뒤에만 (가만히 있을 때 바쁜 커서가 계속 돌지 않게)
        self.activity = input_activity or InputActivity()
        self._outline_window: QRect | None = None
        self.tracker = tracker or PowerPointTracker()
        self.path_provider = path_provider
        self.state_provider = state_provider
        self.collection_builder = collection_builder
        self.patcher_builder = patcher_builder
        self.preview_renderer = preview_renderer
        self.collection = None
        self.active_path: Path | None = None
        self._last_powerpoint_window: tuple[int, str] | None = None
        self._last_path_check = 0.0
        self._workspace: Path | None = None
        self._recursive = False
        self._processing_paths: set[Path] = set()
        self.copy_inspector = copy_inspector
        self._apply_thread: CopyApplyWorker | OriginalApplyWorker | None = None
        self._result_dialog: ApplyResultDialog | None = None
        self._last_copy_outcome: CopyApplyOutcome | None = None
        self._stopping = False
        self._split_worker = None
        self._split_attempts = set()

        self.overlay.manual_requested.connect(self.request_manual)
        self.overlay.settings_requested.connect(lambda: self.show_settings())
        self.overlay.apply_requested.connect(self.apply_selected)
        self.overlay.exit_requested.connect(self.request_exit)
        self.overlay.workspace_requested.connect(self.workspace_requested)
        self.overlay.locate_requested.connect(self.locate_finding)
        self.overlay.slide_requested.connect(self.navigate_to_finding_slide)
        self.overlay.lock_requested.connect(self.show_settings)
        self.overlay.fixes_requested.connect(self.request_fixes)
        self.overlay.view_state_changed.connect(self._redraw_lens)
        self.overlay.geometry_changed.connect(self._sync_lens_exclusions)
        self.overlay.lens_toggled.connect(self.set_lens_enabled)
        self.overlay.proposal_user_toggled.connect(self._panel_proposal_toggled)
        self.settings_dialog.settings_saved.connect(self.restart_collection)
        self.detected.connect(self._on_detected)
        self.completed.connect(self._on_completed)
        self.collection_failed.connect(self._on_collection_failure)
        self.file_activity.connect(self._track_file_activity)
        self.progress.connect(self._on_progress)
        self.failed.connect(self.overlay.show_failure)

        self.timer = QTimer(self)
        self.timer.setInterval(125)
        self.timer.timeout.connect(self.poll_powerpoint)
        self.timer.start()
        self.restart_collection()

    def restart_collection(self):
        if self.collection is not None:
            # 기다리지 않는다: 이전 폴더의 파일을 PowerPoint로 읽는 중이면 그 파일이 끝날 때까지
            # 화면이 멈췄다 (시작 직후 폴더를 바꾸면 수십 초). 이전 결과는 stop 즉시 버려진다.
            self.collection.stop(wait=False)
            self.collection = None
        self._workspace = None
        self._processing_paths.clear()
        self._clear_highlight()
        self.overlay.clear_report()
        try:
            settings = build_workspace_settings_repository().load()
            if settings is None or not settings.workspace.is_dir():
                return
            self._workspace = settings.workspace.resolve()
            self._recursive = settings.recursive
            collection = self.collection_builder(
                self._workspace,
                self._process_snapshot,
                recursive=self._recursive,
                successful_hashes=PolicyAnalysisHashes(),
                on_failure=self.collection_failed.emit,
                on_detected=self.detected.emit,
                on_watch_event=self._watch_event,
                on_collecting=lambda path: self.file_activity.emit(path, "검사 중"),
                on_progress=self.progress.emit,
            )
            if hasattr(collection.collector, "prioritize"):
                collection.collector.prioritize(self.active_path or self.path_provider())
            collection.start()
            self.collection = collection
            LOGGER.info(
                "WORKSPACE_WATCH_STARTED | folder=%s | recursive=%s", self._workspace, self._recursive
            )
            self._load_current_report()
        except Exception as error:
            LOGGER.exception("WORKSPACE_WATCH_FAILED")
            self.failed.emit(f"{type(error).__name__}: {error}")

    def poll_powerpoint(self):
        dialogs = [dialog for dialog in self.overlay.findChildren(QDialog) if dialog.isVisible()]
        owned = {int(dialog.winId()) for dialog in dialogs}
        owned.add(int(self.overlay.winId()))
        owned.update(self.lens.window_ids())
        target = self.tracker.poll(owned)
        if target is None:
            self._last_powerpoint_window = None
        else:
            window = (target.hwnd, target.title)
            now = monotonic()
            # 창이 바뀌면 바로, 그 밖에는 사용자 입력이 있었을 때만 (최소 1초 간격) 문서 경로를 다시 묻는다
            if window != self._last_powerpoint_window or (
                now - self._last_path_check >= 1.0 and self.activity.due("path")
            ):
                self._last_powerpoint_window = window
                self._last_path_check = now
                self._set_active_path(self.path_provider())
            if self._workspace is not None and (
                self.active_path is None or not self._in_workspace(self.active_path)
            ):
                target = None
        previous = self.overlay._target
        if previous is not None and (target is None or previous.hwnd != target.hwnd):
            for dialog in dialogs:
                if dialog is self.settings_dialog:
                    self._settings_suspended_for = previous.hwnd
                    dialog.hide()
                else:
                    dialog.reject()
        self.overlay.set_target(target)
        self._update_lens(target)
        if target is None:
            outline_visible = self.outline.isVisible()
            self.outline.hide()
            if outline_visible:
                _restore_normal_stacking(self.outline)
            return
        if self._settings_suspended_for == target.hwnd:
            self.settings_dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.settings_dialog.show()
            self.settings_dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self._settings_suspended_for = None
        if self.active_path is not None:
            self.overlay.document_label.setText(self.active_path.name)
        if not self.overlay.collapsed:
            self._clear_highlight()
        elif self._highlight is not None:
            # 위치 표시도 창이 움직였거나 사용자 입력이 있었을 때만 PowerPoint에 다시 묻는다
            if target.rect != self._outline_window or self.activity.due("outline"):
                self._outline_window = QRect(target.rect)
                self._show_outline(target, navigate=False)

    def _set_active_path(self, path: Path | None):
        if self.active_path == path:
            return
        self.active_path = path
        if self.collection is not None and hasattr(self.collection.collector, "prioritize"):
            self.collection.collector.prioritize(path)
        self._clear_highlight()
        self.overlay.clear_report()
        self._load_current_report()

    def _load_current_report(self):
        path = self.active_path
        if path is None or not self._in_workspace(path):
            return
        if path in self._processing_paths:
            self.overlay.show_detected(path)
            return
        try:
            policy_key = build_analysis_policy(path).cache_key
            stored = build_report_repository().find_successful(path, policy_key)
            if stored is not None and _sha256(path) == stored.report.deck.source_sha256:
                self.overlay.show_report(stored.report)
        except (OSError, ValueError):
            pass

    def _in_workspace(self, path: Path) -> bool:
        if self._workspace is None:
            return False
        return accepts_path(self._workspace, path, recursive=self._recursive)

    def navigate_to_finding_slide(self, finding: Finding | None):
        report = self.overlay._report
        if finding is None or report is None or self.overlay._target is None or self.overlay._report_stale:
            return
        path = Path(report.deck.source_path)
        slide = _slide_number(finding.shape_key)
        if self.slide_navigator(path, slide):
            LOGGER.info("SLIDE_NAVIGATED | file=%s | slide=%d", path.name, slide)
        else:
            LOGGER.warning("SLIDE_NAVIGATION_FAILED | file=%s | slide=%d", path.name, slide)
            self.overlay.location_label.setText(
                "PowerPoint 기본 편집 보기에서 해당 슬라이드로 이동할 수 있습니다."
            )

    def locate_finding(self, finding: Finding | None):
        report = self.overlay._report
        target = self.overlay._target
        if finding is None or report is None or target is None or self.overlay._report_stale:
            return
        box = finding_location_box(report, finding)
        if box is None:
            self._clear_highlight()
            self.overlay.location_label.setText(
                "이 객체의 슬라이드 위치를 확인할 수 없습니다. 다시 검사해 주세요."
            )
            return
        path = Path(report.deck.source_path)
        self._highlight = (path, _slide_number(finding.shape_key), box)
        if self._show_outline(target, navigate=True):
            self.overlay.set_collapsed(True)
        else:
            self._clear_highlight()
            self.overlay.location_label.setText("저장된 문서의 기본 편집 보기에서 위치를 표시할 수 있습니다.")

    def _show_outline(self, target: PowerPointWindow, *, navigate: bool) -> bool:
        if self._highlight is None:
            return False
        path, slide, box = self._highlight
        rect = self.outline_locator(target, path, slide, box, navigate=navigate)
        if rect is None or rect.width() < 5 or rect.height() < 5:
            self.outline.hide()
            return False
        self.outline.setGeometry(rect)
        if not self.outline.isVisible():
            self.outline.show()
        _raise_over_powerpoint(self.outline, target)
        return True

    def _clear_highlight(self):
        self._highlight = None
        self._outline_window = None
        self.outline.hide()

    def request_manual(self):
        path = self.path_provider()
        self._set_active_path(path)
        if path is None:
            self.overlay.show_failure("저장된 현재 PowerPoint 문서를 확인할 수 없습니다.")
            return
        if not self.request_path(path):
            self.overlay.show_failure("설정에서 현재 문서가 포함된 작업 폴더를 지정하세요.")

    def request_path(self, path: Path) -> bool:
        if self.collection is None or not self._in_workspace(path):
            return False
        if self._split_worker is not None:
            self._split_worker.requestInterruption()
        self.collection.collector.request(path)
        LOGGER.info("MANUAL_ANALYSIS_QUEUED | file=%s", path.name)
        self.detected.emit(path)
        return True

    def _process_snapshot(self, deck):
        started = monotonic()
        analyzer = build_analyzer()
        policy = build_analysis_policy(Path(deck.source_path))
        report = analyzer.analyze_snapshot(deck, policy)
        result = analyzer.planner.last_result
        skip_reasons = {item.finding.id: item.reason for item in result.skipped} if result else {}
        skip_reasons.update(report.proposal_notes)
        analyzed = monotonic()
        build_report_repository().save(report)
        LOGGER.info(
            "ANALYSIS_SAVED | file=%s | slides=%d | findings=%d | analysis_ms=%.0f | storage_ms=%.0f",
            Path(deck.source_path).name,
            deck.slide_count,
            len(report.findings),
            (analyzed - started) * 1000,
            (monotonic() - analyzed) * 1000,
        )
        self.completed.emit(AnalysisOutcome(report, skip_reasons))

    def _on_progress(self, progress):
        if progress.stage == "yielded":
            self.file_activity.emit(progress.source_path, "검사 대기")
        if self.active_path is not None and _same_path(progress.source_path, self.active_path):
            self.overlay.show_progress(progress)

    def request_fixes(self, finding):
        report = self.overlay._report
        if finding is None or report is None or self.overlay._report_stale or self._split_worker is not None:
            return
        if finding.rule_id not in ("typography", "layout.density"):
            self.overlay.proposals_list.setFocus()
            return
        slide = _slide_number(finding.shape_key)
        policy = build_analysis_policy(Path(report.deck.source_path))
        token = (report.deck.source_path, report.deck.source_sha256, policy.cache_key, slide)
        if token in self._split_attempts:
            self.overlay._show_finding(self.overlay.findings_table.currentRow())
            return
        self.overlay._split_busy = True
        self.overlay._update_apply_button()
        self.overlay.progress_bar.setRange(0, 0)
        self.overlay.progress_bar.show()
        self.overlay.status_label.setText("분할 수정안 검증 중…")
        self.overlay.proposal_text.setText(f"{slide}번 슬라이드의 분할 수정안을 검증하고 있습니다…")
        worker = SplitProposalWorker(report, policy, slide, self)
        self._split_worker = worker
        worker.completed.connect(lambda updated: self._on_fixes_ready(updated, token))
        worker.failed.connect(self._on_fixes_failed)
        worker.finished.connect(self._fixes_finished)
        worker.start()

    def _on_fixes_ready(self, report, token):
        self._split_attempts.add(token)
        current = self.overlay._report
        if (
            current is None
            or self.overlay._report_stale
            or current.deck.source_path != report.deck.source_path
            or current.deck.source_sha256 != report.deck.source_sha256
            or current.policy_key != report.policy_key
        ):
            return
        selected = self.overlay.selected_proposal_ids()
        finding_id = self.overlay._finding.id if self.overlay._finding else None
        self.overlay.show_report(report, self.overlay._skip_reasons)
        with QSignalBlocker(self.overlay.proposals_list):
            for pid in selected:
                if pid in self.overlay.proposal_items:
                    self.overlay.proposal_items[pid].setSelected(True)
        for row, finding in enumerate(report.findings):
            if finding.id == finding_id:
                self.overlay.findings_table.setCurrentCell(row, 0)
                self.overlay._show_finding(row)
                break

    def _on_fixes_failed(self, message):
        self.overlay.proposal_text.setText(friendly_error(message))
        self.overlay.status_label.setText("분할 수정안을 준비하지 못했습니다")

    def _fixes_finished(self):
        worker = self._split_worker
        self._split_worker = None
        if worker is not None:
            worker.deleteLater()
        self.overlay._split_busy = False
        self.overlay.progress_bar.setVisible(self.overlay._analysis_busy)
        self.overlay._update_apply_button()

    def _watch_event(self, event):
        if isinstance(event, FileRemoved):
            self.file_activity.emit(event.source_path, "제거")

    def _track_file_activity(self, path: Path, status: str):
        if not self._in_workspace(path):
            return
        if status in {"검사 대기", "검사 중"}:
            self._processing_paths.add(path)
        else:
            self._processing_paths.discard(path)
        if self.active_path is not None and _same_path(path, self.active_path):
            if status in {"검사 대기", "검사 중"}:
                self._clear_highlight()
                self.overlay.show_detected(path)
                if status == "검사 대기":
                    self.overlay.status_label.setText(f"검사 대기 · {path.name}")
            elif status == "제거":
                self._clear_highlight()
                self.overlay.clear_report()

    def _on_detected(self, path: Path):
        self.file_activity.emit(path, "검사 대기")

    def _on_collection_failure(self, failure):
        message = f"{failure.stage}: {failure.error_type}: {failure.message}"
        if failure.source_path is None:
            self.failed.emit(message)
        elif self._in_workspace(failure.source_path):
            self.file_activity.emit(failure.source_path, "검사 대기" if failure.retrying else "검사 실패")
            if not failure.retrying and self.active_path == failure.source_path:
                self.failed.emit(message)

    def _on_completed(self, outcome: AnalysisOutcome | AnalysisReport):
        if isinstance(outcome, AnalysisOutcome):
            report = outcome.report
            skip_reasons = outcome.skip_reasons
        else:
            report = outcome
            skip_reasons = {}
        path = Path(report.deck.source_path)
        current = self.path_provider()
        self._set_active_path(current)
        try:
            if _sha256(path) != report.deck.source_sha256:
                if current is None or not _same_path(path, current):
                    return
                self.overlay.mark_report_stale(
                    "저장된 파일이 다시 변경되었습니다. 자동 검사를 기다리거나 검사 실행을 눌러 주세요."
                )
                return
        except OSError:
            if current is not None and _same_path(path, current):
                self.overlay.mark_report_stale(
                    "현재 파일을 확인할 수 없습니다. 파일을 저장한 뒤 검사 실행을 눌러 주세요."
                )
            return
        self.file_activity.emit(path, "검사 완료")
        if current is not None and _same_path(path, current):
            if report.policy_key is None or report.policy_key == build_analysis_policy(path).cache_key:
                self.overlay.show_report(report, skip_reasons)

    def apply_selected(self):
        proposal_ids = self.overlay.selected_proposal_ids()
        report = self._ready_to_apply("사본 적용 불가", proposal_ids)
        if report is None:
            return
        source = Path(report.deck.source_path)
        default = source.with_name(f"{source.stem}_SlideGuard{source.suffix}")
        chosen, _filter = QFileDialog.getSaveFileName(
            self.overlay,
            "수정된 사본 저장",
            str(default),
            "PowerPoint 프레젠테이션 (*.pptx)",
        )
        if not chosen:
            return
        output = Path(chosen)
        if output.suffix.lower() != ".pptx":
            output = output.with_suffix(".pptx")
        if self.overlay._report is report:
            self.apply_to_output(output, report=report, proposal_ids=proposal_ids)

    def apply_selected_to_original(self):
        """선택한 제안을 원본에 적용 (백업을 먼저 만들고 원본에 저장)."""
        from slideguard.adapters.powerpoint.patcher import default_backup_dir

        report = self._ready_to_apply("원본 적용 불가")
        if report is None:
            return
        backup_dir = default_backup_dir()
        count = len(self.overlay.selected_proposal_ids())
        answer = QMessageBox.question(
            self.overlay,
            "원본에 적용",
            f"선택한 제안 {count}건을 원본에 적용하고 저장합니다.\n"
            f"적용 전 원본은 아래 폴더에 백업합니다.\n\n{backup_dir}\n\n계속할까요?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.overlay.set_apply_busy(True)
        worker = OriginalApplyWorker(
            report,
            self.overlay.selected_proposal_ids(),
            backup_dir,
            build_analysis_policy(Path(report.deck.source_path)),
            patcher_builder=self.patcher_builder,
        )
        self._start_apply(worker, self._on_original_apply_succeeded)

    @Slot(object)
    def _on_original_apply_succeeded(self, outcome: OriginalApplyOutcome):
        if self._stopping:
            return
        self.overlay.set_apply_busy(False)
        current = self.overlay._report
        if current is not None and _same_path(Path(current.deck.source_path), outcome.original_path):
            try:
                pending = (
                    current.deck.source_sha256 == outcome.source_sha256_before
                    or _sha256(outcome.original_path) != current.deck.source_sha256
                )
            except OSError:
                pending = True
            if pending:
                self.overlay.mark_report_stale(
                    "원본에 수정이 저장되었습니다. 자동 검사를 기다리거나 검사 실행을 눌러 주세요."
                )
        self.overlay.status_label.setText(
            f"원본 적용 완료 · 성공 {len(outcome.applied_ids)}건 · 실패 {len(outcome.rejected)}건"
        )
        LOGGER.info(
            "ORIGINAL_APPLIED | file=%s | applied=%d | rejected=%d",
            outcome.original_path.name,
            len(outcome.applied_ids),
            len(outcome.rejected),
        )
        self.poll_powerpoint()
        if not self.overlay.isVisible():
            return
        lines = [f"원본: {outcome.original_path}", f"백업: {outcome.backup_path}"]
        if outcome.was_open:
            lines.append("PowerPoint에 열려 있던 문서에 적용하고 저장했습니다.")
        if outcome.rejected:
            lines.append("적용하지 못한 제안:")
            lines.extend(f"- {reason}" for _proposal_id, reason in outcome.rejected)
        if outcome.warnings:
            lines.append("경고:")
            lines.extend(f"- {warning}" for warning in outcome.warnings)
        lines.append("되돌리려면 파일을 닫은 뒤 백업 파일로 바꾸면 됩니다.")
        box = QMessageBox(
            QMessageBox.Icon.Information, "원본 적용 결과", "\n".join(lines), parent=self.overlay
        )
        open_folder = box.addButton("백업 폴더 열기", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_folder:
            try:
                os.startfile(outcome.backup_path.parent)  # type: ignore[attr-defined]  # Windows 전용
            except (AttributeError, OSError) as error:
                QMessageBox.warning(self.overlay, "폴더 열기 실패", str(error))

    def _ready_to_apply(
        self, blocked_title: str, proposal_ids: tuple[str, ...] | None = None
    ) -> AnalysisReport | None:
        """사본·원본 적용 공통 확인: 선택 충돌, 편집 중인 문서, 저장 여부, 파일 변경."""
        report = self.overlay._report
        if proposal_ids is None:
            proposal_ids = self.overlay.selected_proposal_ids()
        if report is None or not proposal_ids or self._apply_thread is not None or self.overlay._report_stale:
            return None
        source = Path(report.deck.source_path)
        if report.policy_key is not None and report.policy_key != build_analysis_policy(source).cache_key:
            self.overlay.mark_report_stale("검사 설정이 바뀌었습니다. 다시 검사해 주세요.")
            return None
        selected = [item for item in report.proposals if item.id in set(proposal_ids)]
        conflicts = _selection_conflicts(report, selected)
        if conflicts:
            QMessageBox.warning(
                self.overlay,
                "수정 제안 충돌",
                "함께 적용할 수 없는 제안이 있습니다. 선택을 정리해 주세요.\n\n" + "\n".join(conflicts),
            )
            return None

        state = self.state_provider()
        if state.path is None or not _same_path(state.path, source):
            QMessageBox.warning(
                self.overlay,
                blocked_title,
                "현재 편집 중인 문서가 검사한 문서와 다릅니다. 해당 문서를 열고 다시 시도해 주세요.",
            )
            return None
        if not state.saved:
            QMessageBox.warning(
                self.overlay,
                "저장 후 재검사 필요",
                "PowerPoint에 저장하지 않은 편집 내용이 있습니다. 저장 후 다시 검사해 주세요.",
            )
            return None
        try:
            if _sha256(source) != report.deck.source_sha256:
                raise ValueError
        except (OSError, ValueError):
            message = "원본 파일이 검사 후 변경되었습니다. 저장 후 다시 검사해 주세요."
            self.overlay.mark_report_stale(
                "저장된 파일과 검사 결과가 다릅니다. 자동 검사를 기다리거나 검사 실행을 눌러 주세요."
            )
            QMessageBox.warning(
                self.overlay,
                "저장 후 재검사 필요",
                message,
            )
            return None
        return report

    def apply_to_output(
        self,
        output: Path,
        *,
        report: AnalysisReport | None = None,
        proposal_ids: tuple[str, ...] | None = None,
    ):
        report = report or self.overlay._report
        if proposal_ids is None:
            proposal_ids = self.overlay.selected_proposal_ids()
        if report is None or not proposal_ids:
            return
        source = Path(report.deck.source_path)
        if _same_path(source, output):
            QMessageBox.warning(self.overlay, "사본 저장 불가", "원본과 다른 경로를 선택해 주세요.")
            return
        if output.exists():
            QMessageBox.warning(
                self.overlay,
                "사본 저장 불가",
                "이미 존재하는 파일입니다. 원본과 기존 사본을 보호하려면 새 이름을 선택해 주세요.",
            )
            return
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(self.overlay, "사본 저장 불가", str(error))
            return
        self.overlay.set_apply_busy(True)
        worker = CopyApplyWorker(
            report,
            proposal_ids,
            output,
            build_analysis_policy(source),
            patcher_builder=self.patcher_builder,
            preview_renderer=self.preview_renderer,
            copy_inspector=self.copy_inspector,
        )
        self._start_apply(worker, self._on_apply_succeeded)

    def _start_apply(self, worker: CopyApplyWorker | OriginalApplyWorker, on_success):
        worker.setParent(self)
        worker.succeeded.connect(on_success, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_apply_failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._apply_finished, Qt.ConnectionType.QueuedConnection)
        self._apply_thread = worker
        worker.start()

    @Slot(object)
    def _on_apply_succeeded(self, outcome: CopyApplyOutcome):
        if self._stopping:
            return
        self._last_copy_outcome = outcome
        self.overlay.set_apply_busy(False)
        self.overlay.status_label.setText(
            f"사본 적용 완료 · 성공 {len(outcome.applied_ids)}건 · 실패 {len(outcome.rejected)}건"
        )
        LOGGER.info(
            "COPY_APPLIED | file=%s | applied=%d | rejected=%d",
            outcome.output_path.name,
            len(outcome.applied_ids),
            len(outcome.rejected),
        )
        self.poll_powerpoint()
        if not self.overlay.isVisible():
            return
        lines = [
            "선택한 제안을 사본에 저장했습니다.",
            f"성공 {len(outcome.applied_ids)}건 · 적용하지 못함 {len(outcome.rejected)}건",
            "",
            str(outcome.output_path),
        ]
        if outcome.reinspection_error:
            lines.extend(("", "저장된 사본의 재검사는 완료하지 못했습니다."))
        box = QMessageBox(
            QMessageBox.Icon.Information,
            "사본 저장 완료",
            "\n".join(lines),
            parent=self.overlay,
        )
        open_copy = box.addButton("사본 열기", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_copy:
            try:
                os.startfile(outcome.output_path)
            except OSError as error:
                QMessageBox.warning(self.overlay, "사본 열기 실패", str(error))

    @Slot(str)
    def _on_apply_failed(self, message: str):
        if self._stopping:
            return
        self.overlay.set_apply_busy(False)
        LOGGER.warning("APPLY_FAILED | reason=%s", message)
        self.poll_powerpoint()
        if self.overlay.isVisible():
            QMessageBox.warning(self.overlay, "적용 실패", message)
        else:
            self.overlay.location_label.setText(message)

    @Slot()
    def _apply_finished(self):
        if self._apply_thread is not None:
            self._apply_thread.wait()
            self._apply_thread.deleteLater()
        self._apply_thread = None

    def show_settings(self, lock_shape: str | None = None, *, parent: QWidget | None = None):
        if parent is None:
            if self.overlay._target is None:
                return
            parent = self.overlay
        self._settings_suspended_for = None
        if self.settings_dialog.parentWidget() is not parent:
            self.settings_dialog.hide()
            self.settings_dialog.setParent(parent, Qt.WindowType.Dialog)
        self.settings_dialog.load(lock_shape)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    @staticmethod
    def request_exit():
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ---------------------------------------------------------- 슬라이드 렌즈

    # PowerPoint에 현재 슬라이드·위치를 묻는 최소 간격(초). 묻는 일은 작업 스레드에서 한다
    LENS_INTERVAL = 0.2

    def set_lens_enabled(self, enabled: bool):
        self.lens_enabled = bool(enabled)
        if not self.lens_enabled:
            self._hide_lens()
        elif self._lens_target is not None:
            self._lens_checked = 0.0
            self._lens_force = True
            self._update_lens(self._lens_target)

    def _lens_report(self) -> AnalysisReport | None:
        report = self.overlay._report
        if report is None or self.overlay._report_stale or self.active_path is None:
            return None
        if not _same_path(Path(report.deck.source_path), self.active_path):
            return None
        if self._live_hash is not None and report.deck.source_sha256 != self._live_hash:
            # 저장 → 재검사로 결과가 바뀜: 적용한 값은 이제 문서의 일부이므로 기억만 지운다
            self.live_editor.forget()
            self._live_hash = None
            self.lens.clear_notes()
        return report

    def _update_lens(self, target: PowerPointWindow | None):
        self._lens_target = target
        if target is None or not self.lens_enabled or self._stopping or self._lens_report() is None:
            self._hide_lens()
            self._lens_window = None
            return
        if target.monitor_rect is None or target.screen_rect is None:
            self._hide_lens()
            return
        if self._lens_probe is not None or monotonic() - self._lens_checked < self.LENS_INTERVAL:
            return
        window = (target.hwnd, QRect(target.rect))
        # 창이 바뀌었거나 움직였거나, 사용자 입력이 있었을 때만 묻는다.
        # 렌즈가 보이는 중이면 키보드·휠·제자리 클릭에 바로 반응한다 (슬라이드 넘김을 빨리 따라가도록).
        # 사용자가 직접 고치는 중(저장 안 함)이면 타자마다 묻지 않도록 입력이 멈출 때까지 기다린다.
        view = self._lens_view
        quick = view is None or view.saved or self._live_hash is not None
        if not (self._lens_force or window != self._lens_window or self.activity.due("lens", quick=quick)):
            return
        self._lens_force = False
        self._lens_window = window
        self._lens_checked = monotonic()
        probe = SlideViewProbe(self.active_path, target.hwnd, self.slide_view_reader)
        probe.completed.connect(self._on_lens_view, Qt.ConnectionType.QueuedConnection)
        probe.finished.connect(self._lens_probe_finished, Qt.ConnectionType.QueuedConnection)
        self._lens_probe = probe
        probe.start()

    def _on_lens_view(self, view: SlideView | None):
        self._lens_view = view
        self._redraw_lens()

    def _lens_probe_finished(self):
        probe, self._lens_probe = self._lens_probe, None
        if probe is not None:
            probe.wait()
            probe.deleteLater()

    def _redraw_lens(self):
        self._sync_lens_exclusions()
        target, view, report = self._lens_target, self._lens_view, self._lens_report()
        if (
            not self.lens_enabled
            or target is None
            or view is None
            or report is None
            or target.monitor_rect is None
            or target.screen_rect is None
            or not _same_path(view.path, Path(report.deck.source_path))
        ):
            self._hide_lens()
            return
        if not view.saved and self._live_hash is None:
            # 사용자가 직접 고치고 아직 저장하지 않음: 검사 결과 위치가 맞지 않을 수 있어 숨긴다
            self._hide_lens()
            return
        client = _to_qt_rect(view.client_pixels, target.monitor_rect, target.screen_rect)
        slide_rect = _to_qt_rect(view.slide_pixels, target.monitor_rect, target.screen_rect)
        if client.width() < 50 or client.height() < 50 or slide_rect.width() < 50:
            self._hide_lens()
            return
        marks = build_marks(
            report,
            view.slide,
            self.overlay.selected_proposal_ids(),
            applied_ids=self.live_editor.applied_ids,
            locate=finding_location_box,
            skip_reasons=self.overlay._skip_reasons,
        )
        self.lens.set_scene(
            client,
            slide_rect,
            view.slide_size_pt,
            marks,
            scene_key=(str(Path(report.deck.source_path).resolve()).casefold(), view.slide),
        )
        if not self.lens.isVisible():
            self.lens.show()
        _raise_over_powerpoint(self.lens, target)  # PowerPoint 바로 위, SlideGuard 패널 아래
        for handle in self.lens.handles.values():
            if handle.isVisible():
                _raise_over_powerpoint(handle, target)

    def _sync_lens_exclusions(self):
        toolbar = self.overlay
        rectangles = (toolbar.geometry(),) if toolbar.isVisible() and not toolbar.collapsed else ()
        self.lens.set_excluded_rects(rectangles)

    def _hide_lens(self):
        if self.lens.isVisible():
            self.lens.hide()
            _restore_normal_stacking(self.lens)

    # ---------------------------------------------------------- 렌즈 체크: 바로 적용 / 되돌리기

    def _panel_proposal_toggled(self, proposal_ids, checked: bool):
        """수정 후보 목록의 체크: 그 슬라이드로 이동하고, 열린 문서에도 바로 적용·해제한다."""
        report = self.overlay._report
        ids = tuple(str(item) for item in proposal_ids)
        if report is None or not ids:
            return
        first = next((item for item in report.proposals if item.id == ids[0]), None)
        if (
            checked
            and first is not None
            and self.overlay._target is not None
            and not self.overlay._report_stale
        ):
            path = Path(report.deck.source_path)
            slide = _slide_number(first.shape_key)
            if self.slide_navigator(path, slide):
                LOGGER.info("SLIDE_NAVIGATED | file=%s | slide=%d", path.name, slide)
        self.toggle_live_edit(ids, checked)

    def toggle_live_edit(self, proposal_ids, checked: bool):
        report = self._lens_report()
        ids = tuple(str(item) for item in proposal_ids)
        if report is None or not ids:
            return
        if self._live_task is not None:
            self._live_queue.append((ids, checked))  # 앞의 적용이 끝나면 이어서
            return
        key = next((item.shape_key for item in report.proposals if item.id == ids[0]), "")
        if checked:
            wanted = set(self.live_editor.applied_ids) | set(ids)
            conflicts = _selection_conflicts(report, [item for item in report.proposals if item.id in wanted])
            if conflicts:
                self.lens.set_note(
                    key, "이미 적용한 다른 수정과 함께 적용할 수 없습니다. 먼저 그 수정을 해제하세요."
                )
                return
        self.lens.set_note(key, "")
        task = LiveEditTask(self.live_editor, report, ids, checked)
        task.key = key
        task.done.connect(self._on_live_done, Qt.ConnectionType.QueuedConnection)
        task.finished.connect(self._live_task_finished, Qt.ConnectionType.QueuedConnection)
        self._live_task = task
        self.lens.set_busy(True)
        task.start()

    def _on_live_done(self, outcome):
        task = self._live_task
        if task is None:
            return
        self._live_done(outcome, task.key, task.report)

    def _live_done(self, outcome, key: str, report: AnalysisReport):
        self.lens.set_busy(False)
        if isinstance(outcome, str):
            self.lens.set_note(key, outcome)
        else:
            if outcome.rejected:
                self.lens.set_note(key, outcome.rejected[0][1])
            elif outcome.skipped:
                self.lens.set_note(key, f"직접 바꾼 값 {outcome.skipped}개는 그대로 두었습니다.")
            if self.live_editor.applied_ids:
                self._live_hash = report.deck.source_sha256
            # 렌즈·목록 어느 쪽에서 체크했든 목록 체크 상태를 실제 적용 상태에 맞춘다
            task = self._live_task
            if task is not None and outcome.applied:
                self.overlay.set_proposals_checked(outcome.applied, task.checked)
            LOGGER.info(
                "LIVE_EDIT | file=%s | applied=%s | rejected=%d | skipped=%d",
                Path(report.deck.source_path).name,
                ",".join(outcome.applied),
                len(outcome.rejected),
                outcome.skipped,
            )
        self._lens_force = True  # 바뀐 문서 상태(저장 안 됨 등)를 다시 읽는다
        self._redraw_lens()

    def _live_task_finished(self):
        task, self._live_task = self._live_task, None
        if task is not None:
            task.wait()
            task.deleteLater()
        if self._live_queue and not self._stopping:
            ids, checked = self._live_queue.pop(0)
            self.toggle_live_edit(ids, checked)

    def stop(self):
        self._stopping = True
        if self._split_worker is not None:
            self._split_worker.requestInterruption()
            self._split_worker.wait()
            self._fixes_finished()
        self.timer.stop()
        self._clear_highlight()
        self.overlay.hide()
        self.settings_dialog.hide()
        if self._result_dialog is not None:
            self._result_dialog.hide()
        if self.collection is not None:
            self.collection.stop(wait=False)
            self.collection = None
        self._apply_finished()
        self._hide_lens()
        if self._lens_probe is not None:
            self._lens_probe.wait(2000)
            self._lens_probe = None
        if self._live_task is not None:
            self._live_task.wait(5000)
            self._live_task = None


def start_overlay(app: QApplication) -> OverlayController:
    existing = getattr(app, "_slideguard_overlay_controller", None)
    if existing is not None:
        return existing
    app.setQuitOnLastWindowClosed(False)
    controller = OverlayController()
    app.aboutToQuit.connect(controller.stop)
    app._slideguard_overlay_controller = controller
    return controller


def finding_location_box(report: AnalysisReport, finding: Finding) -> Box | None:
    if finding.rule_id == "layout.density":
        return finding.box
    shape = next((item for item in report.deck.shapes if item.key == finding.shape_key), None)
    if shape is not None and shape.locator is not None and shape.locator.kind == "table_cell":
        if finding.rule_id == "layout.overlap":
            # An old intersection cannot be recovered from just one cell's box.
            return finding.box if shape.text_bounds is not None else None
        return shape.text_bounds or shape.box
    return finding.box


def comparison_text(report: AnalysisReport) -> str:
    comparison = report.comparison
    if comparison is None:
        return ""
    text = (
        f"재검사 비교 · 개선 {comparison.improved}건 · 신규 또는 악화 {comparison.new_or_worse}건"
        f" · 유지 {comparison.unchanged}건"
    )
    return text + (f" · 비교 불가 {comparison.indeterminate}건" if comparison.indeterminate else "")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _qt_screen_rect(device_name: str, monitor_rect: tuple[int, int, int, int]) -> QRect:
    screens = QApplication.screens()
    for screen in screens:
        if screen.name() == device_name:
            return screen.geometry()
    monitor_left, monitor_top, *_ = monitor_rect
    for screen in screens:
        geometry = screen.geometry()
        if geometry.left() == monitor_left and geometry.top() == monitor_top:
            return geometry
    return QApplication.primaryScreen().geometry()


def _raise_over_powerpoint(widget: QWidget, target: PowerPointWindow) -> None:
    if not target.foreground:
        if not target.topmost:
            _restore_normal_stacking(widget)
        return
    import win32con
    import win32gui

    hwnd = int(widget.winId())
    flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_NOOWNERZORDER
    try:
        if win32gui.GetForegroundWindow() != target.hwnd:
            return
        ahead = win32gui.GetWindow(target.hwnd, win32con.GW_HWNDPREV)
        if _window_precedes(hwnd, ahead, win32gui, win32con.GW_HWNDPREV):
            return
        # Insert directly above the frame. A generic HWND_TOP request can leave
        # the background tool underneath the foreground application's frame.
        win32gui.SetWindowPos(hwnd, ahead or win32con.HWND_TOP, 0, 0, 0, 0, flags)
        if not target.topmost:
            _restore_normal_stacking(widget)
        ahead = win32gui.GetWindow(target.hwnd, win32con.GW_HWNDPREV)
        if not _window_precedes(hwnd, ahead, win32gui, win32con.GW_HWNDPREV):
            # Some foreground frames can only be covered by a topmost window.
            # Limit that elevation to the time PowerPoint itself has focus.
            if win32gui.GetForegroundWindow() == target.hwnd:
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
    except win32gui.error:
        # A frame can close between the foreground poll and the z-order check.
        LOGGER.debug("POWERPOINT_Z_ORDER_CHANGED", exc_info=True)


def _window_precedes(hwnd, previous, windows, direction) -> bool:
    while previous and previous != hwnd:
        previous = windows.GetWindow(previous, direction)
    return previous == hwnd


def _restore_normal_stacking(widget: QWidget) -> None:
    if QApplication.platformName() != "windows":
        return
    import win32con
    import win32gui

    hwnd = int(widget.winId())
    try:
        if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST:
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
    except win32gui.error:
        LOGGER.debug("POWERPOINT_Z_ORDER_CHANGED", exc_info=True)


def _to_qt_rect(
    window_rect: tuple[int, int, int, int],
    monitor_rect: tuple[int, int, int, int],
    qt_screen_rect: QRect,
) -> QRect:
    left, top, right, bottom = window_rect
    monitor_left, monitor_top, monitor_right, monitor_bottom = monitor_rect
    scale_x = (monitor_right - monitor_left) / qt_screen_rect.width()
    scale_y = (monitor_bottom - monitor_top) / qt_screen_rect.height()
    logical_left = qt_screen_rect.left() + round((left - monitor_left) / scale_x)
    logical_top = qt_screen_rect.top() + round((top - monitor_top) / scale_y)
    logical_right = qt_screen_rect.left() + round((right - monitor_left) / scale_x)
    logical_bottom = qt_screen_rect.top() + round((bottom - monitor_top) / scale_y)
    return QRect(
        logical_left,
        logical_top,
        logical_right - logical_left,
        logical_bottom - logical_top,
    )


def _edit_window(application, path: Path):
    window = application.ActiveWindow
    if window.ViewType in (1, 9) and _same_path(Path(str(window.Presentation.FullName)), path):
        return window
    return None


def _goto_slide(view, slide: int) -> bool:
    if slide < 1:
        return False
    if view.Slide.SlideIndex == slide:
        return True
    view.GotoSlide(slide)
    return view.Slide.SlideIndex == slide


def _navigate_powerpoint_slide(path: Path, slide: int) -> bool:
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        application = window = view = None
        try:
            application = win32com.client.GetActiveObject("PowerPoint.Application")
            window = _edit_window(application, path)
            if window is None:
                return False
            view = window.View
            return _goto_slide(view, slide)
        finally:
            view = window = application = None
            pythoncom.CoUninitialize()
    except Exception:
        LOGGER.debug("POWERPOINT_NAVIGATION_ERROR", exc_info=True)
        return False


def _powerpoint_finding_rect(
    target: PowerPointWindow, path: Path, slide: int, box: Box, *, navigate: bool
) -> QRect | None:
    if target.monitor_rect is None or target.screen_rect is None:
        return None
    try:
        import pythoncom
        import win32com.client
        import win32gui

        pythoncom.CoInitialize()
        application = window = view = None
        try:
            application = win32com.client.GetActiveObject("PowerPoint.Application")
            window = _edit_window(application, path)
            if window is None or window.Presentation.Saved != -1:
                return None
            view = window.View
            if view.Slide.SlideIndex != slide:
                if not navigate or not _goto_slide(view, slide):
                    return None
            x1 = round(window.PointsToScreenPixelsX(box.x))
            y1 = round(window.PointsToScreenPixelsY(box.y))
            x2 = round(window.PointsToScreenPixelsX(box.x + box.width))
            y2 = round(window.PointsToScreenPixelsY(box.y + box.height))
            if x2 <= x1 or y2 <= y1:
                return None
            left, top, right, bottom = win32gui.GetClientRect(target.hwnd)
            client_left, client_top = win32gui.ClientToScreen(target.hwnd, (left, top))
            client_right, client_bottom = win32gui.ClientToScreen(target.hwnd, (right, bottom))
            pixels = (
                max(client_left, x1),
                max(client_top, y1),
                min(client_right, x2),
                min(client_bottom, y2),
            )
            return _to_qt_rect(pixels, target.monitor_rect, target.screen_rect)
        finally:
            view = window = application = None
            pythoncom.CoUninitialize()
    except Exception:
        return None


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _presentation_name(title: str) -> str:
    suffix = " - PowerPoint"
    return title[: -len(suffix)] if title.endswith(suffix) else title or "PowerPoint"


def _rule_name(rule_id: str) -> str:
    if rule_id.startswith("readability."):
        return readability_label(rule_id)
    if rule_id.startswith("layout."):
        return {
            "layout.margin": "가장자리",
            "layout.overflow": "글 넘침",
            "layout.overlap": "겹침",
            "layout.density": "정보량",
        }.get(rule_id, "배치")
    root = rule_id.partition(".")[0]
    return {
        "contrast": "명암비",
        "typography": "글자 크기",
        "layout": "배치",
        "readability": "읽기 편의",
    }.get(root, rule_id)


def _severity(value: str) -> str:
    return {"warning": "문제 발견", "review": "검토 필요", "unknown": "판정 불가"}.get(value, value)


def _value(value) -> str:
    if value is None:
        return "확인할 수 없음"
    if isinstance(value, float):
        return f"{round(value, 1):g}"
    if (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and all(isinstance(v, (int, float)) for v in value)
    ):
        return f"({float(value[0]):.0f}, {float(value[1]):.0f})"
    return str(value)


def _slide_number(shape_key: str) -> int:
    return int(str(shape_key).partition(":")[0])


def _proposal_group(proposal: ChangeProposal) -> str | None:
    identifier = str(proposal.id)
    return identifier.split("#", 1)[1] if "#" in identifier else None


def _normalized_value(value):
    if isinstance(value, dict):
        return tuple(sorted((key, _normalized_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_normalized_value(item) for item in value)
    return value


def _selection_conflicts(report: AnalysisReport, selected: list[ChangeProposal]) -> list[str]:
    messages = []
    split_slides = {p.shape_key.split(":")[0] for p in selected if p.property == "slide.split"}
    if any(p.property != "slide.split" and p.shape_key.split(":")[0] in split_slides for p in selected):
        messages.append("본문 분할안과 같은 슬라이드의 다른 수정안 중 하나를 선택해 주세요.")
    selected_ids = {item.id for item in selected}
    groups: dict[str, set[str]] = {}
    for proposal in report.proposals:
        group = _proposal_group(proposal)
        if group is not None:
            groups.setdefault(group, set()).add(proposal.id)
    for member_ids in groups.values():
        chosen = member_ids & selected_ids
        if chosen and chosen != member_ids:
            messages.append(
                "함께 적용해야 하는 수정안 일부만 선택되었습니다. 관련 제안을 모두 선택하거나 해제해 주세요."
            )

    targets: dict[tuple, list[ChangeProposal]] = {}
    for proposal in selected:
        targets.setdefault(_variant_target(proposal), []).append(proposal)
    for (shape_key, property_name, _before), proposals in targets.items():
        values = {_normalized_value(item.after) for item in proposals}
        if len(values) > 1:
            messages.append(
                f"{shape_key.partition(':')[0]}번 슬라이드의 같은 객체에 서로 다른 {_property_name(property_name)} 변경이 선택되었습니다. 하나만 선택해 주세요."
            )
    return messages


def _variant_target(proposal: ChangeProposal) -> tuple:
    """같은 대상(객체·속성·원래 값)이면 최소/권장 수정 중 하나만 고를 수 있다.

    글자 색·글씨체는 원래 값이 같은 글자에만 적용되므로 원래 값까지 대상에 넣는다
    (한 상자에 회색이 두 가지면 두 후보를 함께 고를 수 있어야 함).
    """
    before = (
        _normalized_value(proposal.before) if proposal.property in ("font.color", "font.family") else None
    )
    return (proposal.shape_key, proposal.property, before)


def _counterparts(report: AnalysisReport, proposal: ChangeProposal) -> list[ChangeProposal]:
    """proposal과 같은 대상의 다른 수정 후보 (그 묶음 전체 포함)."""
    target = _variant_target(proposal)
    own_group = _proposal_group(proposal)
    rivals = [
        item
        for item in report.proposals
        if item.id != proposal.id
        and _variant_target(item) == target
        and (own_group is None or _proposal_group(item) != own_group)
    ]
    groups = {_proposal_group(item) for item in rivals if _proposal_group(item) is not None}
    structural_rivals = [
        p
        for p in report.proposals
        if p.id != proposal.id
        and p.shape_key.split(":")[0] == proposal.shape_key.split(":")[0]
        and (p.property == "slide.split" or proposal.property == "slide.split")
    ]
    return (
        structural_rivals
        + rivals
        + [item for item in report.proposals if _proposal_group(item) in groups and item not in rivals]
    )


def _pick_variant(report: AnalysisReport, *, recommended: bool) -> list[str]:
    """대상마다 후보 하나: recommended=True면 권장 수정이 있는 대상은 권장, 나머지는 최소 수정."""
    rec_targets = {_variant_target(item) for item in report.proposals if is_recommended(item.id)}
    chosen = []
    split_slides = {p.shape_key.split(":")[0] for p in report.proposals if p.property == "slide.split"}
    for item in report.proposals:
        if item.property == "slide.split":
            if recommended:
                chosen.append(item.id)
            continue
        if recommended and item.shape_key.split(":")[0] in split_slides:
            continue
        if recommended:
            replaced = _variant_target(item) in rec_targets or _replaced_bundle(report, item, rec_targets)
            if is_recommended(item.id) or not replaced:
                chosen.append(item.id)
        elif not is_recommended(item.id):
            chosen.append(item.id)
    return chosen


def _replaced_bundle(report: AnalysisReport, item: ChangeProposal, rec_targets: set) -> bool:
    """item이 속한 최소 수정 묶음의 일부가 권장 수정으로 바뀌면 묶음 전체를 권장 쪽으로 넘긴다."""
    group = _proposal_group(item)
    if group is None:
        return False
    return any(
        _variant_target(other) in rec_targets for other in report.proposals if _proposal_group(other) == group
    )


def proposal_change_text(proposal: ChangeProposal) -> str:
    return change_text(proposal)


def _property_name(value: str) -> str:
    return {
        "font.size": "글자 크기",
        "font.color": "글자색",
        "font.family": "글꼴",
        "shape.height": "개체 높이",
        "shape.width": "개체 너비",
        "shape.position": "개체 위치",
        "shape.zorder": "앞뒤 순서",
        "shape.fill": "배경 채우기",
        "text.autofit": "자동 맞춤",
        "paragraph.line_spacing": "줄 간격",
    }.get(value, value)
