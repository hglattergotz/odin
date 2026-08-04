"""`odin platforms` — the discoverability surface for platform/model values.

The point of these tests is drift: every fact in the report must come from the
registry, the backend, or the same config resolution `odin run` uses, so that
registering a backend updates the output (and `--platform`'s choices) with no
second list to maintain.
"""

from __future__ import annotations

import shutil

import pytest

from odin import config, platforms
from odin.backends.registry import available_platforms, get_backend
from odin.cli import _build_parser, main


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Drop ambient platform/model env — both feed the 'active' resolution."""
    monkeypatch.delenv("ODIN_PLATFORM", raising=False)
    monkeypatch.delenv("ODIN_MODEL", raising=False)


# ----------------------------------------------------------------------
# the report content
# ----------------------------------------------------------------------

def test_render_lists_every_registered_platform_by_key_and_product():
    out = platforms.render(cfg={})
    for name in available_platforms():
        assert name in out
        product = get_backend(name).product
        assert product and product in out, f"{name} product name missing"


def test_render_shows_each_platform_binary():
    out = platforms.render(cfg={})
    for name in available_platforms():
        assert get_backend(name).default_binary() in out


def test_binary_marked_found_or_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: None)
    assert "not on PATH" in platforms.render(cfg={})
    monkeypatch.setattr(shutil, "which", lambda b: f"/fake/bin/{b}")
    found = platforms.render(cfg={})
    assert "not on PATH" not in found
    assert "/fake/bin/claude" in found


def test_configured_binary_overrides_the_default(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: None)
    out = platforms.render(cfg={"platforms": {"claude": {"binary": "/opt/claude"}}})
    assert "/opt/claude" in out


def test_model_and_its_source_are_shown_from_config():
    out = platforms.render(cfg={"platforms": {"claude": {"model": "claude-opus-5"}}})
    assert "claude-opus-5" in out
    assert "platforms.claude.model" in out


def test_env_model_wins_and_is_named_as_the_source(monkeypatch):
    monkeypatch.setenv("ODIN_MODEL", "sonnet-4.6")
    out = platforms.render(cfg={"platforms": {"claude": {"model": "from-config"}}})
    assert "sonnet-4.6" in out
    assert "$ODIN_MODEL" in out
    assert "from-config" not in out


def test_unset_model_says_so_rather_than_guessing():
    assert "unset" in platforms.render(cfg={})


def test_active_platform_from_config_is_marked():
    out = platforms.render(cfg={"default_platform": "cursor"})
    assert "← active" in out
    assert "default_platform in config" in out


def test_active_platform_from_env_is_marked(monkeypatch):
    monkeypatch.setenv("ODIN_PLATFORM", "grok")
    out = platforms.render(cfg={})
    assert "← active" in out
    assert "$ODIN_PLATFORM" in out


def test_no_default_platform_says_how_to_set_one():
    out = platforms.render(cfg={})
    assert "no default platform set" in out
    assert "odin config set default_platform" in out
    assert "← active" not in out


def test_unknown_configured_platform_is_flagged_not_silently_shown():
    out = platforms.render(cfg={"default_platform": "claud"})
    assert "not a known platform" in out


def test_model_forms_come_from_the_backend():
    out = platforms.render(cfg={})
    help_line = get_backend("claude").model_help()
    # The help text is wrapped in the report, so compare on a distinctive token.
    assert help_line
    assert "claude-opus-5" in out


def test_config_keys_are_listed_per_platform():
    out = platforms.render(cfg={})
    for name in available_platforms():
        for key in get_backend(name).config_keys():
            assert key in out, f"missing config key {key}"


def test_cursor_only_flags_are_attributed_to_cursor():
    out = platforms.render(cfg={})
    assert "--approve-mcps" in out
    # …and not split across a line break, which would read as a different flag.
    assert "--approve-\nmcps" not in out


def test_instruction_files_marked_present_or_absent(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# rules\n")
    out = platforms.render(cfg={}, project=tmp_path)
    assert "CLAUDE.md ✓" in out
    assert "AGENTS.md ✗" in out


def test_flag_value_table_is_generated_from_the_registry():
    """The footer's `--platform {…}` set must not be a hand-typed list."""
    rows = dict(platforms._flag_values())
    assert "{" + ", ".join(available_platforms()) + "}" in rows["--platform"]
    assert "enabled" in rows["--sandbox"] and "disabled" in rows["--sandbox"]


def test_model_is_documented_as_free_text_not_an_allowlist():
    rows = dict(platforms._flag_values())
    assert "free text" in rows["--model"]


def test_render_never_emits_color_for_a_non_tty():
    assert "\033" not in platforms.render(cfg={}, out=None)


# ----------------------------------------------------------------------
# the flags this makes discoverable
# ----------------------------------------------------------------------

def test_platform_flag_choices_track_the_registry():
    parser = _build_parser()
    args = parser.parse_args(["run", "--platform", "cursor"])
    assert args.platform == "cursor"
    for name in available_platforms():
        assert parser.parse_args(["run", "--platform", name]).platform == name


def test_platform_typo_is_rejected_at_parse_time_with_the_valid_set(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--platform", "claud"])
    err = capsys.readouterr().err
    assert "invalid choice" in err
    for name in available_platforms():
        assert name in err


def test_platform_flag_stays_case_insensitive():
    """`get_backend` lowercases, so the flag must too — `choices` is exact."""
    parser = _build_parser()
    assert parser.parse_args(["run", "--platform", "CLAUDE"]).platform == "claude"
    assert parser.parse_args(["recover", "--platform", " Cursor "]).platform == "cursor"


def test_sandbox_choices_are_enforced():
    parser = _build_parser()
    assert parser.parse_args(["run", "--sandbox", "enabled"]).sandbox == "enabled"
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--sandbox", "yes"])


def test_missing_platform_error_names_the_registered_platforms():
    with pytest.raises(config.PlatformRequiredError) as excinfo:
        config.resolve_platform(None, config={})
    msg = str(excinfo.value)
    for name in available_platforms():
        assert name in msg
    assert "odin platforms" in msg


# ----------------------------------------------------------------------
# the command
# ----------------------------------------------------------------------

def test_cmd_platforms_prints_the_report(tmp_path, capsys):
    assert main(["platforms", "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "odin platforms" in out
    for name in available_platforms():
        assert name in out


def test_cmd_platforms_no_color_is_plain(tmp_path, capsys):
    assert main(["platforms", "--project", str(tmp_path), "--no-color"]) == 0
    assert "\033" not in capsys.readouterr().out
