"""Upload validation and sanitising.

Every uploaded file goes through :class:`ImageProcessor` before it touches
disk. The processor:

1. enforces a byte-size ceiling,
2. checks the declared MIME type against an allow-list,
3. sniffs the magic bytes and rejects content/type mismatches,
4. lets Pillow fully decode the image (rejecting corrupt files and
   decompression bombs via an explicit pixel budget), and
5. re-encodes the pixels, which strips EXIF/GPS metadata and neutralises
   polyglot files. EXIF orientation is applied first so photos stay upright.

All work here is CPU-bound and synchronous; callers run it in a thread.
"""

from __future__ import annotations

import io
import unicodedata
import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass(frozen=True, slots=True)
class ImageFormat:
    """A supported image format."""

    pillow_name: str
    extension: str
    content_type: str


JPEG = ImageFormat("JPEG", "jpg", "image/jpeg")
PNG = ImageFormat("PNG", "png", "image/png")
WEBP = ImageFormat("WEBP", "webp", "image/webp")

SUPPORTED_FORMATS: dict[str, ImageFormat] = {f.pillow_name: f for f in (JPEG, PNG, WEBP)}
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(f.extension for f in SUPPORTED_FORMATS.values())

#: Declared MIME types accepted from the browser. ``image/jpg`` and
#: ``image/pjpeg`` are non-standard aliases some clients still send;
#: ``application/octet-stream`` is sent when the OS doesn't know the type, in
#: which case the magic-byte sniffing below is authoritative.
_DECLARED_TYPE_ALIASES: dict[str, ImageFormat | None] = {
    "image/jpeg": JPEG,
    "image/jpg": JPEG,
    "image/pjpeg": JPEG,
    "image/png": PNG,
    "image/x-png": PNG,
    "image/webp": WEBP,
    "application/octet-stream": None,
    "": None,
}


class InvalidImageError(ValueError):
    """The upload is not an acceptable image. ``str(exc)`` is user-safe."""


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """A validated, metadata-free image ready to be stored."""

    data: bytes
    format: ImageFormat
    width: int
    height: int


def format_megabytes(size: int) -> str:
    """Human-readable size used in user-facing limit messages (``10 MB``, ``0.5 MB``)."""
    return f"{round(size / (1024 * 1024), 2):g} MB"


def sniff_format(data: bytes) -> ImageFormat | None:
    """Identify the image format from its magic bytes."""
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    return None


def safe_display_name(filename: str | None, *, max_length: int = 120) -> str:
    """Return a harmless, human-readable version of a client-supplied filename.

    The result is only ever *displayed* (and HTML-escaped by Jinja); storage
    keys are generated server-side and never derived from it.
    """
    if not filename:
        return "image"
    name = PureWindowsPath(PurePosixPath(filename).name).name
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip(" .")
    if not name:
        return "image"
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: max_length - len(ext) - 2] + "…." + ext
        else:
            name = name[: max_length - 1] + "…"
    return name


class ImageProcessor:
    """Validate and sanitise uploaded images."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_pixels: int,
        quality: int = 90,
    ) -> None:
        if max_bytes <= 0 or max_pixels <= 0:
            raise ValueError("max_bytes and max_pixels must be positive")
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels
        self.quality = quality

    def process(self, data: bytes, *, declared_content_type: str | None) -> ProcessedImage:
        """Validate ``data`` and return a re-encoded, metadata-free copy.

        Raises:
            InvalidImageError: with a message suitable for the end user.
        """
        if not data:
            raise InvalidImageError("The file is empty.")
        if len(data) > self.max_bytes:
            raise InvalidImageError(
                f"The file is larger than the {format_megabytes(self.max_bytes)} limit."
            )

        declared = (declared_content_type or "").split(";", 1)[0].strip().lower()
        if declared not in _DECLARED_TYPE_ALIASES:
            raise InvalidImageError("Only JPEG, PNG and WebP images are supported.")

        detected = sniff_format(data)
        if detected is None:
            raise InvalidImageError("Only JPEG, PNG and WebP images are supported.")
        expected = _DECLARED_TYPE_ALIASES[declared]
        if expected is not None and expected != detected:
            raise InvalidImageError("The file content does not match its declared image type.")

        try:
            return self._decode_and_reencode(data, detected)
        except InvalidImageError:
            raise
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise InvalidImageError("The image is corrupted or could not be read.") from exc

    def _decode_and_reencode(self, data: bytes, detected: ImageFormat) -> ProcessedImage:
        with warnings.catch_warnings():
            # Treat Pillow's bomb warning as an error; our own budget is stricter anyway.
            warnings.simplefilter("error", Image.DecompressionBombWarning)

            # Pass 1: structural verification (cheap, does not decode pixels).
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format != detected.pillow_name:
                    raise InvalidImageError(
                        "The file content does not match its declared image type."
                    )
                width, height = probe.size
                if width <= 0 or height <= 0:
                    raise InvalidImageError("The image has no pixels.")
                if width * height > self.max_pixels:
                    raise InvalidImageError(
                        f"The image is too large ({width}x{height} pixels). "
                        f"Maximum is {self.max_pixels:,} pixels."
                    )
                probe.verify()

            # Pass 2: full decode (verify() leaves the image unusable) + re-encode.
            with Image.open(io.BytesIO(data)) as source:
                source.seek(0)  # first frame only for animated WebP/APNG
                source.load()
                icc_profile = source.info.get("icc_profile")
                image = ImageOps.exif_transpose(source)
                return self._encode(image, detected, icc_profile)

    def _encode(
        self, image: Image.Image, fmt: ImageFormat, icc_profile: bytes | None
    ) -> ProcessedImage:
        # Drop every metadata block (EXIF, XMP, comments...) Pillow might carry over;
        # palette transparency is pixel data, not metadata, so it is kept.
        image.info = {k: v for k, v in image.info.items() if k == "transparency"}
        params: dict[str, object] = {}
        if icc_profile:
            params["icc_profile"] = icc_profile
        if fmt is JPEG:
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            params.update(quality=self.quality, optimize=True, progressive=True)
        elif fmt is PNG:
            if image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"):
                image = image.convert("RGBA")
            params.update(optimize=True)
        else:  # WEBP
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            params.update(quality=self.quality, method=4)

        buffer = io.BytesIO()
        image.save(buffer, format=fmt.pillow_name, **params)
        return ProcessedImage(
            data=buffer.getvalue(), format=fmt, width=image.width, height=image.height
        )
