"""수정 후보 생성기 (C 담당).

입력: 팀원의 스냅샷·검사 결과·정책 (모양은 bindings.py가 번역)
출력: PlanResult (채택 후보 + 대안 + 고치지 않은 이유 + 사용한 기준값)

1) 문제를 세 가지로 분류합니다.
   - 가독성(READABILITY): 누가 봐도 읽기 어려운 문제
       예) 회색 본문, 작은 글씨, 넘침, 슬라이드 밖·가장자리, 가려짐, 자동 축소, 좁은 줄 간격
   - 디자인(DESIGN): 고치면 디자인 의도가 바뀌는 문제
       예) 브랜드색·강조색 글씨, 사진 위 글씨, 장식 글꼴, 제목·본문 위계
   - 검토 전용(REVIEW_ONLY): 자동으로 고치지 않음
       예) 정보 과다(6x6) → 내용 압축·분할은 사람이 판단
           글상자끼리 겹침 → 최소·중간에서는 검토 전용, 최대에서는 디자인 문제로 보고 떼어 놓음

2) 수정 수준 (회의록 결정, STRENGTH_PROFILES)
   - passive (최소)   : 글자 크기·글자 색만 바꿈. 디자인 문제는 그대로 둠
   - balanced (중간)  : + 글씨체·항목 위치. 디자인 문제는 작은 변경으로 해결될 때만
   - aggressive (최대): + 배경색·서식(줄 간격·자동 맞춤)·제목. 디자인 의도보다 가독성을 우선해
                       사진 위 글씨·겹침·넘친 글자와 그림의 충돌까지 고침
                       (겹친 글상자 떼어 놓기, 아래 그림 밀어내기 — 의도적 겹침 계약의 예외, 팀 결정)
   모든 수준에서 '기준을 넘는 범위 안에서 원래 모습에 가장 가까운' 후보를 고릅니다 (글자색도 원래 색 쪽으로).
   기준값은 C가 정하지 않고 모두 B에서 가져옵니다 (Finding.expected → domain/policy.py, standards.py 참고).
   + 사용자 지정: 글자 크기 고정 / 레이아웃 고정 / 검사 제외 (bindings 참고)
   어떤 수준에서도 통과 기준은 낮추지 않습니다. 고치지 않은 문제는 이유와 함께 남습니다.

3) 지켜야 할 계약
   - 고정 항목은 후보를 만들지 않음
   - 측정 불가를 '정상'으로 가정하지 않음
     (사진·그라데이션 위 글씨는 배경을 모르는 채로도 '최악의 배경'에서 기준을 넘는 배경판만 제안.
      B가 렌더링 픽셀로 근사 판정해 '검토 필요'로 보낸 경우도 같음)
   - 의도적 겹침은 자동 수정·이동하지 않음
   - 파일·COM·UI에 접근하지 않음 (순수 계산)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from slideguard.domain.policy import GEOMETRY_TOLERANCE_PT, AnalysisPolicy, FixTargets, FontGoal
from slideguard.planning.bindings import TeamBindings, table_of_cell
from slideguard.planning.color import (
    color_distance,
    contrast_ratio,
    cud_color,
    cud_distinct_colors,
    harmony_score,
    nearest_passing_color,
    relative_luminance,
    saturation,
    vision_ratio,
    worst_contrast_over_unknown,
)
from slideguard.planning.contracts import (
    Category,
    ChangeProposal,
    FindingInfo,
    PlanResult,
    Property,
    Rule,
    ShapeInfo,
    SkippedFinding,
    Status,
)
from slideguard.planning.reflow import MIN_GROW, reflow_slide
from slideguard.planning.same_place import extend_same_place
from slideguard.planning.standards import Standards, build_standards

# ======================================================================
# 수정 수준 (회의록: 최소 / 중간 / 최대 수정)
# ======================================================================

# 속성 → 회의록의 수정 항목 이름
PROPERTY_GROUP = {
    Property.FONT_SIZE: "글자 크기",
    Property.FONT_COLOR: "글자 색",
    Property.FONT_FAMILY: "글씨체",
    Property.FONT_BOLD: "글씨체",
    Property.SHAPE_POSITION: "항목 위치",
    Property.SHAPE_HEIGHT: "항목 위치",
    Property.SHAPE_WIDTH: "항목 위치",
    Property.Z_ORDER: "항목 위치",
    Property.SHAPE_FILL: "배경색",
    Property.AUTOFIT: "서식",
    Property.LINE_SPACING: "서식",
}

_PASSIVE = frozenset({Property.FONT_SIZE, Property.FONT_COLOR})
_BALANCED = _PASSIVE | {
    Property.FONT_FAMILY,
    Property.FONT_BOLD,
    Property.SHAPE_POSITION,
    Property.SHAPE_HEIGHT,
    Property.SHAPE_WIDTH,
    Property.Z_ORDER,
}
_AGGRESSIVE = _BALANCED | {Property.SHAPE_FILL, Property.AUTOFIT, Property.LINE_SPACING}


@dataclass(frozen=True)
class StrengthProfile:
    label: str
    allowed_props: frozenset  # 이 수준에서 바꿀 수 있는 속성
    fix_design: bool  # 디자인 문제를 고칠지
    design_budget: float  # 디자인 문제 후보 1개의 최대 변경 비용 (0~1)
    resolve_collisions: bool  # True면 겹침·충돌을 디자인 의도로 보지 않고 객체를 옮겨 해결


STRENGTH_PROFILES = {
    "passive": StrengthProfile(
        "최소 수정", _PASSIVE, fix_design=False, design_budget=0.0, resolve_collisions=False
    ),
    "balanced": StrengthProfile(
        "중간 수정", _BALANCED, fix_design=True, design_budget=0.15, resolve_collisions=False
    ),
    "aggressive": StrengthProfile(
        "최대 수정", _AGGRESSIVE, fix_design=True, design_budget=1.0, resolve_collisions=True
    ),
}
LEVEL_ORDER = ("passive", "balanced", "aggressive")

# 채도가 이 값 이상인 글자색은 브랜드색·강조색으로 보고 '디자인'으로 분류 (팀 결정)
CHROMATIC_SATURATION = 0.3
# 앞뒤 순서만 바꾸는 변경의 비용 (색·크기·위치는 그대로라 작게 잡음)
Z_ORDER_COST = 0.05
# 글씨체 교체 비용 (분위기가 바뀌는 큰 변경 → 중간 수정 예산으로는 안 됨, 팀 결정)
FONT_FAMILY_COST = 0.3
# 표준 팔레트 (회의록: 가독성 높은 기본 텍스트 색 = 진회색 계열)
PALETTE_DARK = "333333"
PALETTE_LIGHT = "F2F2F2"
# 좌표 비교 허용 오차 (pt) — B 기준 (domain/policy.py)
TOLERANCE = GEOMETRY_TOLERANCE_PT

REVIEW_REASONS = {
    Rule.TEXT_OVERLAP: "겹침은 의도한 배치일 수 있어 사람이 판단 (최대 수정에서는 자동으로 떼어 놓음)",
}


class RuleBasedPlanner:
    def __init__(
        self,
        bindings: TeamBindings | None = None,
        standards: Standards | None = None,
        allow_large_text_option: bool = True,
    ) -> None:
        self.bindings = bindings or TeamBindings()
        # standards를 넘기면 그 기준을 쓰고, 없으면 정책의 용도로 B 기준을 가져옵니다.
        self.standards = standards
        # 색약 맞춤 수정에서 고려할 눈 (설정의 색각 유형). plan()이 정책에서 읽어 채운다. None = 세 가지 모두
        self.vision_kinds: tuple[str, ...] | None = None
        self.allow_large_text_option = allow_large_text_option

    # ==================================================================
    # 공개 메서드
    # ==================================================================

    def plan(self, snapshot: object, findings: list, policy: object) -> PlanResult:
        """최소 수정과 권장 수정 두 가지 후보를 만든다.

        최소 수정: 기준을 겨우 넘기는 후보 (FixTargets.minimum()) → result.proposals
        권장 수정: 정책의 FixTargets 목표까지 고친 후보 중 최소 수정과 값이 다른 것 → result.recommended
        """
        standards = self.standards
        if isinstance(policy, AnalysisPolicy) and policy.vision_kinds:
            self.vision_kinds = policy.vision_kinds  # 예: 적록색약 → ("protan", "deutan")
        else:
            self.vision_kinds = None
        if standards is None:
            standards = build_standards(
                self.bindings.purpose(policy), policy if isinstance(policy, AnalysisPolicy) else None
            )
        result = self._plan_with(snapshot, findings, policy, replace(standards, fix=FixTargets.minimum()))
        result.standards = standards
        if standards.fix.offers_recommended:
            extra = self._plan_with(snapshot, findings, policy, standards)
            result.recommended = _recommended_only(result.proposals, extra.proposals)
        return result

    def _plan_with(
        self, snapshot: object, findings: list, policy: object, standards: Standards
    ) -> PlanResult:
        level = self.bindings.strength(policy)
        profile = STRENGTH_PROFILES[level]
        excluded = self.bindings.excluded_rules(policy)
        slide_w, slide_h = self.bindings.slide_size(snapshot)
        context = {
            "standards": standards,
            "slide_w": slide_w,
            "slide_h": slide_h,
            "font": self.bindings.recommended_font(policy),
        }

        result = PlanResult(standards=standards, strength=level)
        overflows = []  # 슬라이드 재배치에 넘길 넘침 판정
        table_fonts: dict[tuple[int, int], list] = {}  # (슬라이드, 표 도형) → 칸별 글자 크기 판정

        for raw in findings:
            info = self.bindings.read_finding(raw, snapshot)
            category = self._classify(info, profile)
            if info.rule == Rule.OVERFLOW and info.rule not in excluded and info.status == Status.WARNING:
                overflows.append(info)

            def skip(reason, raw=raw, category=category):
                result.skipped.append(SkippedFinding(raw, reason, category))

            # ---- 1. 자동으로 고치지 않는 경우 -------------------------
            if info.rule in excluded:
                skip("사용자 설정으로 이 검사 항목을 제외함")
                continue
            if category == Category.REVIEW_ONLY:
                skip(self._review_reason(info, standards))
                continue
            if info.status == Status.REVIEW and not (
                _is_photo_text(info)
                or info.values.get("confirmed_hierarchy")
                or info.values.get("color_vision")
                or info.rule in CONSISTENCY_FIXES
                or info.rule == COLOR_ONLY_RULE
            ):
                skip("사람의 검토가 필요한 항목")
                continue
            if info.status == Status.UNMEASURABLE and not _is_photo_text(info):
                skip("측정 불가 항목은 자동 수정하지 않음")
                continue
            if info.status == Status.UNKNOWN:
                skip("알 수 없는 판정 상태")
                continue

            table = table_of_cell(getattr(raw, "shape_key", ""))
            if table is not None and info.rule == Rule.FONT_SIZE and info.status == Status.WARNING:
                # 표 칸 글자 크기는 칸마다 고치지 않고 표 하나로 모아 '표 글자 크기'를 한 번에 키운다
                table_fonts.setdefault(table, []).append((raw, info))
                continue
            if not self.bindings.can_patch(info):
                skip("그룹 안 객체·표 칸·차트 글자는 아직 자동 수정하지 않음")
                continue
            shape = self.bindings.find_shape(snapshot, info.slide_index, info.shape_id)
            if shape is None:
                skip("스냅샷에 대상 객체가 없음")
                continue
            if not shape.supported:
                skip("지원하지 않는 객체 종류")
                continue

            if category == Category.DESIGN and not profile.fix_design:
                skip(f"디자인 영역으로 보고 유지 ({profile.label}: 디자인 요소는 고치지 않음)")
                continue

            # ---- 2. 후보 만들기 -------------------------------------
            others = self.bindings.shapes_on_slide(snapshot, info.slide_index)
            candidates, why = self._candidates(info, shape, others, context, category, profile)
            if not candidates:
                skip(why)
                continue

            # 후보 하나 = 수정안 하나. 수정안은 변경 1개 또는 함께 적용할 변경 묶음(list)
            bundles = [c if isinstance(c, list) else [c] for c in candidates]

            # ---- 3. 수정 수준에서 허용하는 속성만 ----------------------
            allowed = [b for b in bundles if all(p.prop in profile.allowed_props for p in b)]
            if not allowed:
                skip(_level_reason([p for b in bundles for p in b], profile))
                continue

            # ---- 4. 고정 항목 제외 (묶음 중 하나라도 고정이면 제외) -------
            unlocked = []
            for bundle in allowed:
                locked = False
                for p in bundle:
                    if self.bindings.is_locked(policy, p.slide_index, p.shape_id, p.prop):
                        locked = True
                if not locked:
                    unlocked.append(bundle)
            if not unlocked:
                skip("고정된 객체·속성이라 후보를 만들지 않음")
                continue

            # ---- 5. 디자인 문제는 수준별 예산 안에서만 -----------------
            # (기준을 낮춘 '덜 고친' 후보는 만들지 않음)
            affordable = unlocked
            if category == Category.DESIGN:
                affordable = [b for b in unlocked if _bundle_cost(b) <= profile.design_budget]
                if not affordable:
                    cheapest = min(_bundle_cost(b) for b in unlocked)
                    skip(
                        f"디자인 영역 수정 예산 초과 (필요 비용 {cheapest:.2f} > "
                        f"{profile.label} 예산 {profile.design_budget:.2f})"
                    )
                    continue

            # ---- 6. 채택: 변경이 가장 작은 수정안 (원래 모습에 가장 가깝게) --
            affordable.sort(key=_bundle_cost)
            result.proposals.extend(affordable[0])
            for bundle in affordable[1:]:
                result.alternatives.extend(bundle)

        for (slide, table_id), cells in table_fonts.items():
            self._table_font(snapshot, slide, table_id, cells, standards, profile, policy, result)
        result.proposals = _merge_same_target(result.proposals, result)
        self._reflow(snapshot, overflows, result, profile, standards, slide_h)
        return result

    # ==================================================================
    # 슬라이드 재배치 (planning/reflow.py)
    # ==================================================================

    def _reflow(self, snapshot, overflows, result, profile, standards, slide_h):
        """글자 키우기·넘침 고치기를 슬라이드 단위로 다시 계산해 한 묶음으로 만든다.

        - 글자를 키우면 상자가 모자랄 글상자는 상자도 늘리고, 그 아래 객체는 늘어난 만큼 내린다.
        - 넘친 글상자도 같은 방법으로 늘린다 (아래 객체가 막아도 함께 내려서 해결).
        - 슬라이드에 다 들어가지 않으면 묶음을 만들지 않고 글자 크기 후보에 이유를 붙인다.
        """
        if not {Property.SHAPE_HEIGHT, Property.SHAPE_POSITION} <= profile.allowed_props:
            return
        slides = {p.slide_index for p in result.proposals if p.prop == Property.FONT_SIZE}
        slides |= {info.slide_index for info in overflows}
        for slide in sorted(slides):
            items = self.bindings.flow_items(snapshot, slide)
            if not items:
                continue
            fonts = [p for p in result.proposals if p.slide_index == slide and p.prop == Property.FONT_SIZE]
            new_sizes = {p.shape_id: float(p.after) for p in fonts}
            needed = {
                info.shape_id: float(info.values["needed_height"])
                for info in overflows
                if info.slide_index == slide and info.values.get("needed_height") is not None
            }
            unmeasured = {
                info.shape_id
                for info in overflows
                if info.slide_index == slide and info.values.get("needed_height") is None
            }
            flow = reflow_slide(
                items,
                new_sizes,
                needed,
                unmeasured,
                slide_h=slide_h,
                bottom_margin=slide_h * standards.safe_margin_ratio.value,
            )
            if not flow.ok:
                warned = {p.shape_id for p in fonts}
                result.proposals = [
                    replace(p, note=f"{p.note} (주의: {flow.reason})")
                    if p.slide_index == slide and p.prop == Property.FONT_SIZE and p.shape_id in warned
                    else p
                    for p in result.proposals
                ]
                continue
            if not flow.changes:
                continue
            self._apply_reflow(slide, flow, items, fonts, overflows, result, slide_h)

    def _apply_reflow(self, slide, flow, items, fonts, overflows, result, slide_h):
        group = f"reflow:{slide}"
        by_id = {item.shape_id: item for item in items}
        touched = set(flow.heights) | set(flow.tops)
        # 같은 객체의 높이·위치를 따로 바꾸던 후보(와 그 묶음)는 재배치가 대신한다
        replaced_groups = {
            p.group
            for p in result.proposals
            if p.slide_index == slide
            and p.shape_id in touched
            and p.prop in (Property.SHAPE_HEIGHT, Property.SHAPE_POSITION)
            and p.group
        }
        result.proposals = [
            p
            for p in result.proposals
            if not (
                p.slide_index == slide
                and (
                    (p.shape_id in touched and p.prop in (Property.SHAPE_HEIGHT, Property.SHAPE_POSITION))
                    or (p.group and p.group in replaced_groups)
                )
            )
        ]
        # 원인 판정: 상자가 늘어난 글상자의 글자 크기 후보 또는 넘침 판정
        overflow_by_shape = {info.shape_id: info for info in overflows if info.slide_index == slide}
        font_by_shape = {p.shape_id: p for p in fonts}
        trigger = next(
            (
                font_by_shape[sid].finding_id if sid in font_by_shape else overflow_by_shape[sid].finding_id
                for sid in sorted(flow.heights, key=lambda sid: by_id[sid].top)
                if sid in font_by_shape or sid in overflow_by_shape
            ),
            "",
        )
        # 상자가 늘어나는 글상자의 글자 크기 후보는 재배치와 한 묶음 (따로 켜면 넘침)
        result.proposals = [
            replace(p, group=group) if p in fonts and p.shape_id in flow.heights else p
            for p in result.proposals
        ]
        for sid in sorted(touched, key=lambda sid: by_id[sid].top):
            item = by_id[sid]
            cause = font_by_shape.get(sid)
            finding_id = (
                cause.finding_id
                if cause is not None
                else (overflow_by_shape[sid].finding_id if sid in overflow_by_shape else trigger)
            )
            if sid in flow.heights:
                why = f"글자 {float(cause.after):g}pt에 맞게" if cause is not None else "넘친 글자를 담게"
                if cause is None and sid not in overflow_by_shape:
                    why = "안의 글상자가 늘어난 만큼"
                result.proposals.append(
                    ChangeProposal(
                        slide_index=slide,
                        shape_id=sid,
                        prop=Property.SHAPE_HEIGHT,
                        before=item.height,
                        after=flow.heights[sid],
                        cost=(flow.heights[sid] - item.height) / slide_h,
                        rule=Rule.OVERFLOW,
                        note=f"슬라이드 재배치: 상자 높이 {item.height:g}pt → {flow.heights[sid]:g}pt ({why})",
                        group=group,
                        finding_id=finding_id,
                    )
                )
            if sid in flow.tops:
                down = flow.tops[sid] - item.top
                result.proposals.append(
                    ChangeProposal(
                        slide_index=slide,
                        shape_id=sid,
                        prop=Property.SHAPE_POSITION,
                        before=(item.left, item.top),
                        after=(item.left, flow.tops[sid]),
                        cost=down / slide_h,
                        rule=Rule.OVERFLOW,
                        note=f"슬라이드 재배치: 위 상자가 늘어난 만큼 {down:g}pt 내림 (간격 유지)",
                        group=group,
                        finding_id=finding_id,
                    )
                )
        # 재배치로 해결된 넘침은 '사람이 판단' 목록에서 뺀다
        solved = {overflow_by_shape[sid].finding_id for sid in flow.heights if sid in overflow_by_shape}
        result.skipped = [s for s in result.skipped if getattr(s.finding, "id", None) not in solved]

    # ==================================================================
    # 분류
    # ==================================================================

    def _classify(self, info: FindingInfo, profile: StrengthProfile) -> str:
        if info.rule == Rule.TEXT_OVERLAP:
            # 최대 수정에서는 겹침을 디자인 의도로 보지 않고 고칠 대상으로 봄
            return Category.DESIGN if profile.resolve_collisions else Category.REVIEW_ONLY
        if info.rule == Rule.DENSITY:
            return Category.REVIEW_ONLY
        if (
            info.rule == Rule.HIERARCHY
            and info.status == Status.WARNING
            and info.values.get("confirmed_hierarchy")
        ):
            return Category.READABILITY
        if info.rule in (Rule.FONT_FAMILY, Rule.HIERARCHY):
            return Category.DESIGN
        if info.rule == Rule.CONTRAST:
            if info.values.get("color_vision"):
                return Category.READABILITY  # 색약 접근성: 강조색이라도 디자인 예산으로 막지 않음
            if _is_photo_text(info):
                return Category.DESIGN
            fg = info.values.get("foreground")
            if fg is not None and saturation(fg) >= CHROMATIC_SATURATION:
                return Category.DESIGN  # 브랜드색·강조색 글씨
        return Category.READABILITY

    def _review_reason(self, info: FindingInfo, standards: Standards) -> str:
        if info.rule == Rule.DENSITY:
            if "char_count" in info.values:
                measured = f"{info.values['char_count']}자"
                limit = info.values.get("required_count", standards.max_chars.value)
                limit = f"{limit:g}자"
            else:
                measured = f"{info.values.get('line_count', '?')}줄"
                limit = info.values.get("required_count", standards.max_lines.value)
                limit = f"{limit:g}줄"
            return f"정보 과다 ({measured} / {standards.label} 기준 {limit}) → 문장 압축·슬라이드 분할은 사람이 판단"
        return REVIEW_REASONS.get(info.rule, "사람이 판단할 항목")

    # ==================================================================
    # 규칙별 후보
    # ==================================================================

    def _candidates(self, info, shape, others, ctx, category, profile):
        s = ctx["standards"]
        if info.rule in CONSISTENCY_FIXES:
            return self._consistency(info, shape, others, ctx, category)
        if info.rule == COLOR_ONLY_RULE:
            return self._color_only(info, shape, category)
        if info.rule == Rule.CONTRAST:
            if _is_photo_text(info):
                return self._scrim(info, shape, s, category)
            return self._contrast(info, shape, s, category)
        if info.rule == Rule.FONT_SIZE:
            return self._font_size(info, shape, s, category)
        if info.rule == Rule.OVERFLOW:
            return self._grow_box(info, shape, others, ctx["slide_h"], category, profile.resolve_collisions)
        if info.rule == Rule.TEXT_OVERLAP:
            return self._separate(info, shape, others, ctx, category)
        if info.rule in (Rule.OFF_SLIDE, Rule.SAFE_MARGIN) and info.finding_id.endswith(":card"):
            return self._fit_card(info, shape, others, ctx, category)
        if info.rule in (Rule.OFF_SLIDE, Rule.SAFE_MARGIN):
            return self._move_inside(info, shape, others, ctx, category)
        if info.rule == Rule.HIDDEN_TEXT:
            return self._bring_front(info, shape, others, category)
        if info.rule == Rule.AUTOFIT_SHRINK:
            return self._autofit_off(info, shape, s, category)
        if info.rule == Rule.LINE_SPACING:
            return self._line_spacing(info, shape, s, category)
        if info.rule == Rule.FONT_FAMILY:
            return self._font_family(info, shape, ctx["font"], category)
        if info.rule == Rule.HIERARCHY:
            return self._hierarchy(info, shape, s, category)
        return [], f"후보 생성 미지원 규칙: {info.rule}"

    def _proposal(
        self, info, shape, prop, before, after, cost, category, note, harmony=None, contrast=None, group=None
    ):
        return ChangeProposal(
            group=group,
            slide_index=shape.slide_index,
            shape_id=shape.shape_id,
            prop=prop,
            before=before,
            after=after,
            cost=round(min(1.0, max(0.0, cost)), 4),
            rule=info.rule,
            category=category,
            note=note,
            harmony=None if harmony is None else round(harmony, 3),
            contrast=None if contrast is None else round(contrast, 2),
            finding_id=info.finding_id,
        )

    # ---- 글자 크기 --------------------------------------------------
    # 비용 = 크기 증가 비율 (2배 이상이면 1.0)

    def _font_size(self, info, shape, standards, category):
        size = info.values.get("size_pt")
        required = info.values.get("required_pt", standards.min_font_pt.value)
        if size is None:
            return [], "글자 크기 측정값이 없음"
        if required is None:
            return [], "B의 글자 크기 기준값이 없음"
        if size <= 0:
            return [], "글자 크기 측정값이 0 이하"
        if size >= required:
            return [], f"현재 {size}pt가 이미 기준 {required}pt 이상 (글자를 줄이는 후보는 만들지 않음)"
        target, basis = required, f"{standards.label} 최소 {required}pt"
        recommended = standards.recommended_font_pt.value
        minimum = standards.min_font_pt.value
        if recommended is not None and minimum and required > minimum:
            # 큰 슬라이드라 B가 기준을 키웠으면(policy.slide_scale) 권장 크기도 같은 비율로
            recommended = round(recommended * required / minimum, 1)
        if (
            standards.fix.font_goal == FontGoal.RECOMMENDED
            and recommended is not None
            and recommended > required
        ):
            target, basis = recommended, f"수정 목표: {standards.label} 권장 {recommended}pt"
        # TODO(C): 글자가 커져 상자를 넘칠 것 같으면 상자 높이 후보를 같은 묶음(group)으로 함께 만든다
        #          (지금은 적용 뒤 재검사에서 넘침이 나와야 높이 후보가 생김 → 2번 슬라이드처럼 빽빽해짐)
        note = f"글자 크기 {size}pt → {target}pt ({basis})"
        cost = _ratio_cost(size, target)
        return [self._proposal(info, shape, Property.FONT_SIZE, size, target, cost, category, note)], ""

    # ---- 표 글자 크기 ------------------------------------------------
    # 표 칸마다 '10pt → 14pt'를 따로 내면 칸 수만큼 후보가 생기고 일부만 고르면 표가 들쭉날쭉해진다.
    # 그래서 표 도형 하나에 후보 하나: 표 안에서 기준보다 작은 글자를 모두 목표 크기로 (patcher가 칸을 돈다).
    # 행 높이는 PowerPoint가 글자에 맞춰 늘리므로 따로 고치지 않는다 (넘치면 재검사에서 다시 나옴).

    def _table_font(self, snapshot, slide, table_id, cells, standards, profile, policy, result):
        raw, info = min(cells, key=lambda item: item[1].values.get("size_pt") or 999)

        def skip(reason):
            for cell_raw, _info in cells:
                result.skipped.append(SkippedFinding(cell_raw, reason, Category.READABILITY))

        if Property.FONT_SIZE not in profile.allowed_props:
            skip(_level_reason([], profile))
            return
        if self.bindings.is_locked(policy, slide, table_id, Property.FONT_SIZE):
            skip("고정된 객체·속성이라 후보를 만들지 않음")
            return
        table = self.bindings.find_shape(snapshot, slide, table_id) or ShapeInfo(
            slide, table_id, "other", True, ()
        )
        table_info = replace(info, shape_id=table_id)
        candidates, why = self._font_size(table_info, table, standards, Category.READABILITY)
        if not candidates:
            skip(why)
            return
        for proposal in candidates:
            note = f"표 전체 {proposal.note} (기준 미달 칸 {len(cells)}개)"
            result.proposals.append(replace(proposal, note=note))

    # ---- 명암비 (단색 배경) ------------------------------------------
    # 후보 A: 색상 유지, 밝기만 조절 (회의록 Level 1: 명도만 미세 조정)
    # 후보 B: 표준 팔레트 진회색 (회의록 Level 2: 표준 팔레트 적용)
    # 후보 C: 큰 글자 기준까지 키우기
    # 모든 수준에서 기준을 넘는 범위 안에서 원래 색에 가장 가까운 후보를 채택합니다.
    # 비용 = 색 변화량(0~1) 또는 크기 증가 비율

    def _contrast(self, info, shape, standards, category):
        fg = info.values.get("foreground")
        bg = info.values.get("background")
        if fg is None or bg is None:
            return [], "명암비 측정값(글자색·배경색)이 없음"
        # 색약 맞춤 판정이면 '보통 눈과 설정에서 고른 색약 눈 모두'에서 기준을 넘는 색을 찾는다
        vision = bool(info.values.get("color_vision"))
        ratio_of = vision_ratio(self.vision_kinds) if vision else contrast_ratio
        ratio = info.values.get("ratio", ratio_of(fg, bg))
        threshold = info.values.get("required_ratio", standards.contrast_normal.value)
        # 검사 기준(threshold)을 넘는 범위에서 얼마나 여유 있게 고칠지는 수정 목표(FixTargets)가 정한다
        required = contrast_goal(threshold, standards)
        goal_text = (
            f"기준 {threshold}:1" if required == threshold else f"기준 {threshold}:1, 목표 {required:g}:1"
        )

        if vision:
            # 색약: Okabe & Ito(CUD)는 '어두운 배경 위 빨간 글자'를 피하고 밝은 글자를 쓰라고 권한다.
            # 기준선(큰 글자 3:1)만 겨우 넘기면 여전히 어둡게 보이므로 일반 글자 기준(4.5:1)까지 올린다.
            required = max(required, standards.contrast_normal.value)
            goal_text = f"기준 {threshold}:1, 색약 목표 {required:g}:1"

        candidates = []

        def color_option(new_color, label):
            harmony = harmony_score(new_color, bg)
            new_ratio = ratio_of(new_color, bg)
            who = "색약 포함 최저 " if vision else ""
            note = f"{label} #{fg} → #{new_color} ({who}명암비 {new_ratio:.2f}:1, {goal_text}, 조화 {harmony:+.2f})"
            cost = color_distance(fg, new_color)
            return self._proposal(
                info, shape, Property.FONT_COLOR, fg, new_color, cost, category, note, harmony, new_ratio
            )

        if vision:
            # 색약 맞춤: 원래 색과 가장 가까운 색각 친화 색 (Okabe & Ito 팔레트, Wong 2011)
            cud = cud_color(fg, bg, required, self.vision_kinds)
            if cud is not None:
                return [color_option(cud, "색각 친화 색(Okabe·Ito)")], ""

        # 후보 A
        new_color = nearest_passing_color(fg, bg, required, ratio_of)
        if new_color is None:
            for fallback in ("000000", "FFFFFF"):
                passes = ratio_of(fallback, bg) >= required
                closer = new_color is None or color_distance(fg, fallback) < color_distance(fg, new_color)
                if passes and closer:
                    new_color = fallback
        if new_color is not None:
            candidates.append(color_option(new_color, "글자색 밝기 조정"))

        # 후보 B
        palette = PALETTE_DARK if relative_luminance(bg) > 0.18 else PALETTE_LIGHT
        if palette != new_color and ratio_of(palette, bg) >= required:
            candidates.append(color_option(palette, "표준 팔레트 글자색"))

        # 후보 C
        large_ratio = standards.contrast_large.value
        # (큰 글자가 돼도 수정 목표를 넘어야 함: 목표가 4.5:1이면 크기만 키워서는 안 됨)
        large_goal = contrast_goal(large_ratio, standards, large=True)
        if not vision and self.allow_large_text_option and required > large_goal and ratio >= large_goal:
            option = self._large_text_option(info, shape, fg, standards, category)
            if option is not None:
                candidates.append(option)

        if not candidates:
            return [], "배경색과 기준을 만족하는 글자색을 찾지 못함"
        return candidates, ""

    def _large_text_option(self, info, shape, fg, standards, category):
        target_runs = [r for r in shape.runs if r.color == fg]
        if not target_runs:
            return None
        smallest = None
        all_bold = True
        for run in target_runs:
            if run.size_pt is None or run.bold is None:
                return None  # 모르는 값이 있으면 추측하지 않음
            if smallest is None or run.size_pt < smallest:
                smallest = run.size_pt
            if not run.bold:
                all_bold = False
        target = standards.large_bold_text_pt.value if all_bold else standards.large_text_pt.value
        if smallest >= target:
            return None
        note = f"글자 크기 {smallest}pt → {target}pt (큰 글자가 되어 명암비 기준 {standards.contrast_large.value}:1 적용)"
        cost = _ratio_cost(smallest, target)
        return self._proposal(info, shape, Property.FONT_SIZE, smallest, target, cost, category, note)

    # ---- 명암비 (사진 배경) → 배경판 ----------------------------------
    # 배경을 측정할 수 없으므로, 아래 배경이 검정~흰색 '무엇이든' 기준을 넘는
    # 가장 옅은 배경판을 찾습니다. 비용 = 배경판 불투명도

    def _scrim(self, info, shape, standards, category):
        fg = info.values.get("foreground")
        if fg is None:
            return [], "사진 위 글씨의 글자색을 알 수 없음"
        large = self._is_large_text(shape, fg, standards)
        target = standards.contrast_large.value if large else standards.contrast_normal.value
        target = contrast_goal(target, standards, large=large)
        # TODO(C, FixTargets.unify_scrims): 같은 슬라이드의 배경판을 한 가지 색·투명도로 맞추기
        #       → planning/extras.py unify_slide_scrims() (NN/g: 가장 나쁜 경우를 기준으로 일관되게)

        colors = info.values.get("text_colors") or (fg,)  # 같은 글상자의 모든 글자색
        best = None
        for scrim in ("000000", "FFFFFF"):
            for step in range(1, 101):
                alpha = step / 100
                worst = min(worst_contrast_over_unknown(color, scrim, alpha) for color in colors)
                if worst >= target:
                    if best is None or alpha < best[1]:
                        best = (scrim, alpha, worst)
                    break
        if best is None:
            return [], "어떤 배경판으로도 기준을 넘길 수 없음 (글자색 확인 필요)"

        scrim, alpha, worst = best
        name = "검정" if scrim == "000000" else "흰색"
        after = {"color": scrim, "transparency": round(1 - alpha, 2)}
        note = (
            f"글상자에 {name} 배경판 (불투명도 {alpha:.0%}) → 사진 색과 관계없이 "
            f"명암비 {worst:.2f}:1 이상 (기준 {target}:1)"
        )
        return [self._proposal(info, shape, Property.SHAPE_FILL, None, after, alpha, category, note)], ""

    def _is_large_text(self, shape, fg, standards) -> bool:
        runs = [r for r in shape.runs if r.color == fg] or list(shape.runs)
        if not runs:
            return False
        for run in runs:
            if run.size_pt is None or run.bold is None:
                return False  # 모르면 더 엄격한 '일반 글자' 기준
            limit = standards.large_bold_text_pt.value if run.bold else standards.large_text_pt.value
            if run.size_pt < limit:
                return False
        return True

    # ---- 넘침 → 상자 높이 키우기 ---------------------------------------
    # 비용 = 늘어난 높이 ÷ 슬라이드 높이 (밀어낸 객체가 있으면 그 이동 거리도 더함)
    # 최대 수정(push=True): 키울 공간에 다른 객체(그림 등)가 있으면 그 객체를 그만큼 아래로 내림

    def _grow_box(self, info, shape, others, slide_h, category, push=False):
        needed = info.values.get("needed_height")
        if needed is None:
            return [], "필요한 상자 높이 측정값이 없음"
        if not shape.has_geometry:
            return [], "상자 좌표를 알 수 없음"
        new_h = math.ceil(needed)
        if new_h <= shape.height + TOLERANCE:
            return [], "상자 높이가 이미 충분함 (측정값 확인 필요)"
        if new_h <= shape.height + MIN_GROW:
            return [], f"모자란 높이가 {MIN_GROW:g}pt 이하라 그대로 둠 (화면에서 잘리지 않는 정도)"
        if shape.top + new_h > slide_h + TOLERANCE:
            return [], "상자를 키우면 슬라이드 아래로 나감 → 글자 줄이기·나누기는 사람이 판단"
        blocked = _new_collisions(shape, (shape.left, shape.top, shape.width, new_h), others)
        note = f"상자 높이 {shape.height:g}pt → {new_h:g}pt (넘친 글자를 상자 안에)"
        cost = (new_h - shape.height) / slide_h
        if not blocked:
            return [
                self._proposal(info, shape, Property.SHAPE_HEIGHT, shape.height, new_h, cost, category, note)
            ], ""

        ids = ", ".join(str(o.shape_id) for o in blocked)
        if not push:
            return (
                [],
                f"상자를 키울 공간에 다른 객체(ID {ids})가 있음 → 사람이 판단 (최대 수정에서는 그 객체를 내림)",
            )

        # 최대 수정: 가로막는 객체를 넘친 만큼 내림 → 상자 키우기와 한 묶음으로 적용
        group = f"overflow:{shape.slide_index}:{shape.shape_id}"
        bottom = shape.top + new_h
        moving_ids = {shape.shape_id} | {o.shape_id for o in blocked}
        bundle = [
            self._proposal(
                info, shape, Property.SHAPE_HEIGHT, shape.height, new_h, cost, category, note, group=group
            )
        ]
        for other in blocked:
            shift = bottom - other.top
            new_rect = (other.left, other.top + shift, other.width, other.height)
            if new_rect[1] + other.height > slide_h + TOLERANCE:
                return [], f"객체(ID {other.shape_id})를 내리면 슬라이드 아래로 나감 → 사람이 판단"
            rest = [o for o in others if o.shape_id not in moving_ids]
            hit = _new_collisions(other, new_rect, rest)
            if hit:
                hit_ids = ", ".join(str(o.shape_id) for o in hit)
                return [], f"객체(ID {other.shape_id})를 내리면 다른 객체(ID {hit_ids})와 겹침 → 사람이 판단"
            before = (other.left, other.top)
            after = (other.left, round(other.top + shift, 2))
            move_note = f"아래 객체 {other.shape_id}을(를) {shift:g}pt 내림 (넘친 글자와 겹치지 않게)"
            bundle.append(
                self._proposal(
                    info,
                    other,
                    Property.SHAPE_POSITION,
                    before,
                    after,
                    shift / slide_h,
                    category,
                    move_note,
                    group=group,
                )
            )
        return [bundle], ""

    # ---- 겹친 객체 떼어 놓기 (최대 수정) ----------------------------------
    # 겹친 상대 객체의 위·아래·왼쪽·오른쪽 중 가장 가까운 빈자리로 대상 글상자를 옮깁니다.
    # 옮긴 자리는 안전 영역(가장자리 5% 안쪽) 안이어야 하고 다른 객체와 새로 겹치면 안 됩니다.
    # 비용 = 이동 거리 ÷ 슬라이드 높이

    def _separate(self, info, shape, others, ctx, category):
        other_id = info.values.get("other_shape_id")
        if other_id is None:
            return [], "겹친 객체 정보가 없음"
        other = next((o for o in others if o.shape_id == int(other_id)), None)
        if other is None or not other.has_geometry or not shape.has_geometry:
            return [], "겹친 객체의 좌표를 알 수 없음"

        slide_w, slide_h = ctx["slide_w"], ctx["slide_h"]
        ratio = ctx["standards"].safe_margin_ratio.value
        mx, my = slide_w * ratio, slide_h * ratio
        options = {
            "아래로": (shape.left, other.top + other.height),
            "위로": (shape.left, other.top - shape.height),
            "오른쪽으로": (other.left + other.width, shape.top),
            "왼쪽으로": (other.left - shape.width, shape.top),
        }
        best = None
        for direction, (left, top) in options.items():
            inside = (
                left >= mx - TOLERANCE
                and top >= my - TOLERANCE
                and left + shape.width <= slide_w - mx + TOLERANCE
                and top + shape.height <= slide_h - my + TOLERANCE
            )
            if not inside:
                continue
            if _new_collisions(shape, (left, top, shape.width, shape.height), others):
                continue
            distance = math.hypot(left - shape.left, top - shape.top)
            if best is None or distance < best[0]:
                best = (distance, direction, left, top)
        if best is None:
            return [], f"객체 {other_id}와 겹치지 않게 옮길 빈자리가 없음 → 사람이 판단"

        distance, direction, left, top = best
        before = (shape.left, shape.top)
        after = (round(left, 2), round(top, 2))
        note = f"겹친 객체 {other_id}와 떨어지도록 {direction} {distance:.0f}pt 이동"
        return [
            self._proposal(
                info, shape, Property.SHAPE_POSITION, before, after, distance / slide_h, category, note
            )
        ], ""

    # ---- 슬라이드 밖·가장자리 → 안전 영역 안으로 ---------------------------
    # 화면에서 잘리는 명확한 문제라서 가장 짧은 거리만 옮깁니다.
    # 안전 영역(가장자리 5% 안쪽)에 들어가면 그곳으로, 너무 크면 슬라이드 안으로만.
    # 비용 = 이동 거리 ÷ 슬라이드 높이

    def _move_inside(self, info, shape, others, ctx, category):
        if not shape.has_geometry:
            return [], "상자 좌표를 알 수 없음"
        slide_w, slide_h = ctx["slide_w"], ctx["slide_h"]
        ratio = ctx["standards"].safe_margin_ratio.value
        mx, my = slide_w * ratio, slide_h * ratio

        fits_safe = (
            shape.width <= slide_w - 2 * mx + TOLERANCE and shape.height <= slide_h - 2 * my + TOLERANCE
        )
        if fits_safe:
            area = (mx, my, slide_w - mx, slide_h - my)
            where = f"가장자리 여백 {ratio:.0%} 안쪽으로"
        elif info.rule == Rule.SAFE_MARGIN:
            return [], "상자가 안전 영역보다 큼 → 크기 조정은 사람이 판단"
        elif shape.width <= slide_w + TOLERANCE and shape.height <= slide_h + TOLERANCE:
            area = (0.0, 0.0, slide_w, slide_h)
            where = "슬라이드 안으로"
        else:
            return [], "상자가 슬라이드보다 큼 → 사람이 판단"

        new_left = min(max(shape.left, area[0]), area[2] - shape.width)
        new_top = min(max(shape.top, area[1]), area[3] - shape.height)
        distance = math.hypot(new_left - shape.left, new_top - shape.top)
        if distance <= TOLERANCE:
            return [], "이미 안쪽에 있음 (측정값 확인 필요)"

        blocked = _new_collisions(shape, (new_left, new_top, shape.width, shape.height), others)
        if blocked:
            ids = ", ".join(str(o.shape_id) for o in blocked)
            return [], f"안으로 옮기면 다른 객체(ID {ids})와 겹침 → 사람이 판단"

        before = (shape.left, shape.top)
        after = (round(new_left, 2), round(new_top, 2))
        note = f"상자 위치 ({shape.left:g}, {shape.top:g}) → ({after[0]:g}, {after[1]:g}) ({where} {distance:.0f}pt)"
        cost = distance / slide_h
        return [self._proposal(info, shape, Property.SHAPE_POSITION, before, after, cost, category, note)], ""

    # ---- 색만으로 구분한 글자 → 색각 친화 색으로 바꾸기 (Okabe & Ito CUD, Wong 2011) ----
    # 판정 id 끝: <바꿀 색들>_<본문색> (color_vision.color_only:<슬라이드>:<도형>:D62728-2CA02C_222222)
    # 빨강·초록처럼 색약 눈에 같아 보이는 색들을, 모든 눈에서 서로·본문과 구분되고 배경과 4.5:1 이상인
    # 팔레트 색으로 함께 바꾼다 (한 묶음). 그런 조합이 없으면 그 글자들을 굵게 해 색 외의 표시를 더한다.

    def _color_only(self, info, shape, category):
        tail = info.finding_id.rsplit(":", 1)[-1]
        marked_part, _sep, body = tail.partition("_")
        colors = [part for part in marked_part.split("-") if len(part) == 6]
        runs = list(getattr(shape, "runs", ()))
        colors = [color for color in colors if any(run.color == color for run in runs)]
        if not colors:
            return [], "구분할 글자를 찾지 못함"
        group = f"cud:{shape.slide_index}:{shape.shape_id}"
        background = info.values.get("background") or "FFFFFF"
        new_colors = cud_distinct_colors(colors, body or "000000", background, kinds=self.vision_kinds)
        if new_colors is not None:
            bundle = [
                self._proposal(
                    info,
                    shape,
                    Property.FONT_COLOR,
                    old,
                    new,
                    color_distance(old, new) / len(colors),
                    category,
                    f"#{old} → #{new} (모든 눈에서 구분되는 색각 친화 색: Okabe & Ito CUD, Wong 2011)",
                    group=group,
                )
                for old, new in zip(colors, new_colors, strict=True)
            ]
            return [bundle], ""
        targets = [color for color in colors if any(r.color == color and r.bold is not True for r in runs)]
        if not targets:
            return [], "구분할 글자가 이미 굵음 → 사람이 판단 (기호·밑줄 추가 등)"
        bundle = [
            self._proposal(
                info,
                shape,
                Property.FONT_BOLD,
                color,
                True,
                0.05,
                category,
                f"#{color} 글자를 굵게 (색만으로 구분하지 않게: Okabe & Ito CUD 원칙 2)",
                group=group,
            )
            for color in targets
        ]
        return [bundle], ""

    # ---- 슬라이드 간 일관성 → 다른 슬라이드의 중앙값에 맞추기 -----------------
    # 제목 위치: 다른 본문 슬라이드 제목의 중앙 위치로 옮김 (다른 객체와 새로 겹치면 만들지 않음)
    # 제목 크기: 다른 본문 슬라이드 제목의 중앙 크기로 (최소 글자 크기 아래로는 내리지 않음)

    def _consistency(self, info, shape, others, ctx, category):
        if not info.values.get("consistency"):
            return [], "일관성 목표값이 없음"
        if info.rule == "consistency.title_position":
            if not shape.has_geometry:
                return [], "상자 좌표를 알 수 없음"
            new_left, new_top = info.values["target_left"], info.values["target_top"]
            blocked = _new_collisions(shape, (new_left, new_top, shape.width, shape.height), others)
            if blocked:
                ids = ", ".join(str(o.shape_id) for o in blocked)
                return [], f"다른 슬라이드 제목 위치로 옮기면 다른 객체(ID {ids})와 겹침 → 사람이 판단"
            distance = math.hypot(new_left - shape.left, new_top - shape.top)
            before, after = (shape.left, shape.top), (round(new_left, 2), round(new_top, 2))
            note = f"제목 위치 ({shape.left:g}, {shape.top:g}) → ({after[0]:g}, {after[1]:g}) (다른 슬라이드 제목과 같은 자리)"
            cost = distance / ctx["slide_h"]
            return [
                self._proposal(info, shape, Property.SHAPE_POSITION, before, after, cost, category, note)
            ], ""
        size, target = info.values["size_pt"], info.values["target_pt"]
        floor = ctx["standards"].min_font_pt.value or 0
        if target < floor:
            return [], f"다른 슬라이드 크기 {target:g}pt가 최소 기준 {floor:g}pt보다 작음 → 사람이 판단"
        note = f"제목 글자 크기 {size:g}pt → {target:g}pt (다른 슬라이드 제목과 같은 크기)"
        cost = _ratio_cost(size, target)
        return [self._proposal(info, shape, Property.FONT_SIZE, size, target, cost, category, note)], ""

    # ---- 슬라이드 밖으로 나간 카드 → 안쪽 글상자와 함께 줄이기 ------------------
    # 카드만 옮기면 옆 카드와 겹치고, 카드만 줄이면 안쪽 글상자가 카드 밖으로 삐져나온다.
    # 그래서 카드 너비를 안전 여백 안쪽까지 줄이고, 카드 안 글상자도 같은 만큼 줄인다 (한 묶음).
    # 너무 많이 줄어들면(원래 너비의 70% 미만) 안전 여백 대신 슬라이드 끝까지만 줄인다.

    def _fit_card(self, info, card, others, ctx, category):
        if not card.has_geometry:
            return [], "카드 좌표를 알 수 없음"
        slide_w = ctx["slide_w"]
        margin = slide_w * ctx["standards"].safe_margin_ratio.value
        inside = [
            other
            for other in others
            if other.shape_id != card.shape_id
            and other.has_geometry
            and other.left >= card.left - TOLERANCE
            and other.top >= card.top - TOLERANCE
            and other.left + other.width <= card.left + card.width + TOLERANCE
            and other.top + other.height <= card.top + card.height + TOLERANCE
        ]
        right = card.left + card.width
        if right > slide_w + TOLERANCE:
            new_right = slide_w - margin
            if new_right - card.left < card.width * 0.7:
                new_right = slide_w  # 여백까지 줄이면 너무 좁아짐 → 슬라이드 끝까지만
            shrink = right - new_right
            new_left = card.left
        elif card.left < -TOLERANCE:
            new_left = margin if card.width - (margin - card.left) >= card.width * 0.7 else 0.0
            shrink = new_left - card.left
            new_right = right
        else:
            return [], "위·아래로 나간 카드는 아직 자동으로 고치지 않음 (사람이 판단)"
        group = f"card:{card.slide_index}:{card.shape_id}"
        note = f"카드 너비 {card.width:g}pt → {card.width - shrink:g}pt (슬라이드 안으로, 안쪽 글상자 함께)"
        cost = shrink / slide_w
        bundle = [
            self._proposal(
                info,
                card,
                Property.SHAPE_WIDTH,
                card.width,
                round(card.width - shrink, 2),
                cost,
                category,
                note,
                group=group,
            )
        ]
        if new_left != card.left:
            bundle.append(
                self._proposal(
                    info,
                    card,
                    Property.SHAPE_POSITION,
                    (card.left, card.top),
                    (round(new_left, 2), card.top),
                    cost,
                    category,
                    note,
                    group=group,
                )
            )
        for item in inside:
            # 카드 테두리와 안쪽 글상자 사이 원래 여유는 그대로 둔다
            pad_left = item.left - card.left
            pad_right = right - (item.left + item.width)
            item_left = max(item.left, new_left + pad_left)
            item_right = min(item.left + item.width, new_right - pad_right)
            if item_right - item_left <= 0:
                return [], "카드 안 개체가 너무 좁아짐 → 사람이 판단"
            if abs(item_left - item.left) > TOLERANCE:
                bundle.append(
                    self._proposal(
                        info,
                        item,
                        Property.SHAPE_POSITION,
                        (item.left, item.top),
                        (round(item_left, 2), item.top),
                        cost,
                        category,
                        note,
                        group=group,
                    )
                )
            if abs((item_right - item_left) - item.width) > TOLERANCE:
                bundle.append(
                    self._proposal(
                        info,
                        item,
                        Property.SHAPE_WIDTH,
                        item.width,
                        round(item_right - item_left, 2),
                        cost,
                        category,
                        note,
                        group=group,
                    )
                )
        return [bundle], ""

    # ---- 가려진 글자 → 맨 앞으로 ---------------------------------------

    def _bring_front(self, info, shape, others, category):
        cover_id = info.values.get("cover_shape_id")
        if cover_id is None:
            return [], "글자를 가린 객체 정보가 없음"
        cover = next((o for o in others if o.shape_id == int(cover_id)), None)
        note = f"글상자를 맨 앞으로 (객체 {cover_id}에 가려짐)"
        if cover is not None and cover.kind == "picture":
            note += " → 사진 위 글씨가 되므로 재검사에서 명암비 확인"
        before = f"behind:{cover_id}"
        return [
            self._proposal(info, shape, Property.Z_ORDER, before, "front", Z_ORDER_COST, category, note)
        ], ""

    # ---- 자동 맞춤 축소 → 해제 -----------------------------------------

    def _autofit_off(self, info, shape, standards, category):
        specified = info.values.get("specified_size_pt")
        effective = info.values.get("effective_size_pt")
        required = info.values.get("required_pt", standards.min_font_pt.value)
        if specified is None or effective is None:
            return [], "지정 크기·실제 표시 크기 측정값이 없음"
        if effective >= required:
            return [], f"축소된 크기 {effective}pt도 기준 {required}pt 이상 (수정 불필요)"
        if specified < required:
            return [], f"지정 크기 {specified}pt 자체가 기준 {required}pt 미만 → 글자 크기 문제로 처리"
        note = (
            f"자동 맞춤(글자 축소) 해제: 표시 크기 {effective}pt → 지정 크기 {specified}pt "
            "(넘치면 재검사에서 상자 높이 후보)"
        )
        cost = _ratio_cost(effective, specified)
        return [self._proposal(info, shape, Property.AUTOFIT, "shrink", "none", cost, category, note)], ""

    # ---- 줄 간격 ------------------------------------------------------

    def _line_spacing(self, info, shape, standards, category):
        current = info.values.get("line_spacing")
        required = info.values.get("required_spacing", standards.min_line_spacing.value)
        if required is None:
            return [], "B에 줄 간격 기준이 없음 (C는 기준을 따로 정하지 않음)"
        if current is None:
            return [], "줄 간격 측정값이 없음"
        if current <= 0:
            return [], "줄 간격 측정값이 0 이하"
        if current >= required:
            return [], f"줄 간격 {current}배가 이미 기준 {required}배 이상"
        note = f"줄 간격 {current}배 → {required}배 (B 기준)"
        cost = _ratio_cost(current, required)
        return [
            self._proposal(info, shape, Property.LINE_SPACING, current, required, cost, category, note)
        ], ""

    # ---- 글씨체 (장식·손글씨 글꼴) ----------------------------------------
    # 회의록: 최소 수정은 글꼴을 건드리지 않고, 공격적 수정이면 고딕으로 통일

    def _font_family(self, info, shape, recommended, category):
        name = info.values.get("font_name")
        if name is None:
            return [], "글꼴 이름 측정값이 없음"
        if name == recommended:
            return [], "이미 권장 글씨체"
        note = f"글씨체 '{name}' → '{recommended}' (장식·손글씨 글꼴을 고딕 계열로)"
        return [
            self._proposal(
                info, shape, Property.FONT_FAMILY, name, recommended, FONT_FAMILY_COST, category, note
            )
        ], ""

    # ---- 시각적 위계 (제목이 본문보다 충분히 크지 않음) ----------------------
    # 제목 상자의 글자 크기를 본문 × 비율까지 키움. 비용 = 크기 증가 비율

    def _hierarchy(self, info, shape, standards, category):
        title = info.values.get("title_size_pt")
        body = info.values.get("body_size_pt")
        target = info.values.get("required_pt")
        if target is None:
            ratio = info.values.get("required_ratio", standards.title_body_ratio.value)
            if body is None or ratio is None:
                return [], "본문 글자 크기 또는 위계 기준을 확인할 수 없음"
            target = body * ratio
        target = math.ceil(target * 2) / 2
        if title is None or title <= 0:
            return [], "제목 글자 크기 측정값이 없음"
        if title >= target:
            return [], f"제목 {title}pt가 이미 수정 목표({target}pt) 이상"
        note = f"제목 크기 {title}pt → {target}pt (시각적 위계)"
        cost = _ratio_cost(title, target)
        return [self._proposal(info, shape, Property.FONT_SIZE, title, target, cost, category, note)], ""


# ======================================================================
# 내부 함수
# ======================================================================


def _recommended_only(minimum: list[ChangeProposal], extra: list[ChangeProposal]) -> list[ChangeProposal]:
    """권장 목표로 만든 후보 중 최소 수정과 값이 다른 것만 '권장 수정'으로 남긴다.

    묶음(group)은 통째로 판단한다: 묶음 안에 하나라도 다르면 묶음 전체를 권장 수정으로 남긴다.
    """

    def target(p):
        return (p.finding_id, p.slide_index, p.shape_id, p.prop, str(p.before).upper())

    chosen = {target(p): p for p in minimum}

    def differs(p):
        same = chosen.get(target(p))
        return same is None or same.after != p.after or same.group != p.group

    changed_groups = {p.group for p in extra if p.group is not None and differs(p)}
    out = []
    for p in extra:
        keep = p.group in changed_groups if p.group is not None else differs(p)
        if not keep:
            continue
        group = None if p.group is None else f"{p.group}~rec"  # 최소 수정 묶음과 섞이지 않게
        out.append(replace(p, variant="recommended", group=group))
    return out


def contrast_goal(threshold: float, standards: Standards, *, large: bool | None = None) -> float:
    """검사 기준 threshold를 넘는 수정 목표 명암비 (domain/policy.py FixTargets)."""
    normal = standards.contrast_normal.value
    if large is None:
        large = threshold < normal  # B가 큰 글자 기준을 적용한 판정
    return standards.fix.contrast_target(threshold, large=large, normal=normal)


def _is_photo_text(info: FindingInfo) -> bool:
    return info.rule == Rule.CONTRAST and info.values.get("background_kind") == "image"


def _ratio_cost(before: float, after: float) -> float:
    """증가 비율. 2배 이상이면 1.0."""
    return min(1.0, abs(after - before) / before)  # 줄이는 후보(일관성)도 변화량만큼


def _bundle_cost(bundle: list[ChangeProposal]) -> float:
    """수정안(변경 묶음)의 비용 = 각 변경 비용의 합 (최대 1.0)"""
    return min(1.0, sum(p.cost for p in bundle))


def _level_reason(candidates: list[ChangeProposal], profile: StrengthProfile) -> str:
    """수정 수준 때문에 막힌 이유: 어떤 항목이 몇 수준부터 가능한지"""
    groups = []
    first_level = None
    for c in candidates:
        group = PROPERTY_GROUP.get(c.prop, c.prop)
        if group not in groups:
            groups.append(group)
        for name in LEVEL_ORDER:
            if c.prop in STRENGTH_PROFILES[name].allowed_props:
                if first_level is None or LEVEL_ORDER.index(name) < LEVEL_ORDER.index(first_level):
                    first_level = name
                break
    need = STRENGTH_PROFILES[first_level].label if first_level else "없음"
    return f"{profile.label}에서는 '{', '.join(groups)}'을(를) 바꾸지 않음 ({need} 이상에서 가능)"


def _overlaps(a: tuple, b: tuple) -> bool:
    """두 사각형 (left, top, width, height)이 허용 오차보다 많이 겹치는지"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap_w = min(ax + aw, bx + bw) - max(ax, bx)
    overlap_h = min(ay + ah, by + bh) - max(ay, by)
    return overlap_w > TOLERANCE and overlap_h > TOLERANCE


