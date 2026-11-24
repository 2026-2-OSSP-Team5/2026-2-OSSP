"""선택한 수정 후보를 복사본 또는 원본(백업 후)에 적용합니다 (C 담당).

[복사본에 적용] apply_to_copy / apply
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

[원본에 적용] apply_to_original / apply_in_place
  1. 원본의 SHA256이 분석 시점 값과 같은지 확인
  2. 백업 폴더에 원본을 복사하고, 백업의 SHA256도 확인
  3. 원본이 PowerPoint에 열려 있으면 그 문서에 적용 (저장 안 한 변경이 있으면 거절)
     닫혀 있으면 창 없이 열어 적용
  4. 하나라도 적용되면 원본 저장 (열려 있던 문서는 닫지 않음)
  5. 하나도 적용 못 하면 저장하지 않고 백업도 지움
  되돌리기: restore_backup(백업, 원본)

입구
  - apply_to_copy(report, proposal_ids, output, policy): 팀 인터페이스 (application/ports.py)
  - apply_to_original(report, proposal_ids, backup_dir, policy): 원본에 적용 (백업 경로를 돌려줌)
  - apply / apply_in_place: C 내부 후보로 직접 적용

속성별 적용 의미:
  font.size              글자 크기가 after보다 작은 런만 after로 올림
  font.color             색이 before와 같은 런만 after로 바꿈
  font.family            글꼴이 before인 런만 after로 바꿈 (라틴·한글 글꼴 모두)
  shape.height           현재 높이가 before일 때만 after로 바꿈
  shape.width            현재 너비가 before일 때만 after로 바꿈
  shape.position         현재 위치가 before (left, top)일 때만 after로 옮김
  shape.zorder           맨 앞으로 가져옴
  shape.fill             채우기가 없을 때만 after {"color", "transparency"}로 채움
  text.autofit           현재 자동 맞춤이 before일 때만 after로 바꿈
  paragraph.line_spacing 줄 간격(배수)이 after보다 좁은 문단만 after로 넓힘

끼워넣는 지점:
  - open_document: 복사본을 여는 함수 (기본 ComDocumentSession)
  - open_original: 원본을 여는 함수 (기본 OriginalDocumentSession, 열린 문서면 그대로 사용)
  - bindings: 정책에서 고정 항목을 읽는 방법 (planning/bindings.py)
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from slideguard.planning.bindings import TeamBindings
from slideguard.planning.contracts import ChangeProposal, Property

from .properties import indexed_property

LOGGER = logging.getLogger("slideguard.patcher")

TOLERANCE = 0.5  # 좌표 비교 허용 오차 (pt)

# ======================================================================
# 결과·예외
# ======================================================================


class ApplyRejected(Exception):
    """적용 전체를 거절하는 경우 (원본 변경, 잘못된 저장 위치 등)."""


@dataclass
class ApplyResult:
    output_path: str | None  # 수정된 파일. 하나도 적용 못 하면 None (복사본도 지움)
    applied: list[ChangeProposal] = field(default_factory=list)
    rejected: list[tuple[ChangeProposal, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    in_place: bool = False  # 원본에 적용했는지
    backup_path: str | None = None  # 원본에 적용했을 때 백업 파일
    was_open: bool = False  # PowerPoint에 열려 있던 문서에 적용했는지


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
    def set_width(self, width: float) -> None: ...
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
        self,
        open_document: DocumentOpener | None = None,
        bindings: TeamBindings | None = None,
        open_original: DocumentOpener | None = None,
    ) -> None:
        self._open_document = open_document or ComDocumentSession
        self._open_original = open_original or OriginalDocumentSession
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
        proposals = self._selected_proposals(report, proposal_ids)
        deck = report.deck
        result = self.apply(deck.source_path, deck.source_sha256, proposals, policy, str(output))
        self.last_result = result
        _raise_if_nothing_applied(result)
        return Path(result.output_path)

    def apply_to_original(
        self,
        report: object,
        proposal_ids: Iterable[str],
        backup_dir: Path | str,
        policy: object,
    ) -> Path:
        """보고서의 후보 중 고른 것을 원본에 적용하고, 만든 백업 파일 경로를 돌려줌.

        원본을 바꾸기 전에 backup_dir에 백업을 만듭니다. 하나도 적용하지 못하면
        원본은 그대로이고 백업도 지운 뒤 ApplyRejected. 상세 결과는 self.last_result.
        """
        proposals = self._selected_proposals(report, proposal_ids)
        deck = report.deck
        result = self.apply_in_place(deck.source_path, deck.source_sha256, proposals, policy, str(backup_dir))
        self.last_result = result
        _raise_if_nothing_applied(result)
        return Path(result.backup_path)

    def _selected_proposals(self, report: object, proposal_ids: Iterable[str]) -> list[ChangeProposal]:
        wanted = list(dict.fromkeys(proposal_ids))  # 순서 유지, 중복 제거
        by_id = {p.id: p for p in report.proposals}
        missing = [pid for pid in wanted if pid not in by_id]
        if missing:
            raise ApplyRejected(f"보고서에 없는 수정 후보입니다: {', '.join(missing)}")
        try:
            return [self._bindings.from_team_proposal(by_id[pid]) for pid in wanted]
        except ValueError as error:
            raise ApplyRejected(str(error)) from error

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

        _warn_partial_groups(result)
        if not result.applied:
            _remove_quietly(output_path)
            result.output_path = None
        return result

    def apply_in_place(
        self,
        original_path: str,
        expected_sha256: str,
        proposals: list[ChangeProposal],
        policy: object,
        backup_dir: str,
    ) -> ApplyResult:
        """백업을 만든 뒤 원본에 직접 적용합니다 (흐름은 파일 맨 위 설명 참고)."""
        original_path = os.path.abspath(original_path)
        if not proposals:
            raise ApplyRejected("적용할 후보가 없습니다.")
        if not os.path.isfile(original_path):
            raise ApplyRejected(f"원본 파일이 없습니다: {original_path}")

        # ---- 1. 원본이 분석한 버전 그대로인지 ------------------------------
        if sha256_of(original_path).lower() != expected_sha256.lower():
            raise ApplyRejected("분석 이후 원본 파일이 변경되었습니다. 저장한 뒤 다시 검사하고 적용하세요.")

        result = ApplyResult(output_path=original_path, in_place=True)

        # ---- 2. 고정 조건 재확인 (바꿀 게 없으면 백업도 만들지 않음) --------------
        to_apply = []
        for p in proposals:
            if self._bindings.is_locked(policy, p.slide_index, p.shape_id, p.prop):
                result.rejected.append((p, "고정된 객체·속성"))
            else:
                to_apply.append(p)
        if not to_apply:
            result.output_path = None
            return result

        # ---- 3. 백업 ----------------------------------------------------
        backup_path = _make_backup(original_path, backup_dir)
        if sha256_of(backup_path).lower() != expected_sha256.lower():
            _remove_quietly(backup_path)
            raise ApplyRejected("백업 파일을 확인할 수 없습니다. 원본은 바꾸지 않았습니다.")
        result.backup_path = backup_path

        # ---- 4. 원본 열어 적용 후 저장 ---------------------------------------
        saved = False
        try:
            with self._open_original(original_path) as doc:
                result.was_open = bool(getattr(doc, "was_open", False))
                # 여는 사이에 다시 저장됐으면 적용하지 않음
                if sha256_of(original_path).lower() != expected_sha256.lower():
                    raise ApplyRejected("적용 직전에 원본이 다시 저장되었습니다. 다시 검사한 뒤 적용하세요.")
                for p in to_apply:
                    self._apply_one(doc, p, result)
                if result.applied:
                    doc.save()
                    saved = True
        except Exception:
            if not saved and not result.applied:
                _remove_quietly(backup_path)  # 원본을 건드리지 않았으면 백업도 필요 없음
                result.backup_path = None
            raise

        _warn_partial_groups(result)
        if not result.applied:
            _remove_quietly(backup_path)
            result.backup_path = None
            result.output_path = None
        return result

    # ------------------------------------------------------------------

    def _apply_one(self, doc: Document, p: ChangeProposal, result: ApplyResult) -> None:
        apply_proposal(doc, p, result)


def apply_proposal(doc: Document, p: ChangeProposal, result: ApplyResult) -> None:
    """후보 하나를 문서에 적용하고 결과를 result에 남긴다 (실시간 적용 live_edit에서도 씀)."""
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
        Property.SHAPE_WIDTH: _apply_width,
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
    get_autofit = getattr(shape, "get_autofit", None)
    if get_autofit is not None and get_autofit() == "shrink":
        return _resize_shrunk_text(shape, target)
    changed = 0
    shrinking = _is_number(p.before) and target < float(p.before) - TOLERANCE
    for run in shape.runs():
        size = run.get_size()
        if size is None:
            continue  # 크기를 모르는 런은 건드리지 않음
        if shrinking:
            # 줄이는 후보(슬라이드 간 일관성: 제목 크기 맞추기)는 '그 크기였던 글자'만 바꾼다
            if abs(size - float(p.before)) <= TOLERANCE:
                run.set_size(target)
                changed += 1
        elif size < target:
            run.set_size(target)
            changed += 1
    fit_rows = getattr(shape, "fit_table_rows", None)
    if changed and fit_rows is not None:
        fit_rows()  # 표: 커진 글자에 맞춰 행 높이를 다시 맞춤 (안 하면 글자가 행 밖으로 삐져나옴)
    return changed


def _resize_shrunk_text(shape: ShapeHandle, target: float):
    """'넘치면 자동 축소' 상자의 글자 크기 고치기.

    이런 상자는 글자를 키워도 PowerPoint가 화면에서 다시 줄여 버린다 (24pt로 써 있어도 13pt로 보임).
    그래서 1) 자동 축소를 끄고 '도형을 텍스트 크기에 맞춤'으로 바꾼다 (글이 잘리지 않고 상자가 늘어남)
          2) 가장 작은 글자가 목표 크기가 되도록 모든 글자를 같은 비율로 맞춘다 (제목·본문 비율 유지).
    늘어난 상자가 다른 개체와 부딪히면 재검사에서 다시 알려 준다.
    (실시간 적용을 해제하면 live_edit이 자동 맞춤과 원래 높이를 함께 되돌린다)
    """
    shape.set_autofit("grow")
    runs = shape.runs()
    sizes = [run.get_size() for run in runs]
    known = [size for size in sizes if size]
    if not known:
        return "글자 크기를 읽지 못해 적용하지 않음"
    factor = target / min(known)
    changed = 0
    for run, size in zip(runs, sizes, strict=True):
        if not size:
            continue  # 크기를 모르는 런은 건드리지 않음
        new_size = round(size * factor * 2) / 2  # 0.5pt 단위
        if abs(new_size - size) > TOLERANCE:
            run.set_size(new_size)
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


def _apply_width(shape: ShapeHandle, p: ChangeProposal):
    _left, _top, width, _height = shape.get_geometry()
    if abs(width - float(p.before)) > TOLERANCE:
        return f"현재 너비 {width:g}pt가 후보 작성 시점({p.before}pt)과 다름"
    shape.set_width(float(p.after))
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


def _raise_if_nothing_applied(result: ApplyResult) -> None:
    if result.output_path is None:
        reasons = "; ".join(reason for _p, reason in result.rejected) or "알 수 없는 이유"
        raise ApplyRejected(f"적용된 수정 후보가 없습니다: {reasons}")


def _warn_partial_groups(result: ApplyResult) -> None:
    # 묶음(예: 상자 키우기 + 아래 그림 내리기) 중 일부만 적용됐으면 알림
    applied_groups = {p.group for p in result.applied if p.group}
    rejected_groups = {p.group for p, _reason in result.rejected if p.group}
    for group in sorted(applied_groups & rejected_groups):
        result.warnings.append(f"묶음 '{group}'의 일부만 적용되었습니다. 재검사로 겹침을 확인하세요.")


def _make_backup(original_path: str, backup_dir: str) -> str:
    """backup_dir/<원본이름>_<날짜-시각>.pptx (같은 이름이 있으면 _2, _3 …)."""
    os.makedirs(backup_dir, exist_ok=True)
    stem, suffix = os.path.splitext(os.path.basename(original_path))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = os.path.join(backup_dir, f"{stem}_{stamp}{suffix}")
    number = 2
    while os.path.exists(candidate):
        candidate = os.path.join(backup_dir, f"{stem}_{stamp}_{number}{suffix}")
        number += 1
    shutil.copy2(original_path, candidate)
    return candidate


def default_backup_dir() -> Path:
    """기본 백업 위치: %LOCALAPPDATA%/SlideGuard/backups (D의 검사 이력 DB와 같은 곳).

    감시 폴더 밖에 두어서 백업 파일이 검사 대상 목록에 섞이지 않게 합니다.
    """
    app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    base = Path(app_data) if app_data else Path.home() / ".local" / "share"
    return base / "SlideGuard" / "backups"


def owner_file_of(path: str) -> str:
    """PowerPoint가 파일을 열면 같은 폴더에 만드는 잠금 파일 (~$파일이름)."""
    folder, name = os.path.split(os.path.abspath(path))
    return os.path.join(folder, "~$" + name)


def restore_backup(backup_path: Path | str, original_path: Path | str) -> Path:
    """백업으로 원본을 되돌립니다. 원본이 PowerPoint에 열려 있으면 거절합니다."""
    backup_path, original_path = str(backup_path), str(original_path)
    if not os.path.isfile(backup_path):
        raise ApplyRejected(f"백업 파일이 없습니다: {backup_path}")
    if os.path.exists(owner_file_of(original_path)):
        raise ApplyRejected("원본이 PowerPoint에 열려 있습니다. 파일을 닫은 뒤 되돌리세요.")
    try:
        shutil.copy2(backup_path, original_path)
    except PermissionError as error:
        raise ApplyRejected(
            "원본 파일이 사용 중이라 되돌릴 수 없습니다. 파일을 닫은 뒤 다시 시도하세요."
        ) from error
    return Path(original_path)


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
MSO_SEND_BACKWARD = 3
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

    def _text_ranges(self, property_name: str, text_range=None) -> list:
        if text_range is None:
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
        if self._is_table():
            # 표는 모든 칸의 글자를 차례로 (1행 1열, 1행 2열, ...)
            return [_ComRun(item) for cell in self._cell_ranges() for item in self._text_ranges("Runs", cell)]
        if self._shape.HasTextFrame != MSO_TRUE:
            return []
        return [_ComRun(item) for item in self._text_ranges("Runs")]

    def fit_table_rows(self) -> list[float] | None:
        """표 행 높이를 글자에 맞게 늘린다. 바꾸기 전 높이 목록을 돌려준다 (표가 아니면 None).

        COM으로 칸 글자 크기를 바꾸면 PowerPoint가 행 높이를 바로 늘리지 않아 글자가 행 밖으로
        삐져나온다 (함정 PPT 7쪽: 18pt로 키우자 표 아래로 넘침). 그래서 행마다
        '칸 글자 높이(BoundHeight) + 칸 위아래 여백' 중 가장 큰 값을 직접 계산해 그만큼 늘린다.
        원래 더 높았던 행은 줄이지 않는다 (일부러 준 여백 유지).
        """
        if not self._is_table():
            return None
        table = self._shape.Table
        rows = table.Rows
        before = [float(rows.Item(index).Height) for index in range(1, rows.Count + 1)]
        after = []
        for row_index, old in enumerate(before, start=1):
            needed = 0.0
            for column in range(1, table.Columns.Count + 1):
                try:
                    frame = table.Cell(row_index, column).Shape.TextFrame2
                    if not str(frame.TextRange.Text):
                        continue
                    height = (
                        float(frame.TextRange.BoundHeight)
                        + float(frame.MarginTop)
                        + float(frame.MarginBottom)
                    )
                except _COM_ERRORS:
                    continue
                needed = max(needed, height)
            if needed > old + TOLERANCE:
                rows.Item(row_index).Height = round(needed, 1)
            after.append(float(rows.Item(row_index).Height))
        LOGGER.info("TABLE_ROWS | shape=%s | 행 높이 %s → %s", getattr(self._shape, "Id", "?"), before, after)
        return before

    def set_row_heights(self, heights: list[float]) -> None:
        rows = self._shape.Table.Rows
        for index, height in enumerate(heights, start=1):
            if index <= rows.Count:
                rows.Item(index).Height = height

    def _is_table(self) -> bool:
        try:
            return getattr(self._shape, "HasTable", 0) == MSO_TRUE
        except _COM_ERRORS:
            return False

    def _cell_ranges(self) -> list:
        table = self._shape.Table
        ranges = []
        seen = set()
        for row in range(1, table.Rows.Count + 1):
            for column in range(1, table.Columns.Count + 1):
                cell = table.Cell(row, column).Shape
                place = (float(getattr(cell, "Left", column)), float(getattr(cell, "Top", row)))
                if place in seen:
                    continue  # 병합된 칸은 같은 자리를 돌려주므로 한 번만
                seen.add(place)
                text_range = cell.TextFrame2.TextRange
                if str(text_range.Text):
                    ranges.append(text_range)
        return ranges

    def paragraphs(self) -> list[_ComParagraph]:
        if self._shape.HasTextFrame != MSO_TRUE:
            return []
        return [_ComParagraph(item) for item in self._text_ranges("Paragraphs")]

    def text_overflows(self) -> bool | None:
        if self._is_table():
            # 표는 행 높이가 글자에 맞춰 늘어나므로 표 아래쪽이 슬라이드 밖으로 나가는지만 본다
            try:
                return self._shape.Top + self._shape.Height > self._slide_height + TOLERANCE
            except _COM_ERRORS:
                return None
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

    def set_width(self, width: float) -> None:
        self._shape.Width = width

    def set_position(self, left: float, top: float) -> None:
        self._shape.Left = left
        self._shape.Top = top

    def bring_to_front(self) -> None:
        self._shape.ZOrder(MSO_BRING_TO_FRONT)

    def get_zorder(self) -> int:
        return int(self._shape.ZOrderPosition)

    def set_zorder(self, position: int) -> None:
        """앞뒤 순서를 position(1=맨 뒤)으로 되돌린다. 한 칸씩 뒤로 보내며 맞춘다."""
        for _ in range(500):
            current = int(self._shape.ZOrderPosition)
            if current <= position:
                return
            self._shape.ZOrder(MSO_SEND_BACKWARD)

    def get_fill(self) -> tuple[str, float] | None:
        fill = self._shape.Fill
        if fill.Visible != MSO_TRUE:
            return None
        return (_bgr_int_to_hex(int(fill.ForeColor.RGB)), float(fill.Transparency))

    def clear_fill(self) -> None:
        self._shape.Fill.Visible = MSO_FALSE

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
    def __init__(self, presentation, *, was_open: bool = False) -> None:
        self._pres = presentation
        self._slide_height = float(presentation.PageSetup.SlideHeight)
        self.was_open = was_open  # 사용자가 PowerPoint에서 열어 둔 문서인지

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
    """with ComDocumentSession(path) as doc: ...  (복사본용: 창 없이 열고, 끝나면 닫음)

    PowerPoint는 실행 중인 앱(COM 서버)을 재사용할 수 있으므로 우리가 연 프레젠테이션만 닫고,
    앱은 종료하지 않습니다. (열린 문서가 없어도 사용자가 켜 둔 PowerPoint일 수 있음)
    """

    def __init__(self, path: str) -> None:
        self._path = os.path.abspath(path)
        self._pres = None
        self._app = None
        self._doc: _ComDocument | None = None
        self._attached = False  # True면 사용자가 연 문서 → 닫지 않음

    def __enter__(self) -> _ComDocument:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            self._app = win32com.client.DispatchEx("PowerPoint.Application")
            self._pres = self._open(self._app)
            self._doc = _ComDocument(self._pres, was_open=self._attached)
            return self._doc
        except Exception:
            self._cleanup()
            raise

    def _open(self, app):
        # Open(파일, ReadOnly, Untitled, WithWindow=False → 사용자 화면에 창을 띄우지 않음)
        return app.Presentations.Open(self._path, MSO_FALSE, MSO_FALSE, MSO_FALSE)

    def __exit__(self, exc_type, exc, tb) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        import gc

        import pythoncom

        try:
            if self._pres is not None and not self._attached:
                self._pres.Close()
        finally:
            # COM 객체 참조를 모두 끊은 뒤에 CoUninitialize (A의 session.py와 같은 순서)
            if self._doc is not None:
                self._doc._pres = None
            self._doc = None
            self._pres = None
            self._app = None
            gc.collect()
            pythoncom.CoUninitialize()


class OriginalDocumentSession(ComDocumentSession):
    """원본용: PowerPoint에 이미 열려 있으면 그 문서를 그대로 쓰고 닫지 않습니다.

    열린 문서에 저장하지 않은 변경이 있거나 읽기 전용이면 거절합니다.
    (디스크의 파일과 화면의 문서가 다르면 어느 쪽을 고쳐야 할지 모르기 때문)
    """

    def _open(self, app):
        target = os.path.normcase(self._path)
        presentations = app.Presentations
        for index in range(1, presentations.Count + 1):
            pres = presentations.Item(index)
            try:
                full_name = os.path.normcase(os.path.abspath(str(pres.FullName)))
            except _COM_ERRORS:
                continue
            if full_name != target:
                continue
            self._attached = True
            if pres.ReadOnly == MSO_TRUE:
                raise ApplyRejected("원본이 PowerPoint에서 읽기 전용으로 열려 있습니다.")
            if pres.Saved != MSO_TRUE:
                raise ApplyRejected(
                    "PowerPoint에 저장하지 않은 변경이 있습니다. 저장한 뒤 다시 검사하고 적용하세요."
                )
            return pres
        return super()._open(app)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
