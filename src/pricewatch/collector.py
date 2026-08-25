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

from playwright.sync_api import Error as PWError
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
        launch_args = ["--disable-blink-features=AutomationControlled"]
        # headed, но окно за пределами экрана — реальный браузер без помех.
        if not config.HEADLESS and getattr(config, "OFFSCREEN", False):
            launch_args += ["--window-position=-32000,-32000", "--window-size=1366,900"]
        self.ctx = self._pw.chromium.launch_persistent_context(
            config.PROFILE_DIR,
            channel=config.CHROME_CHANNEL,
            headless=config.HEADLESS,
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
            ignore_default_args=["--enable-automation"],
            args=launch_args,
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

    def _open(self, url: str, need_selector: str) -> None:
        """Открыть url с ретраем на «мигающий» блок.

        Антибот иногда отдаёт заглушку/пустую страницу даже живому браузеру —
        в браузере это лечится перезагрузкой. Повторяем до BLOCK_RETRIES раз.
        """
        for attempt in range(config.BLOCK_RETRIES + 1):
            blocked = False
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
                self.page.wait_for_timeout(2500)
                if _BLOCK_MARKER in (self.page.title() or "").lower():
                    blocked = True
                else:
                    try:
                        self.page.wait_for_selector(need_selector, timeout=config.NAV_TIMEOUT_MS)
                        return  # успех
                    except PWTimeout:
                        blocked = True  # нужного элемента нет — мягкий блок
            except PWError:
                blocked = True  # навигацию сорвало (редирект/разрушенный контекст)

            if blocked and attempt < config.BLOCK_RETRIES:
                time.sleep(random.uniform(3.0, 6.0))  # пауза перед перезагрузкой

        raise AccessBlocked(
            f"заблокировано после {config.BLOCK_RETRIES + 1} попыток (title={self.page.title()!r})"
        )

    def fetch_listings(self, pages: int = 1, search_url: str | None = None) -> list[Listing]:
        base = search_url or config.SEARCH_URL_TEMPLATE.format(region="moskva")
        out: list[Listing] = []
        for n in range(1, pages + 1):
            url = base if n == 1 else f"{base}&p={n}"
            self._open(url, '[data-marker="item"]')
            for card in self.page.query_selector_all('[data-marker="item"]'):
                listing = _parse_card(card)
                if listing is not None:
                    out.append(listing)
            time.sleep(random.uniform(1.5, 3.0))  # темп между страницами
        return out

    def fetch_details(self, url: str) -> dict:
        # Убираем трекинг-параметры (?context=...): из-за них страница делает
        # клиентский редирект. _open переживает это и ретраит на блок.
        clean_url = url.split("?", 1)[0]
        self._open(clean_url, '[data-marker="item-view/item-description"], h1')
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


# Регион в карточке лежит в <p> без data-marker и с хешированными классами
# («Ростовская обл., Таганрог»). Ищем по гео-словам — устойчиво к смене вёрстки.
_REGION_RX = re.compile(
    r"(област|\bобл\b|\bобл\.|кра[йяю]|респ|окру|Москв|Санкт|Петербург|Ленинградск|\bр-н\b|район)",
    re.IGNORECASE,
)


def _card_region(card) -> str:
    for el in card.query_selector_all("p"):
        t = (el.inner_text() or "").strip()
        if t and len(t) <= 60 and _REGION_RX.search(t):
            return t
    return ""


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
