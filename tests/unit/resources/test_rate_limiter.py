"""Token bucket очереди провайдера.

Ограничение частоты — единственный механизм, удерживающий нагрузку в
пределах настроенных ``requests_per_second``
(``05_RESOURCE_MANAGER.md`` §55-56).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from monik.services.observability import FakeClock
from monik.services.resources import ResourceLimits
from monik.services.resources.limits import RateLimiter
from tests import factories as f


def _limiter(*, requests_per_second: float = 4.9, burst: int = 4) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock(f.NOW)
    limits = ResourceLimits(max_concurrent=4, requests_per_second=requests_per_second, burst=burst)
    return RateLimiter(limits, clock), clock


class TestReservation:
    def test_burst_is_available_immediately(self) -> None:
        limiter, _ = _limiter(burst=4)
        assert [limiter.reserve() for _ in range(4)] == [0.0, 0.0, 0.0, 0.0]

    def test_request_beyond_burst_waits(self) -> None:
        limiter, _ = _limiter(requests_per_second=5.0, burst=5)
        for _ in range(5):
            limiter.reserve()
        assert limiter.reserve() == pytest.approx(0.2)

    def test_waiting_requests_do_not_share_one_slot(self) -> None:
        """Каждый ожидающий получает собственное время (05 §17).

        Прежний цикл «подождать и попробовать снова» будил всех сразу, и
        порядок очереди терялся: побеждал тот, кого раньше запустил
        планировщик, а не тот, кто раньше встал в очередь.
        """
        limiter, _ = _limiter(requests_per_second=5.0, burst=1)
        limiter.reserve()
        delays = [limiter.reserve() for _ in range(3)]
        assert delays == pytest.approx([0.2, 0.4, 0.6])

    def test_tokens_refill_over_time(self) -> None:
        limiter, clock = _limiter(requests_per_second=5.0, burst=5)
        for _ in range(5):
            limiter.reserve()
        clock.advance(timedelta(seconds=1))
        assert [limiter.reserve() for _ in range(5)] == [0.0] * 5

    def test_idle_time_does_not_accumulate_beyond_burst(self) -> None:
        limiter, clock = _limiter(requests_per_second=5.0, burst=5)
        clock.advance(timedelta(seconds=60))
        for _ in range(5):
            assert limiter.reserve() == 0.0
        assert limiter.reserve() > 0.0

    def test_batch_costs_more_than_one_request(self) -> None:
        limiter, _ = _limiter(requests_per_second=5.0, burst=5)
        assert limiter.reserve(units=5) == 0.0
        assert limiter.reserve(units=1) == pytest.approx(0.2)

    def test_zero_units_is_rejected(self) -> None:
        limiter, _ = _limiter()
        with pytest.raises(ValueError, match="at least 1"):
            limiter.reserve(units=0)


class TestSteadyRate:
    """Регрессия: ожидание не должно вырождаться в холостой цикл."""

    def test_configured_rate_is_held_without_spinning(self) -> None:
        """Ровно ``requests_per_second`` после исчерпания запаса.

        Дробная частота вроде 4.9 даёт остаток вещественной арифметики:
        после ожидания токенов не хватало на 10⁻⁶, и запрос ждал снова.
        На управляемых часах, где такая пауза округляется до нуля, это
        был бесконечный цикл.
        """
        limiter, clock = _limiter(requests_per_second=4.9, burst=4)
        for _ in range(4):
            assert limiter.reserve() == 0.0

        waits = 0
        for _ in range(49):
            delay = limiter.reserve()
            if delay > 0:
                waits += 1
                clock.advance(timedelta(seconds=delay))
            assert delay < 1.0

        # Каждый запрос сверх запаса подождал ровно один раз.
        assert waits == 49
        assert clock.monotonic() == pytest.approx(49 / 4.9, abs=1e-6)
