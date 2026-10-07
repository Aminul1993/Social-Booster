from __future__ import annotations

import io

import pytest
from PIL import Image

from services.images import (
    JPEG,
    PNG,
    WEBP,
    ImageProcessor,
    InvalidImageError,
    safe_display_name,
    sniff_format,
)
from tests.helpers import make_image


@pytest.fixture
def processor() -> ImageProcessor:
    return ImageProcessor(max_bytes=2 * 1024 * 1024, max_pixels=4_000_000)


def _jpeg_with_exif(orientation: int = 6) -> bytes:
    image = Image.new("RGB", (40, 20), (10, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation
    exif[0x010F] = "SecretCam"  # Make
    exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}  # GPS IFD
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class TestSniffing:
    @pytest.mark.parametrize(("fmt", "expected"), [("PNG", PNG), ("JPEG", JPEG), ("WEBP", WEBP)])
    def test_detects_supported_formats(self, fmt: str, expected: object) -> None:
        assert sniff_format(make_image(fmt)) is expected

    def test_unknown(self) -> None:
        assert sniff_format(b"GIF89a....") is None
        assert sniff_format(b"RIFF") is None


class TestProcessing:
    @pytest.mark.parametrize(
        ("fmt", "content_type"),
        [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
    )
    def test_valid_images_are_reencoded(
        self, processor: ImageProcessor, fmt: str, content_type: str
    ) -> None:
        result = processor.process(make_image(fmt), declared_content_type=content_type)
        assert result.format.pillow_name == fmt
        assert (result.width, result.height) == (64, 48)
        with Image.open(io.BytesIO(result.data)) as reopened:
            assert reopened.format == fmt

    def test_png_with_alpha_keeps_transparency(self, processor: ImageProcessor) -> None:
        data = make_image("PNG", color=(1, 2, 3), alpha=True)
        result = processor.process(data, declared_content_type="image/png")
        with Image.open(io.BytesIO(result.data)) as reopened:
            assert reopened.mode == "RGBA"

    def test_exif_is_stripped_and_orientation_applied(self, processor: ImageProcessor) -> None:
        result = processor.process(_jpeg_with_exif(), declared_content_type="image/jpeg")
        # Orientation 6 = rotate 90°: 40x20 becomes 20x40.
        assert (result.width, result.height) == (20, 40)
        with Image.open(io.BytesIO(result.data)) as reopened:
            assert not reopened.getexif()
            assert "exif" not in reopened.info

    @pytest.mark.parametrize("declared", ["application/octet-stream", "", None, "image/jpg"])
    def test_lenient_declared_types(self, processor: ImageProcessor, declared: str | None) -> None:
        assert processor.process(make_image("JPEG"), declared_content_type=declared).format is JPEG

    def test_declared_type_with_parameters(self, processor: ImageProcessor) -> None:
        result = processor.process(make_image("PNG"), declared_content_type="image/png; q=1")
        assert result.format is PNG

    def test_rejects_empty(self, processor: ImageProcessor) -> None:
        with pytest.raises(InvalidImageError, match="empty"):
            processor.process(b"", declared_content_type="image/png")

    def test_rejects_oversized_bytes(self) -> None:
        small = ImageProcessor(max_bytes=100, max_pixels=1000)
        with pytest.raises(InvalidImageError, match="larger than"):
            small.process(make_image(), declared_content_type="image/png")

    def test_rejects_disallowed_declared_type(self, processor: ImageProcessor) -> None:
        with pytest.raises(InvalidImageError, match="Only JPEG"):
            processor.process(make_image(), declared_content_type="text/html")

    def test_rejects_unknown_magic_bytes(self, processor: ImageProcessor) -> None:
        with pytest.raises(InvalidImageError, match="Only JPEG"):
            processor.process(b"<svg onload=alert(1)>", declared_content_type="image/png")

    def test_rejects_type_mismatch(self, processor: ImageProcessor) -> None:
        with pytest.raises(InvalidImageError, match="does not match"):
            processor.process(make_image("JPEG"), declared_content_type="image/png")

    def test_rejects_too_many_pixels(self) -> None:
        tight = ImageProcessor(max_bytes=10_000_000, max_pixels=100)
        with pytest.raises(InvalidImageError, match="too large"):
            tight.process(make_image(size=(20, 20)), declared_content_type="image/png")

    def test_rejects_truncated_image(self, processor: ImageProcessor) -> None:
        data = make_image("PNG", size=(300, 300))
        with pytest.raises(InvalidImageError, match="corrupted"):
            processor.process(data[: len(data) // 2], declared_content_type="image/png")

    def test_rejects_polyglot_header_only(self, processor: ImageProcessor) -> None:
        with pytest.raises(InvalidImageError):
            processor.process(b"\x89PNG\r\n\x1a\n" + b"garbage" * 10, declared_content_type=None)

    def test_constructor_validates_limits(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ImageProcessor(max_bytes=0, max_pixels=10)

    def test_palette_and_cmyk_modes(self, processor: ImageProcessor) -> None:
        palette = io.BytesIO()
        Image.new("P", (10, 10)).save(palette, format="PNG")
        assert (
            processor.process(palette.getvalue(), declared_content_type="image/png").format is PNG
        )

        cmyk = io.BytesIO()
        Image.new("CMYK", (10, 10)).save(cmyk, format="JPEG")
        assert processor.process(cmyk.getvalue(), declared_content_type="image/jpeg").format is JPEG

        webp_l = io.BytesIO()
        Image.new("L", (10, 10)).save(webp_l, format="WEBP")
        assert (
            processor.process(webp_l.getvalue(), declared_content_type="image/webp").format is WEBP
        )


class TestDisplayName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, "image"),
            ("", "image"),
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\me\\photo.jpg", "photo.jpg"),
            ("bad\x00name\u202e.png", "badname.png"),
            ("...", "image"),
        ],
    )
    def test_sanitises(self, raw: str | None, expected: str) -> None:
        assert safe_display_name(raw) == expected

    def test_truncates_long_names_keeping_extension(self) -> None:
        name = safe_display_name("a" * 300 + ".jpeg", max_length=50)
        assert len(name) <= 50
        assert name.endswith("….jpeg")

    def test_truncates_long_names_without_extension(self) -> None:
        name = safe_display_name("b" * 300, max_length=20)
        assert len(name) == 20
        assert name.endswith("…")
