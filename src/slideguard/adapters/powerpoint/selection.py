"""Read and reveal the user's selected top-level PowerPoint objects."""

from pathlib import Path

from slideguard.domain.models import Shape


def selected_objects() -> tuple[Path, tuple[Shape, ...]]:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    application = window = presentation = selection = shapes = item = None
    try:
        application = win32com.client.GetActiveObject("PowerPoint.Application")
        window = application.ActiveWindow
        presentation = window.Presentation
        if window.ViewType not in (1, 9) or not presentation.Path:
            raise ValueError("저장된 PowerPoint의 편집 화면에서 객체를 선택하세요.")
        path = Path(str(presentation.FullName)).resolve()
        selection = window.Selection
        if selection.Type not in (2, 3):
            raise ValueError("PowerPoint에서 고정할 도형이나 글상자를 선택하세요.")
        shapes = selection.ShapeRange
        slide = int(window.View.Slide.SlideIndex)
        result = []
        for index in range(1, shapes.Count + 1):
            item = shapes.Item(index)
            text = str(item.TextFrame.TextRange.Text) if item.HasTextFrame == -1 else ""
            result.append(Shape(slide, int(item.Id), str(item.Name), None, text=text))
        return path, tuple(result)
    except ValueError:
        raise
    except Exception:
        raise ValueError("PowerPoint에서 고정할 객체를 선택한 뒤 다시 추가하세요.") from None
    finally:
        item = shapes = selection = presentation = window = application = None
        pythoncom.CoUninitialize()


def reveal_object(path: Path, key: str) -> None:
    import pythoncom
    import win32com.client

    number, shape_id = map(int, key.split(":"))
    pythoncom.CoInitialize()
    application = window = slide = item = None
    try:
        application = win32com.client.GetActiveObject("PowerPoint.Application")
        window = application.ActiveWindow
        if Path(str(window.Presentation.FullName)).resolve() != path.resolve():
            raise ValueError(f"PowerPoint에서 {path.name} 파일을 선택하세요.")
        if window.ViewType not in (1, 9):
            raise ValueError("PowerPoint의 편집 화면에서 객체를 확인할 수 있습니다.")
        slide = window.Presentation.Slides.Item(number)
        for index in range(1, slide.Shapes.Count + 1):
            item = slide.Shapes.Item(index)
            if item.Id == shape_id:
                window.View.GotoSlide(number)
                item.Select()
                window.Activate()
                return
        raise ValueError("이 객체를 현재 파일에서 찾을 수 없습니다. 고정 목록에서 해제할 수 있습니다.")
    except ValueError:
        raise
    except Exception:
        raise ValueError("PowerPoint에서 파일을 연 뒤 다시 확인하세요.") from None
    finally:
        item = slide = window = application = None
        pythoncom.CoUninitialize()
