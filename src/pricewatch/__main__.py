"""Запуск монитора одним проходом: python -m pricewatch"""

from .runner_api import run_once

if __name__ == "__main__":
    run_once()
