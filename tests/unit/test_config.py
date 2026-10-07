from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import BASE_DIR, Environment, RateLimitRule, Settings, get_settings


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


class TestDefaults:
    def test_development_defaults(self) -> None:
        settings = make()
        assert settings.environment is Environment.DEVELOPMENT
        assert settings.session_secret is not None  # ephemeral dev secret
        assert len(settings.session_secret_value) >= 32
        assert not settings.secure_cookies
        assert settings.docs_enabled
        assert settings.max_upload_bytes == 10 * 1024 * 1024
        assert settings.max_request_body_bytes == 10 * settings.max_upload_bytes + 1024 * 1024
        assert settings.database_path == BASE_DIR / "data" / "app.db"
        assert settings.upload_dir == BASE_DIR / "uploads"
        assert settings.ollama_endpoint == "https://ollama.com/v1/chat/completions"
        assert settings.buffer_token_url.endswith("/1/oauth2/token.json")

    def test_secrets_are_hidden(self) -> None:
        settings = make(ollama_api_key="sk-super-secret", buffer_client_secret="bsecret")
        assert "sk-super-secret" not in repr(settings)
        assert "bsecret" not in str(settings.model_dump())
        assert Settings.secret_value(settings.ollama_api_key) == "sk-super-secret"
        assert Settings.secret_value(None) is None

    def test_reads_environment_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:latest")
        monkeypatch.setenv("ALLOWED_HOSTS", "a.example.com, b.example.com")
        monkeypatch.setenv("BUFFER_CLIENT_ID", "  ")
        settings = make()
        assert settings.ollama_model == "llama3.2:latest"
        assert settings.allowed_hosts == ["a.example.com", "b.example.com"]
        assert settings.buffer_client_id is None

    def test_env_example_is_valid(self) -> None:
        settings = Settings(_env_file=BASE_DIR / ".env.example")
        assert settings.environment is Environment.DEVELOPMENT
        assert settings.cookie_secure is None  # empty value -> default
        assert settings.public_base_url is None
        assert settings.rate_limit_rule("upload") == RateLimitRule(20, 60)

    def test_get_settings_is_cached(self) -> None:
        get_settings.cache_clear()
        try:
            assert get_settings() is get_settings()
        finally:
            get_settings.cache_clear()


class TestProduction:
    def test_requires_strong_session_secret(self) -> None:
        with pytest.raises(ValidationError, match="SESSION_SECRET"):
            make(environment="production")
        with pytest.raises(ValidationError, match="SESSION_SECRET"):
            make(environment="production", session_secret="short")

    def test_secure_defaults(self) -> None:
        settings = make(environment="production", session_secret="p" * 40)
        assert settings.is_production
        assert settings.secure_cookies
        assert not settings.docs_enabled

    def test_explicit_overrides(self) -> None:
        settings = make(
            environment="production",
            session_secret="p" * 40,
            cookie_secure=False,
            enable_api_docs=True,
            max_request_body_mb=5,
        )
        assert not settings.secure_cookies
        assert settings.docs_enabled
        assert settings.max_request_body_bytes == 5 * 1024 * 1024


class TestValidation:
    def test_public_base_url(self) -> None:
        assert make(public_base_url="https://x.example.com/").public_base_url == (
            "https://x.example.com"
        )
        assert make(public_base_url="").public_base_url is None
        with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
            make(public_base_url="x.example.com")

    def test_hashtag_bounds(self) -> None:
        with pytest.raises(ValidationError, match="HASHTAGS_MIN"):
            make(hashtags_min=9, hashtags_max=8)

    def test_rate_limit_strings(self) -> None:
        settings = make(rate_limit_upload="5/second", rate_limit_auth=" 3 / hours ")
        assert settings.rate_limit_rule("upload") == RateLimitRule(5, 1)
        assert settings.rate_limit_rule("auth") == RateLimitRule(3, 3600)
        with pytest.raises(ValidationError, match="Invalid rate limit"):
            make(rate_limit_generate="lots")

    @pytest.mark.parametrize("value", ["0/minute", "1/week", "-1/second"])
    def test_rate_limit_rule_parse_errors(self, value: str) -> None:
        with pytest.raises(ValueError, match=r"rate limit|>= 1"):
            RateLimitRule.parse(value)

    def test_absolute_paths_preserved(self, tmp_path: Path) -> None:
        assert make(database_path=tmp_path / "x.db").database_path == tmp_path / "x.db"
