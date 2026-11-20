"""표지·목차·출처·마무리 슬라이드 구분과, 슬라이드 크기에 맞춘 글자 크기 기준."""

from slideguard.checks.slide_kinds import classify, exempt
from slideguard.checks.typography import TypographyRule
from slideguard.domain.models import Box, DeckSnapshot, Finding, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy, slide_scale


def shape(slide, shape_id, text, y=100.0, placeholder=None, size=20.0):
    return Shape(
        slide,
        shape_id,
        f"s{shape_id}",
        Box(50, y, 600, 40),
        text=text,
        runs=(TextRun(text, size),),
        supported=True,
        placeholder_type=placeholder,
    )


def deck(*shapes, slides=6, height=540.0):
    return DeckSnapshot("d.pptx", "abc", slides, height * 16 / 9, height, shapes=shapes)


def finding(key, rule_id, fid=None):
    return Finding(fid or f"{rule_id}:{key}", rule_id, key, "review", "", None, None, "")


def test_special_slides_are_recognised_from_their_text():
    urls = "\n".join(f"https://blog.example.com/{i}" for i in range(6))
    kinds = classify(
        deck(
            shape(1, 2, "도서 예약 시스템", placeholder=3),
            shape(2, 2, "목 차", y=20),
            shape(2, 3, "1. 개발 목적\n2. 기능\n3. 일정"),
            shape(3, 2, "1. 개발 목적 및 필요성", y=20),
            shape(3, 3, "도서관 이용자가 불편함을 겪고 있음"),
            shape(4, 2, "8. 참고 문헌 및 출처", y=20),
            shape(5, 2, "자료", y=20),
            shape(5, 3, urls),  # 제목에 출처라는 말이 없어도 줄 대부분이 주소면 출처
            shape(6, 2, "감사합니다."),
        )
    )
    assert kinds == {1: "cover", 2: "agenda", 3: "body", 4: "references", 5: "references", 6: "closing"}


def test_noise_is_removed_but_real_breakage_is_kept():
    d = deck(shape(1, 2, "표지 제목", placeholder=3), shape(4, 2, "참고문헌", y=20), shape(4, 3, "주소 목록"))
    findings = [
        finding("4:3", "layout.density"),
        finding("4:3", "layout.margin"),
        finding("4:3", "typography"),
        finding("4:3", "contrast"),  # 글씨가 안 보이는 건 출처 슬라이드여도 문제
        finding("4:3", "layout.overflow"),
        finding("1:2", "typography", "typography:1:hierarchy"),
        finding("1:2", "typography"),  # 표지의 글자 크기 자체는 그대로 검사
        finding("3:2", "layout.density"),  # 본문 슬라이드
    ]
    kept, kinds = exempt(d, findings)
    assert [(f.shape_key, f.rule_id) for f in kept] == [
        ("4:3", "contrast"),
        ("4:3", "layout.overflow"),
        ("1:2", "typography"),
        ("3:2", "layout.density"),
    ]
    assert kinds[4] == "references"


def test_font_rule_grows_with_large_slides_but_never_loosens():
    assert slide_scale(540) == 1.0 and slide_scale(810) == 1.5 and slide_scale(405) == 1.0

    def severities(height, size):
        d = deck(shape(3, 3, "본문", size=size), slides=3, height=height)
        return [(f.severity, f.expected) for f in TypographyRule().evaluate(d, AnalysisPolicy())]

    assert severities(540, 18) == []  # 기본 슬라이드: 발표용 권장 18pt 통과
    assert severities(810, 18) == [("warning", 21.0)]  # 1.5배 큰 슬라이드의 18pt = 기본의 12pt
    assert severities(405, 12) == [("warning", 14.0)]  # 작은 슬라이드에서 기준을 낮추지는 않음
