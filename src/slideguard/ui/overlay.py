from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QThread, QTimer, Signal, Slot
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
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

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
from slideguard.domain.policy import AnalysisPolicy, Purpose, Strength, ThresholdOverrides
from slideguard.planning.bindings import TeamBindings
from slideguard.ui.preview import render_slide_previews

OVERLAY_STYLE = """
QWidget { font-family: "Malgun Gothic"; color: #202938; font-size: 11px; }
QFrame#panel, QDialog { background: #ffffff; border: 1px solid #dfe5ee; }
QLabel#brand { font-size: 15px; font-weight: 700; color: #172033; }
QLabel#title { font-size: 14px; font-weight: 700; }
QLabel#muted { color: #758195; }
QLabel#status { color: #2468d8; font-weight: 700; }
QPushButton { min-height: 27px; padding: 0 10px; border: 1px solid #d7dee8;
              border-radius: 4px; background: #ffffff; }
QPushButton:hover { background: #f5f8fc; }
QPushButton:pressed { background: #dfe7f3; }
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
QCheckBox#proposal { spacing: 7px; padding: 3px; }
"""


SHIELD_SIZE = 58


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
        if hwnd is None or not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right <= left or bottom <= top:
            return None
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        monitor_info = win32api.GetMonitorInfo(monitor)
        qt_rect = _qt_screen_rect(monitor_info["Device"], monitor_info["Monitor"])
        rect = _to_qt_rect((left, top, right, bottom), monitor_info["Monitor"], qt_rect)
        return PowerPointWindow(
            hwnd=hwnd,
            rect=rect,
            title=win32gui.GetWindowText(hwnd),
            monitor_rect=monitor_info["Monitor"],
            screen_rect=qt_rect,
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
    report.compare_with(before)
    build_report_repository().save(report)
    return report


class CopyApplyWorker(QObject):
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

    @Slot()
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

            slides = tuple(sorted(slide_changes))
            original_previews: dict[int, bytes] = {}
            copy_previews: dict[int, bytes] = {}
            preview_error = None
            try:
                original_previews = self.preview_renderer(source, slides)
                copy_previews = self.preview_renderer(self.output_path, slides)
            except Exception as error:
                preview_error = f"{type(error).__name__}: {error}"

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
                    original_previews=original_previews,
                    copy_previews=copy_previews,
                    preview_error=preview_error,
                    reinspection=reinspection,
                    reinspection_error=reinspection_error,
                )
            )
        except Exception as error:
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


class OriginalApplyWorker(QObject):
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

    @Slot()
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


