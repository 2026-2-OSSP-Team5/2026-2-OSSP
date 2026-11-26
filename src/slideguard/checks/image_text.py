"""그림 속 글자 검사 (계획서 기술적 난제 1번).

읽기 단계(adapters/powerpoint/image_text.py)가 Windows 문자 인식으로 찾은 그림 속 글자 줄의
추정 크기·명암비를 보고 판정한다. 글상자가 아니라 고칠 수 없으므로 모두 '검토'로 낸다.
  - 크기: 슬라이드 글자 크기 기준(용도·청중·발표 환경 반영)의 최소값보다 작으면
  - 명암비: 일반 글자 기준보다 낮으면 (크기 추정이 큰 글자면 큰 글자 기준)
추정은 근사다: 크기는 줄 높이(잉크)를 글자 크기의 0.85배로 보고 계산, 명암비는 줄 영역 픽셀로 계산.
그림 하나에 판정 하나: 가장 작은(또는 가장 흐린) 줄을 대표로 알린다.
"""

from __future__ import annotations

import json

from slideguard.domain.models import Box, DeckSnapshot, Finding
from slideguard.domain.policy import LARGE_TEXT_PT, AnalysisPolicy

from .scope import target_shapes


class ImageTextRule:
    id = "image_text"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        if not policy.image_text:
            return []
        threshold, _notes = policy.slide_font_limits(deck.width_pt, deck.height_pt)
        findings: list[Finding] = []
        for shape in target_shapes(deck, policy):
            raw = dict(shape.properties).get("image_text")
            if not raw:
                continue
            try:
                lines = json.loads(raw)
            except ValueError:
                continue
            small = [line for line in lines if line.get("size_pt") and line["size_pt"] < threshold.minimum_pt]
            if small:
                line = min(small, key=lambda item: item["size_pt"])
                findings.append(
                    self._finding(
                        shape,
                        "size",
                        line,
                        f"그림 속 글자가 작습니다 (약 {line['size_pt']:g}pt)",
                        line["size_pt"],
                        threshold.minimum_pt,
                        f'그림 속 "{line["text"]}" 줄 높이로 추정한 크기 약 {line["size_pt"]:g}pt < 최소 '
                        f"{threshold.minimum_pt:g}pt. 기준보다 작은 줄 {len(small)}개 (Windows 문자 인식, 근사). "
                        "원본 이미지를 키우거나 글자를 글상자로 다시 쓰세요",
                    )
                )
            faint = []
            for line in lines:
                ratio = line.get("contrast")
                if ratio is None:
                    continue
                required = (
                    policy.large_contrast
                    if (line.get("size_pt") or 0) >= LARGE_TEXT_PT
                    else policy.normal_contrast
                )
                if ratio < required:
                    faint.append((line, required))
            if faint:
                line, required = min(faint, key=lambda item: item[0]["contrast"])
                findings.append(
                    self._finding(
                        shape,
                        "contrast",
                        line,
                        f"그림 속 글자가 배경과 잘 구분되지 않습니다 (약 {line['contrast']:.1f}:1)",
                        line["contrast"],
                        required,
                        f'그림 속 "{line["text"]}" 줄의 글자·배경 픽셀 명암비 약 {line["contrast"]:.2f}:1 < '
                        f"{required:g}:1 (Windows 문자 인식 영역의 픽셀로 추정, 근사)",
                    )
                )
        return findings

    def _finding(self, shape, kind, line, message, measured, expected, evidence) -> Finding:
        x, y, w, h = line.get("box") or (None, None, None, None)
        box = Box(x, y, w, h) if None not in (x, y, w, h) else shape.box
        return Finding(
            id=f"{self.id}.{kind}:{shape.key}",
            rule_id=f"{self.id}.{kind}",
            shape_key=shape.key,
            severity="review",
            message=message,
            measured=measured,
            expected=expected,
            evidence=evidence,
            box=box,
        )
