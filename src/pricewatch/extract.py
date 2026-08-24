"""Извлечение из текста объявления двух вещей: пробы 585 и веса в граммах.

Это ядро всей затеи — «цена за грамм» считается из веса, которого нет в
структурированных полях Авито, только в тексте. Логика чисто регулярочная,
без LLM.
"""

from __future__ import annotations

import re

from . import config

# Вес: число (с , или .) + единица «г/гр/грамм…/g».
# Лукахед (?![а-яёa-z]) не даёт «г» съесть «года», «гб», «город» и т.п.
_WEIGHT_RE = re.compile(
    r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:г|гр|грамм[а-я]*|g)(?![а-яёa-z])",
    re.IGNORECASE,
)

# Проба 585: число 585, не окружённое цифрами и не начало «585 000» (цены).
_PROBA_585_RE = re.compile(r"(?<!\d)585(?!\d)(?!\s\d{3})")


def has_proba_585(text: str) -> bool:
    """Есть ли в тексте признак 585 пробы."""
    return bool(_PROBA_585_RE.search(text or ""))


def parse_weight_grams(text: str) -> tuple[float | None, bool]:
    """Вернуть (вес_в_граммах, неоднозначно?).

    Берём первое правдоподобное значение. Флаг «неоднозначно» поднимается, если
    в тексте несколько разных правдоподобных весов (например, цепь + подвеска) —
    такие случаи позже можно отдавать в LLM.
    """
    candidates: list[float] = []
    for m in _WEIGHT_RE.finditer(text or ""):
        try:
            w = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if config.MIN_WEIGHT_G <= w <= config.MAX_WEIGHT_G:
            candidates.append(w)

    if not candidates:
        return None, False

    ambiguous = len(set(candidates)) > 1
    return candidates[0], ambiguous
