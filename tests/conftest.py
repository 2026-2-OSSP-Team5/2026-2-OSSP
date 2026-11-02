def pytest_addoption(parser):
    parser.addoption(
        "--run-powerpoint",
        action="store_true",
        default=False,
        help="Run desktop PowerPoint COM tests; opens only generated test presentations",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "powerpoint: requires Windows and desktop PowerPoint (opt-in)")
