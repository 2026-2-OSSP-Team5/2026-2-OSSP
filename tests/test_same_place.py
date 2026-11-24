"""같은 자리 일괄 적용 (쪽 번호·머리글 소제목처럼 여러 슬라이드의 같은 자리에 있는 개체)."""

from slideguard.domain.models import (
    AnalysisReport,
    Box,
    ChangeProposal,
    DeckSnapshot,
    Finding,
    Shape,
    TextRun,
)
from slideguard.planning.same_place import extend_same_place, same_place_sets
from slideguard.ui.slide_lens import build_marks


def _page_number(slide, size=10.0, x=880.0):
    """오른쪽 아래 쪽 번호 (자리 표시자 13)."""
    return Shape(
        slide,
        9,
        "쪽 번호",
        Box(x, 500, 60, 24),
        text=str(slide),
        runs=(TextRun(str(slide), size),),
        supported=True,
        placeholder_type=13,
    )


def _deck(*shapes):
    return DeckSnapshot("d.pptx", "0" * 64, 5, 960, 540, shapes=shapes)


def _fix(slide, finding="typography:1:9:1"):
    return ChangeProposal(f"font.size@{slide}:9", finding, f"{slide}:9", "font.size", 10.0, 14.0, 0.1)


def test_same_place_shapes_get_the_same_fix_and_form_one_set():
    # 1·2쪽 쪽 번호에만 판정·후보가 있고 3·4쪽은 판정이 없었음. 5쪽 쪽 번호는 자리가 다름
    deck = _deck(*(_page_number(n) for n in range(1, 5)), _page_number(5, x=40))
    proposals = [_fix(1), _fix(2, "typography:2:9:1")]
    extended = extend_same_place(deck, proposals)
    assert [p.shape_key for p in extended] == ["1:9", "2:9", "3:9", "4:9"]
    assert all(p.after == 14.0 for p in extended)
    assert same_place_sets(deck, extended) == [
        ("font.size@1:9", "font.size@2:9", "font.size@3:9", "font.size@4:9")
    ]


def test_different_value_or_existing_other_fix_is_left_alone():
    deck = _deck(_page_number(1), _page_number(2, size=16.0), _page_number(3))
    other = ChangeProposal("font.size@3:9", "f", "3:9", "font.size", 10.0, 18.0, 0.2)  # 3쪽은 다른 값
    extended = extend_same_place(deck, [_fix(1), other])
    assert [p.id for p in extended] == ["font.size@1:9", "font.size@3:9"]  # 2쪽은 이미 16pt
    assert same_place_sets(deck, extended) == []


def test_lens_card_offers_individual_and_batch_options():
    deck = _deck(*(_page_number(n) for n in range(1, 4)))
    findings = tuple(
        Finding(f"typography:{n}:9:1", "typography", f"{n}:9", "warning", "", 10.0, 14.0, "")
        for n in (1, 2, 3)
    )
    proposals = extend_same_place(deck, [_fix(n, f"typography:{n}:9:1") for n in (1, 2, 3)])
    report = AnalysisReport(deck, findings, tuple(proposals))
    [mark] = build_marks(report, 2)
    labels = [option.label for option in mark.options]
    assert labels[0].startswith("글자 크기") and labels[1].startswith("[같은 자리 3쪽 일괄]")
    assert mark.options[1].proposal_ids == ("font.size@1:9", "font.size@2:9", "font.size@3:9")
