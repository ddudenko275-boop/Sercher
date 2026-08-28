"""Извлечение из текста объявления пробы 585 и веса в граммах.

Реальные объявления Авито обфусцируют текст латинскими буквами-двойниками
(«Пpоба», «Beс») и пишут вес без единицы («Вес: 4,77»). Поэтому:
  1) нормализуем гомоглифы (латиница → кириллица);
  2) ищем вес и по метке «вес/масса: N», и по «N г».
"""

from __future__ import annotations

import re

from . import config

# Латинские буквы-двойники → кириллица (продавцы так прячут текст от парсеров).
_LAT2CYR = str.maketrans({
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "y": "у", "x": "х", "k": "к",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
})


def _normalize(text: str) -> str:
    return (text or "").translate(_LAT2CYR)


# Вес по метке: «вес: 4,77», «масса 4.77» — единица не обязательна.
_WEIGHT_LABEL_RE = re.compile(
    r"(?:вес|масса)\W{0,3}(\d{1,4}(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)
# Вес по единице: «4,2 г», «4.2 гр», «4,2 грамма», «4.2 g».
_WEIGHT_UNIT_RE = re.compile(
    r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:г|гр|грамм[а-я]*|g)(?![а-яёa-z])",
    re.IGNORECASE,
)

# Проба 585: число 585, не окружённое цифрами и не начало «585 000» (цены).
_PROBA_585_RE = re.compile(r"(?<!\d)585(?!\d)(?!\s\d{3})")


def has_proba_585(text: str) -> bool:
    """Есть ли в тексте признак 585 пробы."""
    return bool(_PROBA_585_RE.search(_normalize(text)))


# Признак «цена объявления указана ЗА ГРАММ» (лом/скупка): «цена за грамм»,
# «7250 за грамм», «стоимость грамма», «₽/г», «руб/грамм». Тогда делить на вес НЕ надо.
_PRICE_PER_GRAM_RE = re.compile(
    r"за\s*(?:1\s*)?грамм"
    r"|(?:цена|стоимост\w*)\s+(?:за\s+)?(?:1\s*)?грамма?"
    r"|(?:₽|руб|р)\s*/\s*(?:грамм|гр|г)\b",
    re.IGNORECASE,
)


def is_price_per_gram(text: str) -> bool:
    """Указана ли цена ЗА ГРАММ (тогда ₽/г = цена объявления, без деления на вес)."""
    return bool(_PRICE_PER_GRAM_RE.search(_normalize(text)))


def parse_weight_grams(text: str) -> tuple[float | None, bool]:
    """Вернуть (вес_в_граммах, неоднозначно?).

    Берём первое правдоподобное значение (сперва по метке «вес», затем по «N г»).
    Флаг «неоднозначно» — если правдоподобных значений несколько (цепь+подвеска).
    """
    t = _normalize(text)
    candidates: list[float] = []
    for regex in (_WEIGHT_LABEL_RE, _WEIGHT_UNIT_RE):
        for m in regex.finditer(t):
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
