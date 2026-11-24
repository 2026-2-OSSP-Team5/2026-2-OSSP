"""그림 속 글자 (Windows 문자 인식 결과를 판정에 쓰기)."""

import json
from types import SimpleNamespace

from PIL import Image, ImageDraw

from slideguard.adapters.powerpoint import image_text
from slideguard.checks.image_text import ImageTextRule
from slideguard.domain.models import Box, DeckSnapshot, Shape
from slideguard.domain.policy import AnalysisPolicy


def _picture(**changes):
    shape = Shape(1, 4, "그림 1", Box(96, 54, 480, 270), shape_type=13, supported=True)
    return Shape(**{**shape.__dict__, **changes})


class FakeSlide:
    """그림 영역 왼쪽 위에 연회색 글자 띠가 있는 슬라이드. Export하면 그 그림을 저장한다."""

    def __init__(self):
        self.Shapes = SimpleNamespace(Count=1, Item=lambda _i: SimpleNamespace(Id=4, Visible=-1))

    def Export(self, path, _kind, width, height):
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        scale = width / 960
        # 그림 안 (100, 60)pt 위치에 높이 12pt짜리 글자 줄 (연회색 = 흰 배경과 대비 낮음)
        left, top = round(100 * scale), round(60 * scale)
        for i in range(0, round(200 * scale), 6):
            draw.rectangle((left + i, top, left + i + 3, top + round(12 * scale)), fill=(190, 190, 190))
        image.save(path)


def test_picture_text_lines_become_size_and_contrast_measurements(tmp_path):
    presentation = SimpleNamespace(Slides=SimpleNamespace(Item=lambda _n: FakeSlide()))
    deck = DeckSnapshot("d.pptx", "0" * 64, 1, 960, 540, shapes=(_picture(),))
    scale = image_text.EXPORT_WIDTH / 960

    def runner(paths, _folder):
        # 그림 안 좌표(px): 그림 왼쪽 위가 (96, 54)pt
        line = {"text": "작은 설명 글자", "x": 4 * scale, "y": 6 * scale, "w": 200 * scale, "h": 12 * scale}
        return {str(paths[0]): [line]}

    result = image_text.read_image_text(presentation, deck, tmp_path, runner=runner)
    [line] = json.loads(dict(result.shapes[0].properties)["image_text"])
    assert line["text"] == "작은 설명 글자"
    assert line["size_pt"] == round(12 / image_text.GLYPH_PER_EM, 1)
    assert line["contrast"] < 2  # 연회색 글자 / 흰 배경
    assert line["box"][:2] == [100.0, 60.0]
    assert list(tmp_path.iterdir()) == []  # 임시 그림은 지움


def test_ocr_failure_leaves_the_snapshot_alone(tmp_path):
    presentation = SimpleNamespace(Slides=SimpleNamespace(Item=lambda _n: FakeSlide()))
    deck = DeckSnapshot("d.pptx", "0" * 64, 1, 960, 540, shapes=(_picture(),))

    def broken(_paths, _folder):
        raise RuntimeError("no-ocr-language")

    assert image_text.read_image_text(presentation, deck, tmp_path, runner=broken) is deck


def test_small_and_faint_picture_text_is_reviewed():
    lines = [
        {"text": "출처 표기", "box": [100, 300, 80, 9], "size_pt": 10.6, "contrast": 6.0},
        {"text": "큰 제목", "box": [100, 80, 200, 30], "size_pt": 35.0, "contrast": 1.8},
    ]
    shape = _picture(properties=(("image_text", json.dumps(lines, ensure_ascii=False)),))
    deck = DeckSnapshot("d.pptx", "0" * 64, 1, 960, 540, shapes=(shape,))
    findings = {f.rule_id: f for f in ImageTextRule().evaluate(deck, AnalysisPolicy())}
    assert findings["image_text.size"].measured == 10.6 and findings["image_text.size"].expected == 14
    assert findings["image_text.contrast"].expected == 3.0  # 큰 글자 기준
    assert all(f.severity == "review" for f in findings.values())
    assert ImageTextRule().evaluate(deck, AnalysisPolicy(image_text=False)) == []


def test_ocr_timeout_kills_powershell_and_skips_later_files(tmp_path, monkeypatch):
    """문자 인식이 멈추면 제한 시간 뒤 끝내고, 이후 파일에서는 다시 기다리지 않음 (검사 대기 멈춤 방지)."""
    import subprocess

    from slideguard.adapters.powerpoint import image_text

    class Hanging:
        pid = 1

        def __init__(self, *args, **kwargs):
            assert kwargs["stdin"] is subprocess.DEVNULL  # 입력을 기다리며 멈추지 않도록

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("powershell", timeout)

        def kill(self):
            pass

    monkeypatch.setattr(image_text.subprocess, "Popen", Hanging)
    monkeypatch.setattr(image_text, "_kill_tree", lambda process: None)
    monkeypatch.setattr(image_text, "_disabled_reason", None)
    import pytest

    with pytest.raises(RuntimeError):
        image_text._run_ocr([tmp_path / "a.png"], tmp_path)
    assert image_text._disabled_reason is not None
