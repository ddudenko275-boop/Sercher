"""Состояние здоровья монитора между проходами (data/health.json).

Каждый проход — отдельный процесс, поэтому память о том, «когда последний раз
сбор был живой», «когда крутили IP» и «слали ли уже тревогу» — на диске.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config


def load() -> dict:
    p = Path(config.HEALTH_PATH)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def save(state: dict) -> None:
    p = Path(config.HEALTH_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False), "utf-8")


def get(key: str, default=None):
    return load().get(key, default)


def update(**kw) -> None:
    state = load()
    state.update(kw)
    save(state)
