from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
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
    QVBoxLayout,
    QWidget,
)

from slideguard.bootstrap import (
    build_analysis_policy,
    build_analyzer,
    build_presentation_patcher,
    build_report_repository,
    build_snapshot_collection,
    build_workspace_settings_repository,
)
from slideguard.domain.models import AnalysisReport, ChangeProposal
from slideguard.domain.policy import Purpose
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
QPushButton#shield { min-width: 48px; max-width: 48px; min-height: 48px; max-height: 48px;
                     border-radius: 24px; color: #2468d8; background: #ffffff;
                     border: 2px solid #b9cdf2; font-size: 20px; }
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


class ShieldButton(QPushButton):
    """Draw a centered shield without relying on emoji font metrics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("shield")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        x = center.x() + 0.5
        y = center.y() + 0.5
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
        elif foreground not in owned_hwnds:
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
        )


def active_presentation_path() -> Path | None:
    """Return the saved active presentation path from the running PowerPoint instance."""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
            presentation = app.ActivePresentation
            full_name = str(presentation.FullName) if presentation is not None else ""
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        return None
    if not full_name:
        return None
    path = Path(full_name)
    return path.resolve() if path.exists() else None


def active_presentation_state() -> PresentationState:
    """Return active document identity and whether PowerPoint has unsaved edits."""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
            presentation = app.ActivePresentation
            full_name = str(presentation.FullName) if presentation is not None else ""
            saved = bool(presentation.Saved) if presentation is not None else False
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        return PresentationState(None, False)
    path = Path(full_name) if full_name else None
    if path is None or not path.exists():
        return PresentationState(None, saved)
    return PresentationState(path.resolve(), saved)


class _SkipInitialHashes:
    """Treat current bytes as the startup baseline; later save events still collect."""

    def last_successful_sha256(self, path: Path) -> str | None:
        try:
            return _sha256(path)
        except OSError:
            return None


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
    ):
        super().__init__()
        self.report = report
        self.proposal_ids = proposal_ids
        self.output_path = output_path
        self.policy = policy
        self.patcher_builder = patcher_builder
        self.preview_renderer = preview_renderer

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
            rejected = tuple(
                (bindings.to_team_proposal(item).id, reason) for item, reason in result.rejected
            )
            applied = [
                proposal for proposal in self.report.proposals if proposal.id in set(applied_ids)
            ]
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

            self.succeeded.emit(
                CopyApplyOutcome(
                    output_path=self.output_path,
                    selected_ids=self.proposal_ids,
                    applied_ids=applied_ids,
                    rejected=rejected,
                    warnings=tuple(result.warnings),
                    slide_changes={
                        slide: tuple(proposals) for slide, proposals in slide_changes.items()
                    },
                    original_previews=original_previews,
                    copy_previews=copy_previews,
                    preview_error=preview_error,
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
        self.change_text.setText(
            "\n".join(
                f"{_property_name(item.property)}: {_value(item.before)} → {_value(item.after)}"
                for item in changes
            )
        )

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
    exit_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SlideGuard Overlay")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setStyleSheet(OVERLAY_STYLE)
        self.collapsed = True
        self._target: PowerPointWindow | None = None
        self._report: AnalysisReport | None = None
        self._skip_reasons: dict[str, str] = {}
        self.proposal_items: dict[str, QListWidgetItem] = {}
        self._syncing_proposals = False
        self._analysis_busy = False
        self._apply_busy = False
        self._report_stale = False

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
        layout.setContentsMargins(4, 4, 4, 4)
        self.shield_button = ShieldButton()
        self.shield_button.setToolTip("SlideGuard 열기")
        self.shield_button.clicked.connect(lambda: self.set_collapsed(False))
        layout.addWidget(self.shield_button)
        self.count_badge = QLabel("", self.shield_button)
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
        proposal_layout.addWidget(self.proposals_list, 1)
        self.proposal_text = QLabel("검사 결과를 선택하면 제안 여부와 이유를 표시합니다.", objectName="muted")
        self.proposal_text.setWordWrap(True)
        proposal_layout.addWidget(self.proposal_text)
        self.apply_button = QPushButton("선택한 제안을 사본에 적용 (0)", objectName="primary")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_requested)
        proposal_layout.addWidget(self.apply_button)
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
        maximum = 58 if collapsed else 16777215
        self.setMaximumSize(maximum, maximum)
        if collapsed:
            self.setMask(QRegion(0, 0, 58, 58, QRegion.RegionType.Ellipse))
        else:
            self.clearMask()
        count = len(self._report.findings) if self._report is not None else 0
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

    def _apply_geometry(self):
        if self._target is None:
            return
        target = self._target.rect
        if self.collapsed:
            geometry = QRect(target.right() - 78, target.top() + 58, 58, 58)
            self.count_badge.setGeometry(31, 1, 17, 17)
            if self.geometry() != geometry:
                self.setGeometry(geometry)
        else:
            width = max(760, target.width() - 20)
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
        self.set_analysis_busy(True)

    def show_failure(self, message: str):
        self.status_label.setText("검사 실패")
        self.detail_title.setText("검사에 실패했습니다")
        self.detail_evidence.setText(message)
        self.set_analysis_busy(False)

    def mark_report_stale(self, message: str):
        self._report_stale = True
        self._analysis_busy = False
        self.status_label.setText("재검사 필요")
        self.proposal_text.setText(message)
        self.manual_button.setEnabled(not self._apply_busy)
        self._update_apply_button()

    def show_report(self, report: AnalysisReport, skip_reasons: dict[str, str] | None = None):
        self._report = report
        self._skip_reasons = dict(skip_reasons or {})
        self._report_stale = False
        self.set_analysis_busy(False)
        count = len(report.findings)
        self.status_label.setText(f"검사 완료 · {count}건")
        self.manual_button.setEnabled(True)
        self.count_badge.setText(str(count))
        self.count_badge.setVisible(self.collapsed and count > 0)
        self.findings_table.setRowCount(count)
        for row, finding in enumerate(report.findings):
            values = [finding.shape_key.partition(":")[0], _rule_name(finding.rule_id), _severity(finding.severity)]
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

    def _show_finding(self, row: int, *_args):
        if row < 0:
            return
        item = self.findings_table.item(row, 0)
        finding = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if finding is None:
            return
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
            self.location_label.setText(
                "슬라이드 좌표는 확인됐지만 PowerPoint 편집 화면과의 안전한 좌표 매핑이 없어 "
                "윤곽선 표시는 비활성화했습니다."
            )

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
                label = (
                    f"{slide}번 · {severity} · {_property_name(proposal.property)}\n"
                    f"{_value(proposal.before)} → {_value(proposal.after)}"
                )
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
                findings.get(item.finding_id) is not None
                and findings[item.finding_id].severity == "warning"
                for item in report.proposals
            )
        )
        self.select_review_button.setEnabled(
            any(
                findings.get(item.finding_id) is not None
                and findings[item.finding_id].severity == "review"
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
                ) or (
                    _proposal_group(proposal) is not None
                    and _proposal_group(proposal) in selected_groups
                ):
                    self.proposal_items[proposal.id].setCheckState(Qt.CheckState.Checked)
        finally:
            self._syncing_proposals = False
        self._update_apply_button()

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
        if busy:
            self.apply_button.setText("사본 적용 중…")
            self.apply_button.setEnabled(False)
        else:
            self._update_apply_button()

    def set_analysis_busy(self, busy: bool):
        self._analysis_busy = busy
        self.manual_button.setEnabled(not busy and not self._apply_busy)
        self._update_apply_button()

    def _update_apply_button(self):
        count = len(self.selected_proposal_ids())
        self.apply_button.setText(f"선택한 제안을 사본에 적용 ({count})")
        self.apply_button.setEnabled(
            count > 0
            and not self._analysis_busy
            and not self._apply_busy
            and not self._report_stale
        )


class SettingsDialog(QDialog):
    settings_saved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowTitle("SlideGuard 설정")
        self.resize(520, 450)
        self.setStyleSheet(OVERLAY_STYLE)
        layout = QVBoxLayout(self)

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

        layout.addWidget(QLabel("검사 파라미터", objectName="title"))
        purpose_row = QHBoxLayout()
        purpose_row.addWidget(QLabel("검사 목적"))
        self.purpose = QComboBox()
        self.purpose.addItem("발표용", Purpose.PRESENTATION.value)
        self.purpose.addItem("강의용", Purpose.LECTURE.value)
        self.purpose.addItem("보고서용", Purpose.REPORT.value)
        purpose_row.addWidget(self.purpose)
        purpose_row.addWidget(QLabel("단위 없음 · 기본값 발표용", objectName="muted"), 1)
        layout.addLayout(purpose_row)
        limits = QLabel(
            "현재 엔진에서 사용자 설정으로 실제 전달되는 기준은 검사 목적입니다. "
            "글자 크기·밀도 기준은 목적에 따라 달라집니다. 명암비와 안전 여백은 현재 B 규칙의 고정 기준입니다.",
            objectName="muted",
        )
        limits.setWordWrap(True)
        layout.addWidget(limits)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("저장", objectName="primary")
        save.clicked.connect(self.save)
        actions.addWidget(save)
        layout.addLayout(actions)
        self.load()

    def load(self):
        settings = build_workspace_settings_repository().load()
        if settings is None:
            return
        self.folder_edit.setText(str(settings.workspace))
        self.recursive.setChecked(settings.recursive)
        index = self.purpose.findData(settings.purpose)
        self.purpose.setCurrentIndex(max(0, index))
        self.refresh_files()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "작업 폴더 선택", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)
            self.refresh_files()

    def refresh_files(self):
        self.files.clear()
        folder = Path(self.folder_edit.text())
        if not folder.is_dir():
            return
        paths = folder.rglob("*.pptx") if self.recursive.isChecked() else folder.glob("*.pptx")
        self.files.addItems(str(path.relative_to(folder)) for path in sorted(paths))

    def save(self):
        folder = Path(self.folder_edit.text())
        if not folder.is_dir():
            QMessageBox.warning(self, "설정 저장 실패", "접근 가능한 작업 폴더를 선택하세요.")
            return
        purpose = self.purpose.currentData()
        if purpose not in {item.value for item in Purpose}:
            QMessageBox.warning(self, "설정 저장 실패", "지원하는 검사 목적을 선택하세요.")
            return
        build_workspace_settings_repository().save(folder, self.recursive.isChecked(), purpose)
        self.settings_saved.emit()
        self.accept()


class OverlayController(QObject):
    detected = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        tracker: PowerPointTracker | None = None,
        path_provider=active_presentation_path,
        state_provider=active_presentation_state,
        collection_builder=build_snapshot_collection,
        patcher_builder=build_presentation_patcher,
        preview_renderer=render_slide_previews,
    ):
        super().__init__()
        self.overlay = OverlayWidget()
        self.settings_dialog = SettingsDialog(self.overlay)
        self.tracker = tracker or PowerPointTracker()
        self.path_provider = path_provider
        self.state_provider = state_provider
        self.collection_builder = collection_builder
        self.patcher_builder = patcher_builder
        self.preview_renderer = preview_renderer
        self.collection = None
        self.active_path: Path | None = None
        self._last_powerpoint_title = ""
        self._apply_thread: QThread | None = None
        self._apply_worker: CopyApplyWorker | None = None
        self._result_dialog: ApplyResultDialog | None = None

        self.overlay.manual_requested.connect(self.request_manual)
        self.overlay.settings_requested.connect(self.show_settings)
        self.overlay.apply_requested.connect(self.apply_selected)
        self.overlay.exit_requested.connect(self.request_exit)
        self.settings_dialog.settings_saved.connect(self.restart_collection)
        self.detected.connect(self._on_detected)
        self.completed.connect(self._on_completed)
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
        settings = build_workspace_settings_repository().load()
        if settings is None or not settings.workspace.is_dir():
            return
        self.collection = self.collection_builder(
            settings.workspace,
            self._process_snapshot,
            recursive=settings.recursive,
            successful_hashes=_SkipInitialHashes(),
            on_failure=self._on_collection_failure,
            on_detected=lambda path: self.detected.emit(path),
        )
        self.collection.start()

    def poll_powerpoint(self):
        owned = {int(self.overlay.winId()), int(self.settings_dialog.winId())}
        if self._result_dialog is not None:
            owned.add(int(self._result_dialog.winId()))
        target = self.tracker.poll(owned)
        self.overlay.set_target(target)
        if target is None:
            return
        if target.title != self._last_powerpoint_title or self.active_path is None:
            self._last_powerpoint_title = target.title
            path = self.path_provider()
            self.active_path = path
            if path is not None:
                self.overlay.document_label.setText(path.name)

    def request_manual(self):
        path = self.path_provider()
        self.active_path = path
        if path is None:
            self.overlay.show_failure("저장된 현재 PowerPoint 문서를 확인할 수 없습니다.")
            return
        if self.collection is None:
            self.overlay.show_failure("설정에서 현재 문서가 포함된 작업 폴더를 지정하세요.")
            return
        self.detected.emit(path)
        self.collection.collector.request(path)

    def _process_snapshot(self, deck):
        try:
            analyzer = build_analyzer()
            policy = build_analysis_policy()
            report = AnalysisReport(deck=deck)
            for rule in analyzer.rules:
                report.findings.extend(rule.evaluate(deck, policy))
            report.executed_rules = tuple(rule.id for rule in analyzer.rules)
            report.status = "analyzed" if analyzer.rules else "not_configured"
            skip_reasons = {}
            if analyzer.planner is not None and analyzer.rules:
                report.proposals = analyzer.planner.propose(report, policy)
                result = getattr(analyzer.planner, "last_result", None)
                if result is not None:
                    skip_reasons = {
                        str(getattr(item.finding, "id", "")): item.reason
                        for item in result.skipped
                        if getattr(item.finding, "id", None)
                    }
            build_report_repository().save(report)
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")
            return
        self.completed.emit(AnalysisOutcome(report, skip_reasons))

    def _on_detected(self, path: Path):
        if self.active_path is not None and _same_path(path, self.active_path):
            self.overlay.show_detected(path)

    def _on_collection_failure(self, failure):
        if failure.source_path is None:
            return
        source_path = Path(failure.source_path)
        if self.active_path is not None and _same_path(source_path, self.active_path):
            self.failed.emit(f"{failure.stage}: {failure.error_type}: {failure.message}")

    def _on_completed(self, outcome: AnalysisOutcome | AnalysisReport):
        if isinstance(outcome, AnalysisOutcome):
            report = outcome.report
            skip_reasons = outcome.skip_reasons
        else:
            report = outcome
            skip_reasons = {}
        path = Path(report.deck.source_path)
        current = self.path_provider()
        self.active_path = current
        if current is None or not _same_path(path, current):
            self.overlay.set_analysis_busy(False)
            return
        try:
            if _sha256(current) != report.deck.source_sha256:
                self.overlay.mark_report_stale(
                    "저장된 파일이 다시 변경되었습니다. 자동 검사를 기다리거나 검사 실행을 눌러 주세요."
                )
                return
        except OSError:
            self.overlay.mark_report_stale(
                "현재 파일을 확인할 수 없습니다. 파일을 저장한 뒤 검사 실행을 눌러 주세요."
            )
            return
        self.overlay.show_report(report, skip_reasons)

    def apply_selected(self):
        report = self.overlay._report
        proposal_ids = self.overlay.selected_proposal_ids()
        if report is None or not proposal_ids:
            return
        selected = [item for item in report.proposals if item.id in set(proposal_ids)]
        conflicts = _selection_conflicts(report, selected)
        if conflicts:
            QMessageBox.warning(
                self.overlay,
                "수정 제안 충돌",
                "함께 적용할 수 없는 제안이 있습니다. 선택을 정리해 주세요.\n\n"
                + "\n".join(conflicts),
            )
            return

        state = self.state_provider()
        source = Path(report.deck.source_path)
        if state.path is None or not _same_path(state.path, source):
            QMessageBox.warning(
                self.overlay,
                "사본 적용 불가",
                "현재 편집 중인 문서가 검사한 문서와 다릅니다. 해당 문서를 열고 다시 시도해 주세요.",
            )
            return
        if not state.saved:
            QMessageBox.warning(
                self.overlay,
                "저장 후 재검사 필요",
                "PowerPoint에 저장하지 않은 편집 내용이 있습니다. 저장 후 다시 검사해 주세요.",
            )
            return
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
            return

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
        self.apply_to_output(output)

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
        output.parent.mkdir(parents=True, exist_ok=True)
        self.overlay.set_apply_busy(True)
        self._apply_thread = QThread(self)
        self._apply_worker = CopyApplyWorker(
            report,
            proposal_ids,
            output,
            build_analysis_policy(),
            patcher_builder=self.patcher_builder,
            preview_renderer=self.preview_renderer,
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
        QMessageBox.warning(self.overlay, "사본 적용 실패", message)

    def _apply_finished(self):
        if self._apply_thread is not None:
            self._apply_thread.deleteLater()
        self._apply_thread = None
        self._apply_worker = None

    def show_settings(self):
        self.settings_dialog.load()
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    @staticmethod
    def request_exit():
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def stop(self):
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


def _selection_conflicts(
    report: AnalysisReport, selected: list[ChangeProposal]
) -> list[str]:
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
