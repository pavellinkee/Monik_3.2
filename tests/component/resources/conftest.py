"""Виртуальное время для нагрузочных проверок Resource Manager.

Настроенные ~5 запросов в секунду означают, что полный цикл Level 1
занимает минуты. Ждать их по-настоящему тест не может, а уменьшать
нагрузку нельзя: проверяется именно она. Поэтому время моделируется —
часы двигаются к ближайшему ожидающему, когда выполнять больше нечего.

Измерения при этом остаются точными: rate limiter и circuit breaker
видят ровно то время, которое проспали их клиенты.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Awaitable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["VirtualTime"]

#: Сколько раз уступить управление, прежде чем считать, что выполнять
#: больше нечего и время можно двигать.
_IDLE_TURNS = 400


class VirtualTime:
    """Часы и ожидание, управляемые тестом.

    Реализует протокол ``Clock`` и подходит в качестве ``Sleeper``
    Resource Manager.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start.astimezone(UTC)
        self._monotonic = 0.0
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._sequence = itertools.count()

    # --- протокол Clock ---------------------------------------------------

    def now(self) -> datetime:
        """Текущее смоделированное время."""
        return self._now

    def monotonic(self) -> float:
        """Смоделированные монотонные секунды."""
        return self._monotonic

    # --- протокол Sleeper -------------------------------------------------

    async def sleep(self, seconds: float) -> None:
        """Уснуть до наступления смоделированного момента."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._monotonic + seconds, next(self._sequence), future))
        await future

    # --- управление -------------------------------------------------------

    async def run[T](self, main: Coroutine[Any, Any, T]) -> T:
        """Выполнить работу, продвигая время, пока она не завершится."""
        task = asyncio.ensure_future(main)
        while not task.done():
            await self._settle()
            if task.done() or not self._waiters:
                break
            self._advance_to(self._waiters[0][0])
        return await task

    async def gather[T](self, *awaitables: Awaitable[T]) -> list[T]:
        """``asyncio.gather`` под управляемым временем."""
        return await self.run(asyncio.gather(*awaitables))

    async def _settle(self) -> None:
        """Дать выполниться всему, что готово выполняться сейчас."""
        for _ in range(_IDLE_TURNS):
            await asyncio.sleep(0)

    def _advance_to(self, moment: float) -> None:
        """Передвинуть часы и разбудить всех, чей срок наступил."""
        if moment > self._monotonic:
            self._now += timedelta(seconds=moment - self._monotonic)
            self._monotonic = moment
        while self._waiters and self._waiters[0][0] <= self._monotonic:
            _, _, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(None)
