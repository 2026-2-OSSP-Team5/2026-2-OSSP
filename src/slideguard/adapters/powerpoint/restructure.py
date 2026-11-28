"""Create and validate local slide-split plans using private PowerPoint copies.

Structural edits are transactional and copy-only. Every application is checked
again; a cached validation flag never authorizes an unchecked document change.
"""

import hashlib
import json
import math
import shutil
from collections import Counter, OrderedDict
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock

from slideguard.checks.layout import LayoutRule
from slideguard.domain.models import ChangeProposal
from slideguard.planning.splitting import (
    VERSION,
    SplitUnavailable,
    build_draft,
    canonical_text,
    partition,
    plan_digest,
    verify_manifest,
)

from .patcher import ApplyRejected, ComDocumentSession, sha256_of

TAG_SOURCE = "SLIDEGUARD_SOURCE"
TAG_UNIT = "SLIDEGUARD_UNIT"
_PREVIEWS = OrderedDict()
_PREVIEW_LOCK = RLock()


def split_preview(payload):
    if (
        not payload
        or payload.get("version") != VERSION
        or not payload.get("validated")
        or payload.get("digest") != plan_digest(payload)
    ):
        return ()
    with _PREVIEW_LOCK:
        return _PREVIEWS.get((payload["source_hash"], payload["digest"]), ())


def _cache_preview(payload, images):
    with _PREVIEW_LOCK:
        _PREVIEWS[(payload["source_hash"], payload["digest"])] = tuple(images.values())
        while (
            len(_PREVIEWS) > 8
            or sum(len(image) for pages in _PREVIEWS.values() for image in pages) > 64_000_000
        ):
            _PREVIEWS.popitem(last=False)


def _objects(slide):
    return {int(slide.Shapes.Item(i).Id): slide.Shapes.Item(i) for i in range(1, slide.Shapes.Count + 1)}


def _text(item):
    try:
        if item.HasTextFrame == -1:
            return canonical_text(str(item.TextFrame.TextRange.Text))
    except Exception:
        pass
    return ""


