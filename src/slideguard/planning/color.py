"""색 계산 도우미 (표준 라이브러리만 사용).

명암비 공식은 WCAG 2.x 정의를 따릅니다.
※ checks(B 담당)에도 같은 공식이 있다면 한 곳으로 합쳐 같이 쓰는 것이 좋습니다.
"""

from __future__ import annotations

import colorsys
import math


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """'FF8800' -> (255, 136, 0)"""
    h = hex_color.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"RRGGBB 형식이 아닙니다: {hex_color!r}")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"{r:02X}{g:02X}{b:02X}"


def relative_luminance(hex_color: str) -> float:
    """WCAG 상대 휘도 (0 = 검정, 1 = 흰색)."""
    values = []
    for channel in hex_to_rgb(hex_color):
        c = channel / 255
        # sRGB 감마를 풀어 실제 밝기로 바꿉니다.
        if c <= 0.04045:
            values.append(c / 12.92)
        else:
            values.append(((c + 0.055) / 1.055) ** 2.4)
    r, g, b = values
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    """두 색의 명암비 (1.0 ~ 21.0)."""
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def color_distance(a: str, b: str) -> float:
    """두 색이 얼마나 다른지 0~1로 나타낸 값 (변경 비용 계산용).

    정확한 지각 거리(ΔE)는 아니지만, 후보끼리 비교하기에는 충분합니다.
    """
    ra, ga, ba = hex_to_rgb(a)
    rb, gb, bb = hex_to_rgb(b)
    diff = ((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5
    max_diff = (3 * 255**2) ** 0.5
    return diff / max_diff


def _with_lightness(hex_color: str, lightness: float) -> str:
    """색상(H)과 채도(S)는 그대로 두고 밝기(L)만 바꿉니다."""
    r, g, b = (c / 255 for c in hex_to_rgb(hex_color))
    h, _l, s = colorsys.rgb_to_hls(r, g, b)
    nr, ng, nb = colorsys.hls_to_rgb(h, lightness, s)
    return rgb_to_hex(round(nr * 255), round(ng * 255), round(nb * 255))


def nearest_passing_color(fg: str, bg: str, required: float) -> str | None:
    """글자색의 색상은 유지하고 밝기만 조절해 기준을 넘는 가장 가까운 색을 찾습니다.

    어둡게 하는 방향과 밝게 하는 방향을 모두 찾아 원래 색에 더 가까운 쪽을 고릅니다.
    둘 다 실패하면 None (호출하는 쪽에서 검정/흰색 같은 대안을 판단).
    """
    r, g, b = (c / 255 for c in hex_to_rgb(fg))
    _h, start_l, _s = colorsys.rgb_to_hls(r, g, b)

    candidates = []
    for target_l in (0.0, 1.0):  # 0.0 = 어둡게, 1.0 = 밝게
        # 끝까지 가도 기준을 못 넘으면 이 방향은 불가능
        if contrast_ratio(_with_lightness(fg, target_l), bg) < required:
            continue

        # 이분 탐색: 기준을 넘는 지점 중 원래 밝기에 가장 가까운 곳
        low = start_l  # 기준 미달 쪽
        high = target_l  # 기준 통과 쪽
        for _ in range(30):
            mid = (low + high) / 2
            if contrast_ratio(_with_lightness(fg, mid), bg) >= required:
                high = mid
            else:
                low = mid
        color = _with_lightness(fg, high)

        # 반올림 때문에 기준 바로 아래로 떨어지는 경우를 한 칸씩 보정
        step = 0.002 if target_l == 1.0 else -0.002
        lightness = high
        while contrast_ratio(color, bg) < required and 0.0 <= lightness <= 1.0:
            lightness += step
            color = _with_lightness(fg, min(1.0, max(0.0, lightness)))

        if contrast_ratio(color, bg) >= required:
            candidates.append(color)

    if not candidates:
        return None
    return min(candidates, key=lambda c: color_distance(fg, c))


def blend(top: str, bottom: str, alpha: float) -> str:
    """top 색을 불투명도 alpha로 bottom 위에 겹친 색 (sRGB 값 그대로 섞음, PowerPoint 투명도 가정)."""
    t = hex_to_rgb(top)
    b = hex_to_rgb(bottom)
    mixed = [round(alpha * t[i] + (1 - alpha) * b[i]) for i in range(3)]
    return rgb_to_hex(mixed[0], mixed[1], mixed[2])


def saturation(hex_color: str) -> float:
    """HSV 채도 (0 = 무채색 회색/검정/흰색, 1 = 원색)."""
    values = hex_to_rgb(hex_color)
    high = max(values)
    low = min(values)
    if high == 0:
        return 0.0
    return (high - low) / high


def worst_contrast_over_unknown(fg: str, scrim: str, alpha: float) -> float:
    """배경판(scrim)을 불투명도 alpha로 깔았을 때, 그 아래 배경이 '무엇이든' 보장되는 최소 명암비.

    아래 배경을 모르므로 검정~흰색 전체를 가정합니다.
    섞인 배경의 밝기는 아래 배경이 검정일 때 가장 어둡고 흰색일 때 가장 밝습니다.
    글자 밝기가 그 사이에 있으면 명암비가 1:1까지 떨어질 수 있습니다.
    """
    darkest = blend(scrim, "000000", alpha)
    lightest = blend(scrim, "FFFFFF", alpha)
    l_text = relative_luminance(fg)
    if relative_luminance(darkest) <= l_text <= relative_luminance(lightest):
        return 1.0
    return min(contrast_ratio(fg, darkest), contrast_ratio(fg, lightest))


# ======================================================================
# 색 조화 점수 (Ou & Luo 2006, 두 색 조화 모델)
#   CH = HC + HL + HH. 0보다 크면 조화롭다고 봅니다.
#   상수는 원 논문을 인용한 특허 문서(US 10107687)의 식을 옮겼습니다. 원 논문과 대조 필요.
# ======================================================================


def to_lab(hex_color: str) -> tuple[float, float, float]:
    """sRGB → CIELAB (D65 기준). 반환: (L*, a*, b*)"""
    linear = []
    for channel in hex_to_rgb(hex_color):
        c = channel / 255
        linear.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = linear
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t):
        if t > (6 / 29) ** 3:
            return t ** (1 / 3)
        return t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _lch(hex_color: str) -> tuple[float, float, float]:
    lightness, a, b = to_lab(hex_color)
    chroma = math.hypot(a, b)
    hue = math.degrees(math.atan2(b, a)) % 360
    return lightness, chroma, hue


def _hue_effect(lightness: float, chroma: float, hue: float) -> float:
    """HSY = EC × (HS + EY): 한 색이 조화에 주는 영향"""
    ec = 0.5 + 0.5 * math.tanh(-2 + 0.5 * chroma)
    hs = -0.08 - 0.14 * math.sin(math.radians(hue + 50)) - 0.07 * math.sin(math.radians(2 * hue + 90))
    k = (90 - hue) / 10
    ey = ((0.22 * lightness - 12.8) / 10) * math.exp(k - math.exp(k))
    return ec * (hs + ey)


def harmony_score(color1: str, color2: str) -> float:
    """두 색의 조화 점수 (높을수록 조화로움, 0 초과면 조화)."""
    l1, c1, h1 = _lch(color1)
    l2, c2, h2 = _lch(color2)

    # 색상·채도 차이
    dh = math.radians((h1 - h2 + 180) % 360 - 180)
    delta_h = 2 * math.sqrt(c1 * c2) * math.sin(dh / 2)
    delta_c = c1 - c2
    hc = 0.04 + 0.53 * math.tanh(0.8 - 0.045 * math.sqrt(delta_h**2 + (delta_c / 1.46) ** 2))

    # 밝기
    hl_sum = 0.3 + 0.5 * math.tanh(-4 + 0.029 * (l1 + l2))
    hl_diff = 0.14 + 0.15 * math.tanh(-2 + 0.2 * abs(l1 - l2))

    # 색상 자체의 영향
    hh = _hue_effect(l1, c1, h1) + _hue_effect(l2, c2, h2)
    return hc + hl_sum + hl_diff + hh
