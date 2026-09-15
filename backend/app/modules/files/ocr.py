from __future__ import annotations

import asyncio
import io
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image, ImageOps

from app.core.config import Settings


@dataclass(frozen=True)
class OcrResult:
    text: str
    engine: str
    language: str


class OcrProcessingError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TesseractOcrEngine:
    """Local OCR adapter; product images never leave the platform boundary."""

    def __init__(self, settings: Settings) -> None:
        self.command = settings.ocr_command
        self.languages = settings.ocr_languages
        self.timeout_seconds = settings.ocr_timeout_seconds
        self.max_chars = settings.ocr_max_chars

    async def extract(self, payload: bytes) -> OcrResult:
        prepared = await asyncio.to_thread(_prepare_image, payload)
        try:
            process = await asyncio.create_subprocess_exec(
                self.command,
                "stdin",
                "stdout",
                "-l",
                self.languages,
                "--psm",
                "11",
                "--dpi",
                "300",
                "-c",
                "preserve_interword_spaces=1",
                "tsv",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise OcrProcessingError("OCR_ENGINE_UNAVAILABLE") from exc
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(prepared), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise OcrProcessingError("OCR_TIMEOUT") from exc
        if process.returncode != 0:
            raise OcrProcessingError("OCR_PROCESS_FAILED")
        return OcrResult(
            text=_parse_tesseract_tsv(
                stdout.decode("utf-8", errors="replace"), self.max_chars
            ),
            engine="tesseract-5-tsv-v2",
            language=self.languages,
        )


def _prepare_image(payload: bytes) -> bytes:
    """Normalize JPEG/PNG/WebP into a high-contrast PNG suitable for OCR."""

    try:
        with Image.open(io.BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode in {"RGBA", "LA"}:
                canvas = Image.new("RGB", image.size, "white")
                alpha = image.getchannel("A")
                canvas.paste(image.convert("RGB"), mask=alpha)
                image = canvas
            else:
                image = image.convert("RGB")
            if image.width < 1400:
                scale = min(2.0, 1400 / max(image.width, 1))
                image = image.resize(
                    (round(image.width * scale), round(image.height * scale)),
                    Image.Resampling.LANCZOS,
                )
            grayscale = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
            output = io.BytesIO()
            grayscale.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (OSError, ValueError) as exc:
        raise OcrProcessingError("OCR_IMAGE_DECODE_FAILED") from exc


def _normalize_ocr_text(value: str, max_chars: int) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\x0c", "\n")
    normalized = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or unicodedata.category(character) != "Cc"
    )
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.splitlines()]
    compact: list[str] = []
    for line in lines:
        if not line:
            continue
        if compact and line == compact[-1]:
            continue
        compact.append(line)
    return "\n".join(compact).strip()[:max_chars]


def _parse_tesseract_tsv(value: str, max_chars: int) -> str:
    """Keep only confident, information-bearing lines from Tesseract TSV output."""

    lines: OrderedDict[tuple[str, str, str, str], list[str]] = OrderedDict()
    for raw in value.splitlines()[1:]:
        columns = raw.split("\t", 11)
        if len(columns) != 12:
            continue
        try:
            confidence = float(columns[10])
        except ValueError:
            continue
        word = _normalize_ocr_text(columns[11], max_chars)
        if confidence < 50 or not word:
            continue
        key = (columns[1], columns[2], columns[3], columns[4])
        lines.setdefault(key, []).append(word.replace("\n", " "))
    candidates = [" ".join(words) for words in lines.values()]
    meaningful = [line for line in candidates if _meaningful_ocr_line(line)]
    return _normalize_ocr_text("\n".join(meaningful), max_chars)


def _meaningful_ocr_line(value: str) -> bool:
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", value))
    if cjk_count >= 2:
        return True
    latin_words = re.findall(r"[A-Za-z]{3,}", value)
    if len(latin_words) >= 3 and sum(map(len, latin_words)) >= 12:
        return True
    # Preserve compact dimensions/specifications such as 15cm, 220V and 95%.
    return bool(re.search(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|g|kg|ml|l|v|w|%)\b", value, re.I))
