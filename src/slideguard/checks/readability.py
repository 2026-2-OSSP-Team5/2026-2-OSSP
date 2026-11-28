"""Measured text and graphic checks using configurable project recommendations."""

import json
import math
import re

from slideguard.domain.models import Finding
from slideguard.domain.policy import TITLE_PLACEHOLDERS

from .contrast import contrast_ratio
from .scope import target_shapes

DEFAULT_LINE_SPACING = 1.2
LONG_PARAGRAPH_CHARS = 50
EMPHASIS_SHARE = 0.8
CAPITAL_SHARE = 0.9


def _add(findings, shape, suffix, message, measured, expected, evidence, severity="review", box=None):
    findings.append(
        Finding(
            f"readability.{suffix}:{shape.key}",
            f"readability.{suffix}",
            shape.key,
            severity,
            message,
            measured,
            expected,
            evidence,
            box or shape.text_bounds or shape.box,
        )
    )


def _distance(first, second):
    dx = max(first.x - second.x - second.width, second.x - first.x - first.width, 0)
    dy = max(first.y - second.y - second.height, second.y - first.y - first.height, 0)
    return math.hypot(dx, dy)


def _paragraph_lines(shape, paragraph):
    """Associate visible lines by source range, never by nearly equal baselines.

    Office bounds include line leading. The next (even blank) line can start a
    few millionths of a point before the previous paragraph's reported bottom.
    Old snapshots without source offsets use line centres and both coordinates.
    """
    props = dict(paragraph.properties)
    start, length = props.get("Start"), props.get("Length")
    result = []
    for line in shape.spans:
        if line.kind != "line" or not line.text.strip():
            continue
        line_props = dict(line.properties)
        line_start = line_props.get("Start")
        line_length = line_props.get("Length")
        if all(type(v) in (int, float) for v in (start, length, line_start, line_length)):
            belongs = start <= line_start < start + length and line_start + line_length <= start + length
        elif line.box and paragraph.box:
            centre_y = line.box.y + line.box.height / 2
            belongs = (
                paragraph.box.y <= centre_y < paragraph.box.y + paragraph.box.height
                and line.box.x < paragraph.box.x + paragraph.box.width
                and line.box.x + line.box.width > paragraph.box.x
            )
        else:
            belongs = False
        if belongs:
            result.append(line)
    return result


