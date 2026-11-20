"""슬라이드 종류 구분: 표지·목차·출처·마무리 슬라이드는 일부 검사에서 뺀다.

이런 슬라이드는 일부러 다르게 만든다.
  표지·마무리("감사합니다", "Q&A")  큰 글씨 몇 개, 넓은 여백 → 위계·정보 밀도·여백 판정이 의미 없음
  목차                              항목을 나열하는 슬라이드 → 줄 수(정보 밀도)·위계 판정이 의미 없음
  출처·참고문헌                     청중이 읽는 슬라이드가 아님 → 정보 밀도·여백·글자 크기·위계 판정이 잡음
명암비·넘침·겹침처럼 화면에서 실제로 깨지는 문제는 그대로 검사한다.

구분 방법 (글자만 보고 정한다)
  제목      제목 자리 표시자, 없으면 슬라이드에서 가장 위에 있는 글상자(쪽 번호·바닥글 제외)
  출처      제목에 '참고문헌·출처·Reference…'가 있거나, 글 줄의 절반 이상이 주소(http, www.)
  목차      제목(띄어쓰기 무시)이 '목차·차례·Contents·Agenda·Index'
  마무리    슬라이드 글이 짧고(공백 빼고 40자 이하) '감사합니다·Thank you·Q&A·질문'이 있음
  표지      첫 슬라이드
"""

from __future__ import annotations

from slideguard.domain.models import DeckSnapshot, Finding, Shape
from slideguard.domain.policy import TITLE_PLACEHOLDERS

FOOTER_PLACEHOLDERS = frozenset({13, 15, 16})  # 쪽 번호, 바닥글, 날짜
REFERENCE_WORDS = ("참고문헌", "참고자료", "출처", "reference", "bibliography", "workscited", "sources")
AGENDA_WORDS = ("목차", "차례", "contents", "agenda", "index", "outline")
CLOSING_WORDS = ("감사합니다", "thankyou", "thanks", "q&a", "질문", "질의응답")
CLOSING_MAX_CHARS = 40
URL_SHARE = 0.5

# 종류별로 빼는 검사 (rule_id 앞부분, 또는 ":hierarchy"로 끝나는 위계 판정)
EXEMPT = {
    "cover": ("layout.density", "layout.margin", "hierarchy"),
    "closing": ("layout.density", "layout.margin", "hierarchy"),
    "agenda": ("layout.density", "hierarchy"),
    "references": ("layout.density", "layout.margin", "typography", "hierarchy"),
}
LABELS = {"cover": "표지", "closing": "마무리", "agenda": "목차", "references": "출처", "body": "본문"}


def classify(deck: DeckSnapshot) -> dict[int, str]:
    """슬라이드 번호 → "cover" / "agenda" / "references" / "closing" / "body"."""
    by_slide: dict[int, list[Shape]] = {}
    for shape in deck.shapes:
        if shape.text.strip() and shape.placeholder_type not in FOOTER_PLACEHOLDERS:
            by_slide.setdefault(shape.slide_number, []).append(shape)
    kinds = {}
    for number in range(1, deck.slide_count + 1):
        kinds[number] = _kind(number, by_slide.get(number, []))
    return kinds


def exempt(deck: DeckSnapshot, findings: list[Finding]) -> tuple[list[Finding], dict[int, str]]:
    """특수 슬라이드에서 의미 없는 판정을 뺀다. (남은 판정, 슬라이드 종류)"""
    kinds = classify(deck)
    kept = []
    for finding in findings:
        slide = _slide_of(finding.shape_key)
        rules = EXEMPT.get(kinds.get(slide, "body"), ())
        if any(_matches(finding, rule) for rule in rules):
            continue
        kept.append(finding)
    return kept, kinds


# ---------------------------------------------------------------- 도우미


def _kind(number: int, shapes: list[Shape]) -> str:
    title = _squash(_title_text(shapes))
    lines = [line.strip() for shape in shapes for line in shape.text.splitlines() if line.strip()]
    if any(word in title for word in REFERENCE_WORDS):
        return "references"
    urls = sum(1 for line in lines if "http" in line or "www." in line)
    if len(lines) >= 4 and urls / len(lines) >= URL_SHARE:
        return "references"
    if any(title.endswith(word) or title == word for word in AGENDA_WORDS):
        return "agenda"
    everything = _squash(" ".join(lines))
    if len(everything) <= CLOSING_MAX_CHARS and any(word in everything for word in CLOSING_WORDS):
        return "closing"
    if number == 1:
        return "cover"
    return "body"


def _title_text(shapes: list[Shape]) -> str:
    titles = [shape for shape in shapes if shape.placeholder_type in TITLE_PLACEHOLDERS]
    if titles:
        return titles[0].text
    placed = [shape for shape in shapes if shape.box is not None]
    if not placed:
        return ""
    return min(placed, key=lambda shape: (shape.box.y, shape.box.x)).text


def _squash(text: str) -> str:
    """소문자로, 띄어쓰기·번호·마침표 없이 ("8. 참고 문헌 및 출처" → "참고문헌및출처")."""
    return "".join(ch for ch in text.lower() if not ch.isspace() and ch not in ".:·-_()[]0123456789")


def _matches(finding: Finding, rule: str) -> bool:
    if rule == "hierarchy":
        return finding.id.endswith(":hierarchy") or finding.rule_id.startswith("typography.hierarchy")
    if rule == "typography":
        return finding.rule_id == "typography"
    return finding.rule_id == rule or finding.rule_id.startswith(rule + ".")


def _slide_of(shape_key: str) -> int:
    try:
        return int(str(shape_key).split(":", 1)[0])
    except ValueError:
        return 0
