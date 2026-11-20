"""슬라이드 렌즈: PowerPoint 슬라이드 위에 검사 결과를 직접 겹쳐 보여 주는 오버레이.

게임 화면 위에 카드별 통계 배지를 띄우는 오버레이처럼, 문제가 있는 개체 바로 위에
작은 배지(심각도 칩 + 측정값 + 기준)를 붙이고 개체 테두리를 칠한다.
수정 후보를 선택하면 VS Code의 변경 표시처럼 왼쪽 거터 막대와 "이전 → 이후" 값으로 바뀐다.

구성
  build_marks()      검사 결과 → 슬라이드 좌표의 표시(LensMark) 목록   (화면과 무관, 테스트 가능)
  SlideView          PowerPoint가 지금 보여 주는 슬라이드 번호와 화면 위치
  place_badges()     배지가 서로 겹치지 않게 자리 잡기                   (화면과 무관, 테스트 가능)
  paint_lens()       QPainter로 그리기 (위젯·이미지 어디에나)
  SlideLens          테두리·요약을 그리는 투명 창 (클릭이 PowerPoint로 통과)
  BadgeHandle        배지 하나짜리 작은 창. 마우스를 올리면 수정 후보 체크 목록이 펼쳐진다
                     (체크 → 열린 문서에 바로 적용, 해제 → 원래대로. 실제 적용은 live_edit.LiveEditor)
  SlideViewProbe     PowerPoint에 현재 슬라이드·위치를 묻는 작업 스레드 (UI가 멈추지 않게)
  InputActivity      사용자가 마우스·키보드를 쓴 뒤 잠깐 멈췄을 때만 PowerPoint에 묻게 하는 장치
                     (가만히 있을 때 계속 물으면 PowerPoint가 바쁜 커서를 계속 띄움)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSizeF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from slideguard.domain.models import AnalysisReport, Box, ChangeProposal, Finding

# ------------------------------------------------------------------ 모양 정의

SEVERITY_ORDER = {"warning": 0, "review": 1, "unknown": 2}
# 같은 등급이면 배지에 먼저 보여 줄 판정 (읽기에 직접 영향이 큰 것부터, 여백은 맨 뒤)
RULE_ORDER = (
    "layout.overflow",
    "contrast",
    "typography",
    "layout.density",
    "layout.overlap",
    "typography.hierarchy",
    "layout.margin",
)


def finding_rank(finding: Finding) -> tuple[int, int, int]:
    """배지에 먼저 보여 줄 순서: (상자 자체의 읽기 문제가 아닌가, 등급, 규칙 순서).

    넘침·명암비·글자 크기·겹침은 그 상자의 글을 읽기 어렵게 하는 문제라 등급과 상관없이 먼저 보여 준다.
    정보 밀도는 슬라이드 전체의 줄 수를 '가장 글이 많은 상자'에 붙여 둔 것뿐이라, 앞에 오면
    그 상자가 14줄인 것처럼 보이고 진짜 문제가 가려진다. (함정 PPT 2쪽: 연회색 글자 카드)
    """
    reading = 0 if _rule_group(finding) in READING_RULES else 1
    return reading, SEVERITY_ORDER.get(finding.severity, 9), _rule_position(finding)


# 상자의 글 자체를 읽기 어렵게 하는 판정
READING_RULES = ("layout.overflow", "contrast", "typography", "layout.overlap")


def _rule_group(finding: Finding) -> str:
    rule = finding.rule_id
    if rule.startswith("typography.hierarchy") or finding.id.endswith(":hierarchy"):
        rule = "typography.hierarchy"
    elif rule not in RULE_ORDER:
        rule = rule.partition(".")[0]
    return rule


def _rule_position(finding: Finding) -> int:
    rule = _rule_group(finding)
    return RULE_ORDER.index(rule) if rule in RULE_ORDER else len(RULE_ORDER)


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
    "shape.width": "개체 너비",
    "shape.position": "개체 위치",
    "shape.zorder": "앞뒤 순서",
    "shape.fill": "배경 채우기",
    "text.autofit": "자동 맞춤",
    "paragraph.line_spacing": "줄 간격",
}


@dataclass(frozen=True)
class LensOption:
    """배지를 펼치면 보이는 체크 항목 하나 (함께 적용해야 하는 묶음은 한 항목)."""

    key: str
    proposal_ids: tuple[str, ...]
    label: str  # 예: "글자 크기 12pt → 14pt"
    checked: bool = False  # 지금 문서에 적용되어 있는지


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
    message: str = ""  # 펼쳤을 때 보여 줄 판정 설명
    options: tuple[LensOption, ...] = ()  # 펼쳤을 때 보여 줄 수정 후보 체크 항목
    applied: bool = False  # 수정 후보가 문서에 실제로 적용되어 있음 (아니면 선택만 한 상태)
    quiet: bool = False  # 묶음 때문에 따라 움직이기만 하는 객체: 테두리만 그리고 배지는 띄우지 않음


@dataclass(frozen=True)
class SlideView:
    """PowerPoint 편집 창이 지금 보여 주는 슬라이드와 그 화면 위치(물리 픽셀)."""

    path: Path
    slide: int
    slide_pixels: tuple[int, int, int, int]  # 슬라이드 (left, top, right, bottom)
    client_pixels: tuple[int, int, int, int]  # PowerPoint 창 안쪽 영역
    slide_size_pt: tuple[float, float]  # 슬라이드 크기 (가로, 세로)
    saved: bool = True  # PowerPoint에 저장하지 않은 편집이 없는지


# ------------------------------------------------------------------ 검사 결과 → 표시


def build_marks(
    report: AnalysisReport,
    slide: int,
    selected_ids: tuple[str, ...] | set[str] = (),
    *,
    applied_ids: tuple[str, ...] | set[str] = (),
    locate=None,
) -> list[LensMark]:
    """slide 번호의 검사 결과를 개체별 표시로 묶는다. locate(report, finding) → Box | None.

    selected_ids: 패널에서 선택한 수정 후보 (점선으로 변경 예정 표시)
    applied_ids:  렌즈 체크로 문서에 이미 적용한 수정 후보 (실선으로 변경 표시)
    """
    locate = locate or (lambda _report, finding: finding.box)
    findings: dict[str, list[Finding]] = {}
    tables: set[str] = set()  # 칸 판정을 모아 표 하나로 보여 주는 표 주소
    for finding in report.findings:
        if _slide_of(finding.shape_key) == slide:
            key = _table_key(finding)
            if key != finding.shape_key:
                tables.add(key)
            findings.setdefault(key, []).append(finding)
    proposals: dict[str, list[ChangeProposal]] = {}
    for proposal in report.proposals:
        if _slide_of(proposal.shape_key) == slide:
            proposals.setdefault(proposal.shape_key, []).append(proposal)
    selected = {str(item) for item in selected_ids}
    applied = {str(item) for item in applied_ids}

    marks = []
    for key in sorted(set(findings) | set(proposals), key=_key_order):
        items = sorted(findings.get(key, []), key=finding_rank)
        if key in tables:
            # 표 칸 글자 크기 판정을 표 하나로 묶은 표시: 표 전체 테두리에 배지 하나
            box = _shape_box(report, key) or _union(
                [located for item in items if (located := locate(report, item)) is not None]
            )
        else:
            box = next((located for item in items if (located := locate(report, item)) is not None), None)
        if box is None:
            box = _shape_box(report, key)
        options = _options(report, proposals.get(key, []), applied)
        active = [item for item in proposals.get(key, []) if item.id in applied or item.id in selected]
        message = items[0].message if items else ""
        if active:
            # 위치 이동은 뒤로 (배지에는 글자 크기·상자 높이처럼 주된 변경을 보여 줌)
            active.sort(key=lambda item: item.property == "shape.position")
            first = active[0]
            follower = not items and all(
                item.property == "shape.position" and _group_spans_other_shapes(report, item)
                for item in active
            )
            marks.append(
                LensMark(
                    key,
                    box,
                    "change",
                    PROPERTY_TITLES.get(first.property, first.property),
                    "",
                    # 변경 표시 뒤에 남은 판정(여백 등)도 개수에 넣어 숨지 않게 한다 (함정 PPT 2쪽)
                    more=len(active) - 1 + len(items),
                    before=_short(first.before, first.property),
                    after=_short(first.after, first.property),
                    message=message,
                    options=options,
                    applied=any(item.id in applied for item in active),
                    quiet=follower,
                )
            )
            continue
        if not items:
            continue  # 판정 없이 후보만 남은 개체는 표시하지 않음
        worst = items[0]
        value, target = metric_text(worst)
        marks.append(
            LensMark(
                key,
                box,
                worst.severity,
                rule_title(worst),
                value,
                target,
                more=len(items) - 1,
                message=message,
                options=options,
            )
        )
    return marks


def _options(
    report: AnalysisReport, proposals: list[ChangeProposal], applied: set[str]
) -> tuple[LensOption, ...]:
    """한 개체의 수정 후보를 체크 항목으로. 같은 묶음(id의 #뒤)은 함께 켜고 끈다."""
    groups: dict[str, list[ChangeProposal]] = {}
    for proposal in proposals:
        group = str(proposal.id).split("#", 1)[1] if "#" in str(proposal.id) else str(proposal.id)
        groups.setdefault(group, [])
    for proposal in report.proposals:  # 묶음은 다른 개체의 후보까지 함께 (모두 선택하거나 모두 해제)
        group = str(proposal.id).split("#", 1)[1] if "#" in str(proposal.id) else str(proposal.id)
        if group in groups:
            groups[group].append(proposal)
    options = []
    own = {item.shape_key for item in proposals}
    for group, members in groups.items():
        label = _option_label(members, own)
        ids = tuple(item.id for item in members)
        options.append(LensOption(group, ids, label, checked=all(item in applied for item in ids)))
    return tuple(options)


def _option_label(members: list[ChangeProposal], own_keys: set[str]) -> str:
    """체크 항목 글자: 이 개체의 변경은 값까지, 묶음 때문에 함께 움직이는 다른 개체는 개수로만."""
    parts = [_change_text(item) for item in members if item.shape_key in own_keys]
    followers = [item for item in members if item.shape_key not in own_keys]
    if followers:
        moves = [item for item in followers if item.property == "shape.position"]
        if len(moves) == len(followers):
            parts.append(f"아래 객체 {len({item.shape_key for item in moves})}개 함께 내림")
        else:
            parts.append(f"다른 객체 {len({item.shape_key for item in followers})}개 함께 변경")
    label = " + ".join(parts)
    if any("~rec" in str(item.id).split("#", 1)[0] for item in members):
        label = f"[권장] {label}"
    return label


def _change_text(item: ChangeProposal) -> str:
    title = PROPERTY_TITLES.get(item.property, item.property)
    if item.property == "shape.position" and _is_point(item.before) and _is_point(item.after):
        return f"{title} {_move_text(item.before, item.after)}"
    return f"{title} {_short(item.before, item.property)} → {_short(item.after, item.property)}"


def _move_text(before, after) -> str:
    """(왼쪽, 위) 좌표 변화를 '아래로 12pt' 같은 말로."""
    dx, dy = float(after[0]) - float(before[0]), float(after[1]) - float(before[1])
    words = []
    if abs(dy) >= 0.5:
        words.append(f"{'아래로' if dy > 0 else '위로'} {abs(dy):.0f}pt")
    if abs(dx) >= 0.5:
        words.append(f"{'오른쪽으로' if dx > 0 else '왼쪽으로'} {abs(dx):.0f}pt")
    return " ".join(words) or "그대로"


def _is_point(value) -> bool:
    return isinstance(value, (tuple, list)) and len(value) == 2 and all(_is_number(v) for v in value)


def _group_spans_other_shapes(report: AnalysisReport, proposal: ChangeProposal) -> bool:
    if "#" not in str(proposal.id):
        return False
    group = str(proposal.id).split("#", 1)[1]
    return any(
        item.shape_key != proposal.shape_key
        and "#" in str(item.id)
        and str(item.id).split("#", 1)[1] == group
        for item in report.proposals
    )


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
        # 슬라이드 전체 값임을 밝힌다 (이 상자 하나가 14줄인 것으로 오해하지 않게)
        return f"슬라이드 {measured:g}{unit}", f"최대 {expected:g}{unit}" if limit else ""
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


def paint_lens(
    painter: QPainter,
    slide_rect: QRectF,
    slide_size_pt,
    marks: list[LensMark],
    bounds: QRectF,
    *,
    badges: bool = True,
):
    """slide_rect 위치에 보이는 슬라이드 위로 표시를 그린다. badges=False면 테두리·요약만."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = badge_font()
    shown = [mark for mark in marks if mark.box is not None]
    frames = [map_box(mark.box, slide_rect, slide_size_pt) for mark in shown]
    for mark, frame in zip(shown, frames, strict=True):
        _paint_frame(painter, frame, mark)
    if badges:
        loud = [(mark, frame) for mark, frame in zip(shown, frames, strict=True) if not mark.quiet]
        sizes = [badge_size(mark, font) for mark, _frame in loud]
        placed = place_badges([frame for _mark, frame in loud], sizes, bounds)
        for (mark, _frame), rect in zip(loud, placed, strict=True):
            _paint_badge(painter, rect, mark, font)
    _paint_summary(painter, slide_rect, marks, font, bounds)


def badge_rects(
    slide_rect: QRectF, slide_size_pt, marks: list[LensMark], bounds: QRectF
) -> list[tuple[LensMark, QRectF]]:
    """배지를 그릴 자리 (그리기와 손잡이 창이 같은 자리를 쓰도록)."""
    font = badge_font()
    shown = [mark for mark in marks if mark.box is not None and not mark.quiet]
    frames = [map_box(mark.box, slide_rect, slide_size_pt) for mark in shown]
    sizes = [badge_size(mark, font) for mark in shown]
    return list(zip(shown, place_badges(frames, sizes, bounds), strict=True))


def _paint_frame(painter: QPainter, frame: QRectF, mark: LensMark):
    color = QColor(STYLE[mark.kind][1])
    fill = QColor(color)
    fill.setAlpha(26)
    path = QPainterPath()
    path.addRoundedRect(frame.adjusted(-3, -3, 3, 3), 6, 6)
    painter.fillPath(path, fill)
    pen = QPen(color, 2)
    if mark.kind == "change" and not mark.applied:  # 선택만 한 후보는 점선, 적용된 후보는 실선
        pen.setStyle(Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.drawPath(path)
    if mark.kind == "change":  # VS Code처럼 바뀌는 줄 왼쪽에 굵은 거터 막대
        gutter = QRectF(frame.left() - 9, frame.top() - 3, 4, frame.height() + 6)
        painter.fillRect(gutter, color)


def _paint_badge(painter: QPainter, rect: QRectF, mark: LensMark, font: QFont, *, framed: bool = True):
    chip_text, accent = STYLE[mark.kind]
    accent = QColor(accent)
    if framed:
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
    """테두리·요약을 그리는 투명 창 (클릭은 PowerPoint로 통과).

    interactive=True면 배지는 BadgeHandle 창들이 따로 그리고, 마우스를 올리면 체크 목록이 펼쳐진다.
    체크를 바꾸면 option_toggled(제안 ID 묶음, 켜기 여부)를 보낸다.
    """

    option_toggled = Signal(object, bool)

    def __init__(self, *, interactive: bool = True):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.interactive = interactive
        self._slide_rect = QRectF()
        self._slide_size = (960.0, 540.0)
        self._marks: list[LensMark] = []
        self._handles: dict[str, BadgeHandle] = {}
        self._busy = False
        self._notes: dict[str, str] = {}

    @property
    def marks(self) -> list[LensMark]:
        return list(self._marks)

    @property
    def handles(self) -> dict[str, BadgeHandle]:
        return dict(self._handles)

    def window_ids(self) -> set[int]:
        """SlideGuard가 띄운 창 목록 (PowerPoint 앞 창 판정에서 우리 창으로 취급)."""
        ids = {int(self.winId())} if self.isVisible() else set()
        ids.update(int(handle.winId()) for handle in self._handles.values() if handle.isVisible())
        return ids

    def set_scene(self, geometry: QRect, slide_rect: QRect, slide_size_pt, marks: list[LensMark]):
        """geometry: 창 위치(PowerPoint 안쪽 영역), slide_rect: 그 안에서 슬라이드가 보이는 위치(화면 좌표)."""
        local = QRectF(slide_rect.translated(-geometry.topLeft()))
        if (
            self.geometry() == geometry
            and local == self._slide_rect
            and slide_size_pt == self._slide_size
            and list(marks) == self._marks
        ):
            return  # 바뀐 게 없으면 다시 그리지 않는다
        if self.geometry() != geometry:
            self.setGeometry(geometry)
        self._slide_rect = local
        self._slide_size = slide_size_pt
        self._marks = list(marks)
        self._place_handles(geometry)
        self.update()

    def set_busy(self, busy: bool):
        self._busy = bool(busy)
        for handle in self._handles.values():
            handle.set_busy(self._busy)

    def set_note(self, key: str, text: str):
        """한 배지에 짧은 안내(적용하지 못한 이유 등)를 붙인다. 빈 문자열이면 지운다."""
        if text:
            self._notes[key] = text
        else:
            self._notes.pop(key, None)
        if key in self._handles:
            self._handles[key].set_note(text)

    def clear_notes(self):
        self._notes.clear()
        for handle in self._handles.values():
            handle.set_note("")

    def _place_handles(self, geometry: QRect):
        if not self.interactive:
            return
        bounds = QRectF(QRectF(0, 0, geometry.width(), geometry.height())).intersected(
            self._slide_rect.adjusted(-4, -30, 4, 4)
        )
        wanted = badge_rects(self._slide_rect, self._slide_size, self._marks, bounds)
        keep = set()
        for mark, rect in wanted:
            keep.add(mark.key)
            handle = self._handles.get(mark.key)
            if handle is None:
                handle = BadgeHandle(self)
                handle.toggled.connect(self.option_toggled)
                self._handles[mark.key] = handle
            handle.set_mark(mark, rect.toAlignedRect().translated(geometry.topLeft()))
            handle.set_busy(self._busy)
            handle.set_note(self._notes.get(mark.key, ""))
            if self.isVisible() and not handle.isVisible():
                handle.show()
        for key in set(self._handles) - keep:
            handle = self._handles.pop(key)
            handle.hide()
            handle.deleteLater()

    def showEvent(self, event):
        super().showEvent(event)
        for handle in self._handles.values():
            handle.show()

    def hideEvent(self, event):
        super().hideEvent(event)
        for handle in self._handles.values():
            handle.hide()

    def close(self):
        for handle in self._handles.values():
            handle.close()
        return super().close()

    def paintEvent(self, _event):
        painter = QPainter(self)
        bounds = QRectF(self.rect()).intersected(self._slide_rect.adjusted(-4, -30, 4, 4))
        paint_lens(
            painter, self._slide_rect, self._slide_size, self._marks, bounds, badges=not self.interactive
        )


CARD_WIDTH = 320
ROW_HEIGHT = 28
CHECK_SIZE = 14
CHECK_ON = QColor("#2E90FA")


class BadgeHandle(QWidget):
    """배지 하나짜리 작은 창. 마우스를 올리면 판정 설명과 수정 후보 체크 목록이 펼쳐진다.

    포커스를 가져가지 않으므로(클릭해도 PowerPoint가 앞 창으로 남음) 편집 흐름을 끊지 않는다.
    """

    toggled = Signal(object, bool)  # (제안 ID 묶음, 켜기 여부)

    def __init__(self, owner: QWidget | None = None):
        super().__init__(
            owner,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.mark: LensMark | None = None
        self.anchor = QRect()  # 접혀 있을 때 배지 자리 (화면 좌표)
        self.expanded = False
        self.busy = False
        self.note = ""
        self._hover_row = -1
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(350)
        self._collapse_timer.timeout.connect(self.collapse)

    # ------------------------------------------------------------ 상태
    def set_mark(self, mark: LensMark, anchor: QRect):
        changed = mark != self.mark or anchor != self.anchor
        self.mark, self.anchor = mark, QRect(anchor)
        if changed:
            self._apply_geometry()
            self.update()

    def set_busy(self, busy: bool):
        if busy != self.busy:
            self.busy = busy
            self._apply_geometry()
            self.update()

    def set_note(self, text: str):
        if text != self.note:
            self.note = text
            self._apply_geometry()
            self.update()

    def expand(self):
        self._collapse_timer.stop()
        if not self.expanded:
            self.expanded = True
            self._apply_geometry()
            self.update()

    def collapse(self):
        if self.expanded and not self.busy:
            self.expanded = False
            self._hover_row = -1
            self._apply_geometry()
            self.update()

    # ------------------------------------------------------------ 배치
    def _layout(self) -> dict:
        """펼친 카드 안의 자리들 (그리기와 클릭 판정이 같은 값을 쓴다)."""
        font = badge_font()
        small = QFont(font)
        small.setPointSizeF(9.0)
        width = max(float(self.anchor.width()), CARD_WIDTH)
        inner = width - 24
        y = BADGE_HEIGHT + 8.0
        message = None
        if self.mark is not None and self.mark.message:
            lines = min(3, max(1, _wrapped_lines(self.mark.message, font, inner)))
            message = QRectF(12, y, inner, lines * QFontMetricsF(font).lineSpacing())
            y = message.bottom() + 8
        rows = []
        options = self.mark.options if self.mark is not None else ()
        for _option in options:
            rows.append(QRectF(6, y, width - 12, ROW_HEIGHT))
            y += ROW_HEIGHT
        empty = None
        if not options:
            empty = QRectF(12, y, inner, ROW_HEIGHT)
            y += ROW_HEIGHT
        note = None
        if self.busy or self.note:
            note = QRectF(12, y + 2, inner, QFontMetricsF(small).lineSpacing() * 2)
            y = note.bottom()
        hint = QRectF(12, y + 6, inner, QFontMetricsF(small).lineSpacing())
        return {
            "size": QSizeF(width, hint.bottom() + 10),
            "message": message,
            "rows": rows,
            "empty": empty,
            "note": note,
            "hint": hint,
            "font": font,
            "small": small,
        }

    def _apply_geometry(self):
        if self.anchor.isEmpty():
            return
        if not self.expanded:
            self.setGeometry(self.anchor)
            return
        size = self._layout()["size"].toSize()
        rect = QRect(self.anchor.topLeft(), size)
        screen = QGuiApplication.screenAt(self.anchor.center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            if rect.bottom() > area.bottom():  # 아래가 모자라면 배지 아래쪽을 기준으로 위로 펼친다
                rect.moveBottom(self.anchor.bottom())
            if rect.right() > area.right():
                rect.moveRight(area.right())
            if rect.top() < area.top():
                rect.moveTop(area.top())
            if rect.left() < area.left():
                rect.moveLeft(area.left())
        self.setGeometry(rect)
        # 펼친 카드는 다른 배지 창보다 앞으로 (옆 배지가 체크 항목을 덮어 누를 수 없던 문제)
        self.raise_()

    # ------------------------------------------------------------ 마우스
    def enterEvent(self, event):
        self.expand()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_row = -1
        self._collapse_timer.start()
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        row = self._row_at(event.position()) if self.expanded else -1
        if row != self._hover_row:
            self._hover_row = row
            self.setCursor(Qt.CursorShape.PointingHandCursor if row >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self.expanded:
            self.expand()
            return
        row = self._row_at(event.position())
        if row < 0 or self.busy or self.mark is None:
            return
        option = self.mark.options[row]
        self.toggled.emit(option.proposal_ids, not option.checked)

    def _row_at(self, point: QPointF) -> int:
        if not self.expanded:
            return -1
        for index, rect in enumerate(self._layout()["rows"]):
            if rect.contains(point):
                return index
        return -1

    # ------------------------------------------------------------ 그리기
    def paintEvent(self, _event):
        if self.mark is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = badge_font()
        badge = QRectF(0, 0, self.anchor.width(), self.anchor.height())
        if not self.expanded:
            _paint_badge(painter, badge, self.mark, font)
            return
        layout = self._layout()
        card = QRectF(0.5, 0.5, layout["size"].width() - 1, layout["size"].height() - 1)
        accent = QColor(STYLE[self.mark.kind][1])
        path = QPainterPath()
        path.addRoundedRect(card, 8, 8)
        painter.fillPath(path, QColor(18, 20, 26, 246))
        painter.setPen(QPen(accent, 1.2))
        painter.drawPath(path)
        _paint_badge(painter, badge, self.mark, font, framed=False)
        divider = QColor(accent)
        divider.setAlpha(70)
        painter.setPen(QPen(divider, 1))
        painter.drawLine(QPointF(8, BADGE_HEIGHT + 2), QPointF(card.right() - 8, BADGE_HEIGHT + 2))

        if layout["message"] is not None:
            painter.setFont(font)
            painter.setPen(TEXT_SUB)
            painter.drawText(
                layout["message"],
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                self.mark.message,
            )
        metrics = QFontMetricsF(font)
        for index, (option, rect) in enumerate(zip(self.mark.options, layout["rows"], strict=True)):
            if index == self._hover_row and not self.busy:
                hover = QPainterPath()
                hover.addRoundedRect(rect, 5, 5)
                painter.fillPath(hover, QColor(255, 255, 255, 22))
            box = QRectF(rect.left() + 8, rect.center().y() - CHECK_SIZE / 2, CHECK_SIZE, CHECK_SIZE)
            _paint_check(painter, box, option.checked, dimmed=self.busy)
            label = metrics.elidedText(option.label, Qt.TextElideMode.ElideRight, rect.width() - 40)
            painter.setFont(font)
            painter.setPen(TEXT_MAIN if not self.busy else TEXT_SUB)
            painter.drawText(
                QRectF(box.right() + 10, rect.top(), rect.width() - 40, rect.height()),
                Qt.AlignmentFlag.AlignVCenter,
                label,
            )
        small = layout["small"]
        painter.setFont(small)
        if layout["empty"] is not None:
            painter.setPen(TEXT_SUB)
            painter.drawText(layout["empty"], Qt.AlignmentFlag.AlignVCenter, "자동 수정 후보가 없습니다.")
        if layout["note"] is not None:
            painter.setPen(TEXT_SUB if self.busy else DIFF_BEFORE)
            painter.drawText(
                layout["note"],
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                "PowerPoint에 적용하는 중…" if self.busy else self.note,
            )
        painter.setPen(QColor("#8A90A0"))
        painter.drawText(
            layout["hint"],
            Qt.AlignmentFlag.AlignVCenter,
            "체크하면 바로 적용 · 해제하면 원래대로 · 유지하려면 저장",
        )


def _paint_check(painter: QPainter, box: QRectF, checked: bool, *, dimmed: bool = False):
    path = QPainterPath()
    path.addRoundedRect(box, 3, 3)
    color = QColor(CHECK_ON)
    if dimmed:
        color.setAlpha(120)
    if checked:
        painter.fillPath(path, color)
        pen = QPen(QColor("#FFFFFF"), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawPolyline(
            [
                QPointF(box.left() + 3, box.center().y()),
                QPointF(box.left() + 6, box.bottom() - 3.5),
                QPointF(box.right() - 3, box.top() + 3.5),
            ]
        )
    else:
        painter.setPen(QPen(QColor("#98A2B3"), 1.4))
        painter.drawPath(path)


def _wrapped_lines(text: str, font: QFont, width: float) -> int:
    metrics = QFontMetricsF(font)
    rect = metrics.boundingRect(QRectF(0, 0, width, 10_000), int(Qt.TextFlag.TextWordWrap), text)
    return max(1, round(rect.height() / metrics.lineSpacing()))


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
    """기본 편집 보기에 열린 path의 현재 슬라이드 위치. 알 수 없으면 None.

    저장하지 않은 편집이 있으면 saved=False (검사 결과 위치가 맞지 않을 수 있으므로 화면에서 판단).
    """
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
        saved = presentation.Saved == -1
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
            path,
            slide,
            slide_pixels,
            (client_left, client_top, client_right, client_bottom),
            (width, height),
            saved,
        )
    except Exception:  # noqa: BLE001 - 슬라이드 쇼·여러 슬라이드 보기 등은 표시하지 않음
        return None
    finally:
        view = window = presentation = application = None
        pythoncom.CoUninitialize()


# ------------------------------------------------------------------ 입력이 있을 때만 묻기


class InputActivity:
    """PowerPoint에 COM으로 묻는 일을 "사용자가 무언가 한 뒤"로 줄인다.

    다른 프로그램이 COM으로 계속 물으면 PowerPoint는 그때마다 바쁜 커서(돌아가는 커서)를 띄운다.
    슬라이드·선택·저장 상태는 사용자가 마우스·키보드를 써야만 바뀌므로 입력이 있을 때만 묻는다.

    due(이름)              입력 뒤 settle초 동안 조용해졌을 때 한 번 True (입력 중에는 묻지 않음)
    due(이름, quick=True)  마우스 커서가 그대로인 입력(PageDown·화살표·휠·제자리 클릭)이면
                           기다리지 않고 바로 True. 슬라이드를 넘기는 동작이 대부분 여기에 해당해
                           넘기자마자 렌즈가 바뀐다. 커서가 움직이는 중이면 settle만큼 기다린다.
    reader                 (마지막 입력 시각 표식, 입력 뒤 지난 초) 또는 None. None이면(Windows 아님) 항상 True
    cursor                 마우스 커서 위치 또는 None
    """

    SETTLE = 0.2

    def __init__(self, reader=None, settle: float = SETTLE, cursor=None):
        self.reader = reader or last_input
        self.cursor = cursor or cursor_position
        self.settle = settle
        self._seen: dict[str, object] = {}
        self._cursor: dict[str, object] = {}

    def due(self, consumer: str, *, quick: bool = False) -> bool:
        info = self.reader()
        if info is None:
            return True
        stamp, idle = info
        cursor = self.cursor() if quick else None
        still = cursor is not None and cursor == self._cursor.get(consumer)
        self._cursor[consumer] = cursor
        if self._seen.get(consumer) == stamp:
            return False
        if idle < self.settle and not still:
            return False
        self._seen[consumer] = stamp
        return True

    def reset(self, consumer: str | None = None):
        """다음 due()가 (입력이 없어도) 한 번 True가 되게 한다."""
        if consumer is None:
            self._seen.clear()
        else:
            self._seen.pop(consumer, None)


def cursor_position():
    """마우스 커서 화면 좌표. Windows가 아니거나 실패하면 None."""
    if sys.platform != "win32":
        return None
    try:
        import win32api

        return tuple(win32api.GetCursorPos())
    except Exception:  # noqa: BLE001
        return None


_LAST_INPUT_INFO = None


def last_input():
    """Windows 마지막 입력 시각과 그 뒤 지난 초. Windows가 아니거나 실패하면 None."""
    global _LAST_INPUT_INFO
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        if _LAST_INPUT_INFO is None:

            class LastInputInfo(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            _LAST_INPUT_INFO = LastInputInfo
        info = _LAST_INPUT_INFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return int(info.dwTime), ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 작은 도우미


def _slide_of(shape_key: str) -> int:
    try:
        return int(str(shape_key).partition(":")[0])
    except ValueError:
        return -1


def _table_key(finding: Finding) -> str:
    """표 칸의 글자 크기 판정은 표 주소로 ("7:5:table_cell:2,1" → "7:5").

    표 칸마다 배지가 뜨면 표 전체가 배지로 덮이고(함정 PPT 7쪽: 배지 16개), 수정 후보도 표 하나에
    '표 전체 글자 크기'로 만들어지므로(planner) 표 하나로 보여 준다. 다른 판정은 칸 그대로 둔다.
    """
    key = str(finding.shape_key)
    parts = key.split(":")
    is_size = finding.rule_id == "typography" and not finding.id.endswith(":hierarchy")
    # '판정 불가'(칸 배경색을 몰라 명암비를 못 잼 등)도 칸마다 띄우면 표가 회색 배지로 덮이므로 표 하나로 모은다
    if (
        (is_size or finding.severity == "unknown")
        and len(parts) >= 3
        and parts[2] == "table_cell"
        and parts[1].isdigit()
    ):
        return f"{parts[0]}:{parts[1]}"
    return key


def _union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    left = min(box.x for box in boxes)
    top = min(box.y for box in boxes)
    right = max(box.x + box.width for box in boxes)
    bottom = max(box.y + box.height for box in boxes)
    return Box(left, top, right - left, bottom - top)


def _key_order(key: str):
    return [int(part) if part.isdigit() else 0 for part in str(key).replace("/", ":").split(":")[:2]]


def _shape_box(report: AnalysisReport, key: str) -> Box | None:
    shape = next((item for item in report.deck.shapes if item.key == key), None)
    return None if shape is None else (shape.text_bounds or shape.box)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _short(value, property_name: str) -> str:
    if value is None:
        return "없음"  # 예: 배경 채우기가 없던 상자
    if isinstance(value, dict) and "color" in value:
        # 배경 채우기 {"color": "000000", "transparency": 0.3} → "#000000 (불투명 70%)"
        color = _short(value["color"], property_name)
        transparency = value.get("transparency")
        if _is_number(transparency) and float(transparency) > 0:
            return f"{color} (불투명 {round((1 - float(transparency)) * 100):g}%)"
        return color
    if _is_number(value):
        number = f"{round(float(value), 1):g}"
        return f"{number}pt" if property_name in ("font.size", "shape.height", "shape.width") else number
    if _is_point(value):
        return f"({float(value[0]):.0f}, {float(value[1]):.0f})"
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