def _content(item):
    """Recursive content signature for indivisible tables, groups and pictures."""
    values = [int(item.Type), _text(item)]
    if int(item.Type) == 6:
        values.append([_content(item.GroupItems.Item(i)) for i in range(1, item.GroupItems.Count + 1)])
    if getattr(item, "HasTable", 0) == -1:
        table = item.Table
        values.append(
            [
                [_text(table.Cell(r, c).Shape) for c in range(1, table.Columns.Count + 1)]
                for r in range(1, table.Rows.Count + 1)
            ]
        )
    # A duplicated image retains its embedded media; no export/reimport is performed.
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _cut(item, start, end, expected):
    source = _text(item)
    if source[start:end] != expected:
        raise SplitUnavailable("적용 직전 문단 내용이 원문과 달라졌습니다.")
    text_range = item.TextFrame.TextRange
    left = len(source[:start].encode("utf-16-le")) // 2
    right = len(source[:end].encode("utf-16-le")) // 2
    length = len(source.encode("utf-16-le")) // 2
    if right < length:
        text_range.Characters(right + 1, length - right).Delete()
    if left:
        item.TextFrame.TextRange.Characters(1, left).Delete()
    if expected.endswith("\r") and _text(item) == expected:
        # The boundary becomes a text-frame boundary, not an extra empty line.
        current = item.TextFrame.TextRange
        current.Characters(len(expected.encode("utf-16-le")) // 2, 1).Delete()
    if not _same_text(_text(item), expected):
        raise SplitUnavailable("분할한 문단이 원문 범위와 일치하지 않습니다.")


def _same_text(actual, expected):
    # Office can omit the separator at the end of a new text frame. Interior
    # separators, spaces, punctuation, Unicode and empty paragraphs are preserved.
    return actual == expected or expected.endswith("\r") and actual == expected[:-1]


def _prepare_fonts(path, payloads, deck):
    """Write effective body style into a private copy before Office measures it.

    Office can silently ignore Font.Size writes on imported paragraph defaults.
    Only sizes, target line spacing, wrapping and autofit of planned body ranges
    are changed; other ZIP parts, formatting, relationships and text are retained.
    """
    from zipfile import ZipFile

    from lxml import etree
    from pptx import Presentation

    presentation = Presentation(path)
    namespaces = {
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    }
    drawing = "{" + namespaces["a"] + "}"
    snapshots = {s.key: s for s in deck.shapes}
    changes = {}
    with ZipFile(path) as archive:
        for payload in payloads:
            number, goal = payload["slide"], payload["font_pt"]
            part = str(presentation.slides[number - 1].part.partname).lstrip("/")
            root = etree.fromstring(archive.read(part))
            shapes = {
                int(s.find("p:nvSpPr/p:cNvPr", namespaces).get("id")): s
                for s in root.findall("p:cSld/p:spTree/p:sp", namespaces)
            }
            for shape_id, expected in payload["source_text"]:
                snapshot = snapshots[f"{number}:{shape_id}"]
                intervals, offset = [], 0
                for run in snapshot.runs:
                    text = canonical_text(run.text)
                    if text:
                        size = run.font_size_pt
                        if size is None or not math.isfinite(size) or size <= 0:
                            raise SplitUnavailable("일부 본문의 실제 글자 크기를 확인할 수 없습니다.")
                        intervals.append((offset, offset + len(text), size))
                        offset += len(text)
                if canonical_text("".join(r.text for r in snapshot.runs)) != expected:
                    raise SplitUnavailable("글자 서식 범위가 원문 전체와 일치하지 않습니다.")
                shape = shapes.get(shape_id)
                if shape is None:
                    raise SplitUnavailable("일반 텍스트 상자의 저장 서식을 찾지 못했습니다.")
                body_properties = shape.find("p:txBody/a:bodyPr", namespaces)
                body_properties.set("wrap", "square")
                for child in list(body_properties):
                    if child.tag in {drawing + mode for mode in ("noAutofit", "normAutofit", "spAutoFit")}:
                        body_properties.remove(child)
                body_properties.insert(0, etree.Element(drawing + "noAutofit"))
                paragraphs = shape.findall("p:txBody/a:p", namespaces)
                offset = 0
                for paragraph in paragraphs:
                    paragraph_properties = paragraph.find("a:pPr", namespaces)
                    if paragraph_properties is None:
                        paragraph_properties = etree.Element(drawing + "pPr")
                        paragraph.insert(0, paragraph_properties)
                    line_spacing = paragraph_properties.find("a:lnSpc", namespaces)
                    if line_spacing is not None:
                        paragraph_properties.remove(line_spacing)
                    line_spacing = etree.Element(drawing + "lnSpc")
                    etree.SubElement(line_spacing, drawing + "spcPct").set(
                        "val", str(round(payload["line_spacing"] * 100000))
                    )
                    paragraph_properties.insert(0, line_spacing)
                    default = paragraph.find("a:pPr/a:defRPr", namespaces)
                    inherited = float(default.get("sz", 0)) / 100 if default is not None else 0
                    for child in paragraph:
                        if child.tag == drawing + "br":
                            offset += 1
                            continue
                        if child.tag not in (drawing + "r", drawing + "fld"):
                            continue
                        value = child.find("a:t", namespaces)
                        text = value.text or "" if value is not None else ""
                        end = offset + len(text)
                        if text:
                            sizes = [s for left, right, s in intervals if left < end and right > offset]
                            if not sizes:
                                raise SplitUnavailable("저장된 본문과 글자 서식의 위치가 일치하지 않습니다.")
                            properties = child.find("a:rPr", namespaces)
                            if properties is None:
                                properties = etree.Element(drawing + "rPr")
                                child.insert(0, properties)
                            original = float(properties.get("sz", 0)) / 100
                            size = max(goal, inherited, original, *sizes)
                            properties.set("sz", str(round(size * 100)))
                        offset = end
                    offset += 1  # A paragraph boundary occupies one native CR.
                changes[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    temporary = path.with_name("explicit-fonts-" + path.name)
    try:
        with ZipFile(path) as archive, ZipFile(temporary, "x") as output:
            for entry in archive.infolist():
                output.writestr(entry, changes.get(entry.filename, archive.read(entry.filename)))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _format(item, font, spacing, slide_height):
    if not _text(item):
        return
    # Sizes were written explicitly in _prepare_fonts before opening Office.
    # Reassigning Font.Size through COM can restore the old paragraph default
    # even when the requested value equals the current effective size.
    item.TextFrame.AutoSize = 0
    item.TextFrame2.AutoSize = 0
    item.TextFrame.WordWrap = -1
    source = item.TextFrame.TextRange
    count = int(source.Runs().Count)
    if not 1 <= count <= 4096:
        raise SplitUnavailable("글자 서식 범위를 완전하게 읽지 못했습니다.")
    runs = [source.Runs(index, 1) for index in range(1, count + 1)]
    if "".join(str(run.Text) for run in runs) != str(source.Text):
        raise SplitUnavailable("글자 서식 범위가 본문 전체를 포함하지 않습니다.")
    for run in runs:
        size = float(run.Font.Size)
        if not math.isfinite(size) or size < font - 0.1:
            raise SplitUnavailable("본문의 권장 글자 크기를 확인할 수 없습니다.")
    paragraph = item.TextFrame.TextRange.ParagraphFormat
    paragraph.LineRuleWithin = -1
    paragraph.SpaceWithin = spacing
    item.TextFrame.AutoSize = 0
    item.TextFrame2.AutoSize = 0


def _restore_paragraphs(item, fragments):
    for index, fragment in enumerate(fragments, 1):
        props = dict(fragment.get("properties", ()))
        if not props:
            continue
        paragraph = item.TextFrame.TextRange.Paragraphs(index, 1)
        if props.get("IndentLevel", 0) > 0 and paragraph.IndentLevel != props["IndentLevel"]:
            paragraph.IndentLevel = int(props["IndentLevel"])
        bullet = paragraph.ParagraphFormat.Bullet
        if props.get("BulletType") in (0, 1, 2) and bullet.Type != props["BulletType"]:
            bullet.Type = props["BulletType"]
        if "bullet" in props and (bullet.Visible == -1) != props["bullet"]:
            bullet.Visible = -1 if props["bullet"] else 0
        if props.get("BulletType") == 2:
            if "BulletStyle" in props and bullet.Style != props["BulletStyle"]:
                bullet.Style = props["BulletStyle"]
            if int(bullet.Number) != props["BulletNumber"]:
                bullet.StartValue = props["BulletNumber"]
    _verify_paragraphs(item, fragments)


def _verify_paragraphs(item, fragments):
    for index, fragment in enumerate(fragments, 1):
        props = dict(fragment.get("properties", ()))
        paragraph = item.TextFrame.TextRange.Paragraphs(index, 1)
        if props.get("IndentLevel", 0) > 0 and paragraph.IndentLevel != props["IndentLevel"]:
            raise SplitUnavailable("목록의 들여쓰기 단계가 바뀌었습니다.")
        if props.get("BulletType") == 2 and paragraph.ParagraphFormat.Bullet.Number != props["BulletNumber"]:
            raise SplitUnavailable("자동 번호 목록의 순서를 유지하지 못했습니다.")


def _clone_unit(slide, sources, unit, draft, height):
    pieces = []
    fragments = unit["fragments"]
    source_id = fragments[0]["shape_id"]
    template = sources[source_id]
    first, last = fragments[0], fragments[-1]
    expected = "".join(f["text"] for f in fragments)
    item = template.Duplicate().Item(1)
    _cut(item, first["start"], last["end"], expected)
    _restore_paragraphs(item, fragments)
    atomic = first["whole"] or bool(unit["object_ids"])
    if not atomic:
        item.Width = draft["width"]
        item.Left = draft["left"]
        item.Top = 0
    else:
        item.Left, item.Top = template.Left, template.Top
    _format(item, draft["font_pt"], draft["line_spacing"], height)
    if not atomic:
        item.Width = draft["width"]
    if _text(item):
        frame = item.TextFrame2
        needed = float(frame.TextRange.BoundHeight) + float(frame.MarginTop) + float(frame.MarginBottom)
        item.Height = round(needed + max(2, draft["font_pt"] * 0.15), 2)
    pieces.append((source_id, item, expected, ""))
    for source in unit["object_ids"]:
        template = sources[source]
        clone = template.Duplicate().Item(1)
        clone.Left, clone.Top = template.Left, template.Top
        pieces.append((source, clone, None, _content(template)))
    for _source, clone, _expected, _signature in sorted(
        pieces, key=lambda p: int(sources[p[0]].ZOrderPosition)
    ):
        clone.ZOrder(0)  # Keep the original back-to-front order inside the unit.
    min_x = min(float(p.Left) for _, p, _, _ in pieces)
    min_y = min(float(p.Top) for _, p, _, _ in pieces)
    max_x = max(float(p.Left + p.Width) for _, p, _, _ in pieces)
    max_y = max(float(p.Top + p.Height) for _, p, _, _ in pieces)
    if max_x - min_x > draft["width"] + 0.5:
        for _, piece, _, _ in pieces:
            piece.Delete()
        raise SplitUnavailable("관련 그림·표 묶음의 너비가 안전 영역보다 큽니다.")
    return pieces, (min_x, min_y, max_x - min_x, max_y - min_y)


def _environment(presentation):
    application = presentation.Application
    import winreg

    fonts = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, path, _ = winreg.EnumValue(key, index)
                    fonts.append((name, str(path)))
        except OSError:
            continue
    return {
        "powerpoint": str(application.Version),
        "build": str(getattr(application, "Build", "")),
        "fonts": hashlib.sha256(json.dumps(sorted(fonts), ensure_ascii=False).encode()).hexdigest(),
    }


def _measure(presentation, draft):
    slide = presentation.Slides.Item(draft["slide"]).Duplicate().Item(1)
    try:
        sources = _objects(slide)
        heights, measured = [], []
        for unit in draft["units"]:
            pieces, bounds = _clone_unit(
                slide, sources, unit, draft, float(presentation.PageSetup.SlideHeight)
            )
            heights.append(round(bounds[3], 2))
            measured.append({"width": round(bounds[2], 2), "height": round(bounds[3], 2)})
            measured[-1]["objects"] = {
                str(shape_id): _content(sources[shape_id]) for shape_id in unit["object_ids"]
            }
            for _, piece, _, _ in pieces:
                piece.Delete()
        return heights, measured
    finally:
        slide.Delete()


def _apply_plan(presentation, payload):
    verify_manifest(payload)
    source = presentation.Slides.Item(payload["slide"])
    sources = _objects(source)
    for shape_id, value in payload["source_text"]:
        if shape_id not in sources or _text(sources[shape_id]) != value:
            raise SplitUnavailable("원본 본문이 계획을 만들 때와 달라졌습니다.")
    pages = []
    start = 0
    try:
        # Duplicate from the end so that each new page lands in reading order.
        ranges = []
        for end in payload["cuts"]:
            ranges.append((start, end))
            start = end
        for page_index in reversed(range(len(ranges))):
            page = source if page_index == 0 else source.Duplicate().Item(1)
            if page_index:
                pages.append(page)
                notes = page.NotesPage.Shapes
                for index in range(1, notes.Count + 1):
                    item = notes.Item(index)
                    if int(item.Type) == 14 and item.PlaceholderFormat.Type == 2 and item.HasTextFrame == -1:
                        item.TextFrame.TextRange.Text = ""
            originals = _objects(page)
            top = payload["body_top"]
            begin, end = ranges[page_index]
            for unit_index in range(begin, end):
                unit = payload["units"][unit_index]
                pieces, bounds = _clone_unit(
                    page, originals, unit, payload, float(presentation.PageSetup.SlideHeight)
                )
                if bounds[3] > payload["measured"][unit_index]["height"] + 0.5:
                    raise SplitUnavailable(
                        "현재 렌더링 환경에서 본문 높이가 달라졌습니다. 다시 검사해 주세요."
                    )
                for shape_id, item, expected, signature in pieces:
                    item.Left = round(float(item.Left) + payload["left"] - bounds[0], 2)
                    item.Top = round(float(item.Top) + top - bounds[1], 2)
                    item.Tags.Add(TAG_SOURCE, str(shape_id))
                    item.Tags.Add(TAG_UNIT, str(unit_index))
                    if expected is not None and not _same_text(_text(item), expected):
                        raise SplitUnavailable("재배치한 본문이 원문과 일치하지 않습니다.")
                    if signature and _content(item) != signature:
                        raise SplitUnavailable("그림·표·수식 묶음의 내용이 변경되었습니다.")
                top += payload["measured"][unit_index]["height"] + payload["gap"]
            for shape_id, item in originals.items():
                if shape_id not in payload["repeated_ids"] and not (
                    page_index == 0 and shape_id in payload.get("once_ids", ())
                ):
                    if int(item.Type) == 14:
                        # Deleting a layout placeholder makes Office restore an
                        # empty replacement on save. Keep it empty and hidden.
                        if item.HasTextFrame == -1:
                            item.TextFrame.TextRange.Text = ""
                        item.Visible = 0
                    else:
                        item.Delete()
            _verify_page(page, payload, begin, end)
    except Exception:
        for page in pages:
            page.Delete()
        raise


def _verify_page(page, payload, begin, end):
    seen = Counter()
    expected = {}
    for index in range(begin, end):
        unit = payload["units"][index]
        fragments = unit["fragments"]
        expected[(index, fragments[0]["shape_id"])] = "".join(f["text"] for f in fragments)
        for shape_id in unit["object_ids"]:
            expected[(index, shape_id)] = None
    for item in _objects(page).values():
        unit = item.Tags.Item(TAG_UNIT)
        source = item.Tags.Item(TAG_SOURCE)
        if not unit and not source:
            continue
        key = (int(unit), int(source))
        if key not in expected:
            raise SplitUnavailable("계획에 없는 본문 객체가 생성되었습니다.")
        seen[key] += 1
        if expected[key] is not None and not _same_text(_text(item), expected[key]):
            raise SplitUnavailable("본문의 누락 또는 순서 변경이 발견되었습니다.")
        if expected[key] is not None:
            _verify_paragraphs(item, payload["units"][key[0]]["fragments"])
            text_range = item.TextFrame.TextRange
            for index in range(1, text_range.Runs().Count + 1):
                if float(text_range.Runs(index, 1).Font.Size) < payload["font_pt"] - 0.1:
                    raise SplitUnavailable("저장 후 본문 글자가 권장 크기보다 작게 남았습니다.")
        elif _content(item) != payload["measured"][key[0]]["objects"][str(key[1])]:
            raise SplitUnavailable("저장 후 그림·표 묶음의 내용이 달라졌습니다.")
    if seen != Counter({key: 1 for key in expected}):
        raise SplitUnavailable("본문 객체가 누락되거나 의도하지 않게 중복되었습니다.")
    ordered = sorted(
        (item for item in _objects(page).values() if item.Tags.Item(TAG_UNIT)),
        key=lambda item: (float(item.Top), float(item.Left), int(item.Id)),
    )
    sequence = []
    for item in ordered:
        unit = int(item.Tags.Item(TAG_UNIT))
        if not sequence or sequence[-1] != unit:
            sequence.append(unit)
    if sequence != list(range(begin, end)):
        raise SplitUnavailable("생성된 페이지에서 본문 묶음의 읽기 순서가 바뀌었습니다.")


def _validate_rendered(path, payload, policy):
    verify_manifest(payload)
    from slideguard.bootstrap import build_powerpoint_reader
    from slideguard.ui.preview import render_slide_previews

    pages = tuple(range(payload["slide"], payload["slide"] + len(payload["cuts"])))
    body_ids = {}
    with ComDocumentSession(str(path)) as document:
        begin = 0
        for number, end in zip(pages, payload["cuts"], strict=True):
            page = document._pres.Slides.Item(number)
            _verify_page(page, payload, begin, end)
            body_ids[number] = {
                int(item.Id)
                for item in _objects(page).values()
                if item.Tags.Item(TAG_UNIT)
                and item.Tags.Item(TAG_SOURCE)
                and int(item.Tags.Item(TAG_SOURCE))
                == payload["units"][int(item.Tags.Item(TAG_UNIT))]["fragments"][0]["shape_id"]
            }
            begin = end
        page = None
    # Independently inspect the serialized size: Font2 can disagree with the
    # paragraph default inherited by a saved text box.
    from pptx import Presentation

    saved = Presentation(path)
    for number, shape_ids in body_ids.items():
        for shape in saved.slides[number - 1].shapes:
            if shape.shape_id not in shape_ids or not shape.has_text_frame:
                continue
            if not shape.text_frame.word_wrap or shape.text_frame.auto_size != 0:
                raise SplitUnavailable("저장 후 본문의 자동 맞춤 또는 줄바꿈 설정이 달라졌습니다.")
            for paragraph in shape.text_frame.paragraphs:
                spacing = paragraph.line_spacing
                if paragraph.text.strip() and (
                    not isinstance(spacing, float) or abs(spacing - payload["line_spacing"]) > 0.02
                ):
                    raise SplitUnavailable("저장 후 본문의 줄 간격이 목표와 달라졌습니다.")
                for run in paragraph.runs:
                    if run.text.strip():
                        size = run.font.size or paragraph.font.size
                        if size is None or size.pt < payload["font_pt"] - 0.1:
                            raise SplitUnavailable(
                                "저장된 본문 서식에서 권장 글자 크기를 확인할 수 없습니다."
                            )
    images = render_slide_previews(path, pages)
    if set(images) != set(pages) or any(not value for value in images.values()):
        raise SplitUnavailable("생성한 슬라이드를 모두 렌더링하지 못했습니다.")
    reader = build_powerpoint_reader(render=True)
    incremental = getattr(reader, "read_incremental", None)
    snapshot = incremental(path, slide_numbers=pages) if incremental is not None else reader.read(path)
    scoped = replace(policy, slides=pages)
    repeated_keys = {
        s.key
        for s in snapshot.shapes
        if s.slide_number in pages
        and s.shape_id in payload["repeated_ids"]
        and (s.locator is None or s.locator.kind == "shape" and len(s.locator.shape_path) == 1)
    }
    failures = [
        f
        for f in LayoutRule().evaluate(snapshot, scoped)
        if f.rule_id in ("layout.overflow", "layout.overlap", "layout.margin")
        and not (f.rule_id == "layout.margin" and f.severity == "review" and f.shape_key in repeated_keys)
    ]
    if failures:
        raise SplitUnavailable(
            "생성 후 배치 검사에서 제외됨: " + "; ".join(dict.fromkeys(f.message for f in failures))
        )
    return images


def prepare_split_proposals(report, policy, *, slide_numbers=None, should_cancel=lambda: False):
    """Only validated plans enter report.proposals. Failure reasons remain visible."""
    scope = None if slide_numbers is None else set(slide_numbers)
    report.proposals[:] = [
        p
        for p in report.proposals
        if p.property != "slide.split" or scope is not None and int(p.shape_key.split(":")[0]) not in scope
    ]
    if scope is None:
        report.proposal_notes.clear()
    else:
        ids = {f.id for f in report.findings if int(f.shape_key.split(":")[0]) in scope}
        report.proposal_notes = {k: v for k, v in report.proposal_notes.items() if k not in ids}
    slides = sorted(
        {
            int(f.shape_key.split(":")[0])
            for f in report.findings
            if f.rule_id == "layout.density" or f.rule_id == "typography" and not f.id.endswith(":hierarchy")
        }
    )
    for number in slides:
        if scope is not None and number not in scope:
            continue
        if should_cancel():
            from slideguard.application.ports import ReadInterrupted

            raise ReadInterrupted()
        try:
            draft = build_draft(report.deck, report.findings, policy, number)
            payload = draft.payload()
            with TemporaryDirectory(prefix="slideguard-split-") as temporary:
                path = Path(temporary) / "candidate.pptx"
                shutil.copy2(report.deck.source_path, path)
                if sha256_of(str(path)) != report.deck.source_sha256:
                    raise SplitUnavailable("검사 후 원본 파일이 변경되었습니다.")
                _prepare_fonts(path, [payload], report.deck)
                with ComDocumentSession(str(path)) as document:
                    payload["environment"] = _environment(document._pres)
                    slide = document._pres.Slides.Item(number)
                    if slide.TimeLine.MainSequence.Count or slide.TimeLine.InteractiveSequences.Count:
                        raise SplitUnavailable(
                            "애니메이션의 객체 연결을 유지하기 어려워 이 슬라이드는 분할하지 않았습니다."
                        )
                    slide = None
                    heights, measured = _measure(document._pres, payload)
                    payload["measured"] = measured
                    cuts = partition(heights, draft.body_bottom - draft.body_top, draft.gap)
                    if len(cuts) < 2:
                        raise SplitUnavailable(
                            "권장 글자 크기로 한 장에 배치할 수 있어 분할안을 만들지 않았습니다."
                        )
                    payload["cuts"] = list(cuts)
                    payload["source_hash"] = report.deck.source_sha256
                    payload["policy_key"] = policy.cache_key
                    _apply_plan(document._pres, payload)
                    document.save()
                images = _validate_rendered(path, payload, policy)
            payload["validated"] = True
            payload["digest"] = plan_digest(payload)
            _cache_preview(payload, images)
            finding = next(
                f
                for f in report.findings
                if f.shape_key.startswith(f"{number}:")
                and (f.rule_id == "layout.density" or f.rule_id == "typography")
            )
            explanation = (
                f"본문 순서를 유지해 문단·목록 경계에서 {len(cuts)}장으로 나눕니다. "
                f"본문 {draft.font_pt:g}pt, 줄 간격 {draft.line_spacing:g}배, 여백 {draft.margin_ratio:.0%}. "
                "소제목·하위 항목과 관련 그림은 함께 유지하며 내용 보존·렌더링·배치 검사를 통과했습니다."
            )
            report.proposals.append(
                ChangeProposal(
                    f"slide.split@{number}:{draft.anchor_id}",
                    finding.id,
                    f"{number}:{draft.anchor_id}",
                    "slide.split",
                    1,
                    len(cuts),
                    0.5,
                    payload,
                    explanation,
                )
            )
        except Exception as error:
            reason = (
                str(error)
                if isinstance(error, SplitUnavailable)
                else f"분할 후보 검증 실패: {type(error).__name__}: {error}"
            )
            for finding in report.findings:
                if finding.shape_key.startswith(f"{number}:") and finding.rule_id in (
                    "typography",
                    "layout.density",
                ):
                    report.proposal_notes[finding.id] = reason
    return report


def apply_structural_copy(patcher, report, proposal_ids, output, policy):
    """Apply ordinary edits, then verified splits in descending source-slide order."""
    from slideguard.planning.bindings import TeamBindings

    from .patcher import ApplyResult

    ids = tuple(proposal_ids)
    selected = [p for p in report.proposals if p.id in ids]
    splits = [p for p in selected if p.property == "slide.split"]
    if len(selected) != len(set(ids)):
        raise ApplyRejected("보고서에 없는 수정안이 포함되어 있습니다.")
    split_slides = {int(p.shape_key.split(":")[0]) for p in splits}
    if len(split_slides) != len(splits) or any(
        p.property != "slide.split" and int(p.shape_key.split(":")[0]) in split_slides for p in selected
    ):
        raise ApplyRejected("분할안과 같은 슬라이드의 다른 수정안은 함께 적용할 수 없습니다.")
    source, destination = Path(report.deck.source_path).resolve(), Path(output).resolve()
    if source == destination or destination.exists():
        raise ApplyRejected("새 PPTX 사본을 저장할 위치를 선택해 주세요.")
    if sha256_of(str(source)) != report.deck.source_sha256:
        raise ApplyRejected("검사 후 원본 파일이 변경되었습니다.")
    with TemporaryDirectory(prefix="slideguard-structural-apply-") as temporary:
        candidate = Path(temporary) / "checked.pptx"
        ordinary = [p.id for p in selected if p.property != "slide.split"]
        if ordinary:
            patcher.apply_to_copy(report, ordinary, candidate, policy)
            result = patcher.last_result
        else:
            shutil.copy2(source, candidate)
            result = ApplyResult(str(candidate))
        if sha256_of(str(source)) != report.deck.source_sha256:
            raise ApplyRejected("사본 준비 중 원본이 변경되었습니다.")
        _prepare_fonts(candidate, [p.payload for p in splits], report.deck)
        with ComDocumentSession(str(candidate)) as document:
            for proposal in sorted(splits, key=lambda p: int(p.shape_key.split(":")[0]), reverse=True):
                payload = proposal.payload or {}
                if (
                    not payload.get("validated")
                    or payload.get("version") != VERSION
                    or payload.get("digest") != plan_digest(payload)
                    or payload.get("source_hash") != report.deck.source_sha256
                    or payload.get("policy_key") != policy.cache_key
                    or payload.get("environment") != _environment(document._pres)
                ):
                    raise ApplyRejected(
                        "분할안의 검증 정보나 렌더링 환경이 달라졌습니다. 다시 검사해 주세요."
                    )
                # Re-evaluate all locks, including content objects other than the anchor.
                build_draft(report.deck, report.findings, policy, payload["slide"])
                _apply_plan(document._pres, payload)
            document.save()
        for proposal in splits:
            payload = dict(proposal.payload)
            offset = sum(
                int(p.after) - 1 for p in splits if int(p.shape_key.split(":")[0]) < payload["slide"]
            )
            payload["slide"] += offset
            _validate_rendered(candidate, payload, policy)
        if sha256_of(str(source)) != report.deck.source_sha256:
            raise ApplyRejected("적용 중 원본이 변경되었습니다.")
        # Exclusive creation prevents replacing a file that appeared during validation.
        created = False
        try:
            with candidate.open("rb") as incoming, destination.open("xb") as outgoing:
                created = True
                shutil.copyfileobj(incoming, outgoing)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        bindings = TeamBindings()
        result.applied.extend(bindings.from_team_proposal(p) for p in splits)
        result.output_path = str(destination)
        patcher.last_result = result
        return destination
