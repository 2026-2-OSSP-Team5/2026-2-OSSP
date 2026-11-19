"""슬라이드 렌즈: PowerPoint 슬라이드 위에 검사 결과를 직접 겹쳐 보여 주는 오버레이.

게임 화면 위에 카드별 통계 배지를 띄우는 오버레이처럼, 문제가 있는 개체 바로 위에
작은 배지(심각도 칩 + 측정값 + 기준)를 붙이고 개체 테두리를 칠한다.
수정 후보를 선택하면 VS Code의 변경 표시처럼 왼쪽 거터 막대와 "이전 → 이후" 값으로 바뀐다.

구성
  build_marks()      검사 결과 → 슬라이드 좌표의 표시(LensMark) 목록   (화면과 무관, 테스트 가능)
  SlideView          PowerPoint가 지금 보여 주는 슬라이드 번호와 화면 위치
  place_badges()     배지가 서로 겹치지 않게 자리 잡기                   (화면과 무관, 테스트 가능)
  paint_lens()       QPainter로 그리기 (위젯·이미지 어디에나)
  SlideLens          클릭이 통과하는 투명 창. PowerPoint 바로 위, SlideGuard 패널 아래에 뜬다
  SlideViewProbe     PowerPoint에 현재 슬라이드·위치를 묻는 작업 스레드 (UI가 멈추지 않게)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSizeF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from slideguard.domain.models import AnalysisReport, Box, ChangeProposal, Finding

# ------------------------------------------------------------------ 모양 정의

SEVERITY_ORDER = {"warning": 0, "review": 1, "unknown": 2}

# 종류별 (칩 글자, 강조색). 칩은 게임 오버레이의 등급 칩(S/A/B)처럼 한눈에 구분되게 한다.
STYLE = {
    "warning": ("!", "#F04438"),  # 문제 발견
    "review": ("?", "#F79009"),  # 검토 필요
    "unknown": ("–", "#98A2B3"),  # 판정 불가
    "change": ("Δ", "#2E90FA"),  # 선택한 수정 후보 (VS Code 변경 표시 색)
}
BADGE_BACKGROUND = QColor(18, 20, 26, 236)
BADGE_HEIGHT = 26
SWATCH = 14  # 색 변경을 보여 주는 작은 색 견본 크기
TEXT_MAIN = QColor("#F2F4F7")
TEXT_SUB = QColor("#B9BEC9")
DIFF_BEFORE = QColor("#FDA29B")
DIFF_AFTER = QColor("#75E0A7")

RULE_TITLES = {
    "typography": "글자 크기",
    "contrast": "명암비",
    "layout.overflow": "글 넘침",
    "layout.overlap": "겹침",
    "layout.margin": "여백",
    "layout.density": "정보 밀도",
}
PROPERTY_TITLES = {
    "font.size": "글자 크기",
    "font.color": "글자색",
    "font.family": "글꼴",
    "shape.height": "개체 높이",
    "shape.position": "개체 위치",
    "shape.zorder": "앞뒤 순서",
    "shape.fill": "배경 채우기",
    "text.autofit": "자동 맞춤",
    "paragraph.line_spacing": "줄 간격",
}


@dataclass(frozen=True)
class LensMark:
    """슬라이드 위 표시 하나 (개체 하나에 하나)."""

    key: str  # 개체 키 (예: "3:12")
    box: Box | None  # 슬라이드 좌표(pt). None이면 위치를 몰라 요약에만 셈
    kind: str  # warning / review / unknown / change
    title: str  # 예: "글자 크기"
    value: str  # 예: "11pt"
    target: str = ""  # 예: "최소 14pt"
    more: int = 0  # 같은 개체의 다른 문제 수
    before: str = ""  # change일 때 이전 값
    after: str = ""  # change일 때 이후 값


@dataclass(frozen=True)
class SlideView:
    """PowerPoint 편집 창이 지금 보여 주는 슬라이드와 그 화면 위치(물리 픽셀)."""

    path: Path
    slide: int
    slide_pixels: tuple[int, int, int, int]  # 슬라이드 (left, top, right, bottom)
    client_pixels: tuple[int, int, int, int]  # PowerPoint 창 안쪽 영역
    slide_size_pt: tuple[float, float]  # 슬라이드 크기 (가로, 세로)


# ------------------------------------------------------------------ 검사 결과 → 표시


def build_marks(
    report: AnalysisReport,
    slide: int,
    selected_ids: tuple[str, ...] | set[str] = (),
    *,
    locate=None,
) -> list[LensMark]:
    """slide 번호의 검사 결과를 개체별 표시로 묶는다. locate(report, finding) → Box | None."""
    locate = locate or (lambda _report, finding: finding.box)
    findings: dict[str, list[Finding]] = {}
    for finding in report.findings:
        if _slide_of(finding.shape_key) == slide:
            findings.setdefault(finding.shape_key, []).append(finding)
    selected = {str(item) for item in selected_ids}
    changes: dict[str, list[ChangeProposal]] = {}
    for proposal in report.proposals:
        if proposal.id in selected and _slide_of(proposal.shape_key) == slide:
            changes.setdefault(proposal.shape_key, []).append(proposal)

    marks = []
    for key in sorted(set(findings) | set(changes), key=_key_order):
        items = sorted(findings.get(key, []), key=lambda item: SEVERITY_ORDER.get(item.severity, 9))
        box = next((located for item in items if (located := locate(report, item)) is not None), None)
        if box is None and key in changes:
            box = _shape_box(report, key)
        if key in changes:
            first = changes[key][0]
            marks.append(
                LensMark(
                    key,
                    box,
                    "change",
                    PROPERTY_TITLES.get(first.property, first.property),
                    "",
                    more=len(changes[key]) - 1,
                    before=_short(first.before, first.property),
                    after=_short(first.after, first.property),
                )
            )
            continue
        worst = items[0]
        value, target = metric_text(worst)
        marks.append(
            LensMark(key, box, worst.severity, rule_title(worst), value, target, more=len(items) - 1)
        )
    return marks


def rule_title(finding: Finding) -> str:
    if finding.rule_id.startswith("typography.hierarchy") or finding.id.endswith(":hierarchy"):
        return "제목 크기"
    return RULE_TITLES.get(
        finding.rule_id, RULE_TITLES.get(finding.rule_id.partition(".")[0], finding.rule_id)
    )


def metric_text(finding: Finding) -> tuple[str, str]:
    """배지에 넣을 (측정값, 기준) 짧은 글. 예: ("11pt", "최소 14pt")"""
    rule, measured, expected = finding.rule_id, finding.measured, finding.expected
    number, limit = _is_number(measured), _is_number(expected)
    if finding.severity == "unknown":
        return "판정 불가", ""
    if rule.startswith("typography.hierarchy") or finding.id.endswith(":hierarchy"):
        return (f"{measured:g}pt", f"필요 {expected:g}pt" if limit else "") if number else ("작음", "")
    if rule.startswith("typography") and number:
        return f"{measured:g}pt", f"최소 {expected:g}pt" if limit else ""
    if rule.startswith("contrast") and number:
        return f"{measured:.1f}:1", f"최소 {expected:g}:1" if limit else ""
    if rule == "layout.overflow":
        return (f"{measured:g}pt" if number else "넘침"), ""
    if rule == "layout.margin":
        if number and measured < 0:
            return "슬라이드 밖", ""
        return (f"{measured:g}pt" if number else "가장자리"), (f"여백 {expected:g}pt 이상" if limit else "")
    if rule == "layout.overlap":
        return "겹침", ""
    if rule == "layout.density" and number:
        unit = "자" if finding.id.endswith(":chars") else "줄"
        return f"{measured:g}{unit}", f"최대 {expected:g}{unit}" if limit else ""
    if number and limit:
        return f"{measured:g}", f"기준 {expected:g}"
    return "확인 필요", ""


def summary_counts(marks: list[LensMark]) -> dict[str, int]:
    counts = {"warning": 0, "review": 0, "unknown": 0, "change": 0}
    for mark in marks:
        counts[mark.kind] = counts.get(mark.kind, 0) + 1
    return counts


# ------------------------------------------------------------------ 좌표 변환·배치


def map_box(box: Box, slide_rect: QRectF, slide_size_pt: tuple[float, float]) -> QRectF:
    """슬라이드 좌표(pt)를 화면 좌표로 바꾼다. 슬라이드가 화면에 slide_rect로 보인다고 할 때."""
    width_pt, height_pt = slide_size_pt
    scale_x = slide_rect.width() / width_pt
    scale_y = slide_rect.height() / height_pt
    return QRectF(
        slide_rect.left() + box.x * scale_x,
        slide_rect.top() + box.y * scale_y,
        box.width * scale_x,
        box.height * scale_y,
    )


def place_badges(frames: list[QRectF], sizes: list[QSizeF], bounds: QRectF, gap: float = 4.0) -> list[QRectF]:
    """각 개체 테두리 위쪽 가운데에 배지를 놓되, 앞서 놓은 배지와 겹치면 옆·아래로 비킨다."""
    placed: list[QRectF] = []
    for frame, size in zip(frames, sizes, strict=True):
        above = frame.top() - size.height() - gap  # 개체 바로 위 (글자를 가리지 않게)
        straddle = frame.top() - size.height() * 0.55  # 자리가 없으면 카드 위 배지처럼 테두리에 걸치게
        middle = frame.center().x() - size.width() / 2
        candidates = [
            QPointF(frame.left(), above),
            QPointF(middle, above),
            QPointF(frame.right() - size.width(), above),
            QPointF(frame.left(), straddle),
            QPointF(middle, frame.top() + gap),
            QPointF(middle, frame.bottom() + gap),
        ]
        chosen = None
        for point in candidates:
            rect = _clamp(QRectF(point, size), bounds)
            if not any(rect.adjusted(-gap, -gap, gap, gap).intersects(other) for other in placed):
                chosen = rect
                break
        if chosen is None:  # 자리가 없으면 겹치지 않을 때까지 아래로 내린다
            chosen = _clamp(QRectF(candidates[0], size), bounds)
            while any(chosen.adjusted(-gap, -gap, gap, gap).intersects(other) for other in placed):
                chosen.translate(0, size.height() + gap)
                if chosen.bottom() > bounds.bottom():
                    break
        placed.append(chosen)
    return placed


# ------------------------------------------------------------------ 그리기


def badge_font() -> QFont:
    font = QFont("Malgun Gothic")
    font.setPointSizeF(10.0)
    return font


def badge_size(mark: LensMark, font: QFont) -> QSizeF:
    bold = QFont(font)
    bold.setBold(True)
    main, sub = QFontMetricsF(bold), QFontMetricsF(font)
    width = 8 + 18 + 6  # 여백 + 칩 + 간격
    if mark.kind == "change" and _is_color(mark.before) and _is_color(mark.after):
        width += SWATCH + main.horizontalAdvance(" → ") + SWATCH
    elif mark.kind == "change":
        width += (
            main.horizontalAdvance(mark.before)
            + main.horizontalAdvance(" → ")
            + main.horizontalAdvance(mark.after)
        )
    else:
        width += main.horizontalAdvance(mark.value)
    width += 8 + sub.horizontalAdvance(_label(mark)) + 10
    if mark.more:
        width += sub.horizontalAdvance(f"+{mark.more}") + 8
    return QSizeF(width, BADGE_HEIGHT)


def paint_lens(painter: QPainter, slide_rect: QRectF, slide_size_pt, marks: list[LensMark], bounds: QRectF):
    """slide_rect 위치에 보이는 슬라이드 위로 표시를 그린다."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = badge_font()
    shown = [mark for mark in marks if mark.box is not None]
    frames = [map_box(mark.box, slide_rect, slide_size_pt) for mark in shown]
    for mark, frame in zip(shown, frames, strict=True):
        _paint_frame(painter, frame, mark)
    sizes = [badge_size(mark, font) for mark in shown]
    for mark, rect in zip(shown, place_badges(frames, sizes, bounds), strict=True):
        _paint_badge(painter, rect, mark, font)
    _paint_summary(painter, slide_rect, marks, font, bounds)