class ReadabilityRule:
    id = "readability"

    def evaluate(self, deck, policy):
        findings = []
        shapes = target_shapes(deck, policy)
        goal = policy.fix.line_spacing or DEFAULT_LINE_SPACING
        for shape in shapes:
            if shape.text.strip() and shape.supported:
                self._text(shape, goal, findings)
            self._graphic(shape, policy, findings)
        for caption in shapes:
            if not caption.box or not re.match(
                r"\s*(그림|표\s*\d|Fig\.?\s*\d|Figure\s*\d)", caption.text, re.I
            ):
                continue
            pictures = [
                s
                for s in shapes
                if s.slide_number == caption.slide_number and s.shape_type in (3, 13, 19) and s.box
            ]
            if pictures:
                picture = min(pictures, key=lambda s: (_distance(caption.box, s.box), s.shape_id))
                distance = _distance(caption.box, picture.box)
                if distance > deck.height_pt * 0.12:
                    _add(
                        findings,
                        caption,
                        "caption_distance",
                        "그림과 설명이 멀리 떨어져 있습니다",
                        round(distance, 1),
                        round(deck.height_pt * 0.12, 1),
                        "그림·표로 시작하는 설명과 가장 가까운 그림 사이 거리입니다. 연결 관계는 위치로 추정했습니다.",
                    )
        # This is a local editorial suggestion, not a claim about semantic quality.
        for number in sorted({s.slide_number for s in shapes}):
            on_slide = [s for s in shapes if s.slide_number == number]
            title = next((s for s in on_slide if s.placeholder_type in TITLE_PLACEHOLDERS), None)
            if title and title.text.strip().lower() in {
                "결과",
                "분석",
                "내용",
                "설명",
                "results",
                "analysis",
            }:
                if any(s.shape_type == 3 for s in on_slide):
                    _add(
                        findings,
                        title,
                        "message",
                        "제목에 그래프의 핵심 결과를 덧붙여 보세요",
                        None,
                        "핵심을 드러내는 제목 권장",
                        "현재 제목이 일반적인 주제명이고 그래프가 있습니다. 제목에 결론을 넣으면 내용을 찾기 쉽습니다. 의미 판단이나 문장 자동 생성은 하지 않았습니다.",
                    )
        return findings

    def _text(self, shape, goal, findings):
        is_title = shape.placeholder_type in TITLE_PLACEHOLDERS
        paragraphs = [s for s in shape.spans if s.kind == "paragraph" and s.text.strip()]
        for paragraph in paragraphs:
            props = dict(paragraph.properties)
            size, spacing = props.get("Size"), props.get("SpaceWithin")
            lines = _paragraph_lines(shape, paragraph)
            multiline = len(lines) >= 2
            if multiline and size and spacing and spacing > 0 and props.get("LineRuleWithin") in (-1, 0):
                multiple = spacing if props["LineRuleWithin"] == -1 else spacing / size
                if multiple < goal - 0.02:
                    _add(
                        findings,
                        shape,
                        f"spacing.{paragraph.index}",
                        "본문의 줄 간격이 좁습니다",
                        round(multiple, 2),
                        goal,
                        "줄 사이 여유를 늘리면 다음 줄을 따라 읽기 쉽습니다. 간격과 글상자 높이를 함께 조정하세요.",
                        box=paragraph.box,
                    )
            if not is_title and multiline and props.get("Alignment") in (2, 4, 5, 7):
                _add(
                    findings,
                    shape,
                    f"alignment.{paragraph.index}",
                    "긴 본문의 정렬을 확인해 주세요",
                    str(props["Alignment"]),
                    "왼쪽 정렬 권장",
                    "여러 줄 본문은 왼쪽 정렬로 시작 위치를 맞추면 읽기 쉽습니다. 제목·수식은 유지할 수 있습니다.",
                    box=paragraph.box,
                )
        count = sum(len(run.text.strip()) for run in shape.runs)
        if count < LONG_PARAGRAPH_CHARS or is_title:
            return
        for name, label in (("italic", "기울임"), ("underline", "밑줄")):
            known = [run for run in shape.runs if getattr(run, name) is not None]
            known_count = sum(len(run.text.strip()) for run in known)
            if known_count != count:
                continue
            share = sum(len(run.text.strip()) for run in known if getattr(run, name)) / count
            if share >= EMPHASIS_SHARE:
                _add(
                    findings,
                    shape,
                    name,
                    f"긴 본문 대부분에 {label}이 사용되었습니다",
                    round(share * 100),
                    "강조할 부분에만 사용 권장",
                    "본문의 강조 서식을 줄이고 핵심 단어나 문장에만 남겨 보세요. 고유 표기나 인용은 유지할 수 있습니다.",
                )
        letters = [ch for ch in shape.text if ch.isascii() and ch.isalpha()]
        if len(letters) >= 30 and sum(ch.isupper() for ch in letters) / len(letters) >= CAPITAL_SHARE:
            _add(
                findings,
                shape,
                "capitals",
                "영문 본문 대부분이 대문자입니다",
                len(letters),
                "문장형 대소문자 권장",
                "긴 영문은 대소문자를 구분하면 읽기 쉽습니다. 약어·코드는 제외해 판단하세요.",
            )

    def _graphic(self, shape, policy, findings):
        props = dict(shape.properties)
        color = props.get("meaningful_graphic_color")
        if color and shape.background is not None:
            ratio = contrast_ratio(tuple(bytes.fromhex(color)), shape.background)
            if ratio < 3:
                _add(
                    findings,
                    shape,
                    "graphic_contrast",
                    "선이나 도형이 배경과 구분되기 어렵습니다",
                    round(ratio, 2),
                    3.0,
                    "중요한 그래픽과 확인된 단색 배경을 비교했습니다. 장식 요소라면 유지할 수 있습니다.",
                )
        if props.get("chart_color_only") is True:
            _add(
                findings,
                shape,
                "color_only",
                "그래프의 계열을 색상만으로 구분하고 있습니다",
                None,
                "이름·기호·선 모양 함께 사용",
                "확인된 계열에 직접 이름이나 서로 다른 표식을 추가하면 색 구분이 어려운 사람도 읽기 쉽습니다.",
            )
        raw = props.get("chart_series")
        if raw:
            for series in json.loads(raw):
                if series.get("contrast") is not None and series["contrast"] < 3:
                    _add(
                        findings,
                        shape,
                        "chart_contrast",
                        "그래프 계열과 배경의 밝기 차이가 작습니다",
                        round(series["contrast"], 2),
                        3.0,
                        "계열의 불투명 색과 확인된 단색 플롯 배경을 비교했습니다. 선·막대가 명확히 보이도록 조정해 보세요.",
                    )
                    break
