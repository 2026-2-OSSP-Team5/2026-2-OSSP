"""배경을 따로 지정하지 않은 슬라이드의 배경색 판정 (가짜 PowerPoint 객체).

채우기 없는 글상자는 투명이므로, 뒤에 보이는 슬라이드 배경을
슬라이드 → 레이아웃 → 마스터 순으로 따라가 읽어야 명암비를 판정할 수 있습니다.
"""

from slideguard.adapters.powerpoint.background import effective_slide_background, read_background
from slideguard.domain.models import Box

TRUE, FALSE = -1, 0
WHITE, NAVY = (255, 255, 255), (0, 32, 96)
TEXT = Box(100, 100, 300, 50)


def bgr(rgb):
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


class ColorFormat:
    def __init__(self, rgb):
        self.Type = 1  # msoColorTypeRGB
        self.RGB = bgr(rgb)


class Fill:
    def __init__(self, rgb=WHITE, *, kind=1, visible=TRUE, transparency=0.0):
        self.Visible = visible
        self.Type = kind  # 1 = 단색, 3 = 그라데이션, 6 = 그림
        self.Transparency = transparency
        self.ForeColor = ColorFormat(rgb)


class Background:
    def __init__(self, fill):
        self.Fill = fill


class Effect:
    def __init__(self, visible=FALSE, radius=0):
        self.Visible = visible
        self.Radius = radius


class Decoration:
    def __init__(self, box, *, kind=1, visible=TRUE, rotation=0, shadow=FALSE):
        self.Type = kind
        self.Visible = visible
        self.Left, self.Top, self.Width, self.Height = box.x, box.y, box.width, box.height
        self.Rotation = rotation
        self.Shadow = Effect(visible=shadow)
        self.Glow = Effect()
        self.SoftEdge = Effect()


def placeholder():
    return Decoration(Box(50, 50, 600, 400), kind=14)  # 레이아웃의 제목·본문 자리 표시자


class Shapes:
    def __init__(self, *items):
        self.items = list(items)

    @property
    def Count(self):  # noqa: N802 (COM 이름)
        return len(self.items)

    def Item(self, index):  # noqa: N802
        return self.items[index - 1]


class Master:
    def __init__(self, fill=None, shapes=()):
        self.Background = Background(fill or Fill(WHITE))
        self.Shapes = Shapes(placeholder(), *shapes)


class Layout:
    def __init__(self, fill=None, *, follows=TRUE, display_master=TRUE, shapes=()):
        self.FollowMasterBackground = follows
        self.DisplayMasterShapes = display_master
        self.Background = Background(fill or Fill(NAVY))
        self.Shapes = Shapes(placeholder(), *shapes)


class Slide:
    def __init__(self, layout=None, master=None, *, fill=None, follows=TRUE, display_master=TRUE):
        self.FollowMasterBackground = follows
        self.DisplayMasterShapes = display_master
        self.CustomLayout = layout or Layout()
        self.Master = master or Master()
        self.Background = Background(fill or Fill(NAVY))
        self.HeadersFooters = HeadersFooters()


class Visibility:
    def __init__(self, visible=FALSE):
        self.Visible = visible


class HeadersFooters:
    def __init__(self):
        self.DateAndTime = Visibility()
        self.Footer = Visibility()
        self.SlideNumber = Visibility()


def background(slide):
    return effective_slide_background(slide, TEXT, [])


def test_default_deck_reads_theme_background_through_layout_and_master():
    # 슬라이드·레이아웃 모두 마스터 배경을 따르고, 레이아웃·마스터에는 자리 표시자만 있음
    assert background(Slide()) == WHITE


def test_slide_own_background_is_used():
    assert background(Slide(fill=Fill((250, 240, 200)), follows=FALSE)) == (250, 240, 200)


def test_layout_background_is_used_when_slide_follows_it():
    layout = Layout(Fill((230, 230, 230)), follows=FALSE)
    assert background(Slide(layout)) == (230, 230, 230)


def test_decoration_behind_text_makes_background_unknown():
    logo = Decoration(Box(90, 90, 40, 40))  # 글자 영역과 겹치는 레이아웃 로고
    assert background(Slide(Layout(shapes=(logo,)))) is None


def test_decoration_elsewhere_does_not_matter():
    band = Decoration(Box(0, 500, 720, 40))  # 아래쪽 띠 (글자와 안 겹침)
    assert background(Slide(Layout(shapes=(band,)))) == WHITE


def test_master_decoration_counts_only_when_shown():
    logo = Decoration(Box(90, 90, 40, 40))
    assert background(Slide(master=Master(shapes=(logo,)))) is None
    assert background(Slide(Layout(display_master=FALSE), Master(shapes=(logo,)))) == WHITE
    assert background(Slide(master=Master(shapes=(logo,)), display_master=FALSE)) == WHITE


def test_hidden_decoration_is_ignored():
    logo = Decoration(Box(90, 90, 40, 40), visible=FALSE)
    assert background(Slide(Layout(shapes=(logo,)))) == WHITE


def test_rotated_or_shadowed_decoration_is_treated_as_covering():
    rotated = Decoration(Box(0, 500, 720, 40), rotation=15)
    shadowed = Decoration(Box(0, 500, 720, 40), shadow=TRUE)
    assert background(Slide(Layout(shapes=(rotated,)))) is None
    assert background(Slide(Layout(shapes=(shadowed,)))) is None


def test_gradient_picture_or_transparent_background_stays_unknown():
    for fill in (Fill(kind=3), Fill(kind=6), Fill(transparency=0.3)):
        assert background(Slide(master=Master(fill))) is None


def test_unreadable_inheritance_is_not_guessed():
    slide = Slide()
    slide.FollowMasterBackground = "mixed"
    assert background(slide) is None


class TextBox:
    """채우기 없는 일반 텍스트 상자 (슬라이드에 이 상자 하나)."""

    ZOrderPosition = 1
    AutoShapeType = 1

    def __init__(self):
        self.Line = Effect(visible=FALSE)
        self.Fill = Fill(visible=FALSE)


def test_unfilled_text_box_gets_inherited_background_for_contrast():
    notes = []
    color = read_background(
        TextBox(),
        Slide(),
        box=Box(80, 80, 340, 90),
        rendered_box=TEXT,
        shape_type=17,
        shape_count=1,
        notes=notes,
    )
    assert color == WHITE
