"""여러 도형이 있는 슬라이드의 배경 판정: 글자에 닿는 도형만 엄격하게 확인합니다 (가짜 PowerPoint)."""

from slideguard.domain.models import Box, Shape, TextRun
from tests.test_powerpoint_text import Properties, extract, make_slide, shape

SLIDE_BACKGROUND = (102, 136, 170)  # make_slide의 배경 0xAA8866 (BGR)


def no_effects(item, *, shadow=0):
    item.Shadow = Properties(Visible=shadow)
    item.Glow = Properties(Radius=0)
    item.SoftEdge = Properties(Radius=0)
    return item


def unfilled_target():
    target = no_effects(shape())
    target.Fill.Visible = 0  # 글상자 채우기 없음 → 뒤의 배경이 보임
    return target


def far_picture():
    picture = no_effects(shape(shape_type=13, shape_id=99))
    picture.TextFrame = AssertionError("Do not read unsupported objects")
    picture.Left, picture.Top, picture.Width, picture.Height = 600.0, 400.0, 200.0, 100.0
    picture.AutoShapeType = -2  # 사각형이 아님
    return picture


def bar(rotation, *, shadow=0):
    item = no_effects(shape(shape_id=98), shadow=shadow)
    item.TextFrame = AssertionError("Do not read unsupported objects")
    item.Type = 13
    item.Left, item.Top, item.Width, item.Height = -100.0, 60.0, 300.0, 10.0
    item.Rotation = float(rotation)
    return item


def background_of_target(*others):
    slide = make_slide([*others, unfilled_target()])
    slide.SlideShowTransition = Properties(Hidden=0)  # 실제 PowerPoint처럼 '숨기지 않은 슬라이드'
    result = extract(slide)
    return result.shapes[-1].background


def test_picture_far_from_text_does_not_block_slide_background():
    assert background_of_target(far_picture()) == SLIDE_BACKGROUND


def test_rotation_is_used_to_decide_whether_a_shape_reaches_the_text():
    # 회전 전에는 글자 아래쪽에 있지만, 90° 돌리면 글자 영역을 세로로 지나감
    assert background_of_target(bar(0)) == SLIDE_BACKGROUND
    assert background_of_target(bar(90)) is None


def test_shape_with_spreading_effect_stays_unknown_even_when_far():
    assert background_of_target(bar(0, shadow=-1)) is None


def test_samples_are_ignored_for_shapes_with_known_background():
    # 렌더링 표본은 배경을 모를 때만 쓰임 (모델 기본값 확인)
    item = Shape(
        1, 1, "s", Box(0, 0, 10, 10), runs=(TextRun("a", 20, (0, 0, 0), False),), background=(255, 255, 255)
    )
    assert item.background_samples == ()


def test_picture_behind_text_keeps_background_unknown_for_pixel_judgment():
    picture = far_picture()
    picture.Left, picture.Top = 0.0, 0.0  # 글자 바로 뒤의 사진 → 단색 배경이 아님
    assert background_of_target(picture) is None
