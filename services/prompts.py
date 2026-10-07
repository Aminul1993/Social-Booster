"""Prompt templates for image description and marketing-copy generation.

The models are asked for strict JSON objects (easy to parse, validated
downstream). The copy parser in :mod:`services.ollama` still understands the
original ``CAPTION:`` / ``HASHTAGS:`` line format and free text as fallbacks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from string import Template
from typing import Literal, NotRequired, TypedDict


class ChatMessage(TypedDict):
    """One OpenAI/Ollama-style chat message."""

    role: Literal["system", "user", "assistant"]
    content: str
    #: Base64-encoded JPEGs (Ollama's native format; converted for OpenAI-style APIs).
    images: NotRequired[list[str]]


class Tone(StrEnum):
    """Voice options offered in the UI."""

    FRIENDLY = "friendly"
    PROFESSIONAL = "professional"
    PLAYFUL = "playful"
    INSPIRATIONAL = "inspirational"
    LUXURY = "luxury"
    BOLD = "bold"

    @property
    def label(self) -> str:
        return self.value.capitalize()

    @property
    def description(self) -> str:
        return _TONE_DESCRIPTIONS[self]


_TONE_DESCRIPTIONS: dict[Tone, str] = {
    Tone.FRIENDLY: "warm, conversational and approachable",
    Tone.PROFESSIONAL: "polished, credible and concise",
    Tone.PLAYFUL: "fun, witty and light-hearted, emojis welcome",
    Tone.INSPIRATIONAL: "uplifting and motivating",
    Tone.LUXURY: "elegant, refined and exclusive",
    Tone.BOLD: "energetic, confident and attention-grabbing",
}

SYSTEM_PROMPT = (
    "You are an expert social-media copywriter. You write short, scroll-stopping "
    "captions and pick hashtags people actually search for. You always answer "
    "with valid JSON only - no markdown, no commentary."
)

COPY_PROMPT = Template(
    "Write social-media copy for a photo about these keywords (most important "
    "first): $keywords\n"
    "${description}"
    "Tone of voice: $tone ($tone_description).\n\n"
    "Return a JSON object with exactly two keys:\n"
    '- "caption": one short, punchy sentence of at most $caption_max characters. '
    "Do not put hashtags in the caption.\n"
    '- "hashtags": an array of $min_tags to $max_tags popular, relevant hashtags, '
    "each starting with #, without spaces.\n\n"
    'Example: {"caption": "Golden hour never looked this good.", '
    '"hashtags": ["#goldenhour", "#sunset", "#travel", "#wanderlust", "#photography"]}'
)

REPAIR_PROMPT = (
    "Your previous reply could not be parsed. Reply again with ONLY a JSON object "
    'of the form {"caption": "...", "hashtags": ["#tag1", "#tag2"]} and nothing else.'
)

DESCRIBE_SYSTEM_PROMPT = (
    "You describe photos for social-media marketing. You always answer with valid "
    "JSON only - no markdown, no commentary."
)

DESCRIBE_PROMPT = Template(
    "Describe this image for a social-media post. Return a JSON object with exactly "
    "two keys:\n"
    '- "description": one factual sentence of at most $description_max characters '
    "about what the image shows.\n"
    '- "keywords": an array of up to $max_keywords short lowercase keywords (1-3 words '
    "each) for the main subjects, setting, mood and colours, most prominent first. "
    "No hashtags."
)

#: Upper bound for the stored description (the prompt asks for less).
DESCRIPTION_MAX_CHARS = 300


@dataclass(frozen=True, slots=True)
class CopyRequest:
    """Input for one caption + hashtags generation."""

    keywords: tuple[str, ...]
    tone: Tone = Tone.FRIENDLY
    caption_max_chars: int = 150
    min_hashtags: int = 5
    max_hashtags: int = 8
    #: What the vision model saw; background only, the keywords decide the topic.
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.keywords:
            raise ValueError("At least one keyword is required")
        if not 1 <= self.min_hashtags <= self.max_hashtags:
            raise ValueError("Invalid hashtag bounds")
        if self.caption_max_chars < 20:
            raise ValueError("caption_max_chars must be >= 20")


def build_copy_messages(request: CopyRequest) -> list[ChatMessage]:
    """Render the system + user messages for a :class:`CopyRequest`."""
    description = ""
    if request.description:
        # JSON-encoded like the keywords: model output is data, not instructions.
        description = (
            "Background - what the photo shows (if it conflicts with the keywords, "
            f"follow the keywords): {json.dumps(request.description, ensure_ascii=False)}\n"
        )
    user = COPY_PROMPT.substitute(
        # JSON-encode so keywords are clearly delimited data, not instructions.
        keywords=json.dumps(list(request.keywords), ensure_ascii=False),
        description=description,
        tone=request.tone.value,
        tone_description=request.tone.description,
        caption_max=request.caption_max_chars,
        min_tags=request.min_hashtags,
        max_tags=request.max_hashtags,
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_describe_messages(
    image_base64: str, *, max_keywords: int, description_max_chars: int = 200
) -> list[ChatMessage]:
    """Render the messages asking a vision model to describe one JPEG image."""
    user = DESCRIBE_PROMPT.substitute(
        description_max=description_max_chars, max_keywords=max_keywords
    )
    return [
        {"role": "system", "content": DESCRIBE_SYSTEM_PROMPT},
        {"role": "user", "content": user, "images": [image_base64]},
    ]
