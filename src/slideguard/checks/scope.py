"""Hidden objects are out of scope; unverified visibility is still checked."""

from slideguard.domain.models import DeckSnapshot, Shape
from slideguard.domain.policy import AnalysisPolicy


def hidden_slide_numbers(deck: DeckSnapshot) -> frozenset[int]:
    """확인된 숨김 슬라이드 번호. 정보가 없으면 빈 집합이라 전부 검사한다."""
    return frozenset(slide.number for slide in deck.slides if slide.hidden is True)


def target_shapes(deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Shape]:
    """검사 대상 도형. 숨긴 객체와 정책 범위 밖 슬라이드를 제외한다.

    숨김 여부가 ``None``(확인 불가)이면 검사한다. 건너뛰면 실제 문제를 놓치기 때문에,
    보이지 않는 것을 보고할 위험보다 놓치지 않는 쪽을 택한다.
    """
    hidden = hidden_slide_numbers(deck)
    return [
        shape
        for shape in deck.shapes
        if shape.effective_visible is not False
        and shape.slide_number not in hidden
        and (not policy.slides or shape.slide_number in policy.slides)
    ]
