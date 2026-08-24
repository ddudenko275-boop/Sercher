"""Дедупликация и история цен в SQLite.

Чтобы не слать одно и то же дважды и уметь ловить снижение цены у уже виденных
объявлений. Таблица seen хранит id, текущую/первую цену и метки времени.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .models import Listing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    id          TEXT PRIMARY KEY,
    first_price INTEGER,
    last_price  INTEGER,
    first_seen  REAL,
    last_seen   REAL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def is_empty(conn: sqlite3.Connection) -> bool:
    """База пуста? (первый запуск — нужна базовая линия, без уведомлений)."""
    return conn.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is None


def classify(conn: sqlite3.Connection, listing: Listing) -> str:
    """Вернуть тип события и обновить запись.

    "new"        — объявление раньше не видели;
    "price_drop" — виденное, но цена стала ниже прежней;
    "seen"       — виденное без снижения цены (уведомлять не нужно).
    """
    now = time.time()
    row = conn.execute(
        "SELECT last_price FROM seen WHERE id = ?", (listing.id,)
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO seen (id, first_price, last_price, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (listing.id, listing.price, listing.price, now, now),
        )
        conn.commit()
        return "new"

    prev_price = row[0]
    event = "seen"
    if (
        listing.price is not None
        and prev_price is not None
        and listing.price < prev_price
    ):
        event = "price_drop"

    conn.execute(
        "UPDATE seen SET last_price = ?, last_seen = ? WHERE id = ?",
        (listing.price, now, listing.id),
    )
    conn.commit()
    return event