COLOR_ONLY_RULE = "color_vision.color_only"

# 일관성 판정 가운데 수정 후보를 만드는 것 (본문 크기는 내용 양 때문일 수 있어 사람이 판단)
CONSISTENCY_FIXES = ("consistency.title_position", "consistency.title_size")


def _new_collisions(shape: ShapeInfo, new_rect: tuple, others: list[ShapeInfo]) -> list[ShapeInfo]:
    """바꾼 뒤 '새로' 겹치게 되는 객체 목록.

    원래부터 겹쳐 있던 객체(예: 슬라이드 전체 배경 사진)는 이 변경 탓이 아니므로 제외합니다.
    """
    old_rect = (shape.left, shape.top, shape.width, shape.height)
    result = []
    for other in others:
        if other.shape_id == shape.shape_id or not other.has_geometry:
            continue
        other_rect = (other.left, other.top, other.width, other.height)
        if _overlaps(new_rect, other_rect) and not _overlaps(old_rect, other_rect):
            result.append(other)
    return result


def _merge_same_target(proposals: list[ChangeProposal], result: PlanResult) -> list[ChangeProposal]:
    """같은 객체·같은 속성 후보가 여럿이면 하나로.

    - 글자 크기·상자 높이·줄 간격: 더 큰 값 하나 (둘 다 만족)
    - 그 밖의 속성: 서로 다르면 충돌 → 먼저 나온 것만 남기고 나머지는 대안으로
    """
    bigger_wins = (Property.FONT_SIZE, Property.SHAPE_HEIGHT, Property.LINE_SPACING)
    merged: dict[tuple[int, int, str], ChangeProposal] = {}
    order = []
    for p in proposals:
        key = (p.slide_index, p.shape_id, p.prop)
        if p.prop in (Property.FONT_COLOR, Property.FONT_FAMILY, Property.FONT_BOLD):
            # 글자 색·글씨체는 원래 값(before)이 같은 글자에만 적용된다 (patcher).
            # 한 상자에 회색이 두 가지면 후보도 두 개가 있어야 둘 다 고쳐진다.
            key = (p.slide_index, p.shape_id, p.prop, str(p.before).upper())
        if p.group is not None:
            # 묶음의 일부는 합치지 않고 그대로 둠 (함께 적용돼야 하므로)
            key = (p.slide_index, p.shape_id, f"{p.prop}@{p.group}", str(p.before).upper())
        if key not in merged:
            merged[key] = p
            order.append(key)
            continue
        old = merged[key]
        if p.prop in bigger_wins:
            if p.after > old.after:
                merged[key] = p
        elif p.after != old.after or p.before != old.before:
            result.alternatives.append(p)
    return [merged[k] for k in order]


