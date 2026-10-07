"""Upload validation and sanitising.

Every uploaded file goes through :class:`ImageProcessor` before it touches
disk. The processor:

1. enforces a byte-size ceiling,
2. checks the declared MIME type against an allow-list,
3. sniffs the magic bytes and rejects content/type mismatches,
4. lets Pillow fully decode the image (rejecting corrupt files and
   decompression bombs via an explicit pixel budget),
5. downscales images whose longer side exceeds ``max_dimension``, and
6. re-encodes the pixels, which strips EXIF/GPS metadata and neutralises
   polyglot files. EXIF orientation is applied first so photos stay upright.

All work here is CPU-bound and synchronous; callers run it in a thread.
Decoded pixels cost ~4 bytes each (a 40 MP photo is ~150 MB), so the image is
only ever held once: JPEGs that will be downscaled are decoded at reduced scale
by libjpeg, orientation is applied in place, and the copy the vision model
needs is a small preview made while the pixels are decoded anyway.
"""

from __future__ import annotations

import io
import unicodedata
import warnings
from dataclasses import dataclass, replace
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

#: Short side (px) of the preview handed to the vision model: plenty for a
#: one-sentence description and only ~50-100 kB as the JPEG sent to it.
PREVIEW_SIZE = 512


class InvalidImageError(ValueError):
    """The upload is not an acceptable image. ``str(exc)`` is user-safe."""


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """A validated, metadata-free image ready to be stored."""

    data: bytes
    format: ImageFormat
    width: int
    height: int
    #: Small RGB copy for image tagging (only when requested).
    preview: Image.Image | None = None


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


def make_preview(image: Image.Image, size: int = PREVIEW_SIZE) -> Image.Image:
    """Return an RGB copy of ``image`` whose short side is at most ``size`` px."""
    if image.mode not in ("RGB", "L"):
        # Alpha/palette/16-bit modes can't be downscaled well as they are.
        image = image.convert("RGB")
    scale = size / min(image.size)
    if scale < 1:
        target = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(target, Image.Resampling.BICUBIC, reducing_gap=2.0)
    return image.convert("RGB")


class ImageProcessor:
    """Validate and sanitise uploaded images."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_pixels: int,
        quality: int = 90,
        max_dimension: int | None = None,
    ) -> None:
        if max_bytes <= 0 or max_pixels <= 0:
            raise ValueError("max_bytes and max_pixels must be positive")
        if max_dimension is not None and max_dimension <= 0:
            raise ValueError("max_dimension must be positive (or None to keep the size)")
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels
        self.quality = quality
        self.max_dimension = max_dimension

    def process(
        self, data: bytes, *, declared_content_type: str | None, preview: bool = False
    ) -> ProcessedImage:
        """Validate ``data`` and return a re-encoded, metadata-free copy.

        With ``preview=True`` the result also carries a :func:`make_preview`
        thumbnail, so tagging never has to decode the full-size image again.

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
            return self._decode_and_reencode(data, detected, preview=preview)
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

    def _decode_and_reencode(
        self, data: bytes, detected: ImageFormat, *, preview: bool
    ) -> ProcessedImage:
        with warnings.catch_warnings():
            # Treat Pillow's bomb warning as an error; our own budget is stricter anyway.
            warnings.simplefilter("error", Image.DecompressionBombWarning)

            # Pass 1: structural verification (cheap, does not decode pixels).
            self._verify(data, detected)

            # Pass 2: full decode (verify() leaves the image unusable) + re-encode.
            image, icc_profile = self._decode(data, detected)
            thumbnail = make_preview(image) if preview else None
            processed = self._encode(image, detected, icc_profile)
            return replace(processed, preview=thumbnail)

    def _verify(self, data: bytes, detected: ImageFormat) -> None:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format != detected.pillow_name:
                raise InvalidImageError("The file content does not match its declared image type.")
            width, height = probe.size
            if width <= 0 or height <= 0:
                raise InvalidImageError("The image has no pixels.")
            if width * height > self.max_pixels:
                raise InvalidImageError(
                    f"The image is too large ({width}x{height} pixels). "
                    f"Maximum is {self.max_pixels:,} pixels."
                )
            probe.verify()

    def _decode(self, data: bytes, detected: ImageFormat) -> tuple[Image.Image, bytes | None]:
        """Decode the first frame, upright and within ``max_dimension``."""
        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)  # first frame only for animated WebP/APNG
            if (target := self._fit(source.size)) is not None:
                # JPEG only (no-op otherwise): libjpeg decodes at 1/2, 1/4 or 1/8
                # scale, so a large photo never exists in memory at full size.
                source.draft(None, target)
            source.load()
            icc_profile = source.info.get("icc_profile")
            # In place: the copy-returning variant holds the pixels twice.
            ImageOps.exif_transpose(source, in_place=True)
        image = self._shrink(source)
        if image is source and detected is WEBP:
            # Pillow keeps libwebp's decoder, with two more full-size canvases, alive
            # as long as the image object; a plain copy lets it go before encoding.
            image = source.copy()
        return image, icc_profile

    def _fit(self, size: tuple[int, int]) -> tuple[int, int] | None:
        """``size`` scaled down to fit ``max_dimension``, or ``None`` if it fits."""
        longest = max(size)
        if self.max_dimension is None or longest <= self.max_dimension:
            return None
        scale = self.max_dimension / longest
        return max(1, round(size[0] * scale)), max(1, round(size[1] * scale))

    def _shrink(self, image: Image.Image) -> Image.Image:
        target = self._fit(image.size)
        if target is None:
            return image
        if image.mode in ("1", "P") or "transparency" in image.info:
            # Pillow resizes palette/bilevel images nearest-neighbour, and blending
            # breaks colour-key transparency: switch to true colour first.
            image = image.convert("RGBA" if image.has_transparency_data else "RGB")
        return image.resize(target, Image.Resampling.LANCZOS, reducing_gap=3.0)

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
