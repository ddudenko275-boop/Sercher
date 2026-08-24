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
    # Дальний регион — отсекаем СРАЗУ (регион есть уже из карточки), не открывая
    # ради веса страницу заведомо далёкого объявления.
    if _is_far(listing.region):
        return MatchResult(False, f"слишком далеко: {listing.region}")

    text = f"{listing.title} {listing.description}"
    has_585 = extract.has_proba_585(text)
    weight, ambiguous = extract.parse_weight_grams(text)

    # Проба и вес часто лежат ТОЛЬКО в описании (и бывают обфусцированы). Если
    # по имеющемуся тексту чего-то нет и страницу мы ещё не открывали — открываем.
    # Отказываем «не 585 / вес не найден» лишь когда страница уже прочитана.
    if not has_585 or weight is None:
        if not listing.detailed:
            return MatchResult(False, "нужна страница", needs_page=True)
        reason = "не 585 пробы" if not has_585 else "вес не найден"
        return MatchResult(False, reason, weight_g=weight, ambiguous_weight=ambiguous)

    if listing.price is None:
        return MatchResult(False, "цена не указана", weight_g=weight, ambiguous_weight=ambiguous)

    ppg = listing.price / weight
    threshold = config.MAX_PRICE_PER_GRAM
    if ppg >= threshold:
        return MatchResult(
            False, f"{ppg:,.0f} ₽/г ≥ {threshold}".replace(",", " "),
            weight_g=weight, price_per_gram=ppg, ambiguous_weight=ambiguous,
        )

    # Цена подходит. Доставка (если бы требовалась) — регион уже проверен выше.
    if config.REQUIRE_DELIVERY and listing.has_delivery is False:
        return MatchResult(
            False, "нет Авито Доставки",
            weight_g=weight, price_per_gram=ppg, ambiguous_weight=ambiguous,
        )

    return MatchResult(
        True, f"{ppg:,.0f} ₽/г < {threshold}".replace(",", " "),
        weight_g=weight, price_per_gram=ppg, ambiguous_weight=ambiguous,
    )


def _is_far(region: str) -> bool:
    """Регион продавца в мягком блок-листе дальних регионов?"""
    if not region:
        return False
    return any(marker.lower() in region.lower() for marker in config.FAR_REGIONS)
