"""Отправка совпадений в Telegram через HTTP Bot API (без сторонних библиотек).

Если токен/чат не заданы — режим dry-run: печатаем сообщение в консоль, ничего
не отправляя. Это позволяет отлаживать пайплайн офлайн.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .filter import MatchResult
from .models import Listing

_API = "https://api.telegram.org/bot{token}/sendMessage"


def format_message(listing: Listing, result: MatchResult, event: str = "new") -> str:
    """Собрать текст уведомления (HTML-разметка Telegram)."""
    head = "🔽 Цена снижена" if event == "price_drop" else "🆕 Новое объявление"
    price = f"{listing.price:,} ₽".replace(",", " ") if listing.price else "цена не указана"
    lines = [
        f"<b>{head}</b>",
        _escape(listing.title),
        f"Цена: {price}",
    ]
    if result.price_per_gram is not None:
        lines.append(f"За грамм: <b>{result.price_per_gram:,.0f} ₽/г</b>".replace(",", " "))
    if result.weight_g is not None:
        lines.append(f"Вес: {result.weight_g:g} г")
    if result.ambiguous_weight:
        lines.append("⚠ вес неоднозначный — проверь вручную")
    if listing.url:
        lines.append(listing.url)
    return "\n".join(lines)


def send(text: str) -> bool:
    """Отправить сообщение всем получателям. dry-run считается успехом."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_ids = config.TELEGRAM_CHAT_IDS

    if not token or not chat_ids:
        print("[dry-run: TELEGRAM не настроен, сообщение не отправлено]\n" + text + "\n")
        return True

    ok_all = True
    for chat_id in chat_ids:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            }
        ).encode()
        try:
            with urllib.request.urlopen(_API.format(token=token), data=data, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
                if not payload.get("ok"):
                    ok_all = False
                    print(f"[telegram] {chat_id}: не ок — {payload.get('description')}")
        except urllib.error.URLError as e:
            ok_all = False
            print(f"[telegram] {chat_id}: ошибка отправки — {e}")
    return ok_all


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
