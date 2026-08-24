"""Разовый вход в Авито в профиль монитора.

Запусти это ОДИН раз, войди в свой аккаунт в открывшемся окне, вернись в консоль
и нажми Enter — сессия сохранится в профиле монитора, и дальше монитор будет
переиспользовать её сам.

    python -m pricewatch.login

Окно — отдельный Chrome на профиле монитора; твой обычный Chrome не затрагивается.
Повторять нужно редко — только если сессия протухнет и монитор начнёт получать
блокировки.
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from . import config


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            config.PROFILE_DIR,
            channel=config.CHROME_CHANNEL,
            headless=False,  # вход только с окном
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.avito.ru/", wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print("1) В открывшемся окне войди в свой аккаунт Авито.")
        print("2) Убедись, что видно твоё имя/профиль (ты вошёл).")
        print("3) Вернись сюда и нажми Enter — сессия сохранится.")
        print("=" * 60)
        try:
            input("Нажми Enter после входа... ")
        except EOFError:
            page.wait_for_timeout(180_000)  # запас, если запущено без консоли

        ctx.close()
    print("Готово — сессия сохранена в", config.PROFILE_DIR)


if __name__ == "__main__":
    main()
