"""Shared test fixtures."""

from __future__ import annotations

import pytest

from odin import style


@pytest.fixture(autouse=True)
def _isolate_odin_home(tmp_path_factory, monkeypatch):
    """Point $ODIN_HOME at a throwaway dir, and drop any ambient $ODIN_CONFIG.

    Two things live under it and both must be isolated: the central metrics log
    (the suite must never append to the user's real events.jsonl) and
    config.toml — recovery reads `[recovery]` defaults from it, so a developer's
    own `wait_for_reset = true` would otherwise change what the tests exercise.
    Tests that want a config write one and set $ODIN_CONFIG themselves.
    """
    home = tmp_path_factory.mktemp("odin-home")
    monkeypatch.setenv("ODIN_HOME", str(home))
    monkeypatch.delenv("ODIN_CONFIG", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_color(monkeypatch):
    """Each test starts with a clean color gate.

    Ambient ``NO_COLOR`` / ``ODIN_NO_COLOR`` (common in CI and agent shells)
    would otherwise make TTY-color assertions fail even when the sink claims
    ``isatty()``. Tests that intentionally disable color re-set the env via
    monkeypatch after this fixture runs.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("ODIN_NO_COLOR", raising=False)
    style.set_no_color(False)
    yield
    style.set_no_color(False)
