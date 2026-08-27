"""Сбор объявлений через веб-JSON-API Авито (curl_cffi + cookies от spfa.pro).

Схема (как в эталоне github.com/Duff89/parser_avito):
  1. spfa /api/avito-url/       — обычная ссылка поиска → ссылка на API Авито (кэш).
  2. spfa /api/cookies/mobile/  — персональные cookies под наш прокси-IP + fingerprint.
  3. curl_cffi (impersonate из fingerprint) + cookies + прокси → GET API → JSON.

Главное: у каждого объявления в JSON есть поле `description` — открывать страницы
объявлений НЕ нужно (наша прежняя «стена» с антиботом на страницах уходит).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from curl_cffi import requests as creq

from . import config
from .models import Listing

_SPFA = "https://spfa.pro/api"
_URL_CACHE = Path("data/api_urls.json")


def convert_search_url(search_url: str) -> str:
    """Обычная ссылка поиска Авито → ссылка на API (через spfa, с кэшем на диске).

    Конвертация у spfa бесплатна (лимит ~2/мин), поэтому кэшируем навсегда.
    """
    cache: dict[str, str] = {}
    if _URL_CACHE.exists():
        try:
            cache = json.loads(_URL_CACHE.read_text("utf-8"))
        except Exception:
            cache = {}
    if search_url in cache:
        return cache[search_url]

    r = requests.post(f"{_SPFA}/avito-url/", json={"url": search_url}, timeout=25)
    r.raise_for_status()
    api_url = (r.json() or {}).get("api_url")
    if not api_url:
        raise RuntimeError("spfa не вернул api_url")

    cache[search_url] = api_url
    _URL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _URL_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
    return api_url


class AvitoApi:
    """Тянет страницы API Авито, сам получает/освежает cookies у spfa при блоке."""

    def __init__(self):
        self.api_key = config.SPFA_API_KEY
        self.proxy = config.PROXY
        self._id: str | None = None
        self._cookies: dict | None = None
        self._fingerprint: dict = {}
        self._user_agent: str | None = None

    # --- cookies через spfa ---
    def _buy_cookies(self) -> None:
        r = requests.post(
            f"{_SPFA}/cookies/mobile/",
            json={"api_key": self.api_key, "mobile": True, "proxy": self.proxy},
            timeout=30,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("results", {})
        self._id = data.get("id")
        self._cookies = data.get("cookies")
        self._fingerprint = data.get("fingerprint") or {}
        self._user_agent = data.get("user_agent") or self._fingerprint.get(
            "headers", {}
        ).get("user-agent")
        if not (self._cookies and self._fingerprint.get("impersonate")):
            raise RuntimeError(f"spfa вернул неполные cookies: {data}")

    def _unblock_or_rebuy(self) -> None:
        if self._id:
            try:
                r = requests.post(
                    f"{_SPFA}/unblock/",
                    json={"id": self._id, "api_key": self.api_key, "proxy": self.proxy},
                    timeout=30,
                )
                if r.status_code in (200, 202, 409):
                    time.sleep(5)
                    return
            except requests.RequestException:
                pass
        self._buy_cookies()

    def _session(self) -> "creq.Session":
        if not self._cookies:
            self._buy_cookies()
        impersonate = self._fingerprint.get("impersonate") or "chrome"
        s = creq.Session(impersonate=impersonate)
        headers = self._fingerprint.get("headers") or {}
        if isinstance(headers, dict):
            s.headers.update(headers)
        if self._user_agent:
            s.headers["user-agent"] = self._user_agent
        s.cookies.update(self._cookies or {})
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    # --- выдача ---
    def fetch_page(self, api_url: str, page: int) -> list[Listing]:
        url = _with_page(api_url, page)
        for _ in range(3):
            s = self._session()
            r = s.get(url, timeout=40)
            if r.status_code == 200:
                return _items_to_listings(r.json())
            if r.status_code in (403, 429, 439):
                self._unblock_or_rebuy()
                time.sleep(3)
                continue
            r.raise_for_status()
        return []


def _with_page(api_url: str, page: int) -> str:
    parts = urlsplit(api_url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("p", "page")
    ]
    query.append(("page", str(page)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _items_to_listings(payload: dict) -> list[Listing]:
    def find_items(d):
        if isinstance(d, dict):
            if isinstance(d.get("items"), list):
                return d["items"]
            for v in d.values():
                found = find_items(v)
                if found is not None:
                    return found
        return None

    out: list[Listing] = []
    for it in find_items(payload) or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        price = None
        pd = it.get("priceDetailed") or {}
        if isinstance(pd, dict) and str(pd.get("value", "")).isdigit():
            price = int(pd["value"])
        url_path = it.get("urlPath") or ""
        url = f"https://www.avito.ru{url_path}" if url_path.startswith("/") else url_path
        out.append(
            Listing(
                id=str(it.get("id")),
                title=it.get("title") or "",
                price=price,
                url=url,
                description=it.get("description") or "",
                region=_item_region(it),
                detailed=True,  # описание уже в JSON — страница не нужна
            )
        )
    return out


def _item_region(it: dict) -> str:
    loc = it.get("location") or it.get("geo") or {}
    if isinstance(loc, dict):
        return loc.get("name") or loc.get("namePrepositional") or ""
    return ""


def _main() -> int:
    """Быстрый тест: python -m pricewatch.avito_api  (нужны SPFA_API_KEY и PROXY)."""
    if not (config.SPFA_API_KEY and config.PROXY):
        print("Заполни SPFA_API_KEY и PROXY в .env")
        return 1
    search = config.SEARCH_URL_TEMPLATE.format(region="rostov-na-donu")
    api_url = convert_search_url(search)
    print("api_url:", api_url)
    api = AvitoApi()
    items = api.fetch_page(api_url, 1)
    print(f"объявлений: {len(items)}\n")
    for x in items[:8]:
        print(f"  {x.title[:44]:44} | {x.price} ₽ | рег: {x.region or '—'}")
        print(f"      описание: {x.description[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
