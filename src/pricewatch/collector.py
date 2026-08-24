"""Сборщик объявлений с Авито через Playwright.

Проверенная рабочая формула против антибота (см. config):
  • реальный Chrome (channel="chrome"), не встроенный Chromium;
  • отдельный «прогретый» профиль с cookies;
  • без блокировки ресурсов (иначе ломается JS-челлендж);
  • прогрев через главную и спокойный темп.

Две задачи:
  fetch_listings()  — распарсить страницу ВЫДАЧИ в список Listing (id/title/price/url);
  fetch_details()   — открыть страницу ОБЪЯВЛЕНИЯ и достать описание+регион
                      (нужно, когда веса нет в заголовке).

Запуск проверки: python -m pricewatch.collector
"""

from __future__ import annotations

import re
import sys

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from . import config
from .models import Listing

_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
_HOME = "https://www.avito.ru/"
_BLOCK_MARKER = "доступ ограничен"


class AccessBlocked(RuntimeError):
    """Авито отдал заглушку антибота вместо страницы."""


def _launch(p):
    ctx = p.chromium.launch_persistent_context(
        config.PROFILE_DIR,
        channel=config.CHROME_CHANNEL,
        headless=config.HEADLESS,
        locale="ru-RU",
        viewport={"width": 1366, "height": 900},
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx.add_init_script(_STEALTH)
    return ctx


def _check_blocked(page) -> None:
    if _BLOCK_MARKER in (page.title() or "").lower():
        raise AccessBlocked(f"заглушка антибота (title={page.title()!r})")


def fetch_listings(search_url: str | None = None) -> list[Listing]:
    """Распарсить страницу выдачи в список Listing (id/title/price/url)."""
    url = search_url or config.AVITO_SEARCH_URL
    listings: list[Listing] = []

    with sync_playwright() as p:
        ctx = _launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if config.WARMUP:
                page.goto(_HOME, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
                page.wait_for_timeout(2500)
            page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            page.wait_for_timeout(2500)
            _check_blocked(page)
            page.wait_for_selector('[data-marker="item"]', timeout=config.NAV_TIMEOUT_MS)
            for card in page.query_selector_all('[data-marker="item"]'):
                listing = _parse_card(card)
                if listing is not None:
                    listings.append(listing)
        except PWTimeout as e:
            raise AccessBlocked("карточки не появились — антибот или сменилась разметка") from e
        finally:
            ctx.close()

    return listings


def fetch_details(url: str) -> dict:
    """Открыть страницу объявления, вернуть {'description', 'region'}."""
    with sync_playwright() as p:
        ctx = _launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            page.wait_for_timeout(2500)
            _check_blocked(page)
            desc = _first_text(page, [
                '[data-marker="item-view/item-description"]',
                'div[itemprop="description"]',
            ])
            region = _first_text(page, [
                '[data-marker="item-view/item-address"]',
                'div[itemprop="address"]',
            ])
            return {"description": desc, "region": region}
        finally:
            ctx.close()


def _parse_card(card) -> Listing | None:
    item_id = card.get_attribute("data-item-id")
    title_el = card.query_selector('[data-marker="item-title"]')
    if not item_id or title_el is None:
        return None
    href = title_el.get_attribute("href") or ""
    url = href if href.startswith("http") else f"https://www.avito.ru{href}"
    title = (title_el.inner_text() or "").strip()
    price = _parse_price(card)
    region = _card_region(card)
    return Listing(id=str(item_id), title=title, price=price, url=url, region=region)


def _parse_price(card) -> int | None:
    el = card.query_selector('[data-marker="item-price"]')
    if el:
        digits = re.sub(r"\D", "", el.inner_text() or "")
        if digits:
            return int(digits)
    meta = card.query_selector('meta[itemprop="price"]')
    if meta:
        content = meta.get_attribute("content")
        if content and content.isdigit():
            return int(content)
    return None


def _card_region(card) -> str:
    el = card.query_selector('[data-marker="item-address"]') or card.query_selector(
        'div[class*="geo"]'
    )
    return (el.inner_text() or "").strip() if el else ""


def _first_text(page, selectors) -> str:
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            txt = (el.inner_text() or "").strip()
            if txt:
                return txt
    return ""


def _main() -> int:
    try:
        listings = fetch_listings()
    except AccessBlocked as e:
        print(f"[БЛОКИРОВКА] {e}", file=sys.stderr)
        return 1
    print(f"Получено карточек: {len(listings)}\n")
    for x in listings[:15]:
        print(x)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
