"""Один проход монитора: выдача → дедуп → фильтр → (дозагрузка) → Telegram.

Запускается по расписанию (Планировщик задач Windows) — один проход за вызов.
Первый прогон идёт глубже и уже уведомляет о подходящих текущих предложениях.
Открытий страниц объявлений за проход не больше MAX_DETAIL_FETCHES — что не
успели, добираем на следующих проходах (без долбёжки Авито).

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
    first_run = store.is_empty(conn)
    pages = config.FIRST_RUN_PAGES if first_run else config.PAGES

    sent = 0
    fetches = 0

    try:
        with collector.AvitoSession() as sess:
            try:
                listings = sess.fetch_listings(pages=pages)
            except AccessBlocked as e:
                notify.send(f"⚠ Sercher: Авито заблокировал сбор — {e}")
                print(f"[БЛОКИРОВКА] {e}")
                return 0

            print(f"Карточек в выдаче: {len(listings)} (страниц: {pages}, "
                  f"первый прогон: {first_run})")

            for listing in listings:
                seen, prev_price = store.get_prev_price(conn, listing.id)
                if seen:
                    # Виденное: интересует только снижение цены.
                    if (listing.price is None or prev_price is None
                            or listing.price >= prev_price):
                        continue
                    event = "price_drop"
                else:
                    event = "new"

                result = evaluate(listing)

                # Веса нет в тексте карточки — открываем страницу (в пределах лимита).
                if result.needs_page:
                    if fetches >= config.MAX_DETAIL_FETCHES:
                        continue  # не записываем — добёрём на следующем проходе
                    fetches += 1
                    time.sleep(random.uniform(2.0, 5.0))
                    try:
                        details = sess.fetch_details(listing.url)
                    except AccessBlocked as e:
                        print(f"  пропуск {listing.id}: {e}")
                        continue  # тоже не записываем — повторим позже
                    except Exception as e:  # проблема одной страницы не валит прогон
                        print(f"  пропуск {listing.id}: {type(e).__name__}: {e}")
                        continue
                    listing.description = details.get("description", "")
                    if details.get("region"):
                        listing.region = details["region"]
                    listing.detailed = True
                    result = evaluate(listing)

                store.record(conn, listing)

                if result.matched:
                    notify.send(notify.format_message(listing, result, event))
                    sent += 1
                    print(f"  ✅ {result.reason} — {listing.title}")
    finally:
        conn.close()

    print(f"Открыто страниц объявлений: {fetches} | отправлено уведомлений: {sent}")
    return sent


if __name__ == "__main__":
    run_once()
