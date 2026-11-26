"""User-facing explanations; diagnostic evidence remains available separately.

This changes presentation only. Findings, thresholds and planner inputs retain
their original precision and extraction evidence.
"""

import math
import re
from dataclasses import dataclass
from html import escape

from PySide6.QtWidgets import QDialog, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout


@dataclass(frozen=True)
class FindingExplanation:
    title: str
    summary: str
    measurements: tuple[tuple[str, str], ...]
    guidance: str

    def html(self, *, measurements=True, compact=False):
        parts = [f'<p style="margin:0 0 2px">{escape(self.summary)}</p>']
        if measurements and self.measurements:
            rows = "".join(
                f'<tr><td style="padding:0 12px 1px 0">{escape(label)}</td>'
                f'<td style="padding:0 0 1px"><b>{escape(value)}</b></td></tr>'
                for label, value in self.measurements
            )
            parts.append(f'<table cellspacing="0" cellpadding="0">{rows}</table>')
        details = '<a href="measurement-details" style="color:#1756b2">측정 상세 보기</a>'
        inline = f" &nbsp; {details}" if compact else ""
        parts.append(f'<p style="margin:2px 0"><b>권장:</b> {escape(self.guidance)}{inline}</p>')
        if not compact:
            parts.append(f'<p style="margin:2px 0 0">{details}</p>')
        return "".join(parts)


def _number(value, unit=""):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "확인하지 못함"
    return f"{value:.2f}".rstrip("0").rstrip(".") + unit


def _millimeters(value):
    return _number(value * 25.4 / 72, "mm") if isinstance(value, (int, float)) else "확인하지 못함"


def _pair(current, target, unit, current_label, target_label, suffix=" 이상"):
    return ((current_label, _number(current, unit)), (target_label, _number(target, unit) + suffix))


