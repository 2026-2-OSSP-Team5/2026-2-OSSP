"""슬라이드 간 디자인 일관성."""

from slideguard.checks.consistency import ConsistencyRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy
from slideguard.planning import RuleBasedPlanner


def _title(slide, x=40.0, y=30.0, size=32.0):
    return Shape(
        slide,
        2,
        "제목",
        Box(x, y, 600, 60),
        text=f"제목 {slide}",
        runs=(TextRun(f"제목 {slide}", size),),
        supported=True,
        placeholder_type=1,
    )


def _body(slide, size=20.0):
    return Shape(
        slide,
        3,
        "본문",
        Box(40, 140, 800, 300),
        text="본문 내용",
        runs=(TextRun("본문 내용", size),),
        supported=True,
        placeholder_type=2,
    )


def _deck(*shapes, slides=6):
    return DeckSnapshot("d.pptx", "0" * 64, slides, 960, 540, shapes=shapes)


def test_the_one_title_that_jumps_is_found_and_moved_back():
    # 1쪽은 표지라 빼고 본다. 2~6쪽 가운데 5쪽 제목만 아래·오른쪽으로 밀려 있음
    shapes = [_title(1, 300, 200, 44)]
    shapes += [_title(n) if n != 5 else _title(n, x=90, y=60) for n in range(2, 7)]
    shapes += [_body(n) for n in range(2, 7)]
    deck = _deck(*shapes)
    findings = ConsistencyRule().evaluate(deck, AnalysisPolicy())
    assert [(f.rule_id, f.shape_key) for f in findings] == [("consistency.title_position", "5:2")]
    result = RuleBasedPlanner().plan(deck, findings, AnalysisPolicy())
    assert [(p.prop, p.after) for p in result.proposals] == [("shape.position", (40.0, 30.0))]


def test_title_size_and_body_size_outliers():
    shapes = [_title(n, size=24.0 if n == 3 else 32.0) for n in range(2, 7)]
    shapes += [_body(n, size=12.0 if n == 4 else 20.0) for n in range(2, 7)]
    findings = {
        (f.rule_id, f.shape_key) for f in ConsistencyRule().evaluate(_deck(*shapes), AnalysisPolicy())
    }
    assert findings == {("consistency.title_size", "3:2"), ("consistency.body_size", "4:3")}


def test_no_convention_no_finding():
    """제목 위치가 슬라이드마다 제각각이면 '약속'이 없으므로 아무것도 알리지 않는다."""
    shapes = [_title(n, x=40 + n * 60, y=30 + n * 40) for n in range(2, 7)]
    assert ConsistencyRule().evaluate(_deck(*shapes), AnalysisPolicy()) == []
    few = [_title(n) for n in range(2, 4)] + [_title(4, 200, 200)]
    assert ConsistencyRule().evaluate(_deck(*few, slides=4), AnalysisPolicy()) == []
    assert ConsistencyRule().evaluate(_deck(*shapes), AnalysisPolicy(consistency=False)) == []