# ======================================================================
# 팀 인터페이스 (application/ports.py의 ProposalPlanner)
# ======================================================================


class MinimumChangePlanner:
    """AnalyzePresentation에 끼워 쓰는 수정 후보 생성기.

    RuleBasedPlanner의 채택 후보(proposals)를 팀 ChangeProposal로 바꿔 돌려줍니다.
    대안·고치지 않은 이유·사용한 기준값은 last_result(PlanResult)에서 볼 수 있습니다.
    """

    def __init__(self, planner: RuleBasedPlanner | None = None) -> None:
        self.planner = planner or RuleBasedPlanner()
        self.last_result: PlanResult | None = None

    def propose(self, report: object, policy: object) -> list:
        result = self.planner.plan(report.deck, list(report.findings), policy)
        self.last_result = result
        bindings = self.planner.bindings
        proposals, seen = [], set()
        for p in result.proposals + result.recommended:
            team = bindings.to_team_proposal(p)
            if team.id in seen:
                # 한 상자에 원래 색이 다른 글자가 여럿이면 같은 ID가 나옴 → 원래 값으로 구분
                head, sep, group = team.id.partition("#")
                team = replace(team, id=f"{head}~{str(p.before).upper()}{sep}{group}")
            seen.add(team.id)
            proposals.append(team)
        # 같은 자리(쪽 번호·머리글 등) 개체에도 같은 수정 후보 (고정한 객체·속성은 빼고)
        extended = extend_same_place(report.deck, proposals)
        kept = proposals[:]
        for team in extended[len(proposals) :]:
            slide, shape_id = (int(part) for part in team.shape_key.split(":"))
            if not bindings.is_locked(policy, slide, shape_id, team.property):
                kept.append(team)
        return kept
