"""Plain-language labels for changes and reasons, without exposing internal IDs."""

import colorsys
import re

READABILITY_LABELS = {
    "spacing": "줄 간격",
    "alignment": "본문 정렬",
    "line_length": "한 줄 길이",
    "italic": "기울임 강조",
    "underline": "밑줄 강조",
    "capitals": "영문 대문자",
    "graphic_contrast": "그래픽 대비",
    "chart_contrast": "그래프 대비",
    "color_only": "색상 구분",
    "caption_distance": "그림 설명",
    "message": "제목 내용",
}


def readability_label(rule_id):
    return READABILITY_LABELS.get(rule_id.split(".")[1], "읽기 편의")


def _number(value, unit=""):
    return f"{float(value):.2f}".rstrip("0").rstrip(".") + unit


def _distance(value):
    return _number(float(value) * 25.4 / 72, "mm")


def color_name(value):
    if isinstance(value, (tuple, list)) and len(value) == 3:
        rgb = value
    else:
        color = str(value).lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
            return "색상"
        rgb = tuple(bytes.fromhex(color))
    hue, saturation, brightness = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
    if brightness < 0.12:
        return "검정"
    if saturation < 0.12:
        if brightness > 0.94:
            return "흰색"
        return "진한 회색" if brightness < 0.4 else "연한 회색" if brightness > 0.75 else "회색"
    hue *= 360
    if hue < 15 or hue >= 345:
        name = "빨강"
    elif hue < 45:
        name = "주황"
    elif hue < 70:
        name = "노랑"
    elif hue < 165:
        name = "초록"
    elif hue < 200:
        name = "청록"
    elif hue < 260:
        name = "파랑"
    elif hue < 300:
        name = "보라"
    else:
        name = "분홍"
    prefix = "진한 " if brightness < 0.6 else "연한 " if saturation < 0.4 else ""
    return prefix + name + " 계열"


def change_text(proposal):
    prop, before, after = proposal.property, proposal.before, proposal.after
    if prop == "slide.split":
        return f"본문을 {int(after)}장으로 나누기 · 원문·순서 보존"
    if prop == "shape.position" and isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        dx, dy = float(after[0]) - float(before[0]), float(after[1]) - float(before[1])
        moves = []
        if abs(dy) >= 0.05:
            moves.append(f"{'아래로' if dy > 0 else '위로'} {_distance(abs(dy))}")
        if abs(dx) >= 0.05:
            moves.append(f"{'오른쪽으로' if dx > 0 else '왼쪽으로'} {_distance(abs(dx))}")
        return "위치 · " + (", ".join(moves) if moves else "현재 위치 유지")
    if prop == "font.color":
        old_name, new_name = color_name(before), color_name(after)
        if old_name == new_name and before != after:

            def lightness(value):
                rgb = tuple(bytes.fromhex(value.lstrip("#"))) if isinstance(value, str) else value
                return sum(float(v) * weight for v, weight in zip(rgb, (0.2126, 0.7152, 0.0722), strict=True))

            try:
                difference = lightness(after) - lightness(before)
                adjustment = "더 밝게" if difference > 1 else "더 어둡게" if difference < -1 else "색상 조정"
                new_name += f" ({adjustment})"
            except (ValueError, TypeError):
                new_name = "다른 색상"
        return f"글자색 · {old_name} → {new_name}"
    if prop == "shape.fill":

        def fill(value):
            if value is None:
                return "배경 없음"
            if isinstance(value, dict):
                color = color_name(value.get("color", ""))
                opacity = round((1 - float(value.get("transparency", 0))) * 100)
                return f"{color} (불투명도 {opacity}%)"
            return color_name(value)

        return f"글자 뒤 배경 · {fill(before)} → {fill(after)}"
    if prop == "shape.zorder":
        return "앞뒤 순서 · 글자가 가려지지 않도록 조정"
    if prop == "text.autofit":
        modes = {"none": "사용 안 함", "shrink": "글자 축소", "grow": "글상자 확장"}
        return f"자동 맞춤 · {modes.get(str(before), '기존 설정')} → {modes.get(str(after), '새 설정')}"
    labels = {
        "font.size": ("글자 크기", lambda v: _number(v, "pt")),
        "shape.height": ("글상자 높이", _distance),
        "shape.width": ("글상자 너비", _distance),
        "paragraph.line_spacing": ("줄 간격", lambda v: _number(v, "배")),
        "font.family": ("글꼴", str),
    }
    if prop in labels:
        label, format_value = labels[prop]
        return f"{label} · {format_value(before)} → {format_value(after)}"
    return "객체 서식 변경"


def friendly_reason(reason):
    """Keep already clear reasons; translate internal planner vocabulary."""
    if not reason:
        return ""
    if "고정" in reason and ("후보" in reason or "분할" in reason):
        return "이 객체나 변경할 항목이 고정되어 있습니다. 수정하려면 설정에서 고정을 해제해 주세요."
    if "예산" in reason or "디자인 영역" in reason or "수정 수준" in reason:
        return "현재 수정 강도에서는 원래 디자인이 크게 바뀌는 변경을 제안하지 않습니다. 필요하면 수정 강도를 높여 다시 검사해 주세요."
    if "사람" in reason and ("검토" in reason or "판단" in reason):
        if "공간" in reason or "객체(ID" in reason:
            return (
                "글상자를 늘리면 다른 객체와 겹칠 수 있습니다. 주변에 공간을 확보하거나 본문을 나눠 주세요."
            )
        return "의도한 디자인일 수 있어 자동으로 바꾸지 않았습니다. 해당 위치를 확인해 주세요."
    if "스냅샷" in reason:
        return "검사한 객체를 다시 찾지 못했습니다. PowerPoint에서 저장한 뒤 다시 검사해 주세요."
    if "측정 불가" in reason or "알 수 없는 판정" in reason:
        return "판단에 필요한 정보를 확인하지 못해 자동 수정하지 않았습니다. PowerPoint에서 해당 위치를 확인해 주세요."
    if "그룹 안 객체" in reason or "지원하지 않는 객체" in reason:
        return "이 종류의 객체는 아직 자동으로 수정할 수 없습니다. PowerPoint에서 직접 확인해 주세요."
    if "분할 후보 검증 실패" in reason:
        return "PowerPoint에서 분할 결과를 확인하지 못해 제안에서 제외했습니다. 파일을 저장한 뒤 다시 검사해 주세요."
    if re.search(r"(?:0x[0-9a-fA-F]{6,}|com_error|Traceback|\b[A-Za-z]+Error\b|z_order)", reason):
        return "PowerPoint에서 필요한 정보를 확인하지 못했습니다. 파일을 저장한 뒤 다시 시도해 주세요."
    return reason


def friendly_error(message):
    if "PermissionError" in message or "WinError 5" in message:
        return "파일에 접근할 수 없습니다. 다른 프로그램에서 사용 중인지 확인하거나 다른 저장 폴더를 선택해 주세요."
    if "FileNotFoundError" in message:
        return "파일을 찾지 못했습니다. 파일이 이동되거나 삭제되지 않았는지 확인해 주세요."
    clean = re.sub(r"^[A-Za-z_]+(?:Error|Exception):\s*", "", message)
    if not re.search(r"[가-힣]", clean) or re.search(r"com_error|0x[0-9a-fA-F]+|Traceback|\(-214", clean):
        return "PowerPoint에서 작업을 완료하지 못했습니다. 파일을 저장하고 열려 있는 대화상자를 닫은 뒤 다시 시도해 주세요."
    return clean
