"""Офлайн-проверка ядра-фильтра на тестовых объявлениях.

Запуск: python -m pricewatch.demo
Никакого Авито и API — только логика «585 + цена за грамм».
"""

from __future__ import annotations

from .filter import evaluate
from .models import Listing

# (title, description, price) — набор случаев, включая каверзные.
SAMPLES = [
    ("Цепочка золото 585, вес 4,2 г", "Отличное состояние", 25_000),   # 5952 ₽/г — подходит
    ("Кольцо 585 пробы 3.1 грамма", "", 22_000),                       # 7096 ₽/г — дорого
    ("Серьги золотые 585, 2,8 гр", "новые", 15_000),                   # 5357 ₽/г — подходит
    ("Кольцо золото 585 размер 18, вес 2 г", "", 12_000),              # вес=2 (не 18!), 6000 — подходит
    ("Браслет серебро 925, 5 г", "", 4_000),                          # не 585
    ("Золото 750 пробы, 4 г", "", 30_000),                            # не 585
    ("Золотая цепь 585 пробы", "Отличное состояние, срочно", 30_000), # вес не указан → нужна страница
    ("Цепь золото 585, 5,5 г", "Цена 585 000 руб за коллекцию", 585_000),  # 585 в цене не путаем
    ("Комплект 585: цепь 4 г и подвеска 1,5 г", "", 33_000),          # неоднозначный вес
]


def main() -> int:
    threshold = None
    print(f"{'Итог':<6} {'₽/г':>8}  Причина / объявление")
    print("-" * 70)
    for title, desc, price in SAMPLES:
        listing = Listing(id="0", title=title, price=price, url="", description=desc, detailed=True)
        r = evaluate(listing)
        mark = "✅" if r.matched else ("📄" if r.needs_page else "❌")
        ppg = f"{r.price_per_gram:,.0f}".replace(",", " ") if r.price_per_gram else "—"
        amb = "  ⚠ неоднозначный вес" if r.ambiguous_weight else ""
        price_str = f"{price:,}".replace(",", " ")
        print(f"{mark:<5} {ppg:>8}  {r.reason}{amb}")
        print(f"{'':<6} {'':>8}  «{title}» — {price_str} ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
