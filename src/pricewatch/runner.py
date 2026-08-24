"""Один проход монитора по кольцам близости: выдача → дедуп → фильтр → Telegram.

Каждый проход проверяет ближнее кольцо (Ростов + Москва) всегда, а дальние
регионы — по очереди между проходами (ротация в meta БД). Первый прогон идёт
шире (кольца 0 и 1) и уже уведомляет о текущих подходящих предложениях.
Открытий страниц объявлений за проход — не больше MAX_DETAIL_FETCHES.

Запуск: python -m pricewatch
"""

from __future__ import annotations

import random
import time

from . import collector, config, notify, store
from .collector import AccessBlocked
from .filter import evaluate


def _flatten_farther() -> list[tuple[str, str]]:
    """Дальние кольца (1..) одним списком [(имя, слуг), ...] для ротации."""
    return [item for ring in config.REGION_RINGS[1:] for item in ring.items()]


def _regions_this_run(conn, first_run: bool) -> list[tuple[str, str]]:
    ring0 = list(config.REGION_RINGS[0].items())
    if first_run:
        # Ближнее кольцо + первое дальнее целиком — быстрее охватить рынок.
        ring1 = list(config.REGION_RINGS[1].items())
        store.set_meta(conn, "ring_idx", str(len(config.REGION_RINGS[1])))
        return ring0 + ring1

    farther = _flatten_farther()
    idx = int(store.get_meta(conn, "ring_idx", "0") or 0)
    k = min(config.FARTHER_PER_RUN, len(farther))
    window = [farther[(idx + i) % len(farther)] for i in range(k)]
    store.set_meta(conn, "ring_idx", str((idx + k) % len(farther)))
    return ring0 + window


def _fetch_region(sess, slug: str, pages: int):
    """Выдача одного региона с повтором при блоке (первый заход часто флаки)."""
    url = config.SEARCH_URL_TEMPLATE.format(region=slug)
    for attempt in range(2):
        try:
            return sess.fetch_listings(search_url=url, pages=pages)
        except AccessBlocked:
            if attempt == 0:
                time.sleep(random.uniform(3.0, 6.0))
                continue
            raise


def run_once() -> int:
    conn = store.connect(config.DB_PATH)
    first_run = store.is_empty(conn)
    pages = config.FIRST_RUN_PAGES if first_run else config.PAGES
    regions = _regions_this_run(conn, first_run)

    sent = 0
    fetches = 0
    blocked = 0

    try:
        with collector.AvitoSession() as sess:
            for region_name, slug in regions:
                try:
                    listings = _fetch_region(sess, slug, pages)
                except AccessBlocked as e:
                    blocked += 1
                    print(f"[{region_name}] блок: {e}")
                    continue
                except Exception as e:  # проблема одного региона не валит проход
                    print(f"[{region_name}] ошибка: {type(e).__name__}: {e}")
                    continue

                print(f"[{region_name}] карточек: {len(listings)}")
                for listing in listings:
                    listing.region = region_name  # регион знаем из поиска

                    seen, prev_price = store.get_prev_price(conn, listing.id)
                    if seen:
                        if (listing.price is None or prev_price is None
                                or listing.price >= prev_price):
                            continue  # виденное без снижения цены
                        event = "price_drop"
                    else:
                        event = "new"

                    result = evaluate(listing)

                    # Веса/пробы нет в карточке — открываем страницу (в пределах лимита).
                    if result.needs_page:
                        if fetches >= config.MAX_DETAIL_FETCHES:
                            continue  # не записываем — добёрём на следующем проходе
                        fetches += 1
                        time.sleep(random.uniform(2.0, 5.0))
                        try:
                            details = sess.fetch_details(listing.url)
                        except Exception as e:  # одна страница не валит проход
                            print(f"  пропуск {listing.id}: {type(e).__name__}: {e}")
                            continue
                        listing.description = details.get("description", "")
                        listing.detailed = True
                        result = evaluate(listing)

                    store.record(conn, listing)

                    if result.matched:
                        notify.send(notify.format_message(listing, result, event))
                        sent += 1
                        print(f"  ✅ [{region_name}] {result.reason} — {listing.title}")

                time.sleep(random.uniform(1.5, 3.0))  # пауза между регионами
    finally:
        conn.close()

    if blocked and blocked == len(regions):
        notify.send("⚠ Sercher: Авито блокирует сбор во всех регионах — возможно, "
                    "протухла сессия. Перезапусти: python -m pricewatch.login")

    print(f"Регионов: {len(regions)} | отправлено уведомлений: {sent}")
    return sent


if __name__ == "__main__":
    run_once()
