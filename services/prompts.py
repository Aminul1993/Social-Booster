"""Prompt templates for marketing-copy generation.

The model is asked for a strict JSON object (easy to parse, validated
downstream). The parser in :mod:`services.ollama` still understands the
original ``CAPTION:`` / ``HASHTAGS:`` line format and free text as fallbacks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from string import Template
from typing import Literal, TypedDict


class ChatMessage(TypedDict):
    """One OpenAI/Ollama-style chat message."""

    role: Literal["system", "user", "assistant"]
    content: str


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
    "Write social-media copy for a photo. An image classifier detected these "
    "visual concepts (most likely first): $keywords\n"
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


@dataclass(frozen=True, slots=True)
class CopyRequest:
    """Input for one caption + hashtags generation."""

    keywords: tuple[str, ...]
    tone: Tone = Tone.FRIENDLY
    caption_max_chars: int = 150
    min_hashtags: int = 5
    max_hashtags: int = 8

    def __post_init__(self) -> None:
        if not self.keywords:
            raise ValueError("At least one keyword is required")
        if not 1 <= self.min_hashtags <= self.max_hashtags:
            raise ValueError("Invalid hashtag bounds")
        if self.caption_max_chars < 20:
            raise ValueError("caption_max_chars must be >= 20")


def build_copy_messages(request: CopyRequest) -> list[ChatMessage]:
    """Render the system + user messages for a :class:`CopyRequest`."""
    user = COPY_PROMPT.substitute(
        # JSON-encode so keywords are clearly delimited data, not instructions.
        keywords=json.dumps(list(request.keywords), ensure_ascii=False),
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
