"""Единая модель объявления — к ней приводим данные с любой площадки."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Listing:
    """Одно объявление из поисковой выдачи."""

    id: str  # уникальный id объявления на площадке (для дедупликации)
    title: str
    price: int | None  # в рублях; None, если цена не указана / «Цену уточняйте»
    url: str
    description: str = ""  # текст объявления/сниппет — отсюда достаём вес и пробу
    region: str = ""       # регион продавца (для оценки дальности доставки)
    has_delivery: bool | None = None  # доступна ли Авито Доставка; None — неизвестно
    detailed: bool = False  # прочитали ли страницу объявления (описание/регион)
    source: str = "avito"

    def __str__(self) -> str:
        price = f"{self.price:,} ₽".replace(",", " ") if self.price else "цена не указана"
        return f"[{self.id}] {self.title} — {price}\n    {self.url}"