def _paint_frame(painter: QPainter, frame: QRectF, mark: LensMark):
    color = QColor(STYLE[mark.kind][1])
    fill = QColor(color)
    fill.setAlpha(26)
    path = QPainterPath()
    path.addRoundedRect(frame.adjusted(-3, -3, 3, 3), 6, 6)
    painter.fillPath(path, fill)
    pen = QPen(color, 2)
    if mark.kind == "change":
        pen.setStyle(Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.drawPath(path)
    if mark.kind == "change":  # VS Code처럼 바뀌는 줄 왼쪽에 굵은 거터 막대
        gutter = QRectF(frame.left() - 9, frame.top() - 3, 4, frame.height() + 6)
        painter.fillRect(gutter, color)


def _paint_badge(painter: QPainter, rect: QRectF, mark: LensMark, font: QFont):
    chip_text, accent = STYLE[mark.kind]
    accent = QColor(accent)
    path = QPainterPath()
    path.addRoundedRect(rect, 6, 6)
    painter.fillPath(path, BADGE_BACKGROUND)
    border = QColor(accent)
    border.setAlpha(170)
    painter.setPen(QPen(border, 1))
    painter.drawPath(path)

    bold = QFont(font)
    bold.setBold(True)
    x = rect.left() + 8
    chip = QRectF(x, rect.center().y() - 9, 18, 18)
    chip_path = QPainterPath()
    chip_path.addRoundedRect(chip, 4, 4)
    painter.fillPath(chip_path, accent)
    painter.setFont(bold)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, chip_text)
    x = chip.right() + 6

    def text(value: str, color: QColor, use: QFont, *, strike: bool = False):
        nonlocal x
        use = QFont(use)
        use.setStrikeOut(strike)
        width = QFontMetricsF(use).horizontalAdvance(value)
        painter.setFont(use)
        painter.setPen(color)
        painter.drawText(
            QRectF(x, rect.top(), width + 1, rect.height()), Qt.AlignmentFlag.AlignVCenter, value
        )
        x += width

    def swatch(value: str, edge: QColor):
        nonlocal x
        square = QRectF(x, rect.center().y() - SWATCH / 2, SWATCH, SWATCH)
        painter.fillRect(square, QColor(value))
        painter.setPen(QPen(edge, 1.5))
        painter.drawRect(square)
        x += SWATCH

    if mark.kind == "change" and _is_color(mark.before) and _is_color(mark.after):
        swatch(mark.before, DIFF_BEFORE)  # 이전 색 → 이후 색
        text(" → ", TEXT_SUB, bold)
        swatch(mark.after, DIFF_AFTER)
    elif mark.kind == "change":
        text(mark.before, DIFF_BEFORE, bold, strike=True)  # 이전 값은 빨간 취소선 (diff의 삭제 줄)
        text(" → ", TEXT_SUB, bold)
        text(mark.after, DIFF_AFTER, bold)  # 이후 값은 초록 (diff의 추가 줄)
    else:
        text(mark.value, accent.lighter(125), bold)
    x += 8
    text(_label(mark), TEXT_SUB, font)
    if mark.more:
        x += 8
        text(f"+{mark.more}", TEXT_SUB, font)


