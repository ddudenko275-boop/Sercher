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


def _load_progress() -> dict:
    """{ключ_полосы: "done" | номер_последней_пройденной_страницы} — постраничный чекпоинт."""
    p = Path(config.SWEEP_PROGRESS_PATH)
    if p.exists():
        try:
            return dict(json.loads(p.read_text("utf-8")))
        except Exception:
            return {}
    return {}


def _save_progress(progress: dict) -> None:
    p = Path(config.SWEEP_PROGRESS_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(progress, ensure_ascii=False), "utf-8")


def _recover_blocking(api: AvitoApi, label: str) -> bool:
    """Восстановиться, НЕ бросая полосу: ждём окно ротации (кулдаун) и меняем IP.
    False только если восстановление реально невозможно (баланс/предохранитель)."""
    for _ in range(15):  # до ~16 мин ожидания окна ротации
        if api.recover():
            return True
        r = api.degraded_reason or ""
        if "баланс" in r or "ПРЕДОХРАНИТЕЛЬ" in r:
            log(f"[{label}] восстановление невозможно: {r}")
            return False
        time.sleep(65)  # кулдаун ротации ещё не вышел — подождём и повторим
    return False


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
    # Ограничить прочёс первыми N кругами: PRICEWATCH_RINGS=2 → круги 0 и 1.
    # (PRICEWATCH_RING0=1 — устаревший синоним «только круг 0».)
    rings_env = os.getenv("PRICEWATCH_RINGS")
    if deep and rings_env:
        n = max(1, int(rings_env))
        regions = [it for ring in config.REGION_RINGS[:n] for it in ring.items()]
    elif deep and os.getenv("PRICEWATCH_RING0"):
        regions = list(config.REGION_RINGS[0].items())
    progress = _load_progress() if deep else {}
    if deep:
        log(f"РЕЖИМ: глубокий прочёс с ценовыми полосами — {len(regions)} регионов "
            f"× {len(bands)} полос, до {pages} стр/полоса (полос готово: "
            f"{sum(1 for v in progress.values() if v == 'done')})")
    else:
        log(f"Проход: регионов {len(regions)}, страниц/регион {pages}, "
            f"первый запуск: {first_run}")

    sent = collected = nonzero_regions = 0
    stopped = False  # восстановление стало невозможным (баланс/предохранитель) — прерываем

    for name, slug in regions:
        if stopped:
            break
        try:
            base_url = convert_search_url(config.SEARCH_URL_TEMPLATE.format(region=slug))
        except Exception as e:
            log(f"[{name}] API-URL не получен: {e}")
            continue

        region_hit = False
        for band in bands:
            band_pages = band[2] if band else pages  # своя глубина у каждой полосы
            key = f"{slug}|{band[0]}|{band[1]}" if band else slug
            st = progress.get(key) if deep else None
            if st == "done":
                continue
            start_page = int(st) + 1 if isinstance(st, int) else 1
            api_url = with_price(base_url, band[0], band[1]) if band else base_url
            label = f"{name}{_band_label(band)}"

            def mark_page(pg: int, _k=key) -> None:
                if deep:
                    progress[_k] = pg
                    _save_progress(progress)

            total, unit_sent, completed = _scan_unit(
                api, api_url, band_pages, page_delay, conn, label, start_page, mark_page)
            sent += unit_sent
            collected += total
            region_hit = region_hit or total > 0

            if completed:
                if deep:
                    progress[key] = "done"
                    _save_progress(progress)
                log(f"[{label}] объявлений: {total} (полоса пройдена)")
                time.sleep(random.uniform(*region_delay))
            else:
                # дожать полосу не удалось (баланс/предохранитель) — позиция сохранена,
                # доберём при следующем запуске. Дальше в этом проходе тоже не сможем.
                log(f"[{label}] прервано на стр.{progress.get(key)} — доберу позже")
                stopped = True
                break

        if region_hit:
            nonzero_regions += 1

    conn.close()
    _update_health(collected, nonzero_regions, len(regions), api)
    log(f"Отправлено уведомлений: {sent}")
    return sent


def _scan_unit(api: AvitoApi, api_url: str, max_pages: int, page_delay, conn,
               label: str, start_page: int, mark_page) -> tuple[int, int, bool]:
    """Полностью пролистать полосу [start_page..max_pages], ДОЖИМАЯ через баны:
    бан на странице N → смена IP → продолжаем ТУ ЖЕ страницу (не бросаем полосу и
    не начинаем сначала). → (собрано, отправлено, полоса_завершена?)."""
    total = 0
    unit_sent = 0
    page = start_page
    while page <= max_pages:
        try:
            listings = api.fetch_page(api_url, page)
        except AvitoBlocked:
            if _recover_blocking(api, label):
                continue  # свежий IP → повторяем ТУ ЖЕ страницу, полосу не теряем
            return total, unit_sent, False  # восстановиться нельзя → полоса НЕ завершена
        except Exception as e:
            log(f"[{label}] стр.{page}: {type(e).__name__}: {e}")
            break
        if not listings:
            break  # полоса исчерпана (пустая страница)
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

        mark_page(page)  # постраничный чекпоинт — рестарт продолжит с этой точки
        page += 1
        time.sleep(random.uniform(*page_delay))  # темп между страницами
    return total, unit_sent, True  # полоса завершена/исчерпана


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
