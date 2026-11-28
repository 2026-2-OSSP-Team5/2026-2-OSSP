from __future__ import annotations

import gc
from pathlib import Path
from tempfile import TemporaryDirectory

from slideguard.adapters.powerpoint.copies import saved_pptx_copy


def render_slide_previews(path: Path, slide_numbers: tuple[int, ...]) -> dict[int, bytes]:
    """Render saved slides to PNG bytes without retaining files or COM proxies."""
    if not slide_numbers:
        return {}

    import pythoncom
    import win32com.client

    with saved_pptx_copy(path) as saved, TemporaryDirectory(prefix="slideguard-preview-") as temporary:
        pythoncom.CoInitialize()
        application = presentation = None
        rendered: dict[int, bytes] = {}
        try:
            application = win32com.client.DispatchEx("PowerPoint.Application")
            presentation = application.Presentations.Open(
                str(saved.copy_path), ReadOnly=-1, Untitled=-1, WithWindow=0
            )
            width = 1280
            height = round(width * presentation.PageSetup.SlideHeight / presentation.PageSetup.SlideWidth)
            for number in slide_numbers:
                if number < 1 or number > presentation.Slides.Count:
                    continue
                target = Path(temporary) / f"slide-{number}.png"
                presentation.Slides.Item(number).Export(str(target), "PNG", width, height)
                if target.is_file():
                    rendered[number] = target.read_bytes()
            return rendered
        finally:
            try:
                if presentation is not None:
                    presentation.Close()
            finally:
                presentation = application = None
                gc.collect()
                pythoncom.CoUninitialize()
