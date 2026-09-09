"""Monik — DEX arbitrage scanner.

Реализация утверждённой архитектуры из ``docs/architecture/``.
Слои и их границы описаны в ``docs/architecture/25_PROJECT_STRUCTURE.md``.

Версия приложения объявляется **здесь и только здесь**. Все места, где
приложение сообщает свою версию — startup logs, ``--version``, Telegram
``/status``, уведомления о запуске и восстановлении, diagnostics — читают
её отсюда, а не хранят собственную копию. ``pyproject.toml`` обязан
совпадать с этим значением; совпадение проверяется тестом.
"""

__all__ = ["APPLICATION_NAME", "__version__", "version_label"]

#: Отображаемое имя приложения.
APPLICATION_NAME = "Monik"

#: Версия приложения. Совпадает с версией рабочего репозитория.
__version__ = "3.2.0"


def version_label() -> str:
    """Человекочитаемое обозначение версии, например ``Monik 3.2.0``."""
    return f"{APPLICATION_NAME} {__version__}"