def describe_finding(finding):
    rule, message = finding.rule_id, finding.message
    current, target = finding.measured, finding.expected
    unknown = finding.severity == "unknown"
    hierarchy = rule.startswith("typography.hierarchy") or finding.id.endswith(":hierarchy")
    if unknown:
        if rule == "contrast":
            if "크기" in message or "굵기" in message:
                return FindingExplanation(
                    "명암비 기준을 정하려면 글자 서식을 확인해야 합니다",
                    "글자 크기나 굵기를 읽지 못해 어떤 명암비 기준을 적용할지 정하지 못했습니다.",
                    (),
                    "PowerPoint에서 해당 글자의 크기와 굵기를 확인한 뒤 다시 검사해 주세요.",
                )
            return FindingExplanation(
                "글자와 배경의 밝기 차이를 확인하지 못했습니다",
                "글자색 또는 배경색을 확인하지 못해 잘 구분되는지 자동으로 판단할 수 없습니다.",
                (),
                "해당 글자가 배경 위에서 또렷하게 보이는지 직접 확인해 주세요.",
            )
        if rule.startswith("typography"):
            return FindingExplanation(
                "글자 크기를 확인하지 못했습니다",
                "일부 글자 서식을 읽지 못해 크기를 비교할 수 없습니다.",
                (),
                "PowerPoint에서 해당 글자 크기를 확인한 뒤 다시 검사해 주세요.",
            )
        return FindingExplanation(
            "배치를 자동으로 판단하지 못했습니다"
            if rule.startswith("layout.")
            else "자동으로 판단하지 못했습니다",
            "객체의 위치나 앞뒤 순서 등 판단에 필요한 정보를 확인하지 못했습니다.",
            (),
            "PowerPoint에서 해당 객체가 잘리거나 가려지지 않았는지 확인해 주세요.",
        )
    if rule == "contrast":
        sampled = any(word in finding.evidence for word in ("픽셀", "근사", "렌더링"))
        partial = sampled and finding.severity == "review"
        return FindingExplanation(
            "배경에 따라 글자가 흐리게 보일 수 있습니다" if partial else "글자와 배경이 잘 구분되지 않습니다",
            "배경 밝기가 일정하지 않아 일부 위치의 명암비가 권장 기준보다 낮게 추정됐습니다."
            if partial
            else "확인한 글자와 배경의 밝기 차이가 권장 기준보다 작습니다."
            + (" 화면의 여러 위치를 비교한 추정값입니다." if sampled else ""),
            _pair(current, target, ":1", "추정 명암비" if sampled else "현재 명암비", "권장 명암비"),
            "글자색을 바꾸거나 글자 뒤에 단색 배경을 넣어 보세요.",
        )
    if hierarchy:
        return FindingExplanation(
            "제목과 본문의 크기 차이가 작습니다",
            "제목이 충분히 크지 않아 내용의 구조를 한눈에 구분하기 어려울 수 있습니다.",
            _pair(current, target, "pt", "제목 크기", "권장 제목 크기"),
            "제목을 본문보다 크게 표시해 주세요. 제목·본문 구분이 의도와 맞는지도 확인하세요.",
        )
    if rule == "typography":
        return FindingExplanation(
            "글자가 작아 읽기 어려울 수 있습니다",
            "설정한 용도의 크기 기준보다 작게 표시되어 있습니다.",
            _pair(
                current,
                target,
                "pt",
                "현재 글자 크기",
                "권장 크기" if finding.severity == "review" else "최소 크기",
            ),
            "글자를 키우고, 공간이 부족하면 본문을 여러 장으로 나눠 보세요.",
        )
    if rule == "layout.margin":
        outside = isinstance(current, (int, float)) and current < 0 or "밖" in message
        direction = next((side for side in ("왼쪽", "오른쪽", "위", "아래") if f"({side})" in message), "")
        label = f"{direction}쪽 여백" if direction in ("위", "아래") else f"{direction} 여백".strip()
        metrics = (
            (("밖으로 나온 길이", _millimeters(abs(current))),)
            if outside and isinstance(current, (int, float))
            else ((label, _millimeters(current)), ("권장 여백", _millimeters(target) + " 이상"))
        )
        return FindingExplanation(
            "내용이 슬라이드 밖으로 나가 있습니다" if outside else "내용이 슬라이드 가장자리에 가깝습니다",
            "화면 밖으로 나온 내용은 발표할 때 잘릴 수 있습니다."
            if outside
            else "가장자리와의 간격이 권장 여백보다 좁습니다. 의도한 배치인지 확인해 주세요.",
            metrics,
            "해당 글자나 글을 감싼 도형을 슬라이드 안쪽으로 옮겨 주세요.",
        )
    if rule == "layout.overflow":
        return FindingExplanation(
            "글자가 글상자 밖으로 나가 있습니다",
            "글상자보다 글이 차지하는 영역이 커서 일부가 잘리거나 다른 내용과 겹칠 수 있습니다.",
            (("넘친 길이", _millimeters(current)),),
            "글상자를 넓히거나 높이를 늘려 보세요. 내용이 많으면 여러 슬라이드로 나눠 주세요.",
        )
    if rule == "layout.overlap":
        covered = "가립니다" in message
        return FindingExplanation(
            "도형이 글자를 가릴 수 있습니다" if covered else "글자와 다른 객체가 겹쳐 있습니다",
            "글자 앞에 도형이 놓여 있습니다."
            if covered
            else "두 객체가 같은 영역에 있습니다. 의도한 배경이나 강조 효과라면 유지해도 됩니다.",
            (),
            "글자가 가려진다면 두 객체를 떼어 놓거나 글자를 앞으로 가져와 주세요.",
        )
    if rule == "layout.density":
        lines = finding.id.endswith(":lines")
        unit, noun = ("줄", "줄 수") if lines else ("자", "글자 수")
        return FindingExplanation(
            "한 슬라이드에 읽을 내용이 많습니다",
            "슬라이드 전체의 글상자·표 셀을 합산한 값입니다. 표시된 상자 하나의 줄 수가 아닙니다. "
            "빈 줄·쪽 번호는 제외하며, 자동 줄바꿈을 반영합니다."
            if lines
            else "슬라이드 전체의 글상자·표 셀에서 공백을 제외한 글자 수를 합산했습니다.",
            _pair(current, target, unit, f"현재 {noun}", f"권장 {noun}", " 이하"),
            "문단·목록 경계에서 여러 장으로 나눠 보세요. 분할 제안이 있으면 미리보기로 확인할 수 있습니다.",
        )
    if rule == "layout.line_length":
        return FindingExplanation(
            "한 줄이 깁니다",
            "실제로 표시된 줄을 전각 1자·반각 0.5자로 환산한 길이입니다.",
            _pair(current, target, "자 분량", "현재 한 줄 길이(전각 환산)", "참고 길이", " 이하"),
            "줄을 어디서 끊을지는 직접 판단해 주세요. 자동 수정안은 만들지 않습니다.",
        )
    if rule.startswith("readability."):
        subtype = rule.split(".")[1]
        if subtype in ("graphic_contrast", "chart_contrast"):
            return FindingExplanation(
                "선이나 그래프가 배경과 잘 구분되지 않습니다",
                "정보를 전달하는 선·도형과 배경의 밝기 차이가 작습니다.",
                _pair(current, target, ":1", "현재 명암비", "권장 명암비"),
                "선·막대·도형의 색을 바꿔 배경과 더 뚜렷하게 구분해 주세요.",
            )
        if subtype == "spacing":
            return FindingExplanation(
                "본문의 줄 사이가 좁습니다",
                "줄 사이에 여유가 적어 다음 줄을 따라 읽기 어려울 수 있습니다.",
                _pair(current, target, "배", "현재 줄 간격", "권장 줄 간격"),
                "줄 간격을 늘리고 글상자 높이도 함께 확보해 주세요.",
            )
        if subtype == "alignment":
            alignment = "가운데 정렬" if str(current) == "2" else "양쪽·균등 정렬"
            return FindingExplanation(
                "긴 본문의 정렬을 확인해 주세요",
                "여러 줄 본문은 줄의 시작 위치를 맞추면 읽던 위치를 찾기 쉽습니다.",
                (("현재 정렬", alignment), ("권장 정렬", "왼쪽 정렬")),
                "일반 본문은 왼쪽 정렬로 바꿔 보세요. 제목이나 수식은 의도한 정렬을 유지할 수 있습니다.",
            )
        if subtype == "line_length":
            return FindingExplanation(
                "본문 한 줄이 깁니다",
                "한 줄에 글자가 많으면 다음 줄로 넘어갈 때 읽던 위치를 놓치기 쉽습니다.",
                _pair(current, target, "자", "현재 한 줄 길이", "참고 길이", " 이하"),
                "글상자 폭을 줄이거나 문단을 나눠 보세요. 이 길이는 일률적인 오류 기준은 아닙니다.",
            )
        if subtype in ("italic", "underline"):
            emphasis = "기울임" if subtype == "italic" else "밑줄"
            return FindingExplanation(
                f"본문 대부분에 {emphasis}이 사용되었습니다",
                "강조가 넓게 적용되어 핵심 부분을 구분하거나 긴 글을 읽기 어려울 수 있습니다.",
                ((f"{emphasis} 사용 비율", _number(current, "%")),),
                "핵심 단어나 문장에만 강조를 남겨 보세요. 인용·고유 표기는 유지할 수 있습니다.",
            )
        if subtype == "caption_distance":
            return FindingExplanation(
                "그림과 설명이 떨어져 있습니다",
                "위치상 관련 있어 보이는 그림과 설명 사이가 멉니다. 실제 연결 관계를 확인해 주세요.",
                (("현재 거리", _millimeters(current)), ("참고 거리", _millimeters(target) + " 이하")),
                "해당 그림의 설명이 맞다면 서로 가까이 배치해 주세요.",
            )
        descriptions = {
            "color_only": (
                "그래프를 색상만으로 구분하고 있습니다",
                "색을 구분하기 어렵거나 흑백으로 볼 때 계열을 구별하기 어려울 수 있습니다.",
                "계열 이름, 서로 다른 기호나 선 모양을 함께 표시해 주세요.",
            ),
            "capitals": (
                "영문 본문 대부분이 대문자입니다",
                "긴 영문을 모두 대문자로 쓰면 단어를 구분해 읽기 어려울 수 있습니다.",
                "일반 문장은 대소문자를 구분해 보세요. 약어·코드는 유지할 수 있습니다.",
            ),
            "message": (
                "제목에 그래프의 핵심 결과를 덧붙여 보세요",
                "현재 제목은 일반적인 주제명이라 그래프가 전달하는 핵심을 바로 알기 어렵습니다.",
                "그래프에서 전달하려는 결과를 제목에 직접 적어 주세요.",
            ),
        }
        if subtype in descriptions:
            title, summary, guidance = descriptions[subtype]
            return FindingExplanation(title, summary, (), guidance)
    title = re.sub(r"\s*\([^)]*(?:\d|근사)[^)]*\)\s*$", "", message)
    return FindingExplanation(
        title, "해당 항목을 PowerPoint에서 확인해 주세요.", (), "의도한 표현인지 확인해 주세요."
    )


class TechnicalDetailsDialog(QDialog):
    def __init__(self, title, content, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SlideGuard · 상세 정보")
        self.resize(620, 360)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setPlainText(content)
        layout.addWidget(self.evidence, 1)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class FindingDetailsDialog(TechnicalDetailsDialog):
    def __init__(self, finding, parent=None, *, notes=""):
        content = (
            f"검사: {finding.rule_id}\n대상: {finding.shape_key}\n"
            f"원래 진단: {finding.message}\n\n"
            f"원래 측정값: {finding.measured}\n원래 기준값: {finding.expected}\n\n"
            f"측정·추출 근거\n{finding.evidence}" + (f"\n\n수정안 제외 근거\n{notes}" if notes else "")
        )
        super().__init__(describe_finding(finding).title, content, parent)
        self.setWindowTitle("SlideGuard · 측정 상세")
