import argparse
import importlib.metadata
import importlib.util
import json
import platform
import sqlite3
from dataclasses import asdict
from pathlib import Path

from . import __version__

DEPENDENCIES = {
    "python-pptx": "pptx",
    "PySide6-Essentials": "PySide6",
    "watchdog": "watchdog",
    "pywin32": "win32com",
}


def environment_info() -> dict:
    packages = {}
    for distribution, module in DEPENDENCIES.items():
        try:
            packages[distribution] = {
                "version": importlib.metadata.version(distribution),
                "importable": importlib.util.find_spec(module) is not None,
            }
        except importlib.metadata.PackageNotFoundError:
            packages[distribution] = {"version": None, "importable": False}
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "sqlite": sqlite3.sqlite_version,
        "packages": packages,
        "note": "COM registration and PowerPoint behavior require a separate integration test",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SlideGuard PowerPoint tools")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Show Python and library installation status")
    commands.add_parser("gui", help="Open the desktop window")
    inspect = commands.add_parser("inspect", help="Extract saved PPTX object metadata; rules not enabled")
    inspect.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        info = environment_info()
        print(json.dumps(info, indent=2))
        required = [
            p for name, p in info["packages"].items() if name != "pywin32" or platform.system() == "Windows"
        ]
        return 0 if all(p["importable"] for p in required) else 1
    if args.command == "gui":
        from .ui.desktop import main as desktop_main

        return desktop_main()
    from .bootstrap import build_analyzer
    from .domain.policy import AnalysisPolicy

    try:
        report = build_analyzer().execute(args.file, AnalysisPolicy())
    except (OSError, ValueError) as error:
        parser.exit(2, f"Input error: {error}\n")
    print(json.dumps(asdict(report), ensure_ascii=True, indent=2))
    return 0
