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

    if per_gram:
        # Цена объявления УЖЕ указана за грамм — на вес НЕ делим.
        if not has_585:
            if not listing.detailed:
                return MatchResult(False, "нужна страница", needs_page=True)
            return MatchResult(False, "не 585 пробы")
        if listing.price is None:
            return MatchResult(False, "цена не указана")
        return _decide(float(listing.price), weight, ambiguous)

    # Обычная цена (за изделие) — нужен вес, чтобы посчитать ₽/г.
    if not has_585 or weight is None:
        if not listing.detailed:
            return MatchResult(False, "нужна страница", needs_page=True)
        reason = "не 585 пробы" if not has_585 else "вес не найден"
        return MatchResult(False, reason, weight_g=weight, ambiguous_weight=ambiguous)
    if listing.price is None:
        return MatchResult(False, "цена не указана", weight_g=weight, ambiguous_weight=ambiguous)
    return _decide(listing.price / weight, weight, ambiguous)


def _decide(ppg: float, weight: float | None, ambiguous: bool) -> MatchResult:
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
