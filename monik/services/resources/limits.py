"""Ограничения на использование внешних ресурсов.

Concurrency limit и rate limit — **разные** ограничения
(``12_RESOURCE_MANAGER.md`` §19): первое ограничивает число одновременных
запросов, второе — их частоту. Неограниченная конкурентность запрещена
(``05_RESOURCE_MANAGER.md`` §61).
"""

from __future__ import annotations

from dataclasses import dataclass

from monik.services.observability.clock import Clock

__all__ = ["RateLimiter", "ResourceLimits"]


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Лимиты одного ресурса."""

    max_concurrent: int
    requests_per_second: float
    burst: int

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if self.burst < 1:
            raise ValueError("burst must be at least 1")


class RateLimiter:
    """Token bucket с учётом стоимости запроса.

    Batch не считается автоматически одним запросом: если провайдер
    учитывает каждый элемент отдельно, стоимость передаётся явно
    (``05_RESOURCE_MANAGER.md`` §55-56, ``12_RESOURCE_MANAGER.md`` §48).
    """

    def __init__(self, limits: ResourceLimits, clock: Clock) -> None:
        self._limits = limits
        self._clock = clock
        self._tokens = float(limits.burst)
        self._updated_at = clock.monotonic()

    @property
    def burst(self) -> int:
        """Максимальная стоимость запроса, которую корзина может выдать."""
        return self._limits.burst

    def reserve(self, units: int = 1) -> float:
        """Занять место в очереди и вернуть паузу перед выполнением.

        Стоимость списывается сразу, поэтому корзина может уйти в минус:
        это и есть очередь ожидающих. Возвращённая пауза — время, через
        которое долг будет покрыт пополнением.

        Такая резервация заменяет цикл «подождать и попробовать снова».
        Цикл был неверен дважды: ожидающие просыпались одновременно и
        соревновались за одни и те же токены, теряя порядок очереди, а
        погрешность вещественной арифметики могла оставить не хватать
        десятитысячных долей токена — и цикл повторялся вхолостую.
        """
        if units < 1:
            raise ValueError("units must be at least 1")
        self._refill()
        self._tokens -= units
        if self._tokens >= 0:
            return 0.0
        return -self._tokens / self._limits.requests_per_second

    def _refill(self) -> None:
        now = self._clock.monotonic()
        elapsed = now - self._updated_at
        if elapsed <= 0:
            return
        self._updated_at = now
        self._tokens = min(
            float(self._limits.burst),
            self._tokens + elapsed * self._limits.requests_per_second,
        )
