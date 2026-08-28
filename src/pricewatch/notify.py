"""Отправка совпадений в Telegram через HTTP Bot API (без сторонних библиотек).

Если токен/чат не заданы — режим dry-run: печатаем сообщение в консоль, ничего
не отправляя. Это позволяет отлаживать пайплайн офлайн.
"""

from __future__ import annotations

import time

import requests

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
    if listing.region:
        lines.append(f"Регион: {_escape(listing.region)}")
    if result.ambiguous_weight:
        lines.append("⚠ вес неоднозначный — проверь вручную")
    if (result.price_per_gram is not None
            and result.price_per_gram < config.SUSPICIOUS_PRICE_PER_GRAM):
        lines.append("⚠️ Подозрительно дёшево (ниже цены лома) — возможно развод, проверь внимательно")
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

    url = _API.format(token=token)
    payload_base = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": "false"}
    ok_all = True
    for chat_id in chat_ids:
        sent = False
        for attempt in range(3):  # сеть/лимит — пара повторов
            try:
                r = requests.post(url, data={**payload_base, "chat_id": chat_id}, timeout=20)
                if r.json().get("ok"):
                    sent = True
                    break
                print(f"[telegram] {chat_id}: не ок — {r.json().get('description')}")
            except requests.RequestException as e:
                print(f"[telegram] {chat_id}: ошибка отправки — {e}")
            time.sleep(2)
        ok_all = ok_all and sent
    return ok_all


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
