"""팀원 모델이 '다른 이름'으로 만들어져도 bindings만 바꾸면 끼워진다는 것을 확인합니다."""

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

import pytest
from fakes import FakeFinding, FakeRun, FakeShape, FakeSnapshot

from slideguard.planning import Property, RuleBasedPlanner, TeamBindings


class Verdict(Enum):  # B가 상태를 한글 Enum으로 만든 경우
    FAIL = "경고"
    CHECK = "검토"
    UNKNOWN = "판정불가"


class Level(Enum):
    WEAK = "weak"
    MID = "mid"
    HARD = "hard"


@dataclass
class OtherPolicy:  # 고정 판단을 메서드로, 강도를 다른 이름으로 가진 정책 (용도 필드 없음)
    level: Level = Level.HARD
    frozen: set = field(default_factory=set)

    def is_locked(self, slide, shape_id, prop):
        return (slide, shape_id) in self.frozen


class OurTeamBindings(TeamBindings):
    """실제 팀 코드에 맞출 때 이렇게 상속해서 필요한 부분만 바꿉니다."""

    RULE_NAMES: ClassVar[dict[str, str]] = {"min_contrast": "contrast", "min_font": "font_size"}
    STATUS_NAMES: ClassVar[dict[str, str]] = {"경고": "warning", "검토": "review", "판정불가": "unmeasurable"}
    MEASURED_KEYS: ClassVar[dict[str, str]] = {
        **TeamBindings.MEASURED_KEYS,
        "foreground": "fg",
        "background": "bg",
        "ratio": "value",
        "required_ratio": "threshold",
    }
    STRENGTH_NAMES: ClassVar[dict[str, str]] = {"weak": "passive", "mid": "balanced", "hard": "aggressive"}

    def strength(self, policy):
        return self.STRENGTH_NAMES[policy.level.value]


def finding():
    measured = {"fg": "999999", "bg": "FFFFFF", "value": 2.85, "threshold": 6.0}
    return FakeFinding("min_contrast", 2, 9, Verdict.FAIL, measured)


def snapshot():
    return FakeSnapshot(shapes=[FakeShape(2, 9, [FakeRun("t", 24.0, "#999999", False)])])


def test_differently_named_team_models_plug_in_via_bindings():
    result = RuleBasedPlanner(OurTeamBindings(), allow_large_text_option=False).plan(
        snapshot(), [finding()], OtherPolicy()
    )
    assert len(result.proposals) == 1
    assert result.proposals[0].prop == Property.FONT_COLOR
    assert result.proposals[0].slide_index == 2
    assert result.standards.purpose == "presentation"  # 정책에 용도가 없으면 기본값


def test_policy_method_is_used_for_locks():
    result = RuleBasedPlanner(OurTeamBindings()).plan(snapshot(), [finding()], OtherPolicy(frozen={(2, 9)}))
    assert result.proposals == []


def test_unreadable_lock_info_stops_instead_of_guessing():
    @dataclass
    class NoLockPolicy:
        strength: str = "strong"

    with pytest.raises(TypeError):
        TeamBindings().is_locked(NoLockPolicy(), 1, 1, Property.FONT_SIZE)


def test_unknown_strength_or_purpose_stops_instead_of_guessing():
    @dataclass
    class P:
        strength: str = "ultra"
        purpose: str = "wedding"

    with pytest.raises(ValueError):
        TeamBindings().strength(P())
    with pytest.raises(ValueError):
        TeamBindings().purpose(P())


def test_missing_geometry_is_kept_as_unknown():
    @dataclass
    class BareShape:
        slide_index: int = 1
        shape_id: int = 1
        runs: tuple = ()

    info = TeamBindings().read_shape(BareShape())
    assert not info.has_geometry
    assert info.kind == "other"
