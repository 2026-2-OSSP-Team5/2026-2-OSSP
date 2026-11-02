import gc
import json
import platform
import sys
from types import SimpleNamespace

import pytest

from tests.fixtures.presentations import make_deck


@pytest.fixture(scope="session")
def powerpoint_environment(request, record_testsuite_property):
    if sys.platform != "win32":
        pytest.skip("Desktop PowerPoint COM integration requires Windows")
    if not request.config.getoption("--run-powerpoint"):
        pytest.skip("Desktop PowerPoint integration is opt-in: pass --run-powerpoint")
    pythoncom = pytest.importorskip("pythoncom", reason="pywin32 is not installed")
    client = pytest.importorskip("win32com.client", reason="pywin32 is not installed")
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    application = None
    try:
        try:
            application = client.DispatchEx("PowerPoint.Application")
        except pythoncom.com_error as error:
            if error.hresult & 0xFFFFFFFF in {0x80040154, 0x800401F3}:
                pytest.skip("Desktop PowerPoint COM class is not installed/registered")
            raise  # Activation/busy/permission failures are not passing or skipped tests.
        environment = {
            "Windows": platform.platform(),
            "WindowsBuild": platform.version(),
            "Python": platform.python_version(),
            "PowerPointVersion": str(application.Version),
            "PowerPointBuild": str(application.Build),
        }
        for name, value in environment.items():
            record_testsuite_property(name, value)
        print("\nPOWERPOINT_ENVIRONMENT | " + json.dumps(environment, ensure_ascii=False))
        yield environment
    finally:
        # DispatchEx may reuse an existing server. Never Quit or alter global settings.
        application = None
        gc.collect()
        pythoncom.CoUninitialize()


@pytest.fixture
def editing_document(powerpoint_environment, tmp_path):
    import win32com.client

    folder = tmp_path / "편집 중인 문서"
    folder.mkdir()
    path = folder / "사용자 문서 보존.pptx"
    data = make_deck("편집 전 저장").data
    path.write_bytes(data)
    application = win32com.client.DispatchEx("PowerPoint.Application")
    document = None
    try:
        document = application.Presentations.Open(str(path), ReadOnly=0, Untitled=0, WithWindow=-1)
        document.Slides.Item(1).Shapes.Item(1).TextFrame.TextRange.Text = "미저장 사용자 편집 보존"
        assert document.Saved == 0
        assert document.Windows.Count > 0
        yield SimpleNamespace(application=application, document=document, path=path, saved_bytes=data)
    finally:
        if document is not None:
            # Only this fixture-owned document: discard its generated unsaved edit.
            document.Saved = -1
            document.Close()
        document = application = None
        gc.collect()
