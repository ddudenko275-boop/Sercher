"""Боевой проход монитора через Avito JSON-API (curl_cffi + cookies от spfa).

Для каждого европейского региона: ссылка поиска → API-URL (spfa, кэш) → страницы
JSON с описаниями → фильтр (585 + цена за грамм) → дедуп → Telegram.
Страницы объявлений НЕ открываем — описание уже в JSON.

Запуск: python -m pricewatch
"""

from __future__ import annotations

import random
import time

from . import config, notify, store
from .avito_api import AvitoApi, convert_search_url
from .filter import evaluate


def _all_regions() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for ring in config.REGION_RINGS:
        for name, slug in ring.items():
            out.append((name, slug))
    return out


def run_once() -> int:
    conn = store.connect(config.DB_PATH)
    first_run = store.is_empty(conn)
    pages = config.FIRST_RUN_PAGES if first_run else config.PAGES
    api = AvitoApi()

    regions = _all_regions()
    print(f"Проход: регионов {len(regions)}, страниц/регион {pages}, первый запуск: {first_run}")
    sent = 0

    for name, slug in regions:
        search = config.SEARCH_URL_TEMPLATE.format(region=slug)
        try:
            api_url = convert_search_url(search)
        except Exception as e:
            print(f"[{name}] API-URL не получен: {e}")
            continue

        total = 0
        for page in range(1, pages + 1):
            try:
                listings = api.fetch_page(api_url, page)
            except Exception as e:
                print(f"[{name}] стр.{page}: {type(e).__name__}: {e}")
                break
            if not listings:
                break
            total += len(listings)

            for listing in listings:
                seen, prev_price = store.get_prev_price(conn, listing.id)
                if seen:
                    if (listing.price is None or prev_price is None
                            or listing.price >= prev_price):
                        continue
                    event = "price_drop"
                else:
                    event = "new"

                result = evaluate(listing)
                store.record(conn, listing)

                if result.matched:
                    notify.send(notify.format_message(listing, result, event))
                    sent += 1
                    print(f"  ✅ {result.reason} — {listing.title}")

            time.sleep(random.uniform(0.5, 1.5))  # спокойный темп между страницами

        print(f"[{name}] объявлений: {total}")
        time.sleep(random.uniform(1.0, 2.5))  # темп между регионами

    conn.close()
    print(f"Отправлено уведомлений: {sent}")
    return sent


if __name__ == "__main__":
    run_once()