def _paint_summary(painter: QPainter, slide_rect: QRectF, marks: list[LensMark], font: QFont, bounds: QRectF):
    """슬라이드 왼쪽 위 요약 띠: SlideGuard · 문제 2 · 검토 1 · 변경 1 (위치 모르는 항목 포함)."""
    counts = summary_counts(marks)
    parts = [
        (kind, label)
        for kind, label in (
            ("warning", "문제"),
            ("review", "검토"),
            ("change", "변경"),
            ("unknown", "판정 불가"),
        )
        if counts[kind]
    ]
    if not parts:
        parts = [(None, "이 슬라이드 문제 없음")]
    bold = QFont(font)
    bold.setBold(True)
    metrics, bold_metrics = QFontMetricsF(font), QFontMetricsF(bold)
    width = 10 + bold_metrics.horizontalAdvance("SlideGuard") + 6
    for kind, label in parts:
        width += 12 + 8 + metrics.horizontalAdvance(f"{label} {counts.get(kind, '')}".strip())
    rect = _clamp(QRectF(slide_rect.left() + 8, slide_rect.top() + 8, width + 10, BADGE_HEIGHT), bounds)
    path = QPainterPath()
    path.addRoundedRect(rect, BADGE_HEIGHT / 2, BADGE_HEIGHT / 2)
    painter.fillPath(path, BADGE_BACKGROUND)
    x = rect.left() + 10
    painter.setFont(bold)
    painter.setPen(TEXT_MAIN)
    painter.drawText(QRectF(x, rect.top(), 200, rect.height()), Qt.AlignmentFlag.AlignVCenter, "SlideGuard")
    x += bold_metrics.horizontalAdvance("SlideGuard") + 6
    painter.setFont(font)
    for kind, label in parts:
        x += 6
        if kind is not None:
            painter.setBrush(QColor(STYLE[kind][1]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(x + 3, rect.center().y()), 4, 4)
        x += 12
        value = f"{label} {counts.get(kind, '')}".strip()
        painter.setPen(TEXT_MAIN if kind else TEXT_SUB)
        painter.drawText(QRectF(x, rect.top(), 300, rect.height()), Qt.AlignmentFlag.AlignVCenter, value)
        x += metrics.horizontalAdvance(value)


# ------------------------------------------------------------------ 화면 창


class SlideLens(QWidget):
    """클릭이 PowerPoint로 그대로 통과하는 투명 창."""

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
        self._slide_rect = QRectF()
        self._slide_size = (960.0, 540.0)
        self._marks: list[LensMark] = []

    @property
    def marks(self) -> list[LensMark]:
        return list(self._marks)

    def set_scene(self, geometry: QRect, slide_rect: QRect, slide_size_pt, marks: list[LensMark]):
        """geometry: 창 위치(PowerPoint 안쪽 영역), slide_rect: 그 안에서 슬라이드가 보이는 위치(화면 좌표)."""
        local = QRectF(slide_rect.translated(-geometry.topLeft()))
        if self.geometry() == geometry and local == self._slide_rect and list(marks) == self._marks:
            return  # 바뀐 게 없으면 다시 그리지 않는다
        if self.geometry() != geometry:
            self.setGeometry(geometry)
        self._slide_rect = local
        self._slide_size = slide_size_pt
        self._marks = list(marks)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        bounds = QRectF(self.rect()).intersected(self._slide_rect.adjusted(-4, -30, 4, 4))
        paint_lens(painter, self._slide_rect, self._slide_size, self._marks, bounds)


class SlideViewProbe(QThread):
    """PowerPoint에 지금 보이는 슬라이드와 위치를 묻는다 (COM은 UI 스레드 밖에서)."""

    completed = Signal(object)  # SlideView | None

    def __init__(self, path: Path, hwnd: int, reader=None):
        super().__init__()
        self.path = path
        self.hwnd = hwnd
        self.reader = reader or read_slide_view

    def run(self):
        try:
            self.completed.emit(self.reader(self.path, self.hwnd))
        except Exception:  # noqa: BLE001 - 위치를 못 읽으면 표시만 숨긴다
            self.completed.emit(None)


def read_slide_view(path: Path, hwnd: int) -> SlideView | None:
    """저장된 상태로 기본 편집 보기에 열린 path의 현재 슬라이드 위치. 알 수 없으면 None."""
    import pythoncom
    import win32com.client
    import win32gui

    pythoncom.CoInitialize()
    application = window = view = presentation = None
    try:
        application = win32com.client.GetActiveObject("PowerPoint.Application")
        for index in range(1, application.Windows.Count + 1):
            candidate = application.Windows(index)
            if _same_file(Path(candidate.Presentation.FullName), path):
                window = candidate
                break
        if window is None:
            return None
        presentation = window.Presentation
        if presentation.Saved != -1:  # 저장하지 않은 편집이 있으면 검사 결과 위치가 맞지 않을 수 있다
            return None
        view = window.View
        slide = int(view.Slide.SlideIndex)
        width, height = float(presentation.PageSetup.SlideWidth), float(presentation.PageSetup.SlideHeight)
        slide_pixels = (
            round(window.PointsToScreenPixelsX(0)),
            round(window.PointsToScreenPixelsY(0)),
            round(window.PointsToScreenPixelsX(width)),
            round(window.PointsToScreenPixelsY(height)),
        )
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        client_left, client_top = win32gui.ClientToScreen(hwnd, (left, top))
        client_right, client_bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
        return SlideView(
            path, slide, slide_pixels, (client_left, client_top, client_right, client_bottom), (width, height)
        )
    except Exception:  # noqa: BLE001 - 슬라이드 쇼·여러 슬라이드 보기 등은 표시하지 않음
        return None
    finally:
        view = window = presentation = application = None
        pythoncom.CoUninitialize()


# ------------------------------------------------------------------ 작은 도우미


def _slide_of(shape_key: str) -> int:
    try:
        return int(str(shape_key).partition(":")[0])
    except ValueError:
        return -1


def _key_order(key: str):
    return [int(part) if part.isdigit() else 0 for part in str(key).replace("/", ":").split(":")[:2]]


def _shape_box(report: AnalysisReport, key: str) -> Box | None:
    shape = next((item for item in report.deck.shapes if item.key == key), None)
    return None if shape is None else (shape.text_bounds or shape.box)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _short(value, property_name: str) -> str:
    if _is_number(value):
        return f"{value:g}pt" if property_name == "font.size" else f"{value:g}"
    if isinstance(value, str) and len(value) == 6 and all(char in "0123456789abcdefABCDEF" for char in value):
        return f"#{value.upper()}"
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return "#" + "".join(f"{int(channel):02X}" for channel in value)
    text = str(value)
    return text if len(text) <= 12 else text[:11] + "…"


def _is_color(value: str) -> bool:
    return isinstance(value, str) and len(value) == 7 and value.startswith("#")


def _label(mark: LensMark) -> str:
    return f"{mark.title} · {mark.target}" if mark.target else mark.title


def _clamp(rect: QRectF, bounds: QRectF) -> QRectF:
    rect = QRectF(rect)
    if rect.right() > bounds.right():
        rect.moveRight(bounds.right())
    if rect.left() < bounds.left():
        rect.moveLeft(bounds.left())
    if rect.bottom() > bounds.bottom():
        rect.moveBottom(bounds.bottom())
    if rect.top() < bounds.top():
        rect.moveTop(bounds.top())
    return rect


def _same_file(left: Path, right: Path) -> bool:
    import os

    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
