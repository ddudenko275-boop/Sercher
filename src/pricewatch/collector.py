"""Сбор объявлений с Авито через Playwright — одной сессией Chrome на проход.

Проверенная формула против антибота (см. config): настоящий Chrome
(channel="chrome"), отдельный прогретый профиль с cookies, без блокировки
ресурсов, спокойный темп.

AvitoSession открывает Chrome один раз (context manager) и в рамках одной
сессии умеет:
  fetch_listings(pages)  — распарсить N страниц выдачи в список Listing;
  fetch_details(url)     — открыть страницу объявления, вернуть описание+регион.

Запуск проверки: python -m pricewatch.collector
"""

from __future__ import annotations

import random
import re
import sys
import time

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from . import config
from .models import Listing

_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
_HOME = "https://www.avito.ru/"
_BLOCK_MARKER = "доступ ограничен"


class AccessBlocked(RuntimeError):
    """Авито отдал заглушку антибота вместо страницы."""


class AvitoSession:
    """Одна сессия браузера: прогрев + сбор выдачи + дозагрузка объявлений."""

    def __enter__(self) -> "AvitoSession":
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            config.PROFILE_DIR,
            channel=config.CHROME_CHANNEL,
            headless=config.HEADLESS,
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.ctx.add_init_script(_STEALTH)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        if config.WARMUP:
            self.page.goto(_HOME, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            self.page.wait_for_timeout(2500)
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def _check_blocked(self) -> None:
        if _BLOCK_MARKER in (self.page.title() or "").lower():
            raise AccessBlocked(f"заглушка антибота (title={self.page.title()!r})")

    def fetch_listings(self, pages: int = 1, search_url: str | None = None) -> list[Listing]:
        base = search_url or config.AVITO_SEARCH_URL
        out: list[Listing] = []
        for n in range(1, pages + 1):
            url = base if n == 1 else f"{base}&p={n}"
            self.page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            self.page.wait_for_timeout(2500)
            self._check_blocked()
            try:
                self.page.wait_for_selector('[data-marker="item"]', timeout=config.NAV_TIMEOUT_MS)
            except PWTimeout as e:
                raise AccessBlocked("карточки не появились — антибот или разметка") from e
            for card in self.page.query_selector_all('[data-marker="item"]'):
                listing = _parse_card(card)
                if listing is not None:
                    out.append(listing)
            time.sleep(random.uniform(1.5, 3.0))  # темп между страницами
        return out

    def fetch_details(self, url: str) -> dict:
        self.page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
        self.page.wait_for_timeout(2000)
        self._check_blocked()
        return {
            "description": _first_text(self.page, [
                '[data-marker="item-view/item-description"]',
                'div[itemprop="description"]',
            ]),
            "region": _first_text(self.page, [
                '[data-marker="item-view/item-address"]',
                'div[itemprop="address"]',
            ]),
        }


# --- Тонкие обёртки для разовых вызовов / демо ---

def fetch_listings(search_url: str | None = None, pages: int = 1) -> list[Listing]:
    with AvitoSession() as s:
        return s.fetch_listings(pages=pages, search_url=search_url)


# --- Парсинг ---

def _parse_card(card) -> Listing | None:
    item_id = card.get_attribute("data-item-id")
    title_el = card.query_selector('[data-marker="item-title"]')
    if not item_id or title_el is None:
        return None
    href = title_el.get_attribute("href") or ""
    url = href if href.startswith("http") else f"https://www.avito.ru{href}"
    title = (title_el.inner_text() or "").strip()
    return Listing(
        id=str(item_id),
        title=title,
        price=_parse_price(card),
        url=url,
        region=_card_region(card),
    )


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
        listings = fetch_listings(pages=config.PAGES)
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
