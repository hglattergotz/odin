"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_metrics(tmp_path_factory, monkeypatch):
    """Redirect Odin's central metrics dir to a throwaway location so the test
    suite never appends to the user's real ~/.odin/metrics/events.jsonl."""
    home = tmp_path_factory.mktemp("odin-home")
    monkeypatch.setenv("ODIN_HOME", str(home))
    yield
