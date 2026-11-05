"""★ 팀원 모듈과 C 모듈을 잇는 '유일한' 연결 지점 ★

A(스냅샷, domain/models.py)·B(검사 결과, checks/ · 정책, domain/policy.py)의 모델을
C 내부 형태(contracts.py)로 번역합니다. 팀원 모델의 이름이 바뀌면 이 파일만 고치면 됩니다.

두 가지 입력 형태를 받습니다.
  1) 팀 실제 모델 (domain.models.Shape / Finding / DeckSnapshot, domain.policy.AnalysisPolicy)
     - Finding에는 측정값 1개(measured)와 기준값 1개(expected)만 있으므로
       글자색·배경색·필요한 상자 높이처럼 후보 계산에 필요한 값은 스냅샷에서 찾아 채웁니다.
  2) 테스트 대역 (tests/fakes.py의 FakeShape / FakeFinding / FakePolicy)
     - 규칙별 단위 테스트를 간단히 쓰기 위한 형태입니다.
  어느 쪽인지는 속성 이름으로 구분합니다 (예: Finding에 shape_key가 있으면 실제 모델).

원칙: 읽을 수 없는 값은 추측하지 않습니다.
  - 측정값이 빠지면 그 값 없이 처리합니다 (후보를 만들지 않음).
  - 고정 조건·수정 강도를 읽을 수 없으면 오류를 냅니다.
"""

from __future__ import annotations

import re
from typing import ClassVar

from slideguard.planning.color import contrast_ratio
from slideguard.planning.contracts import (
    USER_LOCK_GROUPS,
    ChangeProposal,
    FindingInfo,
    Property,
    Rule,
    RunInfo,
    ShapeInfo,
    Status,
)
from slideguard.planning.standards import DEFAULT_PURPOSE, DEFAULT_SLIDE_HEIGHT_PT

# 객체 주소 "슬라이드번호:도형ID" (예: "3:12").
# 그룹 안 객체·표 칸·차트 글자 등은 "3:12/7:table_cell:2,1"처럼 더 길어서 이 형태가 아닙니다.
_SIMPLE_KEY = re.compile(r"^(\d+):(\d+)$")

# PowerPoint 도형 종류 번호 (MsoShapeType): 11 = 연결된 그림, 13 = 그림
_PICTURE_TYPES = (11, 13)

# 측정값 비교 허용 오차 (B가 명암비를 소수 둘째 자리로 반올림해서 줌)
_RATIO_TOLERANCE = 0.006


def _enum_text(value: object) -> str:
    """Enum이면 .value를, 문자열이면 그대로 꺼내 소문자로."""
    return str(getattr(value, "value", value)).strip().lower()


