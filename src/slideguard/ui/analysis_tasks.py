"""Presentation of saved-file analysis progress."""


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
