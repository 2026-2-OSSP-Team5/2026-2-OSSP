import os


def pytest_addoption(parser):
    parser.addoption(
        "--run-powerpoint",
        action="store_true",
        default=False,
        help="Run desktop PowerPoint COM tests; opens only generated test presentations",
    )


def pytest_configure(config):
    # 테스트 중에는 진단 기록(slideguard.diagnostics)을 실제 사용자 폴더에 남기지 않는다
    os.environ.setdefault("SLIDEGUARD_LOG", "off")
    config.addinivalue_line("markers", "powerpoint: requires Windows and desktop PowerPoint (opt-in)")
