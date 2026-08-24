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
    text = f"{listing.title} {listing.description}"

    if not extract.has_proba_585(text):
        return MatchResult(False, "не 585 пробы")

    weight, ambiguous = extract.parse_weight_grams(text)
    if weight is None:
        # 585 есть, но вес в тексте не указан — кандидат на дозагрузку страницы.
        return MatchResult(False, "вес не найден в тексте", needs_page=True)

    if listing.price is None:
        return MatchResult(False, "цена не указана", weight_g=weight, ambiguous_weight=ambiguous)

    ppg = listing.price / weight
    threshold = config.MAX_PRICE_PER_GRAM
    if ppg >= threshold:
        return MatchResult(
            False, f"{ppg:,.0f} ₽/г ≥ {threshold}".replace(",", " "),
            weight_g=weight, price_per_gram=ppg, ambiguous_weight=ambiguous,
        )

    # Цена подходит — проверяем доставку и дальность.
    # has_delivery is None (неизвестно) не отсекаем: считаем, что сохранённый
    # поиск на Авито уже отфильтровал по «Авито Доставке».
    if config.REQUIRE_DELIVERY and listing.has_delivery is False:
        return MatchResult(
            False, "нет Авито Доставки",
            weight_g=weight, price_per_gram=ppg, ambiguous_weight=ambiguous,
        )
    if _is_far(listing.region):
        return MatchResult(
            False, f"слишком далеко: {listing.region}",
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