class OverlayWidget(QWidget):
    manual_requested = Signal()
    settings_requested = Signal()
    apply_requested = Signal()
    apply_original_requested = Signal()
    exit_requested = Signal()
    workspace_requested = Signal()
    locate_requested = Signal(object)
    lock_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SlideGuard Overlay")
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setStyleSheet(OVERLAY_STYLE)
        self.collapsed = True
        self._target: PowerPointWindow | None = None
        self._shield_offset: QPoint | None = None
        self._report: AnalysisReport | None = None
        self._skip_reasons: dict[str, str] = {}
        self.proposal_items: dict[str, QListWidgetItem] = {}
        self._syncing_proposals = False
        self._analysis_busy = False
        self._apply_busy = False
        self._report_stale = False
        self._finding: Finding | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
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
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(QLabel("🛡 SlideGuard", objectName="brand"))
        self.document_label = QLabel("PowerPoint 문서를 선택하세요", objectName="muted")
        header.addWidget(self.document_label, 1)
        self.status_label = QLabel("대기 중", objectName="status")
        header.addWidget(self.status_label)
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
        self.collapse_button = QPushButton("접기")
        self.collapse_button.clicked.connect(lambda: self.set_collapsed(True))
        header.addWidget(self.collapse_button)
        header.addSpacing(6)
        self.exit_button = QPushButton("종료", objectName="exit")
        self.exit_button.setToolTip("SlideGuard 종료")
        self.exit_button.clicked.connect(self.exit_requested)
        header.addWidget(self.exit_button)
        layout.addLayout(header)

        body = QHBoxLayout()
        self.findings_table = QTableWidget(0, 3)
        self.findings_table.setHorizontalHeaderLabels(["슬라이드", "검사 항목", "판정"])
        self.findings_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.findings_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.findings_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.findings_table.setShowGrid(False)
        self.findings_table.verticalHeader().setVisible(False)
        self.findings_table.horizontalHeader().setStretchLastSection(True)
        self.findings_table.currentCellChanged.connect(self._show_finding)
        body.addWidget(self.findings_table, 4)

        detail = QFrame(objectName="detail")
        detail_layout = QVBoxLayout(detail)
        self.detail_title = QLabel("검사 결과를 선택하세요", objectName="title")
        self.detail_title.setWordWrap(True)
        detail_layout.addWidget(self.detail_title)
        self.detail_evidence = QLabel("판정 근거가 여기에 표시됩니다.", objectName="muted")
        self.detail_evidence.setWordWrap(True)
        detail_layout.addWidget(self.detail_evidence, 1)
        self.location_label = QLabel("", objectName="muted")
        self.location_label.setWordWrap(True)
        detail_layout.addWidget(self.location_label)
        finding_actions = QHBoxLayout()
        self.locate_button = QPushButton("PPT에서 위치 보기")
        self.locate_button.clicked.connect(lambda: self.locate_requested.emit(self._finding))
        self.locate_button.setEnabled(False)
        finding_actions.addWidget(self.locate_button)
        self.lock_button = QPushButton("이 객체 고정")
        self.lock_button.clicked.connect(lambda: self.lock_requested.emit(self._finding.shape_key))
        self.lock_button.setEnabled(False)
        finding_actions.addWidget(self.lock_button)
        detail_layout.addLayout(finding_actions)
        body.addWidget(detail, 5)

        proposal = QFrame(objectName="proposal")
        proposal_layout = QVBoxLayout(proposal)
        proposal_layout.addWidget(QLabel("수정 제안", objectName="title"))
        selection_actions = QHBoxLayout()
        self.select_warning_button = QPushButton("문제 발견 선택")
        self.select_warning_button.clicked.connect(lambda: self.select_severity("warning"))
        selection_actions.addWidget(self.select_warning_button)
        self.select_review_button = QPushButton("확인 필요 선택")
        self.select_review_button.clicked.connect(lambda: self.select_severity("review"))
        selection_actions.addWidget(self.select_review_button)
        clear = QPushButton("선택 해제")
        clear.clicked.connect(self.clear_proposals)
        selection_actions.addWidget(clear)
        proposal_layout.addLayout(selection_actions)

        self.proposals_list = QListWidget()
        self.proposals_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.proposals_list.itemChanged.connect(self._proposal_item_changed)
        self.proposals_list.currentItemChanged.connect(lambda *_: self._update_apply_button())
        proposal_layout.addWidget(self.proposals_list, 1)
        self.proposal_text = QLabel("검사 결과를 선택하면 제안 여부와 이유를 표시합니다.", objectName="muted")
        self.proposal_text.setWordWrap(True)
        proposal_layout.addWidget(self.proposal_text)
        self.apply_button = QPushButton("선택한 제안을 사본에 적용 (0)", objectName="primary")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_requested)
        proposal_layout.addWidget(self.apply_button)
        direct_actions = QHBoxLayout()
        self.apply_one_button = QPushButton("이 제안만 사본 적용")
        self.apply_one_button.clicked.connect(self._apply_current_proposal)
        self.apply_one_button.setEnabled(False)
        direct_actions.addWidget(self.apply_one_button)
        self.apply_all_button = QPushButton("전체 사본 적용")
        self.apply_all_button.clicked.connect(self._apply_all_proposals)
        self.apply_all_button.setEnabled(False)
        direct_actions.addWidget(self.apply_all_button)
        proposal_layout.addLayout(direct_actions)
        self.apply_original_button = QPushButton("원본에 적용 (백업 후 저장) (0)")
        self.apply_original_button.setEnabled(False)
        self.apply_original_button.clicked.connect(self.apply_original_requested)
        proposal_layout.addWidget(self.apply_original_button)
        body.addWidget(proposal, 4)
        layout.addLayout(body, 1)
        return panel

    def set_collapsed(self, collapsed: bool):
        was_visible = self.isVisible()
        if was_visible:
            # Changing a native window mask while it is visible can leave a stale
            # backing-store strip on Windows, which looks like a second overlay.
            self.hide()
        self.collapsed = collapsed
        self.stack.setCurrentWidget(self.collapsed_page if collapsed else self.expanded_page)
        self.setMinimumSize(0, 0)
        maximum = SHIELD_SIZE if collapsed else 16777215
        self.setMaximumSize(maximum, maximum)
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

    def set_target(self, target: PowerPointWindow | None):
        self._target = target
        if target is None:
            self.hide()
            return
        self.document_label.setText(_presentation_name(target.title))
        self._apply_geometry()
        # Re-showing the always-on-top overlay on every tracker tick can put it
        # back in front of its settings dialog on Windows.
        if not self.isVisible():
            self.show()

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
            width = max(760, target.width() - 20)
            if self._target.screen_rect is not None:
                width = min(width, self._target.screen_rect.width())
            height = max(340, self.expanded_page.minimumSizeHint().height())
            geometry = QRect(target.left() + 10, target.top() + 32, width, height)
            current = self.geometry()
            if (
                current.left() != geometry.left()
                or current.top() != geometry.top()
                or current.width() != geometry.width()
                or current.height() < geometry.height()
            ):
                self.setGeometry(geometry)

    def show_detected(self, path: Path):
        self.status_label.setText(f"검사 중 · {path.name}")
        self._report_stale = True
        self.count_badge.hide()
        self.set_analysis_busy(True)

    def show_failure(self, message: str):
        self.status_label.setText("검사 실패")
        self.detail_title.setText("검사에 실패했습니다")
        self.detail_evidence.setText(message)
        self.set_analysis_busy(False)

    def mark_report_stale(self, message: str):
        self._report_stale = True
        self.count_badge.hide()
        self._analysis_busy = False
        self.status_label.setText("재검사 필요")
        self.proposal_text.setText(message)
        self.manual_button.setEnabled(not self._apply_busy)
        self._update_apply_button()

    def clear_report(self):
        self._report = self._finding = None
        self._report_stale = False
        self._skip_reasons.clear()
        self.count_badge.clear()
        self.count_badge.hide()
        self.findings_table.setRowCount(0)
        self.proposals_list.clear()
        self.proposal_items.clear()
        self.select_warning_button.setEnabled(False)
        self.select_review_button.setEnabled(False)
        self.status_label.setText("대기 중")
        self.detail_title.setText("검사 결과를 선택하세요")
        self.detail_evidence.clear()
        self.location_label.clear()
        self.proposal_text.clear()
        self.set_analysis_busy(False)

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
        self.findings_table.setRowCount(count)
        for row, finding in enumerate(report.findings):
            values = [
                finding.shape_key.partition(":")[0],
                _rule_name(finding.rule_id),
                _severity(finding.severity),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, finding)
                self.findings_table.setItem(row, column, item)
        self._populate_proposals(report)
        if count:
            self.findings_table.selectRow(0)
            self._show_finding(0, 0, -1, -1)
        else:
            self.detail_title.setText("발견된 문제가 없습니다")
            self.detail_evidence.setText("실행된 규칙에서 보고된 항목이 없습니다.")
            self.location_label.clear()
            self._update_apply_button()

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
        self.detail_title.setText(f"{slide} 슬라이드 · {finding.message}")
        self.detail_evidence.setText(
            f"측정값: {_value(finding.measured)}\n검사 기준: {_value(finding.expected)}\n\n{finding.evidence}"
        )
        matching = [item for item in self._report.proposals if item.finding_id == finding.id]
        if matching:
            self.proposal_text.setText(
                f"이 결과에 적용 가능한 제안 {len(matching)}건이 있습니다. "
                "체크박스에서 적용할 제안을 선택하세요."
            )
        else:
            reason = self._skip_reasons.get(finding.id)
            if reason is None:
                if finding.severity == "unknown":
                    reason = "판정 불가 결과는 자동 수정하지 않습니다."
                elif finding.severity == "review":
                    reason = "확인 필요 결과는 현재 자동 수정 대상이 아닙니다."
                else:
                    reason = "이 결과에 안전하게 적용할 수 있는 수정 제안이 없습니다."
            self.proposal_text.setText(reason)
        if finding.box is None:
            self.location_label.setText("화면 위치를 확정할 데이터가 없어 슬라이드 번호만 표시합니다.")
        else:
            self.location_label.setText("위치 보기로 PowerPoint의 해당 슬라이드에 윤곽선을 표시합니다.")

    def _populate_proposals(self, report: AnalysisReport):
        self.proposals_list.blockSignals(True)
        self.proposals_list.clear()
        self.proposal_items.clear()
        findings = {item.id: item for item in report.findings}
        try:
            for proposal in report.proposals:
                finding = findings.get(proposal.finding_id)
                severity = _severity(finding.severity) if finding is not None else "검사 결과"
                slide = _slide_number(proposal.shape_key)
                label = f"{slide}번 · {severity}\n{proposal_change_text(proposal)}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, proposal)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.proposals_list.addItem(item)
                self.proposal_items[proposal.id] = item
        finally:
            self.proposals_list.blockSignals(False)
        self.select_warning_button.setEnabled(
            any(
                findings.get(item.finding_id) is not None and findings[item.finding_id].severity == "warning"
                for item in report.proposals
            )
        )
        self.select_review_button.setEnabled(
            any(
                findings.get(item.finding_id) is not None and findings[item.finding_id].severity == "review"
                for item in report.proposals
            )
        )
        self._update_apply_button()

    def _proposal_item_changed(self, item: QListWidgetItem):
        proposal = item.data(Qt.ItemDataRole.UserRole)
        if proposal is not None:
            self._proposal_toggled(proposal.id)

    def _proposal_toggled(self, proposal_id: str):
        if self._syncing_proposals or self._report is None:
            return
        proposal = next((item for item in self._report.proposals if item.id == proposal_id), None)
        if proposal is None:
            return
        group = _proposal_group(proposal)
        checked = self.proposal_items[proposal_id].checkState()
        if group is not None:
            self._syncing_proposals = True
            try:
                for item in self._report.proposals:
                    if _proposal_group(item) == group:
                        self.proposal_items[item.id].setCheckState(checked)
            finally:
                self._syncing_proposals = False
        self._update_apply_button()
        self.proposals_list.viewport().update()

    def select_severity(self, severity: str):
        if self._report is None:
            return
        findings = {item.id: item for item in self._report.findings}
        self._syncing_proposals = True
        try:
            selected_groups = {
                _proposal_group(proposal)
                for proposal in self._report.proposals
                if findings.get(proposal.finding_id) is not None
                and findings[proposal.finding_id].severity == severity
                and _proposal_group(proposal) is not None
            }
            for proposal in self._report.proposals:
                if (
                    findings.get(proposal.finding_id) is not None
                    and findings[proposal.finding_id].severity == severity
                ) or (_proposal_group(proposal) is not None and _proposal_group(proposal) in selected_groups):
                    self.proposal_items[proposal.id].setCheckState(Qt.CheckState.Checked)
        finally:
            self._syncing_proposals = False
        self._update_apply_button()

    def _apply_current_proposal(self):
        current = self.proposals_list.currentItem()
        if current is not None:
            self.clear_proposals()
            current.setCheckState(Qt.CheckState.Checked)
            self.apply_requested.emit()

    def _apply_all_proposals(self):
        self.proposals_list.blockSignals(True)
        try:
            for item in self.proposal_items.values():
                item.setCheckState(Qt.CheckState.Checked)
        finally:
            self.proposals_list.blockSignals(False)
        self._update_apply_button()
        self.apply_requested.emit()

    def clear_proposals(self):
        self._syncing_proposals = True
        try:
            for item in self.proposal_items.values():
                item.setCheckState(Qt.CheckState.Unchecked)
        finally:
            self._syncing_proposals = False
        self._update_apply_button()

    def selected_proposal_ids(self) -> tuple[str, ...]:
        return tuple(
            proposal_id
            for proposal_id, item in self.proposal_items.items()
            if item.checkState() == Qt.CheckState.Checked
        )

    def set_apply_busy(self, busy: bool):
        self._apply_busy = busy
        self.manual_button.setEnabled(not busy and not self._analysis_busy)
        self._update_apply_button()

    def set_analysis_busy(self, busy: bool):
        self._analysis_busy = busy
        self.manual_button.setEnabled(not busy and not self._apply_busy)
        self._update_apply_button()

    def _update_apply_button(self):
        count = len(self.selected_proposal_ids())
        self.apply_button.setText("적용 중…" if self._apply_busy else f"선택한 제안을 사본에 적용 ({count})")
        ready = self._report is not None and not (
            self._analysis_busy or self._apply_busy or self._report_stale
        )
        self.apply_button.setEnabled(ready and count > 0)
        self.apply_original_button.setText(f"원본에 적용 (백업 후 저장) ({count})")
        self.apply_original_button.setEnabled(ready and count > 0)
        self.apply_one_button.setEnabled(ready and self.proposals_list.currentItem() is not None)
        self.apply_all_button.setEnabled(ready and bool(self.proposal_items))
        self.locate_button.setEnabled(ready and self._finding is not None and self._finding.box is not None)
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
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
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

        policy_page = QWidget()
        self.tabs = tabs
        tabs.addTab(policy_page, "검사 기준·수정 범위")
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
        self.shape_locks_edit = QLineEdit(placeholderText="예 1:2, 3:4 · 결과에서 객체를 선택해 추가")
        policy_form.addRow("고정할 객체", self.shape_locks_edit)
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
        settings = build_workspace_settings_repository().load()
        if settings is not None:
            self.folder_edit.setText(str(settings.workspace))
            self.recursive.setChecked(settings.recursive)
        policy = settings.policy_for(self.document) if settings is not None else AnalysisPolicy()
        self.purpose.setCurrentIndex(self.purpose.findData(policy.purpose.value))
        self.strength.setCurrentIndex(self.strength.findData(policy.strength.value))
        self.slides_edit.setText(", ".join(map(str, policy.slides)))
        for key, checkbox in self.property_locks.items():
            checkbox.setChecked(key in policy.locked_properties)
        for key, editor in self.threshold_fields.items():
            value = getattr(policy.thresholds, key)
            editor.setText("" if value is None else str(value))
        self.shape_locks_edit.setEnabled(self.document is not None)
        self.document_label.setText(
            f"{self.document.name}에만 적용"
            if self.document is not None
            else "저장된 PowerPoint를 열면 객체를 고정할 수 있습니다."
        )
        keys = set(policy.locked_shapes)
        if lock_shape is not None and self.document is not None:
            keys.add(lock_shape)
            self.tabs.setCurrentIndex(1)
        self.shape_locks_edit.setText(", ".join(sorted(keys)))
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
            shapes = frozenset(
                part.strip() for part in self.shape_locks_edit.text().split(",") if part.strip()
            )
            if any(re.fullmatch(r"[1-9]\d*:[1-9]\d*", key) is None for key in shapes):
                raise ValueError("고정할 객체는 슬라이드 번호:객체 ID 형식으로 입력하세요.")
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
            )
            build_workspace_settings_repository().save(
                folder.resolve(), self.recursive.isChecked(), policy=policy, document=self.document
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "설정 저장 실패", str(error))
            return
        self.settings_saved.emit()
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
            | Qt.WindowType.WindowStaysOnTopHint
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
    ):
        super().__init__()
        self.overlay = OverlayWidget()
        self.settings_dialog = SettingsDialog(self.overlay, path_provider=path_provider)
        self.outline = FindingOutline()
        self.outline_locator = outline_locator or _powerpoint_finding_rect
        self._highlight: tuple[Path, int, Box] | None = None
        self.tracker = tracker or PowerPointTracker()
        self.path_provider = path_provider
        self.state_provider = state_provider
        self.collection_builder = collection_builder
        self.patcher_builder = patcher_builder
        self.preview_renderer = preview_renderer
        self.collection = None
        self.active_path: Path | None = None
        self._last_powerpoint_window: tuple[int, str] | None = None
        self._workspace: Path | None = None
        self._recursive = False
        self._processing_paths: set[Path] = set()
        self.copy_inspector = copy_inspector
        self._apply_thread: QThread | None = None
        self._apply_worker: CopyApplyWorker | OriginalApplyWorker | None = None
        self._result_dialog: ApplyResultDialog | None = None

        self.overlay.manual_requested.connect(self.request_manual)
        self.overlay.settings_requested.connect(lambda: self.show_settings())
        self.overlay.apply_requested.connect(self.apply_selected)
        self.overlay.apply_original_requested.connect(self.apply_selected_to_original)
        self.overlay.exit_requested.connect(self.request_exit)
        self.overlay.workspace_requested.connect(self.workspace_requested)
        self.overlay.locate_requested.connect(self.locate_finding)
        self.overlay.lock_requested.connect(self.show_settings)
        self.settings_dialog.settings_saved.connect(self.restart_collection)
        self.detected.connect(self._on_detected)
        self.completed.connect(self._on_completed)
        self.collection_failed.connect(self._on_collection_failure)
        self.file_activity.connect(self._track_file_activity)
        self.failed.connect(self.overlay.show_failure)

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll_powerpoint)
        self.timer.start()
        self.restart_collection()

    def restart_collection(self):
        if self.collection is not None:
            self.collection.stop()
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
            )
            collection.start()
            self.collection = collection
            self._load_current_report()
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")

    def poll_powerpoint(self):
        owned = {int(self.overlay.winId()), int(self.settings_dialog.winId())}
        if self._result_dialog is not None:
            owned.add(int(self._result_dialog.winId()))
        target = self.tracker.poll(owned)
        self.overlay.set_target(target)
        if target is None:
            self.outline.hide()
            self._last_powerpoint_window = None
            return
        window = (target.hwnd, target.title)
        if window != self._last_powerpoint_window or self.active_path is None:
            self._last_powerpoint_window = window
            self._set_active_path(self.path_provider())
        if self.active_path is not None:
            self.overlay.document_label.setText(self.active_path.name)
        if not self.overlay.collapsed:
            self._clear_highlight()
        elif self._highlight is not None:
            self._show_outline(target, navigate=False)

    def _set_active_path(self, path: Path | None):
        if self.active_path == path:
            return
        self.active_path = path
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

    def locate_finding(self, finding: Finding | None):
        report = self.overlay._report
        target = self.overlay._target
        if (
            finding is None
            or finding.box is None
            or report is None
            or target is None
            or self.overlay._report_stale
        ):
            return
        path = Path(report.deck.source_path)
        self._highlight = (path, _slide_number(finding.shape_key), finding.box)
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
        self.outline.show()
        return True

    def _clear_highlight(self):
        self._highlight = None
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
        self.detected.emit(path)
        self.collection.collector.request(path)
        return True

    def _process_snapshot(self, deck):
        analyzer = build_analyzer()
        policy = build_analysis_policy(Path(deck.source_path))
        report = analyzer.analyze_snapshot(deck, policy)
        result = analyzer.planner.last_result
        skip_reasons = {item.finding.id: item.reason for item in result.skipped} if result else {}
        build_report_repository().save(report)
        self.completed.emit(AnalysisOutcome(report, skip_reasons))

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
        report = self._ready_to_apply("사본 적용 불가")
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
            self.apply_to_output(output)

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
        self._apply_thread = QThread(self)
        self._apply_worker = OriginalApplyWorker(
            report,
            self.overlay.selected_proposal_ids(),
            backup_dir,
            build_analysis_policy(Path(report.deck.source_path)),
            patcher_builder=self.patcher_builder,
        )
        self._apply_worker.moveToThread(self._apply_thread)
        self._apply_thread.started.connect(self._apply_worker.run)
        self._apply_worker.succeeded.connect(self._on_original_apply_succeeded)
        self._apply_worker.failed.connect(self._on_apply_failed)
        self._apply_worker.succeeded.connect(self._apply_thread.quit)
        self._apply_worker.failed.connect(self._apply_thread.quit)
        self._apply_thread.finished.connect(self._apply_worker.deleteLater)
        self._apply_thread.finished.connect(self._apply_finished)
        self._apply_thread.start()

    def _on_original_apply_succeeded(self, outcome: OriginalApplyOutcome):
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

    def _ready_to_apply(self, blocked_title: str) -> AnalysisReport | None:
        """사본·원본 적용 공통 확인: 선택 충돌, 편집 중인 문서, 저장 여부, 파일 변경."""
        report = self.overlay._report
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

    def apply_to_output(self, output: Path):
        report = self.overlay._report
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
        self._apply_thread = QThread(self)
        self._apply_worker = CopyApplyWorker(
            report,
            proposal_ids,
            output,
            build_analysis_policy(source),
            patcher_builder=self.patcher_builder,
            preview_renderer=self.preview_renderer,
            copy_inspector=self.copy_inspector,
        )
        self._apply_worker.moveToThread(self._apply_thread)
        self._apply_thread.started.connect(self._apply_worker.run)
        self._apply_worker.succeeded.connect(self._on_apply_succeeded)
        self._apply_worker.failed.connect(self._on_apply_failed)
        self._apply_worker.succeeded.connect(self._apply_thread.quit)
        self._apply_worker.failed.connect(self._apply_thread.quit)
        self._apply_thread.finished.connect(self._apply_worker.deleteLater)
        self._apply_thread.finished.connect(self._apply_finished)
        self._apply_thread.start()

    def _on_apply_succeeded(self, outcome: CopyApplyOutcome):
        self.overlay.set_apply_busy(False)
        self.overlay.status_label.setText(
            f"사본 적용 완료 · 성공 {len(outcome.applied_ids)}건 · 실패 {len(outcome.rejected)}건"
        )
        self._result_dialog = ApplyResultDialog(outcome, self.overlay)
        self._result_dialog.finished.connect(lambda: setattr(self, "_result_dialog", None))
        self._result_dialog.show()
        self._result_dialog.raise_()
        self._result_dialog.activateWindow()

    def _on_apply_failed(self, message: str):
        self.overlay.set_apply_busy(False)
        QMessageBox.warning(self.overlay, "적용 실패", message)

    def _apply_finished(self):
        if self._apply_thread is not None:
            self._apply_thread.deleteLater()
        self._apply_thread = None
        self._apply_worker = None

    def show_settings(self, lock_shape: str | None = None):
        self.settings_dialog.load(lock_shape)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    @staticmethod
    def request_exit():
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def stop(self):
        self.timer.stop()
        self._clear_highlight()
        if self.collection is not None:
            self.collection.stop()
            self.collection = None
        if self._apply_thread is not None:
            self._apply_thread.quit()
            self._apply_thread.wait()


