"""그림 속 글자 읽기 (계획서 기술적 난제 1번, 개발 계획 '이미지 및 복잡한 그래픽 분석').

글자가 그림(캡처·포스터 이미지 등) 안에 있으면 PowerPoint 글상자가 아니라서 지금까지는 아예 못 봤다.
Windows에 들어 있는 문자 인식(Windows.Media.Ocr)으로 그림 속 글자 줄을 찾아,
  - 슬라이드에서 보이는 글자 크기 (줄 높이로 추정)
  - 글자와 바로 뒤 배경의 명암비 (줄 영역 픽셀로 추정)
를 그림 도형의 properties에 ("image_text", JSON) 으로 넣는다. 판정은 checks/image_text.py가 한다.

방법 (검사용 사본에서만: 저장하지 않고 버리는 파일)
  1) 그림이 있는 슬라이드마다 그림 아닌 도형을 잠깐 숨기고 슬라이드를 그림으로 내보냄 (글상자 글자를 섞지 않으려고)
  2) 그림 영역만 잘라 PNG로 저장
  3) PowerShell로 Windows 문자 인식을 한 번에 실행 (추가 설치 없음, Windows 10 이상)
문자 인식을 쓸 수 없으면(언어 팩 없음, PowerShell 실패 등) 조용히 건너뛰고 로그만 남긴다.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from statistics import median
from time import perf_counter

from PIL import Image

LOGGER = logging.getLogger("slideguard.reader")

PICTURE_TYPES = frozenset({13})  # msoPicture
PLACEHOLDER_TYPE = 14  # msoPlaceholder (그림 자리 표시자에 넣은 그림)
EXPORT_WIDTH = 2000  # 슬라이드를 내보낼 너비(px). 작은 글자도 읽히도록 넉넉히
MIN_LINE_PX = 8  # 이보다 낮은 줄은 잡음으로 봄
MIN_PICTURE_SHARE = 0.03  # 슬라이드 면적의 3% 미만인 그림(아이콘·로고)은 읽지 않음 (검사 시간 절약)
GLYPH_PER_EM = (
    0.9  # 단어 높이(잉크) ÷ 글자 크기: 한글 약 0.9 (Noto Sans CJK 22px 글자 → 단어 높이 21px 로 확인)
)
OCR_TIMEOUT_SECONDS = 30  # 문자 인식 전체 제한 시간. 넘으면 PowerShell을 끝내고 그림 속 글자 검사만 건너뜀

# 한 번 실패한 이유 (시간 초과·언어 팩 없음·PowerShell 없음 등). 앱을 다시 켤 때까지 문자 인식을 다시 시도하지 않음
# → 실패하는 PC에서 파일을 검사할 때마다 30초씩 기다리지 않도록
_disabled_reason: str | None = None

_SCRIPT = r"""
param([string]$ListPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { Write-Output '{"error":"no-ocr-language"}'; exit 0 }
$results = @()
foreach ($path in (Get-Content -LiteralPath $ListPath -Encoding UTF8)) {
    if (-not $path) { continue }
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $ocr = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @()
    foreach ($line in $ocr.Lines) {
        $left = 1e9; $top = 1e9; $right = 0; $bottom = 0; $heights = @()
        foreach ($word in $line.Words) {
            $r = $word.BoundingRect
            $left = [Math]::Min($left, $r.X); $top = [Math]::Min($top, $r.Y)
            $right = [Math]::Max($right, $r.X + $r.Width); $bottom = [Math]::Max($bottom, $r.Y + $r.Height)
            $heights += $r.Height
        }
        $lines += @{ text = $line.Text; x = $left; y = $top; w = $right - $left; h = $bottom - $top; hs = $heights }
    }
    $stream.Dispose()
    $results += @{ path = $path; lines = $lines }
}
ConvertTo-Json -InputObject @{ images = $results } -Depth 6 -Compress
"""


def read_image_text(presentation, snapshot, workdir: Path, *, runner=None):
    """그림 도형마다 그림 속 글자 줄을 찾아 properties에 넣은 스냅샷. 그림이 없거나 실패하면 그대로."""
    if sys.platform != "win32" and runner is None:
        return snapshot
    if _disabled_reason is not None and runner is None:
        LOGGER.info("IMAGE_TEXT | 건너뜀 (이전 실패: %s)", _disabled_reason)
        return snapshot
    slide_area = snapshot.width_pt * snapshot.height_pt
    pictures = [
        shape
        for shape in snapshot.shapes
        if _is_picture(shape) and shape.box.width * shape.box.height >= slide_area * MIN_PICTURE_SHARE
    ]
    if not pictures:
        return snapshot
    started = perf_counter()
    crops: dict[str, tuple[object, Path, float]] = {}  # 그림 key → (도형, 잘라낸 PNG, px/pt)
    folder = Path(tempfile.mkdtemp(prefix="slideguard-ocr-", dir=workdir))
    try:
        for number in sorted({shape.slide_number for shape in pictures}):
            on_slide = [shape for shape in pictures if shape.slide_number == number]
            image = _export_pictures_only(presentation, snapshot, number, folder)
            if image is None:
                continue
            scale = image.width / snapshot.width_pt
            for shape in on_slide:
                box = shape.box
                crop = image.crop(
                    (
                        max(0, round(box.x * scale)),
                        max(0, round(box.y * scale)),
                        min(image.width, round((box.x + box.width) * scale)),
                        min(image.height, round((box.y + box.height) * scale)),
                    )
                )
                if crop.width < 16 or crop.height < 16:
                    continue
                target = folder / f"{shape.slide_number}_{shape.shape_id}.png"
                crop.save(target)
                crops[shape.key] = (shape, target, scale)
        if not crops:
            return snapshot
        results = (runner or _run_ocr)([path for _shape, path, _scale in crops.values()], folder)
        updated = {}
        for key, (shape, path, scale) in crops.items():
            lines = results.get(str(path), [])
            measured = _measure_lines(path, lines, scale, shape.box)
            if measured:
                properties = (*shape.properties, ("image_text", json.dumps(measured, ensure_ascii=False)))
                updated[key] = replace(shape, properties=properties)
        LOGGER.info(
            "IMAGE_TEXT | 그림 %d개 중 글자 있는 그림 %d개 | %.2fs",
            len(crops),
            len(updated),
            perf_counter() - started,
        )
        if not updated:
            return snapshot
        return replace(snapshot, shapes=tuple(updated.get(shape.key, shape) for shape in snapshot.shapes))
    except Exception as error:  # noqa: BLE001 - 문자 인식은 덤: 실패해도 검사는 계속
        LOGGER.info("IMAGE_TEXT | 건너뜀: %s: %s", type(error).__name__, error)
        return snapshot
    finally:
        # 지우다 실패해도(파일이 잠깐 잠김 등) 오류를 내지 않음: 여기서 오류가 나면 검사 전체가 실패·재시도됨
        shutil.rmtree(folder, ignore_errors=True)


def _is_picture(shape) -> bool:
    if shape.box is None or shape.box.width <= 0 or shape.box.height <= 0:
        return False
    if shape.shape_type in PICTURE_TYPES:
        return True
    return shape.shape_type == PLACEHOLDER_TYPE and not shape.text.strip()


def _export_pictures_only(presentation, snapshot, number, folder):
    """그림 아닌 도형을 잠깐 숨기고 슬라이드를 PNG로 내보낸 그림 (PIL Image). 실패하면 None."""
    slide = presentation.Slides.Item(number)
    keep = {
        shape.shape_id for shape in snapshot.shapes if shape.slide_number == number and _is_picture(shape)
    }
    hidden = []
    try:
        for index in range(1, slide.Shapes.Count + 1):
            item = slide.Shapes.Item(index)
            if item.Id not in keep and item.Visible:
                item.Visible = 0
                hidden.append(item)
        target = folder / f"slide_{number}.png"
        height = round(EXPORT_WIDTH * snapshot.height_pt / snapshot.width_pt)
        slide.Export(str(target), "PNG", EXPORT_WIDTH, height)
        with Image.open(target) as source:
            return source.convert("RGB")
    except Exception as error:  # noqa: BLE001
        LOGGER.info("IMAGE_TEXT | 슬라이드 %d 내보내기 실패: %s", number, error)
        return None
    finally:
        for item in hidden:
            try:
                item.Visible = -1
            except Exception:  # noqa: BLE001 - 버리는 사본이라 되돌리기 실패는 무시
                pass


def _run_ocr(paths: list[Path], folder: Path) -> dict[str, list[dict]]:
    """PowerShell로 Windows 문자 인식 실행 → {그림 경로: [줄 {text,x,y,w,h,hs}]}

    멈춤 방지:
      - 입력(stdin)은 비워 둠: 창 없는 앱에서 PowerShell이 입력을 기다리며 멈추지 않도록
      - 출력은 파이프 대신 파일로 받음: 시간 초과로 끝낼 때 파이프가 안 닫혀 같이 멈추는 일을 막음
      - OCR_TIMEOUT_SECONDS가 지나면 PowerShell과 그 자식 프로세스까지 끝냄
    """
    global _disabled_reason
    script = folder / "ocr.ps1"
    listing = folder / "images.txt"
    out_path = folder / "ocr_out.txt"
    err_path = folder / "ocr_err.txt"
    script.write_text(_SCRIPT, encoding="utf-8-sig")
    listing.write_text("\n".join(str(path) for path in paths), encoding="utf-8")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        str(listing),
    ]
    with open(out_path, "wb") as out_file, open(err_path, "wb") as err_file:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:  # PowerShell이 없음 등
            _disabled_reason = f"PowerShell 실행 불가: {error}"
            raise
        try:
            returncode = process.wait(timeout=OCR_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            _disabled_reason = f"문자 인식 {OCR_TIMEOUT_SECONDS}초 초과"
            raise RuntimeError(_disabled_reason) from None
    output = out_path.read_bytes().decode("utf-8", errors="replace").strip()
    if returncode != 0 or not output:
        error_text = err_path.read_bytes().decode("utf-8", errors="replace")[-300:]
        _disabled_reason = "문자 인식 실패"
        raise RuntimeError(error_text or "문자 인식 실패")
    data = json.loads(output)
    if "error" in data:
        _disabled_reason = str(data["error"])  # 예: no-ocr-language (언어 팩 없음)
        raise RuntimeError(data["error"])
    images = data.get("images") or []
    if isinstance(images, dict):  # 그림이 하나면 PowerShell이 목록 대신 하나만 줌
        images = [images]
    result = {}
    for image in images:
        lines = image.get("lines") or []
        result[str(image.get("path"))] = [lines] if isinstance(lines, dict) else list(lines)
    return result


def _kill_tree(process) -> None:
    """PowerShell과 그 자식 프로세스를 모두 끝냄 (Windows: taskkill /T). 실패해도 무시."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _measure_lines(path: Path, lines: list[dict], scale: float, box) -> list[dict]:
    """줄마다 (글자, 슬라이드 위 위치, 추정 글자 크기 pt, 추정 명암비)."""
    measured = []
    with Image.open(path) as source:
        image = source.convert("RGB")
        for line in lines:
            text = str(line.get("text", "")).strip()
            x, y, w, h = (float(line.get(key, 0)) for key in ("x", "y", "w", "h"))
            # 글자 높이는 단어 높이의 중앙값 (줄 전체 상자는 잘못 읽은 단어 하나로 커질 수 있음)
            heights = line.get("hs")
            heights = [heights] if isinstance(heights, (int, float)) else list(heights or [])
            glyph = median(float(value) for value in heights) if heights else h
            if len("".join(text.split())) < 2 or glyph < MIN_LINE_PX:
                continue
            size_pt = glyph / scale / GLYPH_PER_EM
            pad = max(2, round(glyph * 0.2))  # 글자 둘레의 배경까지 보이도록 조금 넓게
            area = image.crop(
                (
                    max(0, round(x) - pad),
                    max(0, round(y) - pad),
                    min(image.width, round(x + w) + pad),
                    min(image.height, round(y + h) + pad),
                )
            )
            measured.append(
                {
                    "text": text[:40],
                    "box": [
                        round(box.x + x / scale, 1),
                        round(box.y + y / scale, 1),
                        round(w / scale, 1),
                        round(h / scale, 1),
                    ],
                    "size_pt": round(size_pt, 1),
                    "contrast": _line_contrast(area),
                }
            )
    return measured


def _line_contrast(area) -> float | None:
    """줄 영역의 글자·배경 명암비 근사.

    배경 = 영역 테두리(가장자리 2px) 픽셀의 중앙 밝기 (글자 둘레는 대부분 배경이므로)
    글자 = 안쪽 픽셀 가운데 배경과 밝기가 가장 많이 다른 10%
    (굵은 글자는 글자 픽셀이 절반을 넘을 수 있어 '가장 흔한 밝기'를 배경으로 보면 틀린다)
    """
    width, height = area.size
    if width < 6 or height < 6:
        return None
    raw = area.tobytes()  # RGB 바이트

    def lum(x, y):
        i = (y * width + x) * 3
        return _luminance(raw[i : i + 3])

    border = [lum(x, y) for y in (0, 1, height - 2, height - 1) for x in range(width)]
    border += [lum(x, y) for x in (0, 1, width - 2, width - 1) for y in range(2, height - 2)]
    background = median(border)
    inner = [lum(x, y) for y in range(2, height - 2) for x in range(2, width - 2)]
    if not inner:
        return None
    far = sorted(inner, key=lambda value: abs(value - background), reverse=True)
    ink = far[: max(1, len(far) // 10)]
    text = sum(ink) / len(ink)
    lighter, darker = max(text, background), min(text, background)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def _luminance(pixel) -> float:
    def channel(value):
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = pixel
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
