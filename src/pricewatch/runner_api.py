"""Боевой проход монитора через Avito JSON-API (curl_cffi + cookies от spfa).

Для каждого европейского региона: ссылка поиска → API-URL (spfa, кэш) → страницы
JSON с описаниями → фильтр (585 + цена за грамм) → дедуп → Telegram.
Страницы объявлений НЕ открываем — описание уже в JSON.

Запуск: python -m pricewatch
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

from . import config, health, notify, store
from .avito_api import AvitoApi, AvitoBlocked, convert_search_url, with_price
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


def _band_label(band) -> str:
    if not band:
        return ""
    lo, hi = band[0], band[1]
    return f" {lo // 1000}k-{'∞' if hi is None else str(hi // 1000) + 'k'}"


def _load_progress() -> set[str]:
    p = Path(config.SWEEP_PROGRESS_PATH)
    if p.exists():
        try:
            return set(json.loads(p.read_text("utf-8")))
        except Exception:
            return set()
    return set()


def _mark_progress(done: set[str], key: str) -> None:
    done.add(key)
    p = Path(config.SWEEP_PROGRESS_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(done), ensure_ascii=False), "utf-8")


def run_once() -> int:
    conn = store.connect(config.DB_PATH)
    first_run = store.is_empty(conn)
    deep = getattr(config, "DEEP_SWEEP", False)
    pages = config.MAX_PAGES_DEEP if deep else (
        config.FIRST_RUN_PAGES if first_run else config.PAGES)
    # Глубокий прочёс: быстрее темп + ценовые полосы (обход лимита ~5000/запрос).
    page_delay = config.DEEP_PAGE_DELAY_SEC if deep else config.PAGE_DELAY_SEC
    region_delay = config.DEEP_REGION_DELAY_SEC if deep else config.REGION_DELAY_SEC
    bands = config.PRICE_BANDS if deep else [None]

    api = AvitoApi()
    api.refresh_cookies_if_old()  # не ждём блокировки протухших cookies в проходе
    time.sleep(random.uniform(*getattr(config, "START_JITTER_SEC", (0, 0))))

    regions = _all_regions()
    # Фокус на круге 0 (PRICEWATCH_RING0=1) — эффективный первый глубокий анализ.
    if deep and os.getenv("PRICEWATCH_RING0"):
        regions = list(config.REGION_RINGS[0].items())
    done = _load_progress() if deep else set()
    if deep:
        log(f"РЕЖИМ: глубокий прочёс с ценовыми полосами — {len(regions)} регионов "
            f"× {len(bands)} полос, до {pages} стр/полоса (уже сделано: {len(done)})")
    else:
        log(f"Проход: регионов {len(regions)}, страниц/регион {pages}, "
            f"первый запуск: {first_run}")

    sent = collected = nonzero_regions = 0
    consecutive_blocks = 0  # блоков подряд — копим до порога, потом восстанавливаемся

    for name, slug in regions:
        try:
            base_url = convert_search_url(config.SEARCH_URL_TEMPLATE.format(region=slug))
        except Exception as e:
            log(f"[{name}] API-URL не получен: {e}")
            continue

        region_hit = False
        for band in bands:
            band_pages = band[2] if band else pages  # своя глубина у каждой полосы
            key = f"{slug}|{band[0]}|{band[1]}" if band else slug
            if deep and key in done:
                continue
            api_url = with_price(base_url, band[0], band[1]) if band else base_url
            label = f"{name}{_band_label(band)}"

            total, blocked, unit_sent = _scan_unit(api, api_url, band_pages, page_delay, conn, label)
            sent += unit_sent

            if blocked:
                consecutive_blocks += 1
                log(f"[{label}] заблокирован (подряд: {consecutive_blocks})")
                if consecutive_blocks >= config.RECOVER_AFTER_BLOCKS and api.recover():
                    consecutive_blocks = 0
                time.sleep(random.uniform(*region_delay))
                continue

            consecutive_blocks = 0
            collected += total
            region_hit = region_hit or total > 0
            if deep:
                _mark_progress(done, key)
            log(f"[{label}] объявлений: {total}")
            time.sleep(random.uniform(*region_delay))

        if region_hit:
            nonzero_regions += 1

    conn.close()
    _update_health(collected, nonzero_regions, len(regions), api)
    log(f"Отправлено уведомлений: {sent}")
    return sent


def _scan_unit(api: AvitoApi, api_url: str, pages: int, page_delay, conn,
               label: str) -> tuple[int, bool, int]:
    """Пролистать один юнит (регион или регион×полоса). → (собрано, заблокирован?, отправлено)."""
    total = 0
    unit_sent = 0
    for page in range(1, pages + 1):
        try:
            listings = api.fetch_page(api_url, page)
        except AvitoBlocked:
            return total, True, unit_sent
        except Exception as e:
            log(f"[{label}] стр.{page}: {type(e).__name__}: {e}")
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
                unit_sent += 1
                log(f"  ✅ {result.reason} — {listing.title}")

        time.sleep(random.uniform(*page_delay))  # темп между страницами
    return total, False, unit_sent


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
