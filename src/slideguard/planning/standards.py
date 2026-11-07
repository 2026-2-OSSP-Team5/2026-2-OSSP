"""수정 후보가 쓰는 가독성 기준값 — 모두 B(domain/policy.py)에서 가져옵니다.

C는 기준값을 따로 정하지 않습니다. 검사(B)와 수정 후보(C)가 같은 기준을 써야
"검사에서는 통과인데 수정 후보는 더 고치라고 하는" 어긋남이 생기지 않습니다.

기준값을 고르는 순서 (planner.py)
  1) 검사 결과 Finding.expected — B가 그 판정에 실제로 쓴 값
  2) 없으면 이 모듈의 값 — domain/policy.py의 같은 기준값
  3) B에 그 기준 자체가 없으면 None — 그 항목은 수정 후보를 만들지 않음
     (예: 줄 간격, 제목/본문 크기 비율)
"""

from __future__ import annotations

from dataclasses import dataclass

from slideguard.domain import policy as team
from slideguard.domain.policy import Purpose

SOURCE = "B 기준 (domain/policy.py)"
NO_TEAM_CRITERION = "B에 이 기준이 없음 → 수정 후보를 만들지 않음"

PURPOSE_LABELS = {
    Purpose.PRESENTATION: "발표용",
    Purpose.LECTURE: "강의용",
    Purpose.REPORT: "보고용",
}


@dataclass(frozen=True)
class Criterion:
    value: float | None  # None이면 B에 기준이 없음
    basis: str


@dataclass(frozen=True)
class Standards:
    purpose: str
    label: str
    min_font_pt: Criterion  # 이 크기 미만이면 B가 경고
    recommended_font_pt: Criterion  # 이 크기 미만이면 B가 검토 필요
    large_text_pt: Criterion  # 큰 글자 기준 (명암비 3:1 적용)
    large_bold_text_pt: Criterion  # 굵은 글씨의 큰 글자 기준
    contrast_normal: Criterion
    contrast_large: Criterion
    safe_margin_ratio: Criterion
    max_lines: Criterion  # 정보 밀도: 슬라이드 전체 줄 수
    max_chars: Criterion  # 정보 밀도: 슬라이드 전체 글자 수 (공백 제외)
    min_line_spacing: Criterion  # B에 없음
    title_body_ratio: Criterion  # B에 없음

    def criteria(self) -> list[tuple[str, Criterion]]:
        """화면·보고서 출력용 (이름, 기준) 목록"""
        return [
            ("최소 글자 크기(pt)", self.min_font_pt),
            ("권장 글자 크기(pt)", self.recommended_font_pt),
            ("큰 글자 기준(pt)", self.large_text_pt),
            ("큰 글자 기준-굵게(pt)", self.large_bold_text_pt),
            ("명암비-일반 글자", self.contrast_normal),
            ("명암비-큰 글자", self.contrast_large),
            ("가장자리 여백(비율)", self.safe_margin_ratio),
            ("최대 줄 수", self.max_lines),
            ("최대 글자 수", self.max_chars),
            ("최소 줄 간격(배)", self.min_line_spacing),
            ("제목/본문 크기 비", self.title_body_ratio),
        ]


def build_standards(purpose: Purpose = Purpose.PRESENTATION) -> Standards:
    """B의 기준값으로 Standards를 만듭니다. purpose는 B의 Purpose."""
    purpose = Purpose(purpose)  # 문자열로 와도 B의 이름만 허용 (모르는 값이면 ValueError)
    font = team.font_size_threshold(purpose)
    density = team.density_threshold(purpose)
    label = PURPOSE_LABELS[purpose]
    return Standards(
        purpose=purpose.value,
        label=label,
        min_font_pt=Criterion(font.minimum_pt, f"{SOURCE} {label} 최소 크기"),
        recommended_font_pt=Criterion(font.recommended_pt, f"{SOURCE} {label} 권장 크기"),
        large_text_pt=Criterion(team.LARGE_TEXT_PT, f"{SOURCE} WCAG 큰 글자"),
        large_bold_text_pt=Criterion(team.LARGE_BOLD_TEXT_PT, f"{SOURCE} WCAG 큰 글자(굵게)"),
        contrast_normal=Criterion(team.CONTRAST_NORMAL, f"{SOURCE} WCAG 2.2 AA 일반 글자"),
        contrast_large=Criterion(team.CONTRAST_LARGE, f"{SOURCE} WCAG 2.2 AA 큰 글자"),
        safe_margin_ratio=Criterion(team.SAFE_MARGIN_RATIO, f"{SOURCE} 안전 여백"),
        max_lines=Criterion(density.max_lines, f"{SOURCE} {label} 정보 밀도(줄)"),
        max_chars=Criterion(density.max_chars, f"{SOURCE} {label} 정보 밀도(글자)"),
        min_line_spacing=Criterion(None, NO_TEAM_CRITERION),
        title_body_ratio=Criterion(None, NO_TEAM_CRITERION),
    )
