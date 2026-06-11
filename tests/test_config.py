"""Tests for read-only config loading + platform/model resolution (Batch A2).

Covers: path resolution ($ODIN_CONFIG / $ODIN_HOME), missing & malformed
files, partial TOML, and the full flag → env → config → fallback order for
both `resolve_platform` and `resolve_model`.
"""

from __future__ import annotations

import pytest

from odin import config

# Env vars that perturb resolution — cleared before each test so the host
# environment can't leak in.
_ENV_KEYS = ("ODIN_CONFIG", "ODIN_HOME", "ODIN_PLATFORM", "ODIN_MODEL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


# --- path resolution -------------------------------------------------------


def test_config_path_prefers_odin_config(monkeypatch, tmp_path):
    explicit = tmp_path / "custom.toml"
    monkeypatch.setenv("ODIN_CONFIG", str(explicit))
    monkeypatch.setenv("ODIN_HOME", str(tmp_path / "home"))
    assert config.config_path() == explicit


def test_config_path_falls_back_to_odin_home(monkeypatch, tmp_path):
    monkeypatch.setenv("ODIN_HOME", str(tmp_path))
    assert config.config_path() == tmp_path / "config.toml"


def test_odin_home_defaults_under_user_home(monkeypatch):
    monkeypatch.delenv("ODIN_HOME", raising=False)
    assert config.odin_home().name == ".odin"


# --- load_config -----------------------------------------------------------


def test_load_missing_file_is_empty(tmp_path):
    assert config.load_config(tmp_path / "nope.toml") == {}


def test_load_malformed_file_is_empty(tmp_path):
    path = _write_config(tmp_path, "this = = not toml")
    assert config.load_config(path) == {}


def test_load_directory_is_empty(tmp_path):
    assert config.load_config(tmp_path) == {}


def test_load_partial_toml(tmp_path):
    path = _write_config(tmp_path, 'default_platform = "cursor"\n')
    assert config.load_config(path) == {"default_platform": "cursor"}


def test_load_uses_config_path_when_unset(monkeypatch, tmp_path):
    _write_config(tmp_path, 'default_platform = "cursor"\n')
    monkeypatch.setenv("ODIN_HOME", str(tmp_path))
    assert config.load_config() == {"default_platform": "cursor"}


# --- resolve_platform ------------------------------------------------------


def test_platform_fallback_is_claude():
    assert config.resolve_platform(config={}) == "claude"


def test_platform_flag_wins(monkeypatch):
    monkeypatch.setenv("ODIN_PLATFORM", "cursor")
    cfg = {"default_platform": "kiro"}
    assert config.resolve_platform("Claude", config=cfg) == "claude"


def test_platform_env_beats_config(monkeypatch):
    monkeypatch.setenv("ODIN_PLATFORM", "Cursor")
    assert config.resolve_platform(config={"default_platform": "kiro"}) == "cursor"


def test_platform_from_config(tmp_path):
    cfg = {"default_platform": "cursor"}
    assert config.resolve_platform(config=cfg) == "cursor"


def test_platform_blank_flag_ignored(monkeypatch):
    monkeypatch.setenv("ODIN_PLATFORM", "cursor")
    assert config.resolve_platform("   ", config={}) == "cursor"


def test_platform_loads_config_when_not_passed(monkeypatch, tmp_path):
    _write_config(tmp_path, 'default_platform = "cursor"\n')
    monkeypatch.setenv("ODIN_HOME", str(tmp_path))
    assert config.resolve_platform() == "cursor"


# --- resolve_model ---------------------------------------------------------


def test_model_unset_by_default():
    assert config.resolve_model(config={}) is None


def test_model_flag_wins(monkeypatch):
    monkeypatch.setenv("ODIN_MODEL", "env-model")
    cfg = {"platforms": {"claude": {"model": "cfg-model"}}}
    assert config.resolve_model("flag-model", platform="claude", config=cfg) == "flag-model"


def test_model_env_beats_config(monkeypatch):
    monkeypatch.setenv("ODIN_MODEL", "env-model")
    cfg = {"platforms": {"claude": {"model": "cfg-model"}}}
    assert config.resolve_model(platform="claude", config=cfg) == "env-model"


def test_model_from_config_per_platform():
    cfg = {
        "platforms": {
            "claude": {"model": "claude-sonnet-4-6"},
            "cursor": {"model": "composer-2.5-fast"},
        }
    }
    assert config.resolve_model(platform="claude", config=cfg) == "claude-sonnet-4-6"
    assert config.resolve_model(platform="cursor", config=cfg) == "composer-2.5-fast"


def test_model_not_lowercased():
    cfg = {"platforms": {"claude": {"model": "Claude-Sonnet-4-6"}}}
    assert config.resolve_model(platform="claude", config=cfg) == "Claude-Sonnet-4-6"


def test_model_missing_platform_section_is_none():
    cfg = {"platforms": {"cursor": {"model": "x"}}}
    assert config.resolve_model(platform="claude", config=cfg) is None


def test_model_blank_values_treated_as_unset(monkeypatch):
    monkeypatch.setenv("ODIN_MODEL", "   ")
    cfg = {"platforms": {"claude": {"model": "  "}}}
    assert config.resolve_model("  ", platform="claude", config=cfg) is None


def test_model_malformed_platforms_section_is_none():
    assert config.resolve_model(platform="claude", config={"platforms": "oops"}) is None
