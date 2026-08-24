"""Один проход монитора: выдача → дедуп → фильтр → (дозагрузка) → Telegram.

Запускается по расписанию (Планировщик задач Windows / cron) — один проход за
вызов, интервал задаёт планировщик. Так проще и надёжнее долгоживущего процесса.

Запуск: python -m pricewatch
"""

from __future__ import annotations

import random
import time

from . import collector, config, notify, store
from .collector import AccessBlocked
from .filter import evaluate


def run_once() -> int:
    """Вернуть число отправленных уведомлений."""
    conn = store.connect(config.DB_PATH)

    try:
        listings = collector.fetch_listings()
    except AccessBlocked as e:
        notify.send(f"⚠ Sercher: Авито заблокировал сбор — {e}")
        print(f"[БЛОКИРОВКА] {e}")
        return 0

    print(f"Карточек в выдаче: {len(listings)}")

    # Первый запуск: только запоминаем текущее состояние, ничего не шлём и не
    # дозагружаем — иначе завалим Авито десятками заходов на страницы.
    if store.is_empty(conn):
        for listing in listings:
            store.classify(conn, listing)
        print("Базовая линия установлена (первый запуск, уведомлений нет).")
        return 0

    sent = 0

    for listing in listings:
        event = store.classify(conn, listing)
        if event == "seen":
            continue  # уже видели без снижения цены — не трогаем

        result = evaluate(listing)

        # Веса нет в заголовке/сниппете — открываем страницу объявления.
        if result.needs_page:
            time.sleep(random.uniform(2.0, 5.0))  # спокойный темп
            try:
                details = collector.fetch_details(listing.url)
            except AccessBlocked as e:
                print(f"  пропуск {listing.id}: {e}")
                continue
            listing.description = details.get("description", "")
            if details.get("region"):
                listing.region = details["region"]
            result = evaluate(listing)

        if result.matched:
            notify.send(notify.format_message(listing, result, event))
            sent += 1
            print(f"  ✅ {result.reason} — {listing.title}")

    print(f"Отправлено уведомлений: {sent}")
    return sent


if __name__ == "__main__":
    raise SystemExit(0 if run_once() >= 0 else 1)
