"""Единый вывод в лог с меткой времени — чтобы по monitor.log был таймлайн."""

from __future__ import annotations

from time import strftime


def log(msg: str) -> None:
    print(f"{strftime('%m-%d %H:%M:%S')} {msg}", flush=True)
