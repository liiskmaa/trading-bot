"""
Unit tests for config/loader.py — YAML config with env var overrides.
"""

import pytest
from config.loader import Config


def write_config(tmp_path, content: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return str(p)


class TestBasicYamlReading:
    def test_reads_string_value(self, tmp_path):
        path = write_config(tmp_path, "section:\n  key: hello\n")
        cfg = Config(path)
        assert cfg.str("section", "key") == "hello"

    def test_reads_int_value(self, tmp_path):
        path = write_config(tmp_path, "redis:\n  port: 6379\n")
        cfg = Config(path)
        assert cfg.int("redis", "port") == 6379

    def test_reads_float_value(self, tmp_path):
        path = write_config(tmp_path, "risk:\n  threshold: 7.5\n")
        cfg = Config(path)
        assert abs(cfg.float("risk", "threshold") - 7.5) < 1e-9

    def test_reads_bool_true(self, tmp_path):
        path = write_config(tmp_path, "trading:\n  live_confirmation: true\n")
        cfg = Config(path)
        assert cfg.bool("trading", "live_confirmation") is True

    def test_reads_bool_false(self, tmp_path):
        path = write_config(tmp_path, "trading:\n  live_confirmation: false\n")
        cfg = Config(path)
        assert cfg.bool("trading", "live_confirmation") is False

    def test_returns_default_for_missing_key(self, tmp_path):
        path = write_config(tmp_path, "section:\n  other: val\n")
        cfg = Config(path)
        assert cfg.get("section", "missing", default="fallback") == "fallback"

    def test_returns_default_for_missing_section(self, tmp_path):
        path = write_config(tmp_path, "section:\n  key: val\n")
        cfg = Config(path)
        assert cfg.str("missing_section", "key", default="x") == "x"


class TestEnvVarOverride:
    def test_env_var_overrides_yaml_value(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "ai_filter:\n  base_url: http://localhost:11434\n")
        monkeypatch.setenv("AI_FILTER__BASE_URL", "http://ollama:11434")
        cfg = Config(path)
        assert cfg.str("ai_filter", "base_url") == "http://ollama:11434"

    def test_yaml_value_used_when_no_env_var(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "section:\n  key: yaml_value\n")
        monkeypatch.delenv("SECTION__KEY", raising=False)
        cfg = Config(path)
        assert cfg.get("section", "key") == "yaml_value"

    def test_int_accessor_converts_env_var_string(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "redis:\n  port: 6379\n")
        monkeypatch.setenv("REDIS__PORT", "6380")
        cfg = Config(path)
        assert cfg.int("redis", "port") == 6380

    def test_float_accessor_converts_env_var_string(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "risk:\n  threshold: 7.0\n")
        monkeypatch.setenv("RISK__THRESHOLD", "8.5")
        cfg = Config(path)
        assert abs(cfg.float("risk", "threshold") - 8.5) < 1e-9

    def test_bool_accessor_converts_true_string(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "trading:\n  live_confirmation: false\n")
        monkeypatch.setenv("TRADING__LIVE_CONFIRMATION", "true")
        cfg = Config(path)
        assert cfg.bool("trading", "live_confirmation") is True

    def test_bool_accessor_converts_1_string(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "trading:\n  live_confirmation: false\n")
        monkeypatch.setenv("TRADING__LIVE_CONFIRMATION", "1")
        cfg = Config(path)
        assert cfg.bool("trading", "live_confirmation") is True

    def test_env_var_beats_yaml_even_when_yaml_key_exists(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "section:\n  key: from_yaml\n")
        monkeypatch.setenv("SECTION__KEY", "from_env")
        cfg = Config(path)
        assert cfg.get("section", "key") == "from_env"

    def test_env_var_for_missing_yaml_key_still_works(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "section:\n  other: val\n")
        monkeypatch.setenv("SECTION__KEY", "env_only")
        cfg = Config(path)
        assert cfg.get("section", "key") == "env_only"

    def test_deep_nesting_uses_double_underscore(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, "a:\n  b:\n    c: yaml\n")
        monkeypatch.setenv("A__B__C", "env")
        cfg = Config(path)
        assert cfg.get("a", "b", "c") == "env"
