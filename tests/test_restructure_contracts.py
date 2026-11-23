import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from slideguard.adapters.powerpoint import restructure
from slideguard.adapters.powerpoint.patcher import ApplyRejected, PowerPointPatcher
from slideguard.adapters.storage.sqlite import _report_from_json
from slideguard.domain.models import ChangeProposal, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy
from slideguard.planning.splitting import SplitUnavailable, plan_digest
from tests.test_overlay import _proposal_report


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def _report(source):
    source.write_bytes(b"original presentation bytes")
    report = _proposal_report(source, hashlib.sha256(source.read_bytes()).hexdigest())
    payload = {
        "version": 1,
        "source_hash": report.deck.source_sha256,
        "policy_key": AnalysisPolicy().cache_key,
        "environment": {"version": "test"},
        "slide": 1,
        "cuts": [1, 2],
        "validated": True,
    }
    payload["digest"] = plan_digest(payload)
    proposal = ChangeProposal(
        "slide.split@1:2",
        report.findings[0].id,
        "1:2",
        "slide.split",
        1,
        2,
        0.5,
        payload,
        "본문을 2장으로 나눕니다.",
    )
    report.proposals.append(proposal)
    return report, proposal


def test_structural_payload_and_explanations_survive_storage_round_trip(tmp_path):
    report, proposal = _report(tmp_path / "source.pptx")
    report.proposal_notes[report.findings[0].id] = "다른 후보는 배치 공간이 부족합니다."
    restored = _report_from_json(json.dumps(asdict(report)))
    assert restored.proposals[-1] == proposal
    assert restored.proposal_notes == report.proposal_notes


def test_render_failure_never_creates_output_even_after_cached_validation(tmp_path, monkeypatch):
    report, proposal = _report(tmp_path / "source.pptx")
    original = (tmp_path / "source.pptx").read_bytes()
    destination = tmp_path / "result.pptx"

    class Session:
        def __init__(self, path):
            pass

        def __enter__(self):
            return SimpleNamespace(_pres=object(), save=lambda: None)

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(restructure, "ComDocumentSession", Session)
    monkeypatch.setattr(restructure, "_environment", lambda _p: {"version": "test"})
    monkeypatch.setattr(restructure, "build_draft", lambda *_a: None)
    monkeypatch.setattr(restructure, "_apply_plan", lambda *_a: None)
    monkeypatch.setattr(restructure, "_prepare_fonts", lambda *_a: None)

    def reject(*_args):
        raise SplitUnavailable("저장 후 겹침 발견")

    monkeypatch.setattr(restructure, "_validate_rendered", reject)
    with pytest.raises(SplitUnavailable, match="겹침"):
        PowerPointPatcher().apply_to_copy(report, [proposal.id], destination, AnalysisPolicy())
    assert not destination.exists()
    assert (tmp_path / "source.pptx").read_bytes() == original


def test_structural_original_and_competing_edits_are_rejected(tmp_path):
    report, proposal = _report(tmp_path / "source.pptx")
    with pytest.raises(ApplyRejected, match="사본"):
        PowerPointPatcher().apply_to_original(report, [proposal.id], tmp_path, AnalysisPolicy())
    with pytest.raises(ApplyRejected, match="함께"):
        PowerPointPatcher().apply_to_copy(
            report, [proposal.id, report.proposals[0].id], tmp_path / "result.pptx", AnalysisPolicy()
        )


def test_tampered_plan_cannot_supply_a_cached_preview(tmp_path):
    report, proposal = _report(tmp_path / "source.pptx")
    altered = dict(proposal.payload, cuts=[2, 3])
    assert restructure.split_preview(altered) == ()
    assert report.proposals[-1] == proposal


def test_private_font_preparation_preserves_text_larger_type_and_other_zip_parts(tmp_path):
    from zipfile import ZipFile

    from pptx import Presentation
    from pptx.util import Inches, Pt

    path = tmp_path / "private.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    body = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(4))
    paragraph = body.text_frame.paragraphs[0]
    paragraph.font.size = Pt(12)
    paragraph.text = "원문과 공백 그대로 "
    larger = paragraph.add_run()
    larger.text, larger.font.size = "큰 강조", Pt(26)
    presentation.save(path)
    with ZipFile(path) as archive:
        original = {name: archive.read(name) for name in archive.namelist()}
    snapshot = Shape(1, body.shape_id, body.name, None, body.text, (TextRun(body.text, 12),))
    payload = {"slide": 1, "font_pt": 18, "line_spacing": 1.2, "source_text": [(body.shape_id, body.text)]}
    restructure._prepare_fonts(path, [payload], SimpleNamespace(shapes=(snapshot,)))
    result = Presentation(path).slides[0].shapes[0]
    assert result.text == body.text
    assert [r.font.size.pt for r in result.text_frame.paragraphs[0].runs] == [18, 26]
    assert result.text_frame.word_wrap
    assert result.text_frame.auto_size == 0
    assert result.text_frame.paragraphs[0].line_spacing == 1.2
    with ZipFile(path) as archive:
        assert set(archive.namelist()) == set(original)
        assert all(
            archive.read(name) == data for name, data in original.items() if name != "ppt/slides/slide1.xml"
        )


def test_unknown_native_font_rejects_preparation_without_changing_private_copy(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    path = tmp_path / "private.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    body = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(4))
    body.text = "확인할 수 없는 서식"
    presentation.save(path)
    original = path.read_bytes()
    snapshot = Shape(1, body.shape_id, body.name, None, body.text, (TextRun(body.text),))
    with pytest.raises(SplitUnavailable, match="글자 크기"):
        restructure._prepare_fonts(
            path,
            [{"slide": 1, "font_pt": 18, "line_spacing": 1.2, "source_text": [(body.shape_id, body.text)]}],
            SimpleNamespace(shapes=(snapshot,)),
        )
    assert path.read_bytes() == original