def _normalize_color(value: object) -> str | None:
    """(255, 136, 0), '#ff8800', 'FF8800' → 'FF8800'. 모르면 None."""
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        if len(value) != 3:
            return None
        try:
            r, g, b = (int(v) for v in value)
        except (TypeError, ValueError):
            return None
        if not all(0 <= v <= 255 for v in (r, g, b)):
            return None
        return f"{r:02X}{g:02X}{b:02X}"
    text = str(value).strip().lstrip("#").upper()
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def _number(value: object) -> float | None:
    """숫자로 읽을 수 있으면 float, 아니면 None (문자열 측정값 등)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_shape_key(key: str) -> tuple[int, int] | None:
    """'3:12' → (3, 12). 그룹 안 객체·표 칸처럼 긴 주소면 None."""
    match = _SIMPLE_KEY.match(str(key))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def shape_key_of(slide_index: int, shape_id: int) -> str:
    return f"{slide_index}:{shape_id}"


class TeamBindings:
    # ------------------------------------------------------------------
    # B(검사 엔진)와 맞출 이름
    # ------------------------------------------------------------------

    # B의 규칙 ID → C 내부 이름 (contracts.Rule)
    # 실제 규칙(checks/): contrast, typography, layout.margin/overflow/overlap/density
    # layout.margin은 상태에 따라 갈림 (_team_rule 참고)
    RULE_NAMES: ClassVar[dict[str, str]] = {
        # 실제 B 규칙 ID
        "typography": Rule.FONT_SIZE,
        "layout.overflow": Rule.OVERFLOW,
        "layout.overlap": Rule.TEXT_OVERLAP,
        "layout.density": Rule.DENSITY,
        # C 내부 이름 그대로 (테스트 대역, 아직 B에 없는 규칙)
        "contrast": Rule.CONTRAST,
        "font_size": Rule.FONT_SIZE,
        "overflow": Rule.OVERFLOW,
        "off_slide": Rule.OFF_SLIDE,
        "hidden_text": Rule.HIDDEN_TEXT,
        "text_overlap": Rule.TEXT_OVERLAP,
        "autofit_shrink": Rule.AUTOFIT_SHRINK,
        "line_spacing": Rule.LINE_SPACING,
        "font_family": Rule.FONT_FAMILY,
        "safe_margin": Rule.SAFE_MARGIN,
        "density": Rule.DENSITY,
        "hierarchy": Rule.HIERARCHY,
    }

    # B의 판정 상태 → C 내부 이름
    # 실제 Finding.severity: "warning" / "review" / "unknown"(판정 불가)
    STATUS_NAMES: ClassVar[dict[str, str]] = {
        "warning": Status.WARNING,
        "review": Status.REVIEW,
        "unknown": Status.UNMEASURABLE,
        "unmeasurable": Status.UNMEASURABLE,
    }

    # (테스트 대역 전용) C 내부 측정값 이름 → FakeFinding.measured 키 이름
    MEASURED_KEYS: ClassVar[dict[str, str]] = {
        # 명암비
        "foreground": "foreground",  # 글자색 "RRGGBB"
        "background": "background",  # 배경색 "RRGGBB" (단색일 때)
        "ratio": "ratio",
        "required_ratio": "required_ratio",
        "background_kind": "background_kind",  # "solid" / "image" / "unknown"
        # 글자 크기
        "size_pt": "size_pt",
        "required_pt": "required_pt",
        # 넘침
        "needed_height": "needed_height",  # 글자를 다 담는 데 필요한 상자 높이(pt)
        # 자동 맞춤 축소
        "specified_size_pt": "specified_size_pt",  # 사용자가 지정한 크기
        "effective_size_pt": "effective_size_pt",  # 자동 맞춤으로 줄어든 실제 크기
        # 가려짐·겹침
        "cover_shape_id": "cover_shape_id",  # 글자를 가린 객체 ID
        "other_shape_id": "other_shape_id",  # 겹친 다른 글자 상자 ID
        # 줄 간격
        "line_spacing": "line_spacing",  # 현재 줄 간격(배수)
        "required_spacing": "required_spacing",
        # 글꼴
        "font_name": "font_name",  # 문제가 된 글꼴 이름
        # 정보 밀도
        "line_count": "line_count",  # 줄(항목) 수
        "max_words": "max_words",  # 가장 긴 줄의 단어 수
        # 시각적 위계
        "title_size_pt": "title_size_pt",
        "body_size_pt": "body_size_pt",
    }
    COLOR_KEYS: ClassVar[tuple[str, ...]] = ("foreground", "background")

    # ------------------------------------------------------------------
    # 정책(B의 domain/policy.py)과 맞출 이름
    # ------------------------------------------------------------------

    # 팀 정책의 수정 수준 이름 → C 내부 이름 (회의록: 최소/중간/최대 수정)
    # 실제 Strength: light / standard / strong
    STRENGTH_NAMES: ClassVar[dict[str, str]] = {
        "light": "passive",
        "standard": "balanced",
        "strong": "aggressive",
        "passive": "passive",
        "balanced": "balanced",
        "aggressive": "aggressive",
        "normal": "balanced",
        "최소": "passive",
        "중간": "balanced",
        "최대": "aggressive",
    }

    # 사용자 지정 체크박스 이름 → contracts.USER_LOCK_GROUPS 키
    USER_LOCK_NAMES: ClassVar[dict[str, str]] = {
        "font_size": "font_size",  # 글자 크기 고정
        "layout": "layout",  # 레이아웃/좌표 고정
        "font_family": "font_family",  # 글씨체 고정
        "color": "color",  # 색 고정
    }

    # C 내부 속성 → 팀 정책의 고정 속성 이름 (domain/policy.py PROPERTIES)
    # 여러 개면 그중 하나라도 고정되어 있으면 바꾸지 않음
    POLICY_PROPERTY_NAMES: ClassVar[dict[str, tuple[str, ...]]] = {
        Property.FONT_SIZE: ("font_size",),
        Property.AUTOFIT: ("font_size",),  # 자동 맞춤 해제도 표시되는 글자 크기를 바꿈
        Property.FONT_COLOR: ("font_color",),
        Property.SHAPE_FILL: ("font_color",),  # 배경판도 글자와 배경의 색 관계를 바꿈
        Property.FONT_FAMILY: ("font_family",),
        Property.SHAPE_POSITION: ("position",),
        Property.Z_ORDER: ("position",),
        Property.SHAPE_HEIGHT: ("size",),
        Property.LINE_SPACING: ("font_size", "size"),  # 글자가 차지하는 높이를 바꿈
    }

    # 권장 글씨체 (회의록: 기본적으로 고딕 권장). 정책의 recommended_font로 바꿀 수 있음
    RECOMMENDED_FONT = "맑은 고딕"

    # 팀 정책의 발표 목적 이름 → standards.PURPOSES 키
    # 실제 Purpose: presentation / lecture / report
    PURPOSE_NAMES: ClassVar[dict[str, str]] = {
        "presentation": "presentation",
        "projector": "presentation",
        "lecture": "lecture",
        "report": "document",  # 보고용 = 개인 화면·인쇄로 읽는 배포 자료
        "online": "online",
        "document": "document",
    }

    # ------------------------------------------------------------------
    # 검사 결과(Finding) 읽기
    # ------------------------------------------------------------------

    def read_finding(self, finding: object, snapshot: object | None = None) -> FindingInfo:
        """snapshot을 주면 Finding에 없는 값(글자색·배경색·필요 높이 등)을 스냅샷에서 채웁니다."""
        if hasattr(finding, "shape_key"):
            return self._read_team_finding(finding, snapshot)
        return self._read_fake_finding(finding)

    def can_patch(self, info: FindingInfo) -> bool:
        """자동 수정할 수 있는 주소인지 (그룹 안 객체·표 칸·차트 글자는 아직 아님)."""
        return info.shape_id >= 0

    def _read_fake_finding(self, finding: object) -> FindingInfo:
        rule_id = str(finding.rule_id)
        measured = getattr(finding, "measured", None) or {}

        values = {}
        for c_key, b_key in self.MEASURED_KEYS.items():
            value = measured.get(b_key)
            if value is None:
                continue
            if c_key in self.COLOR_KEYS:
                value = _normalize_color(value)
                if value is None:
                    continue  # 형식이 이상한 색은 '모름'으로
            values[c_key] = value

        return FindingInfo(
            rule=self.RULE_NAMES.get(rule_id, rule_id),
            slide_index=int(finding.slide_index),
            shape_id=int(finding.shape_id),
            status=self.STATUS_NAMES.get(_enum_text(finding.status), Status.UNKNOWN),
            values=values,
            finding_id=str(getattr(finding, "id", "") or ""),
        )

    def _read_team_finding(self, finding: object, snapshot: object | None) -> FindingInfo:
        rule_id = str(finding.rule_id)
        key = str(finding.shape_key)
        status = self.STATUS_NAMES.get(_enum_text(finding.severity), Status.UNKNOWN)
        rule = self._team_rule(rule_id, status)

        address = parse_shape_key(key)
        if address is None:
            # 긴 주소(그룹 안 객체 등): 슬라이드 번호만 읽고, 도형 ID는 -1 (can_patch=False)
            slide_index, shape_id = int(key.split(":", 1)[0]), -1
        else:
            slide_index, shape_id = address

        shape = None
        if snapshot is not None and address is not None:
            shape = self._find_team_shape(snapshot, key)

        measured = _number(finding.measured)
        expected = _number(finding.expected)
        values: dict = {}

        if rule == Rule.CONTRAST:
            # B: measured = 명암비, expected = 기준 명암비. 색은 스냅샷에서 찾음
            if measured is not None:
                values["ratio"] = measured
            if expected is not None:
                values["required_ratio"] = expected
            if shape is not None:
                values.update(self._contrast_colors(shape, measured))
        elif rule == Rule.FONT_SIZE:
            # B: measured = 글자 크기, expected = 기준 크기
            if measured is not None:
                values["size_pt"] = measured
            if expected is not None:
                values["required_pt"] = expected
        elif rule == Rule.OVERFLOW:
            # B: measured = 넘친 길이. 필요한 상자 높이는 스냅샷에서 계산
            if shape is not None:
                needed = _needed_height(shape)
                if needed is not None:
                    values["needed_height"] = needed
        elif rule == Rule.TEXT_OVERLAP:
            other = _overlap_partner(str(getattr(finding, "id", "")), rule_id, key)
            if other is not None:
                values["other_shape_id"] = other
        elif rule == Rule.DENSITY:
            # B: id 끝이 ":lines"면 줄 수, ":chars"면 글자 수
            finding_id = str(getattr(finding, "id", ""))
            if finding_id.endswith(":lines") and measured is not None:
                values["line_count"] = int(measured)

        return FindingInfo(
            rule=rule,
            slide_index=slide_index,
            shape_id=shape_id,
            status=status,
            values=values,
            finding_id=str(getattr(finding, "id", "") or ""),
        )

    def _team_rule(self, rule_id: str, status: str) -> str:
        if rule_id == "layout.margin":
            # B: 슬라이드 밖으로 나가면 warning, 안전 여백만 침범하면 review
            return Rule.OFF_SLIDE if status == Status.WARNING else Rule.SAFE_MARGIN
        return self.RULE_NAMES.get(rule_id, rule_id)

    def _contrast_colors(self, shape: object, ratio: float | None) -> dict:
        """B가 판정한 명암비와 같은 값이 나오는 글자색을 찾음. 못 찾으면 비워 둠 (추측 금지)."""
        background = _normalize_color(getattr(shape, "background", None))
        if background is None or ratio is None:
            return {}
        for run in getattr(shape, "runs", ()) or ():
            if not str(getattr(run, "text", "")).strip():
                continue
            color = _normalize_color(getattr(run, "color", None))
            if color is None:
                continue
            if abs(contrast_ratio(color, background) - ratio) <= _RATIO_TOLERANCE:
                return {"foreground": color, "background": background, "background_kind": "solid"}
        return {}

    # ------------------------------------------------------------------
    # 스냅샷(A) 읽기
    # ------------------------------------------------------------------

    def slide_size(self, snapshot: object) -> tuple[float, float]:
        width = getattr(snapshot, "width_pt", None)
        height = getattr(snapshot, "height_pt", None)
        if width is None or height is None:
            width = getattr(snapshot, "slide_width", None)  # 테스트 대역
            height = getattr(snapshot, "slide_height", None)
        if width is None or height is None:
            return (960.0, DEFAULT_SLIDE_HEIGHT_PT)  # PowerPoint 16:9 기본 크기
        return (float(width), float(height))

    def shapes_on_slide(self, snapshot: object, slide_index: int) -> list[ShapeInfo]:
        """충돌 검사에 쓸 같은 슬라이드의 객체들 (숨긴 객체·그룹 안 객체 제외)."""
        result = []
        for shape in snapshot.shapes:
            if _slide_of(shape) != slide_index:
                continue
            if _is_team_shape(shape):
                if getattr(shape, "effective_visible", None) is False:
                    continue
                if parse_shape_key(shape.key) is None:
                    continue  # 그룹 안 객체는 그룹 전체 상자로 대신함
            result.append(self.read_shape(shape))
        return result

    def find_shape(self, snapshot: object, slide_index: int, shape_id: int) -> ShapeInfo | None:
        for shape in snapshot.shapes:
            if _slide_of(shape) != slide_index or shape.shape_id != shape_id:
                continue
            if _is_team_shape(shape) and parse_shape_key(shape.key) is None:
                continue  # 같은 ID를 가진 표 칸·차트 글자 등은 건너뜀
            return self.read_shape(shape)
        return None

    def _find_team_shape(self, snapshot: object, key: str) -> object | None:
        for shape in getattr(snapshot, "shapes", ()):
            if getattr(shape, "key", None) == key:
                return shape
        return None

    def read_shape(self, shape: object) -> ShapeInfo:
        if _is_team_shape(shape):
            return self._read_team_shape(shape)
        return self._read_fake_shape(shape)

    def _read_team_shape(self, shape: object) -> ShapeInfo:
        runs = tuple(
            RunInfo(
                size_pt=_number(run.font_size_pt),
                color=_normalize_color(run.color),
                bold=run.bold,
            )
            for run in shape.runs
        )
        if shape.shape_type in _PICTURE_TYPES:
            kind = "picture"
        elif runs or str(shape.text).strip():
            kind = "text"
        else:
            kind = "other"
        box = shape.box  # 도형 상자 (patcher가 바꾸는 Left/Top/Width/Height와 같은 기준)
        return ShapeInfo(
            slide_index=int(shape.slide_number),
            shape_id=int(shape.shape_id),
            kind=kind,
            supported=bool(shape.supported) and parse_shape_key(shape.key) is not None,
            runs=runs,
            left=None if box is None else float(box.x),
            top=None if box is None else float(box.y),
            width=None if box is None else float(box.width),
            height=None if box is None else float(box.height),
        )

    def _read_fake_shape(self, shape: object) -> ShapeInfo:
        runs = []
        for run in getattr(shape, "runs", ()) or ():
            runs.append(
                RunInfo(
                    size_pt=None if run.size_pt is None else float(run.size_pt),
                    color=_normalize_color(run.color),
                    bold=run.bold,
                )
            )
        kind = getattr(shape, "kind", None)
        if kind is None:
            kind = "text" if runs else "other"

        def number(name):
            value = getattr(shape, name, None)
            return None if value is None else float(value)

        return ShapeInfo(
            slide_index=int(shape.slide_index),
            shape_id=int(shape.shape_id),
            kind=str(kind),
            supported=bool(getattr(shape, "supported", True)),
            runs=tuple(runs),
            left=number("left"),
            top=number("top"),
            width=number("width"),
            height=number("height"),
        )

    # ------------------------------------------------------------------
    # 정책 읽기
    # ------------------------------------------------------------------

    def is_locked(self, policy: object, slide_index: int, shape_id: int, prop: str) -> bool:
        # 1) 사용자 지정 체크박스 (글자 크기 고정, 레이아웃 고정 …)
        if prop in self.user_locked_properties(policy):
            return True
        # 2) 팀 실제 정책: allows_change(shape_key, 속성) → 객체 고정·속성 고정·슬라이드 범위를 한 번에 확인
        if hasattr(policy, "allows_change"):
            team_props = self.POLICY_PROPERTY_NAMES.get(prop)
            if not team_props:
                return True  # 정책에서 확인할 수 없는 속성은 바꾸지 않음
            key = shape_key_of(slide_index, shape_id)
            return any(not policy.allows_change(key, name) for name in team_props)
        # 3) 정책에 is_locked 메서드가 있으면 그대로 사용
        if hasattr(policy, "is_locked"):
            return bool(policy.is_locked(slide_index, shape_id, prop))
        # 4) (테스트 대역) 객체·속성 단위 고정
        has_shapes = hasattr(policy, "locked_shapes")
        has_props = hasattr(policy, "locked_properties")
        if has_shapes or has_props or hasattr(policy, "user_locks"):
            if has_shapes and (slide_index, shape_id) in policy.locked_shapes:
                return True
            return bool(has_props and prop in policy.locked_properties)
        raise TypeError(
            "정책에서 고정 항목을 읽을 수 없습니다. TeamBindings.is_locked를 실제 정책에 맞추세요."
        )

    def user_locked_properties(self, policy: object) -> set[str]:
        result = set()
        for raw in getattr(policy, "user_locks", None) or ():
            name = _enum_text(raw)
            if name not in self.USER_LOCK_NAMES:
                raise ValueError(
                    f"알 수 없는 사용자 고정 항목: {name!r}. TeamBindings.USER_LOCK_NAMES에 추가하세요."
                )
            result.update(USER_LOCK_GROUPS[self.USER_LOCK_NAMES[name]])
        return result

    def excluded_rules(self, policy: object) -> set[str]:
        """사용자가 검사에서 뺀 규칙 (회의록: 명암비 검사 제외 등). C 내부 규칙 이름으로."""
        result = set()
        for raw in getattr(policy, "excluded_rules", None) or ():
            name = str(getattr(raw, "value", raw))
            result.add(self.RULE_NAMES.get(name, name))
        return result

    def recommended_font(self, policy: object) -> str:
        return str(getattr(policy, "recommended_font", None) or self.RECOMMENDED_FONT)

    def strength(self, policy: object) -> str:
        name = _enum_text(policy.strength)
        if name not in self.STRENGTH_NAMES:
            raise ValueError(f"알 수 없는 수정 강도: {name!r}. TeamBindings.STRENGTH_NAMES에 추가하세요.")
        return self.STRENGTH_NAMES[name]

    def purpose(self, policy: object) -> str:
        """정책에 용도가 없으면 기본값(발표). 이름을 모르면 추측하지 않고 오류."""
        raw = getattr(policy, "purpose", None)
        if raw is None:
            return DEFAULT_PURPOSE
        name = _enum_text(raw)
        if name not in self.PURPOSE_NAMES:
            raise ValueError(f"알 수 없는 발표 목적: {name!r}. TeamBindings.PURPOSE_NAMES에 추가하세요.")
        return self.PURPOSE_NAMES[name]

    def standard_overrides(self, policy: object) -> dict:
        """사용자가 바꾼 기준값·시청 조건 (standards.build_standards의 overrides)"""
        return dict(getattr(policy, "standard_overrides", None) or {})

    # ------------------------------------------------------------------
    # 수정 후보 ↔ 팀 모델 (domain.models.ChangeProposal)
    # ------------------------------------------------------------------
    # 팀 ChangeProposal에는 묶음(group) 칸이 없어서 id에 함께 적습니다.
    #   id = "<속성>@<shape_key>"  또는  "<속성>@<shape_key>#<묶음 이름>"
    # property에는 C 내부 속성 이름(Property.*)을 그대로 씁니다.
    # before/after는 속성에 따라 숫자·색 문자열·(left, top)·{"color", "transparency"} 입니다.

    def to_team_proposal(self, proposal: ChangeProposal) -> object:
        from slideguard.domain.models import ChangeProposal as TeamChangeProposal

        key = shape_key_of(proposal.slide_index, proposal.shape_id)
        proposal_id = f"{proposal.prop}@{key}"
        if proposal.group:
            proposal_id += f"#{proposal.group}"
        return TeamChangeProposal(
            id=proposal_id,
            finding_id=proposal.finding_id,
            shape_key=key,
            property=proposal.prop,
            before=proposal.before,
            after=proposal.after,
            cost=proposal.cost,
        )

    def from_team_proposal(self, proposal: object) -> ChangeProposal:
        address = parse_shape_key(proposal.shape_key)
        if address is None:
            raise ValueError(f"자동 수정할 수 없는 객체 주소입니다: {proposal.shape_key!r}")
        group = None
        if "#" in str(proposal.id):
            group = str(proposal.id).split("#", 1)[1]
        before, after = proposal.before, proposal.after
        if proposal.property == Property.SHAPE_POSITION:
            before, after = tuple(before), tuple(after)  # JSON 저장 후 list로 돌아와도 비교되게
        return ChangeProposal(
            slide_index=address[0],
            shape_id=address[1],
            prop=str(proposal.property),
            before=before,
            after=after,
            cost=float(proposal.cost),
            rule="",
            group=group,
            finding_id=str(proposal.finding_id),
        )


# ======================================================================
# 내부 함수 (실제 모델 전용)
# ======================================================================


def _is_team_shape(shape: object) -> bool:
    return hasattr(shape, "slide_number")


def _slide_of(shape: object) -> int:
    if _is_team_shape(shape):
        return int(shape.slide_number)
    return int(shape.slide_index)


def _needed_height(shape: object) -> float | None:
    """글자를 다 담는 데 필요한 상자 높이 = 실제 글자 높이 + 위아래 안쪽 여백.

    도형 상자(box), 글자 영역(rendered_box), 안쪽 여백을 뺀 영역(content_box)을
    모두 알 때만 계산합니다. 하나라도 모르면 None.
    """
    box = getattr(shape, "box", None)
    rendered = getattr(shape, "rendered_box", None)
    content = getattr(shape, "content_box", None)
    if box is None or rendered is None or content is None:
        return None
    margins = box.height - content.height
    if margins < 0:
        return None
    return round(rendered.height + margins, 2)


def _overlap_partner(finding_id: str, rule_id: str, anchor_key: str) -> int | None:
    """B의 겹침 Finding id "layout.overlap:<a.key>:<b.key>"에서 상대 객체 ID를 찾음."""
    prefix = f"{rule_id}:"
    if not finding_id.startswith(prefix):
        return None
    rest = finding_id[len(prefix) :]
    other_key = None
    if rest.startswith(anchor_key + ":"):
        other_key = rest[len(anchor_key) + 1 :]
    elif rest.endswith(":" + anchor_key):
        other_key = rest[: -len(anchor_key) - 1]
    if other_key is None:
        return None
    address = parse_shape_key(other_key)
    return None if address is None else address[1]
