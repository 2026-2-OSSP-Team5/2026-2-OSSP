"""On-demand slide reconstruction and shared progress wording."""

from copy import deepcopy
from hashlib import file_digest
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from slideguard.application.ports import ReadInterrupted


def progress_text(progress):
    stage, _, number = progress.stage.partition(":")
    labels = {
        "queued": "우선 검사 대기 중",
        "yielded": "우선 검사를 위해 잠시 대기 중",
        "compare": "변경된 슬라이드 비교 중",
        "read": "도형·텍스트 읽는 중",
        "render": "화면 측정 중",
        "check": "가독성 판단 중",
    }
    text = labels.get(stage, "검사 준비 중")
    if number:
        text += f" · {number}번"
    if progress.total:
        text += f" · {progress.completed}/{progress.total}장"
    if progress.reused:
        text += f" · 이전 측정 {progress.reused}장 재사용"
    return text


class SplitProposalWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, report, policy, slide, parent=None):
        super().__init__(parent)
        self.report = deepcopy(report)
        self.policy = policy
        self.slide = slide

    def run(self):
        from slideguard.adapters.powerpoint.restructure import prepare_split_proposals
        from slideguard.bootstrap import build_analysis_policy, build_report_repository

        try:
            prepare_split_proposals(
                self.report,
                self.policy,
                slide_numbers=(self.slide,),
                should_cancel=self.isInterruptionRequested,
            )
            if not self.isInterruptionRequested():
                source = Path(self.report.deck.source_path)
                with source.open("rb") as stream:
                    current_hash = file_digest(stream, "sha256").hexdigest()
                if (
                    current_hash != self.report.deck.source_sha256
                    or build_analysis_policy(source).cache_key != self.policy.cache_key
                ):
                    raise ValueError("검사 후 파일이나 설정이 변경되었습니다. 다시 검사해 주세요.")
                build_report_repository().save(self.report)
                self.completed.emit(self.report)
        except ReadInterrupted:
            pass
        except Exception as error:
            self.failed.emit(str(error))
