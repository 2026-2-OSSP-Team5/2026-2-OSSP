"""자동 축소 상자의 실제 글자 크기 계산 (함정 PPT 4쪽: 화면 20pt를 파일에 저장된 비율대로 13pt로 읽음)."""

from slideguard.adapters.powerpoint import autofit
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from tests.test_powerpoint_text import Collection


class FakeTextBox:
    """PowerPoint 글상자 흉내: 자동 축소가 켜져 있으면 파일에 저장된 비율로 줄인 크기를 돌려준다."""

    def __init__(self, base, stored_scale, lines, height):
        self.base = base  # 원래 글자 크기
        self.stored_scale = stored_scale  # 파일에 저장된 (틀릴 수 있는) 축소 비율
        self.lines = lines
        self.Height = height
        self.Id = 5
        self.HasTextFrame = -1
        box = self

        class Font:
            @property
            def Size(self):
                return box.base * box.stored_scale if box.TextFrame2.AutoSize == 2 else box.base

            @Size.setter
            def Size(self, value):
                box.base = value

        font = Font()

        class Run:
            Start = 1
            Text = "① 급행 노선"

        Run.Font = font

        class TextRange:
            Text = "① 급행 노선"

            def Runs(self, index, length):
                return Run()

            @property
            def BoundHeight(self):
                return box.lines * box.base * 1.2

        class Frame:
            AutoSize = 2
            MarginTop = 3.6
            MarginBottom = 3.6

        Frame.TextRange = TextRange()
        self.TextFrame2 = Frame()


def _run(box, stored_size):
    shape = Shape(
        4,
        5,
        "개선안 목록",
        Box(43, 137, 547, 245),
        text="① 급행",
        runs=(TextRun("① 급행", stored_size),),
        supported=True,
    )
    deck = DeckSnapshot("t.pptx", "0" * 64, 4, 960, 540, shapes=(shape,))
    slide = type("Slide", (), {"Shapes": Collection([box])})()
    presentation = type("P", (), {"Slides": Collection([None, None, None, slide])})()
    return autofit.refresh_shrunk_text(presentation, deck)


def test_stale_shrink_ratio_is_replaced_by_what_actually_fits():
    # 24pt 6줄 = 172.8pt. 줄 간격을 20% 줄여도 상자 안쪽(125 - 7.2)에는 85%여야 들어간다
    box = FakeTextBox(base=24.0, stored_scale=0.55, lines=6, height=125.0)
    snapshot = _run(box, 13.2)
    assert snapshot.shapes[0].runs[0].font_size_pt == 20.4  # 24 × 85% (화면 20pt 근처)
    assert box.TextFrame2.AutoSize == 2 and box.base == 24.0  # 사본도 원래대로


def test_correct_stored_ratio_is_left_alone():
    box = FakeTextBox(base=24.0, stored_scale=0.55, lines=6, height=84.0)  # 정말 55%까지 줄여야 들어감
    deck_size = 13.2
    snapshot = _run(box, deck_size)
    assert snapshot.shapes[0].runs[0].font_size_pt == deck_size