def start_overlay(app: QApplication) -> OverlayController:
    app.setQuitOnLastWindowClosed(False)
    controller = OverlayController()
    app.aboutToQuit.connect(controller.stop)
    app._slideguard_overlay_controller = controller
    return controller


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
            window = application.ActiveWindow
            if window.ViewType not in (1, 9) or not _same_path(Path(str(window.Presentation.FullName)), path):
                return None
            if window.Presentation.Saved != -1:
                return None
            view = window.View
            if view.Slide.SlideIndex != slide:
                if not navigate:
                    return None
                view.GotoSlide(slide)
                if view.Slide.SlideIndex != slide:
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
    root = rule_id.partition(".")[0]
    return {"contrast": "명암비", "typography": "글자 크기", "layout": "배치"}.get(root, rule_id)


def _severity(value: str) -> str:
    return {"warning": "문제 발견", "review": "검토 필요", "unknown": "판정 불가"}.get(value, value)


def _value(value) -> str:
    return "확인할 수 없음" if value is None else str(value)


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
    selected_ids = {item.id for item in selected}
    groups: dict[str, set[str]] = {}
    for proposal in report.proposals:
        group = _proposal_group(proposal)
        if group is not None:
            groups.setdefault(group, set()).add(proposal.id)
    for group, member_ids in groups.items():
        chosen = member_ids & selected_ids
        if chosen and chosen != member_ids:
            messages.append(f"묶음 '{group}'의 제안을 모두 선택하거나 모두 해제해 주세요.")

    targets: dict[tuple[str, str], list[ChangeProposal]] = {}
    for proposal in selected:
        targets.setdefault((proposal.shape_key, proposal.property), []).append(proposal)
    for (shape_key, property_name), proposals in targets.items():
        values = {_normalized_value(item.after) for item in proposals}
        if len(values) > 1:
            messages.append(
                f"{shape_key}의 {_property_name(property_name)}에 서로 다른 변경값이 선택되었습니다."
            )
    return messages


def proposal_change_text(proposal: ChangeProposal) -> str:
    return f"{_property_name(proposal.property)} · {_value(proposal.before)} → {_value(proposal.after)}"


def _property_name(value: str) -> str:
    return {
        "font.size": "글자 크기",
        "font.color": "글자색",
        "font.family": "글꼴",
        "shape.height": "개체 높이",
        "shape.position": "개체 위치",
        "shape.zorder": "앞뒤 순서",
        "shape.fill": "배경 채우기",
        "text.autofit": "자동 맞춤",
        "paragraph.line_spacing": "줄 간격",
    }.get(value, value)
