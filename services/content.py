"""Normalisation helpers for captions, hashtags and keywords.

These functions are shared by the AI parser (to clean model output) and by the
web layer (to clean what the user typed), so both paths enforce the same rules.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

#: Instagram's hard caption limit; the strictest common ceiling across networks.
MAX_CAPTION_LENGTH = 2200
#: Instagram allows at most 30 hashtags per post.
MAX_HASHTAGS = 30
MAX_HASHTAG_LENGTH = 64
MAX_KEYWORDS = 10
MAX_KEYWORD_LENGTH = 64

_SPLIT_RE = re.compile(r"[\s,;]+")
_NON_WORD_RE = re.compile(r"[^\w]", re.UNICODE)
_KEYWORD_SPLIT_RE = re.compile(r"[,;\n\r]+")
_KEYWORD_ALLOWED_RE = re.compile(r"[^\w\s&'\-.]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_hashtag(token: str) -> str:
    body = unicodedata.normalize("NFC", token).lstrip("#")
    body = _NON_WORD_RE.sub("", body).strip("_")
    return body[:MAX_HASHTAG_LENGTH]


def normalize_hashtags(value: str | Iterable[str], *, limit: int = MAX_HASHTAGS) -> list[str]:
    """Turn free text or a list into a clean, de-duplicated list of ``#tags``.

    Accepts space/comma separated input, missing ``#`` prefixes and stray
    punctuation. Duplicates are removed case-insensitively, order is preserved.

    >>> normalize_hashtags("#Summer, beach  #summer #sun!")
    ['#Summer', '#beach', '#sun']
    """
    tokens: Iterable[str] = _SPLIT_RE.split(value) if isinstance(value, str) else value
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for part in _SPLIT_RE.split(str(token)):
            body = _clean_hashtag(part)
            if not body or body.casefold() in seen:
                continue
            seen.add(body.casefold())
            result.append(f"#{body}")
            if len(result) >= limit:
                return result
    return result


def keyword_to_hashtag(keyword: str) -> str | None:
    """Convert a visual concept such as ``"golden retriever"`` to ``#goldenretriever``."""
    tags = normalize_hashtags(["".join(_SPLIT_RE.split(keyword))], limit=1)
    return tags[0] if tags else None


def normalize_caption(text: str, *, max_length: int = MAX_CAPTION_LENGTH) -> str:
    """Strip control characters and excess blank lines; enforce ``max_length``."""
    cleaned = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in cleaned.split("\n")]
    cleaned = _MANY_NEWLINES_RE.sub("\n\n", "\n".join(lines)).strip()
    return truncate_words(cleaned, max_length)


def truncate_words(text: str, max_length: int, *, ellipsis: str = "…") -> str:
    """Shorten ``text`` to ``max_length`` characters without cutting a word in half."""
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text
    budget = max_length - len(ellipsis)
    if budget <= 0:
        return text[:max_length]
    cut = text[:budget]
    space = cut.rfind(" ")
    if space >= budget // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + ellipsis


def normalize_keywords(
    value: str | Iterable[str],
    *,
    limit: int = MAX_KEYWORDS,
    max_length: int = MAX_KEYWORD_LENGTH,
) -> list[str]:
    """Clean user- or model-supplied visual concepts.

    Splits on commas/semicolons/newlines, removes characters that have no place
    in a keyword, collapses whitespace and de-duplicates case-insensitively.
    """
    tokens: Iterable[str] = _KEYWORD_SPLIT_RE.split(value) if isinstance(value, str) else value
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        cleaned = _KEYWORD_ALLOWED_RE.sub(" ", unicodedata.normalize("NFC", str(token)))
        cleaned = " ".join(cleaned.replace("_", " ").split())[:max_length].strip(" -.'")
        if not cleaned or cleaned.casefold() in seen:
            continue
        seen.add(cleaned.casefold())
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def compose_post_text(caption: str, hashtags: Iterable[str]) -> str:
    """Final text sent to the social network: caption, blank line, hashtags."""
    tags = " ".join(hashtags)
    caption = caption.strip()
    if caption and tags:
        return f"{caption}\n\n{tags}"
    return caption or tags
