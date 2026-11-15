"""The arXiv gate: one request at a time, at least PACE_S seconds apart, and a retry after HTTP 429."""

import itertools
import time
import types

from llm_engine.sources import arxiv_search as ax


def test_the_gate_paces_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "GATE", str(tmp_path / "gate"))
    monkeypatch.setattr(ax, "PACE_S", 0.3)
    monkeypatch.setattr(ax, "RETRY_S", (0.05,))
    codes, stamps = [429, 200, 200], []

    def get(url, params, timeout):
        stamps.append(time.time())
        return types.SimpleNamespace(status_code=codes.pop(0), headers={}, text="")

    monkeypatch.setattr(ax.requests, "get", get)
    assert ax._paced_get({}, 5).status_code == 200  # the 429, then its retry
    assert ax._paced_get({}, 5).status_code == 200
    assert len(stamps) == 3 and all(b - a >= 0.29 for a, b in itertools.pairwise(stamps))
