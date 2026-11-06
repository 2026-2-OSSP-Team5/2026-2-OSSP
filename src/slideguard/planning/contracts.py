"""C 담당 모듈(planning, patcher)이 '내부에서' 쓰는 데이터 형태.

다른 팀원의 모델(DeckSnapshot, Finding, AnalysisPolicy …)을 직접 import하지 않습니다.
팀원 모델은 bindings.py에서 아래 형태로 '번역'된 뒤에만 사용합니다.

표준 라이브러리만 사용합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from slideguard.planning.standards import Standards

# ======================================================================
# 이름 상수
# ======================================================================


class Property:
    """수정할 수 있는 속성"""

    FONT_SIZE = "font.size"  # 글자 크기
    FONT_COLOR = "font.color"  # 글자색
    FONT_FAMILY = "font.family"  # 글씨체
    SHAPE_HEIGHT = "shape.height"  # 상자 높이
    SHAPE_POSITION = "shape.position"  # 상자 위치 (슬라이드 밖 → 안)
    Z_ORDER = "shape.zorder"  # 앞뒤 순서
    SHAPE_FILL = "shape.fill"  # 상자 배경 채우기 (사진 위 글씨용 배경판)
    AUTOFIT = "text.autofit"  # 자동 맞춤
    LINE_SPACING = "paragraph.line_spacing"  # 줄 간격


class Rule:
    """검사 규칙 (C 내부 이름)"""

    CONTRAST = "contrast"  # 명암비 (단색 배경 / 사진 배경)
    FONT_SIZE = "font_size"  # 글자 크기 미달
    OVERFLOW = "overflow"  # 글자가 상자를 넘침
    OFF_SLIDE = "off_slide"  # 상자가 슬라이드 밖으로 나감
    HIDDEN_TEXT = "hidden_text"  # 다른 객체가 글자를 가림
    TEXT_OVERLAP = "text_overlap"  # 글자 상자끼리 겹침
    AUTOFIT_SHRINK = "autofit_shrink"  # 자동 맞춤으로 글자가 기준 미만으로 축소됨
    LINE_SPACING = "line_spacing"  # 줄 간격이 너무 좁음
    FONT_FAMILY = "font_family"  # 장식·손글씨 글꼴
    SAFE_MARGIN = "safe_margin"  # 슬라이드 가장자리 여백 침범
    DENSITY = "density"  # 정보 밀도 과다 (6x6)
    HIERARCHY = "hierarchy"  # 제목이 본문과 구분되지 않음 (시각적 위계)


# 사용자 지정 체크박스 → 고정되는 속성 (회의록: 글자 크기 고정, 레이아웃/좌표 고정 …)
USER_LOCK_GROUPS = {
    "font_size": (Property.FONT_SIZE, Property.AUTOFIT),  # 자동 맞춤 해제도 표시 크기를 바꿈
    "layout": (Property.SHAPE_POSITION, Property.SHAPE_HEIGHT, Property.Z_ORDER),
    "font_family": (Property.FONT_FAMILY,),
    "color": (Property.FONT_COLOR, Property.SHAPE_FILL),
}


class Status:
    """검사 결과 상태 (C 내부 이름)"""

    WARNING = "warning"  # 기준 미달이 측정으로 확인됨
    REVIEW = "review"  # 사람이 판단해야 함
    UNMEASURABLE = "unmeasurable"  # 판정 불가
    UNKNOWN = "unknown"  # 번역할 수 없는 상태


class Category:
    """수정 후보의 성격 → 수정 강도에 따라 고칠지 말지가 달라짐"""

    READABILITY = "readability"  # 명확한 가독성 문제 (모든 강도에서 수정 대상)
    DESIGN = "design"  # 디자인 의도와 겹치는 문제 (강도가 높을 때만 수정)
    REVIEW_ONLY = "review_only"  # 자동으로 고치지 않음 (사람이 판단)


# ======================================================================
# 입력: 팀원 모델을 번역한 결과 (bindings.py가 만들어 줌)
# ======================================================================


@dataclass(frozen=True)
class RunInfo:
    """글자 덩어리 하나. 모르는 값은 None (추측 금지)."""

    size_pt: float | None
    color: str | None  # "RRGGBB" 대문자
    bold: bool | None


@dataclass(frozen=True)
class ShapeInfo:
    slide_index: int
    shape_id: int
    kind: str  # "text" / "picture" / "other"
    supported: bool
    runs: tuple[RunInfo, ...]
    left: float | None = None  # 좌표 단위 pt. 모르면 None
    top: float | None = None
    width: float | None = None
    height: float | None = None

    @property
    def has_geometry(self) -> bool:
        return None not in (self.left, self.top, self.width, self.height)


@dataclass(frozen=True)
class FindingInfo:
    """검사 결과 하나. values의 키 이름은 C 내부 이름 (bindings.MEASURED_KEYS 참고)."""

    rule: str
    slide_index: int
    shape_id: int
    status: str
    values: dict = field(default_factory=dict)
    finding_id: str = ""  # 팀 Finding.id (수정 후보가 어느 문제에서 나왔는지)


# ======================================================================
# 출력
# ======================================================================


@dataclass(frozen=True)
class ChangeProposal:
    """수정 후보 하나. patcher가 적용하는 의미는 patcher.py 참고."""

    slide_index: int
    shape_id: int
    prop: str  # Property.*
    before: object
    after: object
    cost: float  # 0에 가까울수록 원본에 가까운 변경 (0~1)
    rule: str
    category: str = Category.READABILITY
    note: str = ""
    harmony: float | None = None  # 글자색 후보일 때 배경과의 색 조화 점수 (0 초과 = 조화)
    contrast: float | None = None  # 글자색 후보일 때 바꾼 뒤 명암비
    group: str | None = None  # 함께 적용해야 하는 변경 묶음 이름 (예: 상자 키우기 + 아래 그림 내리기)
    finding_id: str = ""  # 이 후보를 만든 팀 Finding.id


@dataclass(frozen=True)
class SkippedFinding:
    """후보를 만들지 않은 문제와 이유. finding은 팀원(B)의 원래 객체 그대로."""

    finding: object
    reason: str
    category: str = Category.READABILITY


@dataclass
class PlanResult:
    proposals: list[ChangeProposal] = field(default_factory=list)
    alternatives: list[ChangeProposal] = field(default_factory=list)
    skipped: list[SkippedFinding] = field(default_factory=list)
    standards: Standards | None = None  # 이번 계산에 쓴 기준값 (화면에 근거 표시용)
    strength: str = ""
