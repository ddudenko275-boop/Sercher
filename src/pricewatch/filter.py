"""Решение «подходит объявление или нет» по нашим условиям: 585 + цена за грамм."""

from __future__ import annotations

from dataclasses import dataclass

from . import config, extract
from .models import Listing


@dataclass
class MatchResult:
    matched: bool
    reason: str
    weight_g: float | None = None
    price_per_gram: float | None = None
    ambiguous_weight: bool = False
    needs_page: bool = False  # вес не найден в тексте — надо открыть объявление


def evaluate(listing: Listing) -> MatchResult:
    """Прогнать объявление через жёсткие условия."""
    # Дальний регион — отсекаем сразу (регион есть из карточки/JSON).
    if _is_far(listing.region):
        return MatchResult(False, f"слишком далеко: {listing.region}")

    text = f"{listing.title} {listing.description}"
    has_585 = extract.has_proba_585(text)
    per_gram = extract.is_price_per_gram(text)
    weight, ambiguous = extract.parse_weight_grams(text)

    # Нужны ТОЛЬКО чистые золотые украшения от ПРОДАВЦА. Отсекаем всё, что не оно:
    if extract.is_junk(text):
        return MatchResult(False, "скам/дропшип или люкс-бренд со вставками")
    if extract.is_buyer(text):
        return MatchResult(False, "скупщик (покупает золото, не продаёт)")
    if extract.is_watch(text):
        return MatchResult(False, "часы, не украшение")
    if extract.is_other_metal(text):
        return MatchResult(False, "не чистое золото (серебро/сталь/позолота)")
    if config.EXCLUDE_STONES and extract.has_stones(text):
        return MatchResult(False, "со вставками/камнями", weight_g=weight)

    if per_gram:
        # Цена объявления УЖЕ указана за грамм — на вес НЕ делим.
        if not has_585:
            if not listing.detailed:
                return MatchResult(False, "нужна страница", needs_page=True)
            return MatchResult(False, "не 585 пробы")
        if listing.price is None:
            return MatchResult(False, "цена не указана")
        if not _weight_ok(weight):
            return MatchResult(False, _weight_reason(weight), weight_g=weight)
        return _decide(float(listing.price), weight, ambiguous)

    # Обычная цена (за изделие) — нужен вес, чтобы посчитать ₽/г.
    if not has_585 or weight is None:
        if not listing.detailed:
            return MatchResult(False, "нужна страница", needs_page=True)
        reason = "не 585 пробы" if not has_585 else "вес не найден"
        return MatchResult(False, reason, weight_g=weight, ambiguous_weight=ambiguous)
    if not _weight_ok(weight):
        return MatchResult(False, _weight_reason(weight), weight_g=weight, ambiguous_weight=ambiguous)
    if listing.price is None:
        return MatchResult(False, "цена не указана", weight_g=weight, ambiguous_weight=ambiguous)
    # Перекрёстная проверка: если цена_объявления × вес ≈ полной цене из описания,
    # значит в поле цены указан ₽/ГРАММ (частый случай у лома) — тогда НЕ делим.
    if _price_is_per_gram_by_math(listing.price, weight, text):
        return _decide(float(listing.price), weight, ambiguous)
    return _decide(listing.price / weight, weight, ambiguous)


def _price_is_per_gram_by_math(price: int | None, weight: float | None, text: str) -> bool:
    """Похоже ли, что цена объявления — это ₽/грамм, а не полная цена изделия?

    Признак: цена × вес совпадает (±15%) с одной из «полных цен», указанных прямо
    в описании. Полная цена по определению больше цены за грамм, поэтому суммы,
    не превышающие саму цену, игнорируем — это защищает обычные объявления.
    """
    if not price or not weight:
        return False
    implied_full = price * weight
    for money in extract.parse_money_amounts(text):
        if money <= price:
            continue
        if abs(implied_full - money) <= 0.15 * money:
            return True
    return False


def _weight_ok(weight: float | None) -> bool:
    """Проходит ли вес порог «не менее N граммов» (0 — порога нет)."""
    minimum = config.MIN_ITEM_WEIGHT_G
    if not minimum:
        return True
    return weight is not None and weight >= minimum


def _weight_reason(weight: float | None) -> str:
    minimum = config.MIN_ITEM_WEIGHT_G
    if weight is None:
        return f"вес не указан (нужно ≥ {minimum:g} г)"
    return f"{weight:g} г < {minimum:g} г"


def _decide(ppg: float, weight: float | None, ambiguous: bool) -> MatchResult:
    # Нереально низкая цена за грамм — это не сделка, а ошибка разбора
    # (код модели вместо веса и т.п.). Не шлём.
    if ppg < config.MIN_PLAUSIBLE_PRICE_PER_GRAM:
        reason = f"{ppg:,.0f} ₽/г — ошибка веса/описания".replace(",", " ")
        return MatchResult(False, reason, weight_g=weight, price_per_gram=ppg, ambiguous_weight=ambiguous)
    threshold = config.MAX_PRICE_PER_GRAM
    reason = f"{ppg:,.0f} ₽/г {'<' if ppg < threshold else '≥'} {threshold}".replace(",", " ")
    return MatchResult(
        ppg < threshold, reason,
        weight_g=weight, price_per_gram=ppg, ambiguous_weight=ambiguous,
    )


def _is_far(region: str) -> bool:
    """Регион продавца в мягком блок-листе дальних регионов?"""
    if not region:
        return False
    return any(marker.lower() in region.lower() for marker in config.FAR_REGIONS)
