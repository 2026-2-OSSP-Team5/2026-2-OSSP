from dataclasses import replace

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from slideguard.domain.models import AnalysisReport, ChangeProposal, DeckSnapshot, Finding
from slideguard.ui.finding_details import describe_finding
from slideguard.ui.overlay import OverlayWidget, PowerPointWindow
from slideguard.ui.user_text import change_text, friendly_error, friendly_reason


def _sample():
    return Finding(
        "contrast:3:2:1",
        "contrast",
        "3:2",
        "review",
        "배경이 고르지 않아 일부 위치에서 명암비가 부족할 수 있습니다 (최저 1.69:1, 근사)",
        1.69,
        3.0,
        "글자 #FF0000 / 렌더링된 주변 픽셀 52개 중 가는 선 등 20%를 뺀 가장 불리한 #385D8A → 1.69:1 < 3.0:1",
    )


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_sample_contrast_explains_uncertainty_and_preserves_full_precision(application):
    finding = _sample()
    explanation = describe_finding(finding)
    assert explanation.measurements == (("추정 명암비", "1.69:1"), ("권장 명암비", "3:1 이상"))
    assert "수 있습니다" in explanation.title
    document = QTextDocument()
    document.setHtml(explanation.html())
    plain = document.toPlainText()
    assert "1.69:1" in plain and "단색 배경" in plain and "측정 상세" in plain
    assert all(technical not in plain for technical in ("#FF0000", "52개", "20%", "#385D8A"))
    assert "52개" in finding.evidence and finding.measured == 1.69


@pytest.mark.parametrize(
    ("rule", "fid", "measured", "expected", "wanted"),
    [
        ("typography", "typography:3:2:1", 12, 18, "12pt"),
        ("layout.margin", "layout.margin:3:2", -72, 36, "25.4mm"),
        ("layout.overflow", "layout.overflow:3:2", 7.2, 0, "2.54mm"),
        ("layout.density", "layout.density:3:lines", 12, 6, "12줄"),
        ("layout.density", "layout.density:3:chars", 180, 100, "180자"),
        ("layout.line_length", "layout.line_length:3:2", 40.5, 40, "40.5자 분량"),
        ("readability.spacing.1", "spacing:3:2", 1, 1.2, "1.2배 이상"),
        ("readability.italic", "italic:3:2", 85, "핵심에만 강조", "85%"),
    ],
)
def test_measurements_have_meaningful_units(rule, fid, measured, expected, wanted):
    explanation = describe_finding(
        replace(_sample(), rule_id=rule, id=fid, measured=measured, expected=expected)
    )
    assert any(wanted in value for _label, value in explanation.measurements)


def test_alignment_code_and_unknown_values_are_not_shown_as_readability_scores():
    alignment = describe_finding(replace(_sample(), rule_id="readability.alignment.1", measured="2"))
    assert alignment.measurements == (("현재 정렬", "가운데 정렬"), ("권장 정렬", "왼쪽 정렬"))
    unknown = describe_finding(replace(_sample(), severity="unknown", measured=None, expected=None))
    assert not unknown.measurements
    assert "확인하지 못했습니다" in unknown.title
    assert "None" not in unknown.html()


def test_readability_table_fits_every_column_with_vertical_scrollbar(application):
    widget = OverlayWidget()
    try:
        widget.set_target(PowerPointWindow(1, QRect(0, 0, 1440, 900), "test.pptx - PowerPoint"))
        widget.set_collapsed(False)
        deck = DeckSnapshot("test.pptx", "hash", 30, 960, 540)
        widget.show_report(AnalysisReport(deck, findings=[_sample()] * 40))
        table = widget.findings_table
        for width in (190, 220, 300, 420):
            table.setFixedWidth(width)
            application.processEvents()
            assert table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            assert table.horizontalScrollBar().maximum() == 0
            assert abs(table.horizontalHeader().length() - table.viewport().width()) <= 1
            for column in range(3):
                label = table.horizontalHeaderItem(column).text()
                assert table.columnWidth(column) >= table.fontMetrics().horizontalAdvance(label) + 12
            assert table.verticalScrollBar().maximum() > 0
    finally:
        widget.close()


def test_measurement_details_keep_original_evidence_available(application):
    widget = OverlayWidget()
    try:
        widget.show_report(
            AnalysisReport(DeckSnapshot("test.pptx", "hash", 3, 960, 540), findings=[_sample()])
        )
        assert "#FF0000" not in widget.detail_evidence.text()
        widget.detail_evidence.linkActivated.emit("measurement-details")
        dialog = widget._measurement_dialog
        assert dialog.evidence.isReadOnly()
        assert _sample().evidence in dialog.evidence.toPlainText()
        dialog.close()
    finally:
        widget.close()


def test_proposals_describe_direction_and_colors_instead_of_coordinates_and_codes():
    proposal = ChangeProposal("position", "f", "3:2", "shape.position", (50, 100), (50, 172), 0.1)
    assert change_text(proposal) == "위치 · 아래로 25.4mm"
    color = replace(proposal, property="font.color", before="FF0000", after="660000")
    assert "빨강" in change_text(color) and "진한" in change_text(color)
    assert "FF0000" not in change_text(color)
    size = replace(proposal, property="font.size", before=12, after=18)
    assert change_text(size) == "글자 크기 · 12pt → 18pt"


def test_internal_skip_and_error_messages_offer_recovery_steps():
    assert "고정을 해제" in friendly_reason("고정된 객체·속성이라 후보를 만들지 않음")
    budget = friendly_reason("디자인 영역 수정 예산 초과 (필요 비용 0.72 > 표준 예산 0.5)")
    assert "수정 강도" in budget and "0.72" not in budget
    blocked = friendly_reason("상자를 키울 공간에 다른 객체(ID 6,7)가 있음 → 사람이 판단")
    assert "공간" in blocked and "ID" not in blocked
    assert "다시 시도" in friendly_error("com_error: (-2147418111, 'Call was rejected', None, None)")
    assert "다른 저장 폴더" in friendly_error("PermissionError: access denied")
