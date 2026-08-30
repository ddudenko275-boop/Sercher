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

from . import config, health
from .logutil import log
from .models import Listing

_SPFA = "https://spfa.pro/api"
_URL_CACHE = Path("data/api_urls.json")
_COOKIES_CACHE = Path("data/cookies.json")


class AvitoBlocked(Exception):
    """Авито вернул блокировку (403/429/439) — регион заблокирован."""


def convert_search_url(search_url: str) -> str:
    """Обычная ссылка поиска Авито → ссылка на API (через spfa, с кэшем на диске).

    Конвертация у spfa бесплатна, но лимит ~2/мин — на 429 ждём и повторяем.
    """
    cache: dict[str, str] = {}
    if _URL_CACHE.exists():
        try:
            cache = json.loads(_URL_CACHE.read_text("utf-8"))
        except Exception:
            cache = {}
    if search_url in cache:
        return cache[search_url]

    api_url = None
    for attempt in range(4):
        try:
            r = requests.post(f"{_SPFA}/avito-url/", json={"url": search_url}, timeout=25)
        except requests.RequestException:
            if attempt == 0:
                time.sleep(3)
                continue
            raise
        if r.status_code == 429:  # лимит spfa — ждём и повторяем
            time.sleep(32)
            continue
        if r.status_code >= 500:  # spfa лёг — не устраиваем шторм ретраев
            if attempt == 0:
                time.sleep(3)
                continue
            r.raise_for_status()
        r.raise_for_status()
        api_url = (r.json() or {}).get("api_url")
        break
    if not api_url:
        raise RuntimeError("spfa не вернул api_url (лимит или ошибка)")

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
        self._ts: float = 0.0            # когда куплены cookies (для возраста)
        self._exit_ip_saved: str | None = None  # exit-IP прокси на момент покупки cookies
        self._last_recover: float = 0.0  # время последней попытки восстановления
        self._recover_count = 0          # сколько раз восстанавливались за процесс
        self.degraded_reason: str | None = None  # причина деградации (для алерта)
        self._load_cookies()

    def _load_cookies(self) -> None:
        """Подтянуть ранее купленные cookies с диска (чтобы не покупать заново)."""
        if not _COOKIES_CACHE.exists():
            return
        try:
            d = json.loads(_COOKIES_CACHE.read_text("utf-8"))
            self._id = d.get("id")
            self._cookies = d.get("cookies")
            self._fingerprint = d.get("fingerprint") or {}
            self._user_agent = d.get("user_agent")
            self._ts = float(d.get("ts") or 0.0)
            self._exit_ip_saved = d.get("exit_ip")
        except Exception:
            pass

    def _save_cookies(self) -> None:
        self._ts = time.time()
        _COOKIES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _COOKIES_CACHE.write_text(json.dumps({
            "id": self._id,
            "cookies": self._cookies,
            "fingerprint": self._fingerprint,
            "user_agent": self._user_agent,
            "ts": self._ts,
            "exit_ip": self._exit_ip_saved,
        }, ensure_ascii=False), "utf-8")

    def cookie_age(self) -> float | None:
        """Возраст текущих cookies в секундах (None — если их нет)."""
        if not self._cookies or not self._ts:
            return None
        return time.time() - self._ts

    def refresh_cookies_if_old(self) -> bool:
        """Проактивно освежить cookies, если они старше COOKIE_MAX_AGE_SEC.

        Дешевле и надёжнее, чем ждать блокировки протухших cookies в разгар прохода.
        """
        age = self.cookie_age()
        if age is not None and age > config.COOKIE_MAX_AGE_SEC:
            try:
                log(f"[cookies] возраст {age/3600:.1f}ч — проактивно перевыпускаю (до протухания 12ч)")
                self._buy_cookies()
                return True
            except Exception as e:
                log(f"[cookies] проактивный перевыпуск не удался: {e}")
        return False

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
        self._exit_ip_saved = self._exit_ip()  # запомнить, под каким IP взяты cookies
        self._save_cookies()
        self._count_purchase()

    def _exit_ip(self) -> str | None:
        """Текущий внешний IP прокси (через ipify). Доказательство: сам ли прокси
        сменил IP (тогда cookies протухли не из-за бана) или IP тот же (бан Авито)."""
        if not self.proxy:
            return None
        proxy_url = self.proxy if "://" in self.proxy else f"http://{self.proxy}"
        try:
            r = requests.get("https://api.ipify.org",
                             proxies={"http": proxy_url, "https": proxy_url}, timeout=15)
            if r.status_code == 200:
                return r.text.strip()
        except Exception:
            pass
        return None

    def _purchases_today(self) -> int:
        from datetime import date
        st = health.load()
        if st.get("rebuy_date") != date.today().isoformat():
            return 0
        return int(st.get("rebuy_count", 0))

    def _count_purchase(self) -> None:
        from datetime import date
        st = health.load()
        today = date.today().isoformat()
        if st.get("rebuy_date") != today:
            st["rebuy_date"] = today
            st["rebuy_count"] = 0
        st["rebuy_count"] = int(st.get("rebuy_count", 0)) + 1
        health.save(st)

    def _balance(self) -> float | None:
        """Текущий баланс spfa (₽). Дёшев и работает даже когда выдача cookies тупит."""
        try:
            r = requests.post(f"{_SPFA}/balance/", json={"api_key": self.api_key}, timeout=20)
            if r.status_code == 200:
                return float((r.json() or {}).get("balance"))
        except Exception:
            pass
        return None

    def _rotate_ip(self) -> bool:
        """Дёрнуть смену IP по ссылке ротации. Гейт по кулдауну — на стороне recover()."""
        url = getattr(config, "CHANGEIP_URL", "")
        if not url:
            return False
        try:
            r = requests.get(url, timeout=30)
            body = (r.text or "").lower()
            if r.status_code == 200 and ("ok" in body or "success" in body or "new ip" in body):
                log("[recover] IP прокси сменён по ссылке")
                time.sleep(12)  # дать прокси применить новый IP
                return True
            log(f"[recover] ротация IP не удалась: HTTP {r.status_code}")
        except Exception as e:
            log(f"[recover] ротация IP ошибка: {type(e).__name__}: {e}")
        return False

    def recover(self) -> bool:
        """Восстановление после стойкого бана. ПОКУПКА cookies — ТОЛЬКО НА НОВЫЙ IP.

        Новые cookies на том же живом IP не лечат бан (у Авито бан IP-шный), поэтому
        весь контроль трат — в ПОЛИТИКЕ РОТАЦИИ, адаптивной по времени жизни IP:
          • IP умер мгновенно (<60с) — плохая раздача пула, переротация по базе, без штрафа;
          • пожил и умер (мин) — горит от НАШЕЙ нагрузки → удваиваем кулдаун (бэкоф),
            не бьёмся в ту же стену (иначе утренние 35 покупок);
          • прожил >2ч — нагрузка ок → сбрасываем кулдаун к базе.
        Дневной потолок покупок — теперь предохранитель от бага, а не регулятор.
        """
        now = time.time()
        if now - self._last_recover < config.RECOVER_MIN_INTERVAL_SEC:
            return False
        self._last_recover = now
        if self._recover_count >= config.MAX_RECOVERIES_PER_RUN:
            self.degraded_reason = "исчерпан лимит авто-восстановлений за проход"
            return False

        st = health.load()
        ip_born = float(st.get("last_rotation", 0) or 0)
        cooldown = float(st.get("rotate_cooldown", config.ROTATE_COOLDOWN_SEC)
                         or config.ROTATE_COOLDOWN_SEC)
        lifetime = (now - ip_born) if ip_born else 1e9

        # Адаптация кулдауна по времени жизни ПРОШЛОГО IP.
        if lifetime > config.ROTATE_LIFETIME_RESET:
            cooldown = config.ROTATE_COOLDOWN_SEC            # жил долго — база
        elif lifetime >= config.ROTATE_INSTANT_DEATH:
            cooldown = min(cooldown * 2, config.ROTATE_COOLDOWN_MAX)  # горит от нагрузки — бэкоф
            log(f"[recover] IP прожил {lifetime / 60:.0f} мин — бэкоф ротации до {cooldown / 60:.0f} мин")
        # lifetime < INSTANT_DEATH: плохой IP из пула — кулдаун не меняем

        def _persist():
            st["rotate_cooldown"] = cooldown
            health.save(st)

        # ПРЕДОХРАНИТЕЛЬ (не регулятор): при верной политике недостижим; сработал = баг.
        if self._purchases_today() >= config.MAX_REBUYS_PER_DAY:
            self.degraded_reason = (f"ПРЕДОХРАНИТЕЛЬ: {config.MAX_REBUYS_PER_DAY} покупок/сутки — "
                                    f"значит баг в логике, а не нормальный режим")
            log(f"[recover] {self.degraded_reason}")
            _persist()
            return False

        bal = self._balance()
        if bal is not None and bal < config.MIN_SPFA_BALANCE:
            self.degraded_reason = f"баланс spfa {bal:.0f}₽ — пополни, cookies не купить"
            log(f"[recover] {self.degraded_reason}")
            _persist()
            return False

        cur_ip = self._exit_ip()
        if cur_ip and self._exit_ip_saved and cur_ip != self._exit_ip_saved:
            # прокси сам сменил IP — законная покупка «после смены IP», changeip НЕ нужен
            log(f"[recover] прокси сменил IP сам ({self._exit_ip_saved}→{cur_ip}) — беру cookies")
        else:
            # тот же IP забанен → нужна ротация, но по кулдауну (мгновенную смерть гейтим базой)
            gate = config.ROTATE_COOLDOWN_SEC if lifetime < config.ROTATE_INSTANT_DEATH else cooldown
            if lifetime < gate:
                self.degraded_reason = f"ротация через ~{(gate - lifetime) / 60:.0f} мин (бэкоф) — денег не жгу"
                log(f"[recover] жду окна ротации ещё ~{gate - lifetime:.0f}с (IP горят от темпа)")
                _persist()
                return False
            if not self._rotate_ip():
                _persist()
                return False

        # IP новый (наш changeip или самопроизвольный) → фиксируем «рождение» и покупаем cookies
        st["last_rotation"] = now
        _persist()
        try:
            self._buy_cookies()
            self._recover_count += 1
            log("[recover] cookies перевыпущены под новый IP — продолжаю")
            return True
        except Exception as e:
            self.degraded_reason = f"spfa не отдал cookies: {e}"
            log(f"[recover] {self.degraded_reason}")
            return False

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
            # spfa хранит прокси как «логин:пароль@host:port»; curl_cffi нужна схема.
            proxy_url = self.proxy if "://" in self.proxy else f"http://{self.proxy}"
            s.proxies = {"http": proxy_url, "https": proxy_url}
        return s

    # --- выдача ---
    def fetch_page(self, api_url: str, page: int) -> list[Listing]:
        url = _with_page(api_url, page)
        # Различаем два вида блока:
        #   429 = РЕЙТ-ЛИМИТ («притормози») — проходит сам, ждём и повторяем ТЕМИ ЖЕ
        #         cookies (0 ₽); перевыпуск бесполезен (IP тот же).
        #   403/439 = БАН IP — ожидание НЕ помогает, эскалируем БЫСТРО (иначе теряем
        #         минуты на мёртвом IP), раннер сменит IP.
        soft = [15, 30]  # ожидания для 429
        si = 0
        for attempt in range(4):
            r = self._session().get(url, timeout=40)
            if r.status_code == 200:
                return _items_to_listings(r.json())
            if r.status_code == 429:
                if si < len(soft):
                    time.sleep(soft[si])
                    si += 1
                    continue
                raise AvitoBlocked("429 (рейт-лимит не прошёл)")
            if r.status_code in (403, 439):
                if attempt == 0:
                    time.sleep(8)  # один короткий ретрай на случай «мигания» антибота
                    continue
                raise AvitoBlocked(f"HTTP {r.status_code} (бан IP)")
            r.raise_for_status()
        raise AvitoBlocked("блок не прошёл")


def with_price(api_url: str, pmin: int | None, pmax: int | None) -> str:
    """Добавить к API-URL ценовую полосу (priceMin/priceMax) для глубокого прочёса.

    priceMax ставим всегда (при None — заглушка 99999999): пара priceMin+priceMax
    подтверждена рабочей, а одиночный priceMin — нет.
    """
    parts = urlsplit(api_url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("priceMin", "priceMax")
    ]
    if pmin is not None:
        query.append(("priceMin", str(pmin)))
    query.append(("priceMax", str(pmax if pmax is not None else 99_999_999)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


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
