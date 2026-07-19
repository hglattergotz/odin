"""Shared test fixtures."""

from __future__ import annotations

import pytest

from odin import style


@pytest.fixture(autouse=True)
def _isolate_metrics(tmp_path_factory, monkeypatch):
    """Redirect Odin's central metrics dir to a throwaway location so the test
    suite never appends to the user's real ~/.odin/metrics/events.jsonl."""
    home = tmp_path_factory.mktemp("odin-home")
    monkeypatch.setenv("ODIN_HOME", str(home))
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
