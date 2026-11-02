"""복사본 적용 동작 테스트. PowerPoint 대신 가짜 문서를 사용합니다.
(실제 COM 동작은 Windows + PowerPoint에서 따로 확인해야 합니다.)
"""

from contextlib import contextmanager

import pytest
from fakes import FakePolicy

from slideguard.adapters.powerpoint.patcher import ApplyRejected, PowerPointPatcher, sha256_of
from slideguard.planning import ChangeProposal, Property

# ---------------------------------------------------------------- 가짜 문서


class DocRun:
    def __init__(self, size, color, font="맑은 고딕"):
        self.size = size
        self.color = color
        self.font = font

    def get_size(self):
        return self.size

    def set_size(self, v):
        self.size = v

    def get_color(self):
        return self.color

    def set_color(self, v):
        self.color = v

    def get_font_names(self):
        return [self.font]

    def set_font_name(self, name):
        self.font = name


class DocParagraph:
    def __init__(self, spacing):
        self.spacing = spacing

    def get_line_spacing(self):
        return self.spacing

    def set_line_spacing(self, v):
        self.spacing = v


class DocShape:
    def __init__(self, runs=(), geometry=(50, 50, 400, 100), overflow=False, fill=None, autofit="none", spacings=()):
        self._runs = list(runs)
        self.geometry = list(geometry)
        self.overflow = overflow
        self.fill = fill
        self.autofit = autofit
        self._paragraphs = [DocParagraph(s) for s in spacings]
        self.in_front = False

    def runs(self):
        return self._runs

    def paragraphs(self):
        return self._paragraphs

    def text_overflows(self):
        return self.overflow

    def get_geometry(self):
        return tuple(self.geometry)

    def set_height(self, h):
        self.geometry[3] = h

    def set_position(self, left, top):
        self.geometry[0] = left
        self.geometry[1] = top

    def bring_to_front(self):
        self.in_front = True

    def get_fill(self):
        return self.fill

    def set_fill(self, color, transparency):
        self.fill = (color, transparency)

    def get_autofit(self):
        return self.autofit

    def set_autofit(self, mode):
        self.autofit = mode


class Doc:
    def __init__(self, shape):
        self.shapes = {(1, 5): shape}
        self.saved = False

    def find_shape(self, slide, shape_id):
        return self.shapes.get((slide, shape_id))

    def save(self):
        self.saved = True


def opener_for(doc):
    @contextmanager
    def opener(path):
        yield doc

    return opener


@pytest.fixture
def original(tmp_path):
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"fake pptx bytes")
    return path


def proposal(prop, before, after):
    return ChangeProposal(1, 5, prop, before, after, 0.1, "test")


def run_apply(original, tmp_path, shape, proposals, policy=None):
    # 이미 있는 파일에는 쓰지 않으므로, 호출할 때마다 새 출력 이름을 씀
    number = len(list(tmp_path.glob("out*.pptx")))
    out = tmp_path / f"out{number}.pptx"
    return PowerPointPatcher(opener_for(Doc(shape))).apply(
        str(original), sha256_of(str(original)), proposals, policy or FakePolicy(), str(out)
    )


# ---------------------------------------------------------------- 공통 안전장치


def test_applies_to_copy_and_keeps_original(original, tmp_path):
    runs = [DocRun(12.0, "999999"), DocRun(30.0, "000000")]
    before = sha256_of(str(original))
    result = run_apply(
        original,
        tmp_path,
        DocShape(runs),
        [proposal(Property.FONT_SIZE, 12.0, 22.0), proposal(Property.FONT_COLOR, "999999", "595959")],
    )
    assert len(result.applied) == 2
    assert runs[0].size == 22.0 and runs[1].size == 30.0  # 큰 글씨는 줄이지 않음
    assert runs[0].color == "595959" and runs[1].color == "000000"
    assert sha256_of(str(original)) == before


def test_rejects_when_original_changed(original, tmp_path):
    stale = sha256_of(str(original))
    original.write_bytes(b"user saved again")
    out = tmp_path / "out.pptx"
    with pytest.raises(ApplyRejected):
        PowerPointPatcher(opener_for(Doc(DocShape()))).apply(
            str(original), stale, [proposal(Property.Z_ORDER, "behind:9", "front")], FakePolicy(), str(out)
        )
    assert not out.exists()


def test_refuses_to_write_over_original(original):
    with pytest.raises(ApplyRejected):
        PowerPointPatcher(opener_for(Doc(DocShape()))).apply(
            str(original),
            sha256_of(str(original)),
            [proposal(Property.Z_ORDER, "b", "front")],
            FakePolicy(),
            str(original),
        )


