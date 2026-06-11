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


# --- dotted-key get/set ----------------------------------------------------


def test_get_in_reads_nested_key():
    cfg = {"platforms": {"claude": {"model": "sonnet"}}}
    assert config.get_in(cfg, "platforms.claude.model") == "sonnet"


def test_get_in_missing_key_is_none():
    assert config.get_in({"platforms": {}}, "platforms.claude.model") is None
    assert config.get_in({}, "default_platform") is None


def test_get_in_stops_at_non_dict():
    assert config.get_in({"a": 1}, "a.b.c") is None


def test_set_in_creates_intermediate_tables():
    cfg = {}
    config.set_in(cfg, "platforms.claude.model", "sonnet")
    assert cfg == {"platforms": {"claude": {"model": "sonnet"}}}


def test_set_in_preserves_siblings():
    cfg = {"default_platform": "claude", "platforms": {"claude": {"binary": "claude"}}}
    config.set_in(cfg, "platforms.claude.model", "sonnet")
    assert cfg["default_platform"] == "claude"
    assert cfg["platforms"]["claude"] == {"binary": "claude", "model": "sonnet"}


def test_set_in_replaces_non_table_segment():
    cfg = {"platforms": "oops"}
    config.set_in(cfg, "platforms.claude.model", "x")
    assert cfg == {"platforms": {"claude": {"model": "x"}}}


# --- parse_value -----------------------------------------------------------


def test_parse_value_coercions():
    assert config.parse_value("true") is True
    assert config.parse_value("False") is False
    assert config.parse_value("42") == 42
    assert config.parse_value("2.5") == 2.5
    assert config.parse_value("sonnet") == "sonnet"
    assert config.parse_value("composer-2.5-fast") == "composer-2.5-fast"


# --- TOML writer -----------------------------------------------------------


def test_dump_toml_top_level_and_tables():
    cfg = {
        "default_platform": "claude",
        "platforms": {
            "claude": {"binary": "claude", "verbose": True, "model": "sonnet"},
            "cursor": {"model": "composer-2.5-fast"},
        },
    }
    text = config.dump_toml(cfg)
    assert text == (
        'default_platform = "claude"\n'
        "\n"
        "[platforms.claude]\n"
        'binary = "claude"\n'
        "verbose = true\n"
        'model = "sonnet"\n'
        "\n"
        "[platforms.cursor]\n"
        'model = "composer-2.5-fast"\n'
    )


def test_dump_toml_empty_is_empty_string():
    assert config.dump_toml({}) == ""


def test_dump_toml_no_header_for_pure_subtable_parent():
    # `platforms` holds only sub-tables, so it gets no `[platforms]` header.
    text = config.dump_toml({"platforms": {"claude": {"model": "x"}}})
    assert "[platforms]" not in text
    assert "[platforms.claude]" in text


def test_dump_toml_is_parseable_round_trip():
    import tomllib

    cfg = {
        "default_platform": "cursor",
        "platforms": {"claude": {"model": "claude-opus-4-8", "verbose": True}},
    }
    parsed = tomllib.loads(config.dump_toml(cfg))
    assert parsed == cfg


def test_dump_toml_escapes_strings():
    text = config.dump_toml({"k": 'a"b\\c'})
    assert text == 'k = "a\\"b\\\\c"\n'


# --- save_config (atomic write) --------------------------------------------


def test_save_config_writes_atomically(tmp_path):
    target = tmp_path / "sub" / "config.toml"
    saved = config.save_config({"default_platform": "cursor"}, target)
    assert saved == target
    assert target.read_text() == 'default_platform = "cursor"\n'
    # No temp files left behind in the dir.
    assert [p.name for p in target.parent.iterdir()] == ["config.toml"]


def test_save_then_load_round_trips(tmp_path):
    target = tmp_path / "config.toml"
    cfg = {"platforms": {"claude": {"model": "sonnet"}}}
    config.save_config(cfg, target)
    assert config.load_config(target) == cfg


def test_set_then_get_persists(tmp_path):
    target = tmp_path / "config.toml"
    cfg = config.load_config(target)  # missing -> {}
    config.set_in(cfg, "platforms.claude.model", "sonnet")
    config.save_config(cfg, target)
    reloaded = config.load_config(target)
    assert config.get_in(reloaded, "platforms.claude.model") == "sonnet"


# --- interactive ask_config ------------------------------------------------


def test_ask_config_sets_default_platform(monkeypatch):
    import io

    from odin import prompts

    # menu: [1] default platform -> pick item 2 (cursor); then [3] done.
    in_ = io.StringIO("1\n2\n3\n")
    cfg = prompts.ask_config(
        {}, platforms=["claude", "cursor"], suggestions={}, in_=in_, out=io.StringIO()
    )
    assert cfg["default_platform"] == "cursor"


def test_ask_config_sets_and_unsets_model():
    import io

    from odin import prompts

    sugg = {"claude": ["claude-opus-4-8", "claude-sonnet-4-6"]}
    # [2] set model -> platform [1] claude -> model [2] sonnet -> done.
    in_ = io.StringIO("2\n1\n2\n3\n")
    cfg = prompts.ask_config({}, platforms=["claude"], suggestions=sugg, in_=in_, out=io.StringIO())
    assert cfg["platforms"]["claude"]["model"] == "claude-sonnet-4-6"

    # Now unset it: [2] set model -> platform [1] -> option 4 (unset) -> done.
    in_ = io.StringIO("2\n1\n4\n3\n")
    cfg = prompts.ask_config(cfg, platforms=["claude"], suggestions=sugg, in_=in_, out=io.StringIO())
    assert "model" not in cfg["platforms"]["claude"]


def test_ask_config_other_free_text_model():
    import io

    from odin import prompts

    sugg = {"claude": ["claude-opus-4-8"]}
    # [2] set model -> platform [1] -> option 2 (Other) -> "my-model" -> done.
    in_ = io.StringIO("2\n1\n2\nmy-model\n3\n")
    cfg = prompts.ask_config({}, platforms=["claude"], suggestions=sugg, in_=in_, out=io.StringIO())
    assert cfg["platforms"]["claude"]["model"] == "my-model"


def test_ask_config_done_immediately_no_change():
    import io

    from odin import prompts

    cfg = prompts.ask_config(
        {"default_platform": "claude"}, platforms=["claude"], in_=io.StringIO("\n"), out=io.StringIO()
    )
    assert cfg == {"default_platform": "claude"}
