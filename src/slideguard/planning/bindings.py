"""★ 팀원 모듈과 C 모듈을 잇는 '유일한' 연결 지점 ★

A(스냅샷)·B(검사 결과, 정책)의 실제 코드가 생기면 이 파일만 고치면 됩니다.
기본 구현은 ARCHITECTURE.md 표를 보고 '가정한 이름'입니다.
이름이 다르면 이 파일의 이름표를 고치거나, TeamBindings를 상속해 필요한 메서드만 덮어쓰세요.

원칙: 읽을 수 없는 값은 추측하지 않습니다.
  - 측정값이 빠지면 그 값 없이 처리합니다 (후보를 만들지 않음).
  - 고정 조건·수정 강도를 읽을 수 없으면 오류를 냅니다.
"""

from __future__ import annotations

from typing import ClassVar

from slideguard.planning.contracts import USER_LOCK_GROUPS, FindingInfo, RunInfo, ShapeInfo, Status
from slideguard.planning.standards import DEFAULT_PURPOSE, DEFAULT_SLIDE_HEIGHT_PT


def _enum_text(value: object) -> str:
    """Enum이면 .value를, 문자열이면 그대로 꺼내 소문자로."""
    return str(getattr(value, "value", value)).strip().lower()


def _normalize_color(value: object) -> str | None:
    """'#ff8800', 'FF8800' → 'FF8800'. 모르면 None."""
    if value is None:
        return None
    text = str(value).strip().lstrip("#").upper()
    if len(text) != 6:
        return None
    return text


class TeamBindings:
    # ------------------------------------------------------------------
    # B(검사 엔진)와 맞출 이름
    # ------------------------------------------------------------------

    # B의 규칙 ID → C 내부 이름 (contracts.Rule)
    RULE_NAMES: ClassVar[dict[str, str]] = {
        "contrast": "contrast",
        "font_size": "font_size",
        "overflow": "overflow",
        "off_slide": "off_slide",
        "hidden_text": "hidden_text",
        "text_overlap": "text_overlap",
        "autofit_shrink": "autofit_shrink",
        "line_spacing": "line_spacing",
        "font_family": "font_family",
        "safe_margin": "safe_margin",
        "density": "density",
        "hierarchy": "hierarchy",
    }

    # B의 판정 상태 → C 내부 이름
    STATUS_NAMES: ClassVar[dict[str, str]] = {
        "warning": Status.WARNING,
        "review": Status.REVIEW,
        "unmeasurable": Status.UNMEASURABLE,
    }

    # C 내부 측정값 이름 → B의 Finding.measured 키 이름
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
    STRENGTH_NAMES: ClassVar[dict[str, str]] = {
        "passive": "passive",
        "balanced": "balanced",
        "aggressive": "aggressive",
        "최소": "passive",
        "중간": "balanced",
        "최대": "aggressive",
        "light": "passive",
        "normal": "balanced",
        "strong": "aggressive",
    }

    # 사용자 지정 체크박스 이름 → contracts.USER_LOCK_GROUPS 키
    USER_LOCK_NAMES: ClassVar[dict[str, str]] = {
        "font_size": "font_size",  # 글자 크기 고정
        "layout": "layout",  # 레이아웃/좌표 고정
        "font_family": "font_family",  # 글씨체 고정
        "color": "color",  # 색 고정
    }

    # 권장 글씨체 (회의록: 기본적으로 고딕 권장). 정책의 recommended_font로 바꿀 수 있음
    RECOMMENDED_FONT = "맑은 고딕"

    # 팀 정책의 발표 목적 이름 → standards.PURPOSES 키
    PURPOSE_NAMES: ClassVar[dict[str, str]] = {
        "presentation": "presentation",
        "projector": "presentation",
        "lecture": "lecture",
        "online": "online",
        "document": "document",
    }

    # ------------------------------------------------------------------
    # 검사 결과(Finding) 읽기
    # ------------------------------------------------------------------

    def read_finding(self, finding: object) -> FindingInfo:
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
        )

    # ------------------------------------------------------------------
    # 스냅샷(A) 읽기
    # ------------------------------------------------------------------

    def slide_size(self, snapshot: object) -> tuple[float, float]:
        width = getattr(snapshot, "slide_width", None)
        height = getattr(snapshot, "slide_height", None)
        if width is None or height is None:
            return (960.0, DEFAULT_SLIDE_HEIGHT_PT)  # PowerPoint 16:9 기본 크기
        return (float(width), float(height))

    def shapes_on_slide(self, snapshot: object, slide_index: int) -> list[ShapeInfo]:
        result = []
        for shape in snapshot.shapes:
            if shape.slide_index == slide_index:
                result.append(self.read_shape(shape))
        return result

    def find_shape(self, snapshot: object, slide_index: int, shape_id: int) -> ShapeInfo | None:
        for shape in snapshot.shapes:
            if shape.slide_index == slide_index and shape.shape_id == shape_id:
                return self.read_shape(shape)
        return None

    def read_shape(self, shape: object) -> ShapeInfo:
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
        # 2) 정책에 is_locked 메서드가 있으면 그대로 사용
        if hasattr(policy, "is_locked"):
            return bool(policy.is_locked(slide_index, shape_id, prop))
        # 3) 객체·속성 단위 고정
        has_shapes = hasattr(policy, "locked_shapes")
        has_props = hasattr(policy, "locked_properties")
        if has_shapes or has_props or hasattr(policy, "user_locks"):
            if has_shapes and (slide_index, shape_id) in policy.locked_shapes:
                return True
            return bool(has_props and prop in policy.locked_properties)
        raise TypeError("정책에서 고정 항목을 읽을 수 없습니다. TeamBindings.is_locked를 실제 정책에 맞추세요.")

    def user_locked_properties(self, policy: object) -> set[str]:
        result = set()
        for raw in getattr(policy, "user_locks", None) or ():
            name = _enum_text(raw)
            if name not in self.USER_LOCK_NAMES:
                raise ValueError(f"알 수 없는 사용자 고정 항목: {name!r}. TeamBindings.USER_LOCK_NAMES에 추가하세요.")
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
