"""Боевой проход монитора через Avito JSON-API (curl_cffi + cookies от spfa).

Для каждого европейского региона: ссылка поиска → API-URL (spfa, кэш) → страницы
JSON с описаниями → фильтр (585 + цена за грамм) → дедуп → Telegram.
Страницы объявлений НЕ открываем — описание уже в JSON.

Запуск: python -m pricewatch
"""

from __future__ import annotations

import random
import time

from . import config, health, notify, store
from .avito_api import AvitoApi, AvitoBlocked, convert_search_url
from .filter import evaluate
from .logutil import log


def _all_regions() -> list[tuple[str, str]]:
    """Регионы кольцами (сначала ближние). Внутри кольца порядок можно мешать —
    так проходы выглядят «живее» и не бьют всегда по одному шаблону."""
    out: list[tuple[str, str]] = []
    for ring in config.REGION_RINGS:
        items = list(ring.items())
        if getattr(config, "SHUFFLE_WITHIN_RING", False):
            random.shuffle(items)
        out.extend(items)
    return out


def run_once() -> int:
    conn = store.connect(config.DB_PATH)
    first_run = store.is_empty(conn)
    deep = getattr(config, "DEEP_SWEEP", False)
    if deep:
        pages = config.MAX_PAGES_DEEP  # разовый глубокий прочёс всего рынка
        print("РЕЖИМ: глубокий прочёс — все страницы, круг за кругом с круга 0")
    else:
        pages = config.FIRST_RUN_PAGES if first_run else config.PAGES
    # Для глубокого прочёса темп быстрее (разовый); для обычных проходов — спокойный.
    page_delay = config.DEEP_PAGE_DELAY_SEC if deep else config.PAGE_DELAY_SEC
    region_delay = config.DEEP_REGION_DELAY_SEC if deep else config.REGION_DELAY_SEC
    api = AvitoApi()
    # Проактивно освежить cookies, если протухают (не ждём блокировки в проходе).
    api.refresh_cookies_if_old()

    # Небольшой случайный сдвиг старта — проходы не строго по расписанию.
    time.sleep(random.uniform(*getattr(config, "START_JITTER_SEC", (0, 0))))

    regions = _all_regions()
    log(f"Проход: регионов {len(regions)}, страниц/регион {pages}, первый запуск: {first_run}")
    sent = 0
    collected = 0        # всего собрано объявлений за проход
    nonzero_regions = 0  # сколько регионов реально отдали данные (для heartbeat)
    consecutive_blocks = 0  # блоков подряд — копим до порога, потом восстанавливаемся

    for name, slug in regions:
        search = config.SEARCH_URL_TEMPLATE.format(region=slug)
        try:
            api_url = convert_search_url(search)
        except Exception as e:
            log(f"[{name}] API-URL не получен: {e}")
            continue

        total = 0
        blocked = False
        for page in range(1, pages + 1):
            try:
                listings = api.fetch_page(api_url, page)
            except AvitoBlocked:
                blocked = True
                break
            except Exception as e:
                log(f"[{name}] стр.{page}: {type(e).__name__}: {e}")
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
                    log(f"  ✅ {result.reason} — {listing.title}")

            time.sleep(random.uniform(*page_delay))  # темп между страницами

        if blocked:
            # Блок региона. Копим подряд; тратимся на восстановление только после
            # порога — одиночный блок (рейт-лимит) не стоит денег.
            consecutive_blocks += 1
            log(f"[{name}] заблокирован (подряд: {consecutive_blocks})")
            if consecutive_blocks >= config.RECOVER_AFTER_BLOCKS:
                if api.recover():
                    consecutive_blocks = 0
            time.sleep(random.uniform(*region_delay))
            continue

        consecutive_blocks = 0
        collected += total
        if total > 0:
            nonzero_regions += 1
        log(f"[{name}] объявлений: {total}")
        time.sleep(random.uniform(*region_delay))  # темп между регионами

    conn.close()
    _update_health(collected, nonzero_regions, len(regions), api)
    log(f"Отправлено уведомлений: {sent}")
    return sent


def _update_health(collected: int, nonzero_regions: int, total_regions: int,
                   api: AvitoApi) -> None:
    """Обновить здоровье и, если сбор реально деградировал, один раз предупредить.

    «Здоров» = данные пришли хотя бы с ПОЛОВИНЫ регионов (или собрано ≥100). Иначе
    один живой регион среди сплошных нулей больше НЕ маскирует деградацию (был баг).
    """
    import time as _t

    healthy = nonzero_regions >= max(1, total_regions // 2) or collected >= 100
    state = health.load()
    now = _t.time()
    state.setdefault("last_ok", now)

    if healthy:
        if state.get("alerted"):
            notify.send_alert("✅ Sercher: сбор данных восстановлен, объявления снова идут.")
        state["last_ok"] = now
        state["alerted"] = False
    else:
        idle_h = (now - state["last_ok"]) / 3600
        if now - state["last_ok"] > config.ALERT_AFTER_SEC and not state.get("alerted"):
            reason = api.degraded_reason or "похоже на бан IP или сбой spfa"
            notify.send_alert(
                f"⚠️ Sercher: сбор данных нарушен уже ~{idle_h:.0f} ч "
                f"(рабочих регионов {nonzero_regions}/{total_regions}).\n"
                f"Причина: {reason}.\n"
                f"Проверь баланс spfa и прокси."
            )
            state["alerted"] = True

    health.save(state)


if __name__ == "__main__":
    run_once()
