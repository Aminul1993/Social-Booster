from __future__ import annotations

import pytest

from services.content import (
    MAX_CAPTION_LENGTH,
    compose_post_text,
    keyword_to_hashtag,
    normalize_caption,
    normalize_hashtags,
    normalize_keywords,
    truncate_words,
)


class TestNormalizeHashtags:
    def test_accepts_free_text_and_dedupes_case_insensitively(self) -> None:
        assert normalize_hashtags("#Summer, beach  #summer #sun!") == ["#Summer", "#beach", "#sun"]

    def test_accepts_list_with_embedded_spaces(self) -> None:
        assert normalize_hashtags(["#a b", "##c", "", "  "]) == ["#a", "#b", "#c"]

    def test_strips_punctuation_and_keeps_unicode(self) -> None:
        assert normalize_hashtags("#café! #日本 #-x-") == ["#café", "#日本", "#x"]

    def test_respects_limit(self) -> None:
        assert normalize_hashtags(" ".join(f"t{i}" for i in range(50)), limit=3) == [
            "#t0",
            "#t1",
            "#t2",
        ]

    def test_truncates_very_long_tags(self) -> None:
        (tag,) = normalize_hashtags("x" * 200)
        assert len(tag) == 65  # '#' + 64

    def test_underscores_trimmed_at_edges_only(self) -> None:
        assert normalize_hashtags("_my_tag_") == ["#my_tag"]


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [("golden retriever", "#goldenretriever"), ("  ", None), ("!!!", None)],
)
def test_keyword_to_hashtag(keyword: str, expected: str | None) -> None:
    assert keyword_to_hashtag(keyword) == expected


class TestCaption:
    def test_normalize_collapses_whitespace_and_blank_lines(self) -> None:
        raw = "  Hello\t  world \r\n\r\n\r\n\r\nSecond\x07 line  "
        assert normalize_caption(raw) == "Hello world\n\nSecond line"

    def test_normalize_enforces_max_length(self) -> None:
        assert len(normalize_caption("word " * 1000)) <= MAX_CAPTION_LENGTH

    def test_truncate_keeps_whole_words(self) -> None:
        assert truncate_words("The quick brown fox jumps", 15) == "The quick…"

    def test_truncate_noop_when_short(self) -> None:
        assert truncate_words("short", 10) == "short"

    def test_truncate_hard_cut_without_spaces(self) -> None:
        assert truncate_words("abcdefghijklmnop", 6) == "abcde…"

    @pytest.mark.parametrize(("limit", "expected"), [(0, ""), (1, "a")])
    def test_truncate_tiny_limits(self, limit: int, expected: str) -> None:
        assert truncate_words("abc def", limit) == expected


class TestKeywords:
    def test_splits_cleans_and_dedupes(self) -> None:
        raw = "golden_retriever, Tennis ball;tennis ball\n<script>, , rock & roll"
        assert normalize_keywords(raw) == [
            "golden retriever",
            "Tennis ball",
            "script",
            "rock & roll",
        ]

    def test_limit_and_length(self) -> None:
        result = normalize_keywords([f"k{i}" for i in range(20)] + ["x" * 100], limit=12)
        assert len(result) == 12

    def test_max_length(self) -> None:
        assert normalize_keywords(["y" * 100], max_length=10) == ["y" * 10]


@pytest.mark.parametrize(
    ("caption", "tags", "expected"),
    [
        ("Hi", ["#a", "#b"], "Hi\n\n#a #b"),
        ("Hi ", [], "Hi"),
        ("", ["#a"], "#a"),
    ],
)
def test_compose_post_text(caption: str, tags: list[str], expected: str) -> None:
    assert compose_post_text(caption, tags) == expected
