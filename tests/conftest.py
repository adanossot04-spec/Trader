from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.settings import DEFAULTS, Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cfg = copy.deepcopy(DEFAULTS)
    cfg["engine"]["mode"] = "mock"
    cfg["data"]["sources"] = ["synthetic"]      # тести не ходять у мережу
    cfg["llm"]["provider"] = "none"
    cfg["notify"]["telegram"] = False
    cfg["schedule"]["enabled"] = False
    cfg["watchlist"] = ["NVDA", "AAPL"]
    # Глибина (SEC/Nasdaq/Yahoo) у тестах вимкнена: юніт-тести мають бути офлайнові
    # й швидкі. Логіку самого шару перевіряє tests/test_depth.py з підставними даними.
    cfg["depth"] = {**(cfg.get("depth") or {}), "enabled": False, "live_quote": False}
    return Settings(
        config=cfg,
        base_dir=tmp_path,
        data_dir=tmp_path,
        db_path=tmp_path / "assistant.db",
        reports_dir=tmp_path / "reports",
        outbox_path=tmp_path / "outbox.log",
        cache_dir=tmp_path / "cache",
        env={},
    )


@pytest.fixture
def book_dict() -> dict:
    return {
        "currency": "USD",
        "cash": 10000.0,
        "prev_close_equity": 10000.0,
        "positions": [],
    }
