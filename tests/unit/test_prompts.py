from __future__ import annotations

import pytest

from services.prompts import SYSTEM_PROMPT, CopyRequest, Tone, build_copy_messages


def test_messages_embed_keywords_tone_and_limits() -> None:
    request = CopyRequest(
        keywords=("golden retriever", 'tennis "ball"'),
        tone=Tone.PLAYFUL,
        caption_max_chars=120,
        min_hashtags=4,
        max_hashtags=6,
    )
    system, user = build_copy_messages(request)
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    assert user["role"] == "user"
    content = user["content"]
    assert '["golden retriever", "tennis \\"ball\\""]' in content  # JSON-encoded data
    assert "Tone of voice: playful" in content
    assert "at most 120 characters" in content
    assert "4 to 6" in content
    assert '"caption"' in content
    assert '"hashtags"' in content


@pytest.mark.parametrize("tone", list(Tone))
def test_every_tone_has_label_and_description(tone: Tone) -> None:
    assert tone.label
    assert tone.description


@pytest.mark.parametrize(
    "kwargs",
    [
        {"keywords": ()},
        {"keywords": ("a",), "min_hashtags": 0},
        {"keywords": ("a",), "min_hashtags": 9, "max_hashtags": 8},
        {"keywords": ("a",), "caption_max_chars": 5},
    ],
)
def test_request_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"keyword|hashtag|caption_max_chars"):
        CopyRequest(**kwargs)  # type: ignore[arg-type]
