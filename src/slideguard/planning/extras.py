"""수정 품질 개선 로드맵 (뼈대). 2026-11-20 평가 결과에서 나온 항목.

이미 들어간 것
  - 명암비 수정 목표 파라미터: domain/policy.py ContrastGoal / FixTargets.contrast_target
  - 글자 크기 수정 목표 파라미터: FontGoal (최소 → 권장 크기)
  - 한 상자 안 회색 두 가지 중 하나만 고치던 문제: planner._merge_same_target

아래 함수들은 자리만 잡아 둔 것이다. 구현할 때 tests/test_fix_targets.py의 skip을 지운다.
각 항목의 근거와 우선순위는 팀 문서 "SlideGuard 수정 품질 개선안"을 따른다.
"""

from __future__ import annotations

from slideguard.planning.contracts import ChangeProposal


def unify_slide_scrims(proposals: list[ChangeProposal]) -> list[ChangeProposal]:
    """같은 슬라이드의 사진 위 배경판(shape.fill)을 한 가지 모양으로 맞춘다.

    근거: NN/g "Text Over Images" — 이미지마다 다른 처리를 하지 말고 가장 나쁜 경우를 기준으로 일관되게.
    규칙(안): 글자색이 밝은 상자끼리는 검정판, 어두운 상자끼리는 흰판으로 묶고,
             묶음 안에서 가장 진한 불투명도 하나로 통일한다 (명암비는 그만큼 더 좋아짐).
    사용: FixTargets.unify_scrims 가 True일 때 RuleBasedPlanner.plan 끝에서 호출.
    """
    # TODO(C): 구현
    return proposals


def box_height_for_font(
    height_pt: float, text_height_pt: float, old_pt: float, new_pt: float
) -> float | None:
    """글자를 old_pt → new_pt로 키울 때 넘치지 않으려면 필요한 상자 높이. 늘릴 필요 없으면 None.

    근거: 평가에서 12→14pt만 바꾸면 상자가 그대로라 줄이 늘어 더 빽빽해짐(2번 슬라이드).
    방법(안): 글자 높이는 대략 크기에 비례 → text_height × (new/old) + 위아래 여백.
             줄 바꿈이 늘어나는 효과는 가로폭 기준으로 한 번 더 보정 (B의 줄 수 측정값 사용).
    사용: planner._font_size 에서 shape.height 후보를 같은 group으로 함께 만든다.
    """
    # TODO(C): 구현 (B 스냅샷의 rendered_box 높이를 text_height_pt로 넘겨받음)
    raise NotImplementedError


# TODO(B·C·A) 새 검사/수정 항목 — 자세한 근거는 팀 문서
#   - 양쪽 정렬 → 왼쪽 정렬: WCAG 1.4.8. B 검사(문단 정렬값) + Property.ALIGNMENT + patcher 적용 필요
#   - 한 줄 길이: WCAG 1.4.8 (한국어 등 CJK 40자 이하). B 검사만 (수정은 사람이 판단)
#   - 줄 간격: FixTargets.line_spacing 을 정하면 standards.min_line_spacing 으로 이미 연결됨.
#              B가 줄 간격 측정값(line_spacing)과 판정을 내보내야 후보가 생김
#   - 렌즈에서 대안 보여 주기: PlanResult.alternatives(예: 표준 팔레트 #333333)를
#              LensOption으로 함께 보여 주고 사용자가 고르게 (ui/slide_lens.py, 브랜치 #32 이후)
