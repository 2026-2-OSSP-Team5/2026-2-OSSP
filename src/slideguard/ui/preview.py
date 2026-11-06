from __future__ import annotations

import gc
from pathlib import Path
from tempfile import TemporaryDirectory


def render_slide_previews(path: Path, slide_numbers: tuple[int, ...]) -> dict[int, bytes]:
    """Render saved slides to PNG bytes without retaining files or COM proxies."""
    if not slide_numbers:
        return {}

    import pythoncom
    import win32com.client

    source = Path(path).resolve(strict=True)
    pythoncom.CoInitialize()
    application = presentation = None
    open_count_before = 0
    rendered: dict[int, bytes] = {}
    try:
        application = win32com.client.DispatchEx("PowerPoint.Application")
        open_count_before = application.Presentations.Count
        presentation = application.Presentations.Open(str(source), ReadOnly=-1, Untitled=-1, WithWindow=0)
        with TemporaryDirectory(prefix="slideguard-preview-") as temporary:
            directory = Path(temporary)
            for number in slide_numbers:
                if number < 1 or number > presentation.Slides.Count:
                    continue
                target = directory / f"slide-{number}.png"
                presentation.Slides.Item(number).Export(str(target), "PNG", 1280, 720)
                if target.is_file():
                    rendered[number] = target.read_bytes()
        return rendered
    finally:
        try:
            if presentation is not None:
                presentation.Close()
            if application is not None and open_count_before == 0 and application.Presentations.Count == 0:
                application.Quit()
        finally:
            presentation = application = None
            gc.collect()
            pythoncom.CoUninitialize()