def test_locked_rechecked_at_apply_time(original, tmp_path):
    shape = DocShape(geometry=(50, 50, 400, 60))
    policy = FakePolicy(locked_properties=frozenset({Property.SHAPE_HEIGHT}))
    result = run_apply(original, tmp_path, shape, [proposal(Property.SHAPE_HEIGHT, 60, 90)], policy)
    assert shape.geometry[3] == 60
    assert result.output_path is None  # 아무것도 적용 안 됨 → 복사본 삭제


def test_failure_while_editing_removes_partial_copy(original, tmp_path):
    @contextmanager
    def broken(path):
        raise RuntimeError("PowerPoint 열기 실패")
        yield  # pragma: no cover

    out = tmp_path / "o.pptx"
    with pytest.raises(RuntimeError):
        PowerPointPatcher(broken).apply(
            str(original), sha256_of(str(original)), [proposal(Property.Z_ORDER, "b", "front")], FakePolicy(), str(out)
        )
    assert not out.exists()


# ---------------------------------------------------------------- 속성별 적용


def test_height_applied_and_drift_rejected(original, tmp_path):
    shape = DocShape(geometry=(50, 50, 400, 60))
    assert run_apply(original, tmp_path, shape, [proposal(Property.SHAPE_HEIGHT, 60, 90)]).applied
    assert shape.geometry[3] == 90

    moved = DocShape(geometry=(50, 50, 400, 75))  # 사용자가 이미 높이를 바꿈
    result = run_apply(original, tmp_path, moved, [proposal(Property.SHAPE_HEIGHT, 60, 90)])
    assert result.applied == []
    assert "현재 높이" in result.rejected[0][1]


def test_position_applied(original, tmp_path):
    shape = DocShape(geometry=(800, 100, 300, 60))
    run_apply(original, tmp_path, shape, [proposal(Property.SHAPE_POSITION, (800, 100), (660, 100))])
    assert shape.geometry[:2] == [660, 100]


def test_bring_to_front(original, tmp_path):
    shape = DocShape()
    run_apply(original, tmp_path, shape, [proposal(Property.Z_ORDER, "behind:9", "front")])
    assert shape.in_front


def test_scrim_applied_only_when_no_fill(original, tmp_path):
    shape = DocShape()
    after = {"color": "000000", "transparency": 0.4}
    run_apply(original, tmp_path, shape, [proposal(Property.SHAPE_FILL, None, after)])
    assert shape.fill == ("000000", 0.4)

    filled = DocShape(fill=("FF0000", 0.0))
    result = run_apply(original, tmp_path, filled, [proposal(Property.SHAPE_FILL, None, after)])
    assert result.applied == []
    assert filled.fill == ("FF0000", 0.0)


def test_autofit_turned_off_and_overflow_warned(original, tmp_path):
    shape = DocShape(autofit="shrink", overflow=True)
    result = run_apply(original, tmp_path, shape, [proposal(Property.AUTOFIT, "shrink", "none")])
    assert shape.autofit == "none"
    assert any("넘칩니다" in w for w in result.warnings)


def test_line_spacing_widens_only_tight_paragraphs(original, tmp_path):
    shape = DocShape(spacings=(0.7, 1.2, None))
    run_apply(original, tmp_path, shape, [proposal(Property.LINE_SPACING, 0.7, 1.0)])
    assert [p.spacing for p in shape.paragraphs()] == [1.0, 1.2, None]


def test_font_family_changes_only_matching_runs(original, tmp_path):
    runs = [DocRun(24, "000000", "궁서체"), DocRun(24, "000000", "Arial")]
    run_apply(original, tmp_path, DocShape(runs), [proposal(Property.FONT_FAMILY, "궁서체", "맑은 고딕")])
    assert [r.font for r in runs] == ["맑은 고딕", "Arial"]


def test_partially_applied_bundle_is_warned(original, tmp_path):
    height = ChangeProposal(1, 5, Property.SHAPE_HEIGHT, 60, 90, 0.1, "overflow", group="overflow:1:5")
    push = ChangeProposal(1, 7, Property.SHAPE_POSITION, (50, 170), (50, 190), 0.1, "overflow", group="overflow:1:5")
    shape = DocShape(geometry=(50, 100, 400, 60))  # 객체 7은 문서에 없음 → 내리기 실패
    result = run_apply(original, tmp_path, shape, [height, push])
    assert any("일부만 적용" in w for w in result.warnings)
