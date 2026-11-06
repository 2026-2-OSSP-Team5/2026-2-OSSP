"""선택한 수정 후보를 '복사본'에 적용합니다 (C 담당).

흐름:
  1. 저장 위치 확인 (원본 덮어쓰기·기존 파일 덮어쓰기 금지)
  2. 원본을 복사하고, 복사본의 SHA256이 분석 시점 값과 같은지 확인
  3. 고정 조건을 다시 확인
  4. PowerPoint COM으로 복사본을 창 없이 열어 변경
     → 현재 값이 후보를 만들 때와 다르면 그 후보는 거절
  5. 글자가 커진 객체는 넘침 여부를 경고로 남김
  6. 저장 후 닫기 (사용자의 PowerPoint 앱은 종료하지 않음)

재검사는 여기서 하지 않습니다. 호출하는 쪽이 ApplyResult.output_path를
다시 검사에 넣어 주세요.

입구는 두 가지입니다.
  - apply_to_copy(report, proposal_ids, output, policy): 팀 인터페이스 (application/ports.py)
  - apply(original_path, expected_sha256, proposals, policy, output_path): C 내부 후보로 직접 적용

속성별 적용 의미:
  font.size              글자 크기가 after보다 작은 런만 after로 올림
  font.color             색이 before와 같은 런만 after로 바꿈
  font.family            글꼴이 before인 런만 after로 바꿈 (라틴·한글 글꼴 모두)
  shape.height           현재 높이가 before일 때만 after로 바꿈
  shape.position         현재 위치가 before (left, top)일 때만 after로 옮김
  shape.zorder           맨 앞으로 가져옴
  shape.fill             채우기가 없을 때만 after {"color", "transparency"}로 채움
  text.autofit           현재 자동 맞춤이 before일 때만 after로 바꿈
  paragraph.line_spacing 줄 간격(배수)이 after보다 좁은 문단만 after로 넓힘

끼워넣는 지점:
  - open_document: 문서를 여는 함수 (기본 ComDocumentSession).
    A(이창환)가 COM 공용 코드를 만들면 그것으로 바꿔 넣으면 됩니다.
  - bindings: 정책에서 고정 항목을 읽는 방법 (planning/bindings.py)
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from slideguard.planning.bindings import TeamBindings
from slideguard.planning.contracts import ChangeProposal, Property

from .properties import indexed_property

TOLERANCE = 0.5  # 좌표 비교 허용 오차 (pt)

# ======================================================================
# 결과·예외
# ======================================================================


class ApplyRejected(Exception):
    """적용 전체를 거절하는 경우 (원본 변경, 잘못된 저장 위치 등)."""


@dataclass
class ApplyResult:
    output_path: str | None  # 하나도 적용 못 하면 None (복사본도 지움)
    applied: list[ChangeProposal] = field(default_factory=list)
    rejected: list[tuple[ChangeProposal, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ======================================================================
# 문서 인터페이스 (COM 구현과 테스트용 가짜가 같은 모양)
# ======================================================================


class RunHandle(Protocol):
    def get_size(self) -> float | None: ...
    def set_size(self, size_pt: float) -> None: ...
    def get_color(self) -> str | None: ...  # "RRGGBB"
    def set_color(self, hex_color: str) -> None: ...
    def get_font_names(self) -> list[str]: ...  # 라틴·한글 글꼴 이름
    def set_font_name(self, name: str) -> None: ...


class ParagraphHandle(Protocol):
    def get_line_spacing(self) -> float | None: ...  # 배수. 고정 pt 등 모르면 None
    def set_line_spacing(self, multiple: float) -> None: ...


class ShapeHandle(Protocol):
    def runs(self) -> list[RunHandle]: ...
    def paragraphs(self) -> list[ParagraphHandle]: ...
    def text_overflows(self) -> bool | None: ...  # 모르면 None
    def get_geometry(self) -> tuple[float, float, float, float]: ...  # left, top, width, height
    def set_height(self, height: float) -> None: ...
    def set_position(self, left: float, top: float) -> None: ...
    def bring_to_front(self) -> None: ...
    def get_fill(self) -> tuple[str, float] | None: ...  # (색, 투명도). 채우기 없으면 None
    def set_fill(self, hex_color: str, transparency: float) -> None: ...
    def get_autofit(self) -> str | None: ...  # "none" / "shrink" / "grow"
    def set_autofit(self, mode: str) -> None: ...


class Document(Protocol):
    def find_shape(self, slide_index: int, shape_id: int) -> ShapeHandle | None: ...
    def save(self) -> None: ...


DocumentOpener = Callable[[str], AbstractContextManager[Document]]


# ======================================================================
# 적용기
# ======================================================================


class PowerPointPatcher:
    def __init__(
        self, open_document: DocumentOpener | None = None, bindings: TeamBindings | None = None
    ) -> None:
        self._open_document = open_document or ComDocumentSession
        self._bindings = bindings or TeamBindings()
        self.last_result: ApplyResult | None = None  # apply_to_copy의 상세 결과 (거절 이유·경고)

    def apply_to_copy(
        self,
        report: object,
        proposal_ids: Iterable[str],
        output: Path | str,
        policy: object,
    ) -> Path:
        """팀 인터페이스: 보고서의 후보 중 고른 것만 복사본에 적용하고 복사본 경로를 돌려줌.

        하나도 적용하지 못하면 ApplyRejected. 거절 이유·경고는 self.last_result에 남습니다.
        """
        wanted = list(dict.fromkeys(proposal_ids))  # 순서 유지, 중복 제거
        by_id = {p.id: p for p in report.proposals}
        missing = [pid for pid in wanted if pid not in by_id]
        if missing:
            raise ApplyRejected(f"보고서에 없는 수정 후보입니다: {', '.join(missing)}")
        try:
            proposals = [self._bindings.from_team_proposal(by_id[pid]) for pid in wanted]
        except ValueError as error:
            raise ApplyRejected(str(error)) from error

        deck = report.deck
        result = self.apply(deck.source_path, deck.source_sha256, proposals, policy, str(output))
        self.last_result = result
        if result.output_path is None:
            reasons = "; ".join(reason for _p, reason in result.rejected) or "알 수 없는 이유"
            raise ApplyRejected(f"적용된 수정 후보가 없습니다: {reasons}")
        return Path(result.output_path)

    def apply(
        self,
        original_path: str,
        expected_sha256: str,
        proposals: list[ChangeProposal],
        policy: object,
        output_path: str,
    ) -> ApplyResult:
        # ---- 1. 저장 위치 확인 ----------------------------------------
        same = os.path.normcase(os.path.abspath(original_path)) == os.path.normcase(
            os.path.abspath(output_path)
        )
        if same:
            raise ApplyRejected("원본 파일에는 직접 적용하지 않습니다. 다른 저장 위치를 지정하세요.")
        if os.path.exists(output_path):
            raise ApplyRejected(f"이미 존재하는 파일입니다: {output_path}")
        if not proposals:
            raise ApplyRejected("적용할 후보가 없습니다.")

        # ---- 2. 복사 후 SHA256 확인 ------------------------------------
        shutil.copy2(original_path, output_path)
        if sha256_of(output_path).lower() != expected_sha256.lower():
            _remove_quietly(output_path)
            raise ApplyRejected("분석 이후 원본 파일이 변경되었습니다. 다시 검사한 뒤 적용하세요.")

        result = ApplyResult(output_path=output_path)

        # ---- 3. 고정 조건 재확인 ---------------------------------------
        to_apply = []
        try:
            for p in proposals:
                if self._bindings.is_locked(policy, p.slide_index, p.shape_id, p.prop):
                    result.rejected.append((p, "고정된 객체·속성"))
                else:
                    to_apply.append(p)
        except Exception:
            _remove_quietly(output_path)
            raise

        # ---- 4~6. 문서 열어 적용 ---------------------------------------
        if to_apply:
            try:
                with self._open_document(output_path) as doc:
                    for p in to_apply:
                        self._apply_one(doc, p, result)
                    if result.applied:
                        doc.save()
            except Exception:
                _remove_quietly(output_path)
                raise

        # 묶음(예: 상자 키우기 + 아래 그림 내리기) 중 일부만 적용됐으면 알림
        applied_groups = {p.group for p in result.applied if p.group}
        rejected_groups = {p.group for p, _reason in result.rejected if p.group}
        for group in sorted(applied_groups & rejected_groups):
            result.warnings.append(f"묶음 '{group}'의 일부만 적용되었습니다. 재검사로 겹침을 확인하세요.")

        if not result.applied:
            _remove_quietly(output_path)
            result.output_path = None
        return result

    # ------------------------------------------------------------------

    def _apply_one(self, doc: Document, p: ChangeProposal, result: ApplyResult) -> None:
        where = f"슬라이드 {p.slide_index} 객체 {p.shape_id}"

        shape = doc.find_shape(p.slide_index, p.shape_id)
        if shape is None:
            result.rejected.append((p, f"{where}를 찾을 수 없음"))
            return

        appliers = {
            Property.FONT_SIZE: _apply_font_size,
            Property.FONT_COLOR: _apply_font_color,
            Property.FONT_FAMILY: _apply_font_family,
            Property.SHAPE_HEIGHT: _apply_height,
            Property.SHAPE_POSITION: _apply_position,
            Property.Z_ORDER: _apply_zorder,
            Property.SHAPE_FILL: _apply_fill,
            Property.AUTOFIT: _apply_autofit,
            Property.LINE_SPACING: _apply_line_spacing,
        }
        applier = appliers.get(p.prop)
        if applier is None:
            result.rejected.append((p, f"적용 미지원 속성: {p.prop}"))
            return

        # applier는 바꾼 개수(int) 또는 거절 이유(str)를 돌려줌
        outcome = applier(shape, p)
        if isinstance(outcome, str):
            result.rejected.append((p, f"{where}: {outcome}"))
            return
        if outcome == 0:
            result.rejected.append((p, f"{where}의 현재 값이 후보 작성 시점과 달라 적용하지 않음"))
            return

        result.applied.append(p)

        # 글자가 커지는 변경은 넘칠 수 있으므로 확인 (최종 판단은 재검사에서)
        if p.prop in (Property.FONT_SIZE, Property.AUTOFIT, Property.LINE_SPACING):
            overflow = shape.text_overflows()
            if overflow is True:
                result.warnings.append(f"{where}: 변경 후 텍스트가 상자를 넘칩니다.")
            elif overflow is None:
                result.warnings.append(f"{where}: 넘침 여부를 확인하지 못했습니다. 재검사 결과를 확인하세요.")


# ======================================================================
# 속성별 적용 함수 (바꾼 개수 또는 거절 이유를 돌려줌)
# ======================================================================


def _apply_font_size(shape: ShapeHandle, p: ChangeProposal):
    target = float(p.after)
    changed = 0
    for run in shape.runs():
        size = run.get_size()
        if size is not None and size < target:  # 크기를 모르는 런은 건드리지 않음
            run.set_size(target)
            changed += 1
    return changed


def _apply_font_color(shape: ShapeHandle, p: ChangeProposal):
    before = str(p.before).upper()
    after = str(p.after).upper()
    changed = 0
    for run in shape.runs():
        color = run.get_color()
        if color is not None and color.upper() == before:
            run.set_color(after)
            changed += 1
    return changed


def _apply_font_family(shape: ShapeHandle, p: ChangeProposal):
    changed = 0
    for run in shape.runs():
        if str(p.before) in run.get_font_names():
            run.set_font_name(str(p.after))
            changed += 1
    return changed


def _apply_height(shape: ShapeHandle, p: ChangeProposal):
    _left, _top, _width, height = shape.get_geometry()
    if abs(height - float(p.before)) > TOLERANCE:
        return f"현재 높이 {height:g}pt가 후보 작성 시점({p.before}pt)과 다름"
    shape.set_height(float(p.after))
    return 1


def _apply_position(shape: ShapeHandle, p: ChangeProposal):
    left, top, _width, _height = shape.get_geometry()
    before_left, before_top = p.before
    if abs(left - before_left) > TOLERANCE or abs(top - before_top) > TOLERANCE:
        return f"현재 위치 ({left:g}, {top:g})가 후보 작성 시점과 다름"
    after_left, after_top = p.after
    shape.set_position(float(after_left), float(after_top))
    return 1


def _apply_zorder(shape: ShapeHandle, p: ChangeProposal):
    if p.after != "front":
        return f"지원하지 않는 순서 값: {p.after}"
    shape.bring_to_front()
    return 1


def _apply_fill(shape: ShapeHandle, p: ChangeProposal):
    if shape.get_fill() is not None:
        return "이미 배경 채우기가 있음 (후보 작성 시점과 다름)"
    shape.set_fill(str(p.after["color"]), float(p.after["transparency"]))
    return 1


def _apply_autofit(shape: ShapeHandle, p: ChangeProposal):
    current = shape.get_autofit()
    if current != p.before:
        return f"현재 자동 맞춤 설정({current})이 후보 작성 시점({p.before})과 다름"
    shape.set_autofit(str(p.after))
    return 1


def _apply_line_spacing(shape: ShapeHandle, p: ChangeProposal):
    target = float(p.after)
    changed = 0
    for paragraph in shape.paragraphs():
        spacing = paragraph.get_line_spacing()
        if spacing is not None and spacing < target:  # 고정 pt 간격 등 모르는 문단은 건드리지 않음
            paragraph.set_line_spacing(target)
            changed += 1
    return changed


# ======================================================================
# 파일 도우미
# ======================================================================


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ======================================================================
# PowerPoint COM 구현 (Windows + 데스크톱 PowerPoint 필요, 아직 실기 검증 전)
#   - pywin32는 안에서 import → 다른 OS에서도 이 모듈 import·테스트 가능
#   - COM은 이 객체를 쓰는 스레드에서 초기화·해제 (UI 스레드에서 쓰지 말 것)
# ======================================================================

try:
    import pywintypes  # pywin32

    _COM_ERRORS: tuple[type[Exception], ...] = (pywintypes.com_error, AttributeError, TypeError, ValueError)
except ImportError:
    _COM_ERRORS = (AttributeError, TypeError, ValueError)

MSO_FALSE = 0
MSO_TRUE = -1
MSO_BRING_TO_FRONT = 0
AUTOSIZE_NAMES = {0: "none", 1: "grow", 2: "shrink"}  # msoAutoSizeNone / ShapeToFitText / TextToFitShape
AUTOSIZE_VALUES = {"none": 0, "grow": 1, "shrink": 2}


def _bgr_int_to_hex(value: int) -> str:
    # COM의 RGB 값은 0xBBGGRR 순서입니다.
    r = value & 0xFF
    g = (value >> 8) & 0xFF
    b = (value >> 16) & 0xFF
    return f"{r:02X}{g:02X}{b:02X}"


def _hex_to_bgr_int(hex_color: str) -> int:
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return r + (g << 8) + (b << 16)


class _ComRun:
    def __init__(self, text_range) -> None:
        self._tr = text_range

    def get_size(self) -> float | None:
        try:
            return float(self._tr.Font.Size)
        except _COM_ERRORS:
            return None

    def set_size(self, size_pt: float) -> None:
        self._tr.Font.Size = size_pt

    def get_color(self) -> str | None:
        try:
            return _bgr_int_to_hex(int(self._tr.Font.Fill.ForeColor.RGB))
        except _COM_ERRORS:
            return None

    def set_color(self, hex_color: str) -> None:
        self._tr.Font.Fill.ForeColor.RGB = _hex_to_bgr_int(hex_color)

    def get_font_names(self) -> list[str]:
        # 한글은 NameFarEast, 영문은 Name 글꼴로 표시됩니다.
        names = []
        for attr in ("Name", "NameFarEast"):
            try:
                value = getattr(self._tr.Font, attr)
            except _COM_ERRORS:
                continue
            if value:
                names.append(str(value))
        return names

    def set_font_name(self, name: str) -> None:
        self._tr.Font.Name = name
        self._tr.Font.NameFarEast = name


class _ComParagraph:
    def __init__(self, text_range) -> None:
        self._pf = text_range.ParagraphFormat

    def get_line_spacing(self) -> float | None:
        try:
            if self._pf.LineRuleWithin != MSO_TRUE:
                return None  # 고정 pt 간격은 배수로 비교할 수 없어 '모름'
            return float(self._pf.SpaceWithin)
        except _COM_ERRORS:
            return None

    def set_line_spacing(self, multiple: float) -> None:
        self._pf.LineRuleWithin = MSO_TRUE
        self._pf.SpaceWithin = multiple


class _ComShape:
    def __init__(self, shape, slide_height: float) -> None:
        self._shape = shape
        self._slide_height = slide_height

    def _text_ranges(self, property_name: str) -> list:
        text_range = self._shape.TextFrame2.TextRange
        text = str(text_range.Text)
        if not text:
            return []
        # TextRange2.Runs/Paragraphs are indexed properties, not collections.
        # They clamp an out-of-range index to the last item, so stop when the
        # returned source position repeats or the complete text is covered.
        maximum = max(1, len(text.encode("utf-16-le")) // 2)
        result = []
        covered = ""
        previous_start = None
        for index in range(1, maximum + 1):
            item = indexed_property(text_range, property_name, index, 1)
            start = int(item.Start)
            if previous_start is not None and start <= previous_start:
                break
            previous_start = start
            value = str(item.Text)
            result.append(item)
            covered += value
            if covered == text:
                break
            if not text.startswith(covered):
                raise ApplyRejected(f"{property_name} 범위를 원문과 일치하게 확인할 수 없습니다.")
        if covered != text:
            raise ApplyRejected(f"{property_name} 범위가 원문 전체를 포함하지 않습니다.")
        return result

    def runs(self) -> list[_ComRun]:
        if self._shape.HasTextFrame != MSO_TRUE:
            return []
        return [_ComRun(item) for item in self._text_ranges("Runs")]

    def paragraphs(self) -> list[_ComParagraph]:
        if self._shape.HasTextFrame != MSO_TRUE:
            return []
        return [_ComParagraph(item) for item in self._text_ranges("Paragraphs")]

    def text_overflows(self) -> bool | None:
        try:
            frame = self._shape.TextFrame2
            auto = frame.AutoSize
            if auto == AUTOSIZE_VALUES["shrink"]:
                return True  # PowerPoint가 글자를 다시 줄여 버릴 수 있는 설정
            if auto == AUTOSIZE_VALUES["grow"]:
                return self._shape.Top + self._shape.Height > self._slide_height + TOLERANCE
            needed = frame.TextRange.BoundHeight + frame.MarginTop + frame.MarginBottom
            return needed > self._shape.Height + TOLERANCE
        except _COM_ERRORS:
            return None

    def get_geometry(self) -> tuple[float, float, float, float]:
        s = self._shape
        return (float(s.Left), float(s.Top), float(s.Width), float(s.Height))

    def set_height(self, height: float) -> None:
        self._shape.Height = height

    def set_position(self, left: float, top: float) -> None:
        self._shape.Left = left
        self._shape.Top = top

    def bring_to_front(self) -> None:
        self._shape.ZOrder(MSO_BRING_TO_FRONT)

    def get_fill(self) -> tuple[str, float] | None:
        fill = self._shape.Fill
        if fill.Visible != MSO_TRUE:
            return None
        return (_bgr_int_to_hex(int(fill.ForeColor.RGB)), float(fill.Transparency))

    def set_fill(self, hex_color: str, transparency: float) -> None:
        fill = self._shape.Fill
        fill.Visible = MSO_TRUE
        fill.Solid()
        fill.ForeColor.RGB = _hex_to_bgr_int(hex_color)
        fill.Transparency = transparency

    def get_autofit(self) -> str | None:
        try:
            return AUTOSIZE_NAMES.get(int(self._shape.TextFrame2.AutoSize))
        except _COM_ERRORS:
            return None

    def set_autofit(self, mode: str) -> None:
        self._shape.TextFrame2.AutoSize = AUTOSIZE_VALUES[mode]


class _ComDocument:
    def __init__(self, presentation) -> None:
        self._pres = presentation
        self._slide_height = float(presentation.PageSetup.SlideHeight)

    def find_shape(self, slide_index: int, shape_id: int) -> _ComShape | None:
        if slide_index < 1 or slide_index > self._pres.Slides.Count:
            return None
        shapes = self._pres.Slides(slide_index).Shapes
        for i in range(1, shapes.Count + 1):
            shape = shapes(i)
            if shape.Id == shape_id:
                return _ComShape(shape, self._slide_height)
        return None  # 그룹 안 객체는 아직 찾지 않음

    def save(self) -> None:
        self._pres.Save()


class ComDocumentSession(AbstractContextManager):
    """with ComDocumentSession(path) as doc: ...

    PowerPoint는 실행 중인 앱(COM 서버)을 재사용할 수 있으므로
    우리가 연 프레젠테이션만 닫고, 열기 전에 아무것도 열려 있지 않았을 때만 앱을 종료합니다.
    """

    def __init__(self, path: str) -> None:
        self._path = os.path.abspath(path)
        self._pres = None
        self._app = None
        self._open_count_before = 0

    def __enter__(self) -> _ComDocument:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            self._app = win32com.client.DispatchEx("PowerPoint.Application")
            self._open_count_before = self._app.Presentations.Count
            # Open(파일, ReadOnly, Untitled, WithWindow=False → 사용자 화면에 창을 띄우지 않음)
            self._pres = self._app.Presentations.Open(self._path, MSO_FALSE, MSO_FALSE, MSO_FALSE)
            return _ComDocument(self._pres)
        except Exception:
            self._cleanup()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        import pythoncom

        try:
            if self._pres is not None:
                self._pres.Close()
            if self._app is not None and self._open_count_before == 0 and self._app.Presentations.Count == 0:
                self._app.Quit()
        finally:
            self._pres = None
            self._app = None
            pythoncom.CoUninitialize()
