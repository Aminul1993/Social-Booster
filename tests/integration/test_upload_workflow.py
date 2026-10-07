"""Upload workflow: browser -> /upload -> validation -> storage -> ResNet tagging -> cards."""

from __future__ import annotations

import io
import re
import threading
from typing import Any

import pytest
from PIL import Image

from services.images import ImageProcessor, ProcessedImage
from services.vision import VisionLabel
from tests.conftest import AppFactory
from tests.helpers import (
    AppClient,
    FailingClassifier,
    FakeClassifier,
    make_client,
    make_image,
    toast,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("mock_http")]

DRAFT_ID_RE = re.compile(r'id="draft-([0-9a-f]{32})"')


def jpeg_with_gps() -> bytes:
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (0, 128, 0)).save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


async def test_multiple_images_become_tagged_cards(client: AppClient) -> None:
    response = await client.upload(
        ("dog.png", make_image("PNG"), "image/png"),
        ("beach.jpg", jpeg_with_gps(), "image/jpeg"),
    )
    assert response.status_code == 200
    assert toast(response) == {
        "level": "success",
        "message": "Uploaded 2 images. Generate copy for each card.",
    }
    ids = DRAFT_ID_RE.findall(response.text)
    assert len(ids) == 2
    html = response.text
    assert "golden retriever" in html  # labels + confidence
    assert "82%" in html
    assert 'value="golden retriever, tennis ball, Labrador retriever"' in html  # keywords
    assert "Generate caption &amp; hashtags" in html

    # Persisted: a fresh page load shows the same drafts.
    page = (await client.get("/")).text
    assert all(f'id="draft-{draft_id}"' in page for draft_id in ids)
    assert "No drafts yet" in page  # empty-state markup exists but is hidden via CSS :has()

    # Stored files are re-encoded and stripped of EXIF/GPS.
    stored = list(client.container.settings.upload_dir.iterdir())
    assert len(stored) == 2
    jpeg = next(path for path in stored if path.suffix == ".jpg")
    with Image.open(jpeg) as image:
        assert not image.getexif()

    # And publicly served with safe headers.
    served = await client.get(f"/uploads/{jpeg.name}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["X-Content-Type-Options"] == "nosniff"
    assert served.headers["Cross-Origin-Resource-Policy"] == "cross-origin"

    metrics = (await client.get("/metrics")).text
    assert 'mab_uploads_total{outcome="accepted"} 2.0' in metrics


async def test_bad_files_are_skipped_not_fatal(client: AppClient) -> None:
    response = await client.upload(
        ("ok.webp", make_image("WEBP"), "image/webp"),
        ("evil.html", b"<script>alert(1)</script>", "text/html"),
        ("fake.png", make_image("JPEG"), "image/png"),
    )
    assert response.status_code == 200
    message = toast(response)
    assert message["level"] == "warning"
    assert message["message"].startswith("Uploaded 1 image. Skipped:")
    assert "evil.html (Only JPEG, PNG and WebP images are supported.)" in message["message"]
    assert "fake.png (The file content does not match" in message["message"]
    assert len(DRAFT_ID_RE.findall(response.text)) == 1


async def test_all_invalid_is_an_error(client: AppClient) -> None:
    response = await client.upload(("notes.txt", b"hello", "text/plain"))
    assert response.status_code == 400
    assert response.headers["HX-Reswap"] == "none"
    assert toast(response)["message"].startswith("No images were uploaded: notes.txt")


async def test_empty_file_field(client: AppClient) -> None:
    response = await client.upload(("", b"", "application/octet-stream"))
    assert response.status_code in (400, 422)


def noisy_png(size: int = 200) -> bytes:
    """Random pixels do not compress: reliably larger than tiny limits."""
    buffer = io.BytesIO()
    Image.effect_noise((size, size), 100).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


async def test_limits(app_factory: AppFactory) -> None:
    app = app_factory(max_files_per_upload=2, max_upload_size_mb=0.01, max_drafts_per_session=2)
    async with make_client(app) as client:
        too_many = await client.upload(*[(f"{i}.png", make_image(), "image/png") for i in range(3)])
        assert too_many.status_code == 400
        assert "at most 2 images" in toast(too_many)["message"]

        oversized = await client.upload(
            ("small.png", make_image(), "image/png"), ("huge.png", noisy_png(), "image/png")
        )
        assert oversized.status_code == 200
        assert "huge.png (larger than 0.01 MB)" in toast(oversized)["message"]

        overflow = await client.upload(
            ("a.png", make_image(), "image/png"), ("b.png", make_image(), "image/png")
        )
        assert overflow.status_code == 200
        assert "b.png (draft limit reached)" in toast(overflow)["message"]

        full = await client.upload(("c.png", make_image(), "image/png"))
        assert full.status_code == 400
        assert "maximum number of drafts" in toast(full)["message"]


async def test_vision_failure_degrades_gracefully(app_factory: AppFactory) -> None:
    async with make_client(app_factory(classifier=FailingClassifier)) as client:
        response = await client.upload(("x.png", make_image(), "image/png"))
        assert response.status_code == 200
        assert "be analysed automatically" in response.text
        assert 'name="keywords" value=""' in response.text
        metrics = (await client.get("/metrics")).text
        assert "mab_vision_failures_total 1.0" in metrics


async def test_images_are_decoded_one_at_a_time(
    client: AppClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch must not hold every decoded image in memory at once."""
    active = 0
    peak = 0
    lock = threading.Lock()
    original = ImageProcessor.process

    def tracking(self: ImageProcessor, *args: Any, **kwargs: Any) -> ProcessedImage:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            threading.Event().wait(0.02)
            return original(self, *args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ImageProcessor, "process", tracking)
    response = await client.upload(*[(f"{i}.png", make_image(), "image/png") for i in range(4)])
    assert response.status_code == 200
    assert len(DRAFT_ID_RE.findall(response.text)) == 4
    assert peak == 1


async def test_vision_gets_a_downscaled_preview(app_factory: AppFactory) -> None:
    seen: list[tuple[int, int]] = []

    class Recording(FakeClassifier):
        def predict(self, image: Image.Image, top_k: int) -> list[VisionLabel]:
            seen.append(image.size)
            return super().predict(image, top_k)

    async with make_client(app_factory(classifier=Recording)) as client:
        photo = make_image("JPEG", size=(1600, 1200))
        response = await client.upload(("photo.jpg", photo, "image/jpeg"))
        assert response.status_code == 200
        assert "golden retriever" in response.text
    assert seen == [(683, 512)]


async def test_large_images_are_downscaled(client: AppClient) -> None:
    wide = make_image("JPEG", size=(4096, 1024))
    response = await client.upload(("wide.jpg", wide, "image/jpeg"))
    assert response.status_code == 200
    stored = next(client.container.settings.upload_dir.iterdir())
    with Image.open(stored) as image:
        assert image.size == (2048, 512)  # IMAGE_MAX_DIMENSION default


async def test_vision_disabled(app_factory: AppFactory) -> None:
    async with make_client(app_factory(vision_backend="disabled")) as client:
        response = await client.upload(("x.png", make_image(), "image/png"))
        assert response.status_code == 200
        assert "Detected in the image" not in response.text


async def test_drafts_are_isolated_between_sessions(client: AppClient) -> None:
    draft_id = await client.upload_one()

    client.http.cookies.clear()  # a different browser
    await client.open()
    assert f"draft-{draft_id}" not in (await client.get("/")).text
    assert (await client.delete(f"/drafts/{draft_id}")).status_code == 404
    assert (await client.patch(f"/drafts/{draft_id}", data={"caption": "x"})).status_code == 404
    generate = await client.post("/generate", data={"draft_id": draft_id, "keywords": "x"})
    assert generate.status_code == 404
    assert len(list(client.container.settings.upload_dir.iterdir())) == 1


async def test_delete_flow(client: AppClient) -> None:
    draft_id = await client.upload_one()
    response = await client.delete(f"/drafts/{draft_id}")
    assert response.status_code == 200
    assert response.text == ""
    assert toast(response)["message"] == "Draft deleted."
    assert not list(client.container.settings.upload_dir.iterdir())
    again = await client.delete(f"/drafts/{draft_id}")
    assert again.status_code == 404
    assert "no longer exists" in toast(again)["message"]


async def test_invalid_draft_id_path(client: AppClient) -> None:
    response = await client.delete("/drafts/../../etc")
    assert response.status_code in (404, 405, 422)
