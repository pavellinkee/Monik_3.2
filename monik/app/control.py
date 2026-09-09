"""Управление сканером во время работы.

Оператор должен уметь остановить и снова запустить сканирование, не
подключаясь к серверу. Управление намеренно устроено просто:

* **остановка** запрещает начинать новые циклы Level 1. Уже принятые
  Level 2 Job доводятся до конца — прерывать безопасную операцию ради
  освобождения ресурса нельзя (``05_RESOURCE_MANAGER.md`` §18);
* **запуск** снова разрешает циклы;
* **перезапуск** — это остановка процесса с отдельным кодом возврата.
  Поднимает процесс обратно менеджер служб, а не приложение: собственного
  механизма перезапуска Monik не заводит.

Состояние живёт только в памяти: после рестарта сканер снова разрешён.
Иначе одна команда «Остановить» тихо выключила бы сканер навсегда.
"""

from __future__ import annotations

import asyncio

from monik.domain.enums.control import ScannerRunState
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["RESTART_EXIT_CODE", "ScannerSwitch"]

_LOGGER = get_logger("app.control")

#: Код возврата, которым процесс сообщает о запрошенном перезапуске.
#: Менеджер служб обязан быть настроен на автоматический перезапуск
#: (``24_DEPLOYMENT.md``): без этого команда просто остановит приложение.
RESTART_EXIT_CODE = 3


class ScannerSwitch:
    """Разрешает или запрещает новые циклы сканирования."""

    def __init__(self) -> None:
        self._paused = False
        self._restart_requested = asyncio.Event()

    # --- состояние ---------------------------------------------------------

    def state(self) -> ScannerRunState:
        """Текущее состояние сканирования."""
        if self._restart_requested.is_set():
            return ScannerRunState.RESTARTING
        return ScannerRunState.PAUSED if self._paused else ScannerRunState.RUNNING

    @property
    def is_running(self) -> bool:
        """Разрешено ли начинать новый цикл."""
        return not self._paused and not self._restart_requested.is_set()

    @property
    def restart_requested(self) -> bool:
        """Запрошен ли перезапуск процесса."""
        return self._restart_requested.is_set()

    async def wait_for_restart(self) -> None:
        """Дождаться запроса на перезапуск."""
        await self._restart_requested.wait()

    # --- команды -----------------------------------------------------------

    def start(self) -> bool:
        """Разрешить сканирование. ``True``, если состояние изменилось."""
        if not self._paused:
            return False
        self._paused = False
        _LOGGER.info("scanning resumed", extra=log_fields(state=self.state().value))
        return True

    def stop(self) -> bool:
        """Запретить новые циклы. ``True``, если состояние изменилось."""
        if self._paused:
            return False
        self._paused = True
        _LOGGER.warning("scanning paused by operator", extra=log_fields(state=self.state().value))
        return True

    def request_restart(self) -> None:
        """Запросить перезапуск процесса."""
        if self._restart_requested.is_set():
            return
        self._restart_requested.set()
        _LOGGER.warning("restart requested by operator", extra=log_fields(state="restarting"))
