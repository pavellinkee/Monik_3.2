"""``/stats`` показывает реальные значения, а не нули.

Регрессия: источник статистики создавался, но метрик не получал, и
команда всегда отвечала нулями. Реестр метрик — единственный источник
этих чисел (``28_OBSERVABILITY.md`` §29): второй системы счётчиков нет.
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator

import pytest

from monik.app.container import _MetricsStatsSource
from monik.config.sections.database import DatabaseConfig
from monik.infrastructure.db import Database, MigrationRunner
from monik.services.observability import FakeClock
from monik.services.observability.metrics import MetricsRegistry
from tests import factories as f
from tests.component.notifications.conftest import build_notifications


@pytest.fixture
async def database(tmp_path: pathlib.Path) -> AsyncIterator[Database]:
    instance = Database(DatabaseConfig(path=str(tmp_path / "stats.db"), busy_timeout_seconds=1.0))
    await instance.connect()
    await MigrationRunner(instance).upgrade()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(f.NOW)


@pytest.fixture
def metrics() -> MetricsRegistry:
    return MetricsRegistry()


async def test_statistics_come_from_a_real_run(
    database: Database, clock: FakeClock, metrics: MetricsRegistry
) -> None:
    """Полный путь: Level 1 → Level 2 → уведомление → ``/stats``."""
    harness = await build_notifications(database, clock, metrics=metrics)
    await harness.dispatcher.dispatch_pending()

    snapshot = _MetricsStatsSource(metrics).snapshot()

    assert snapshot.scans_completed >= 1
    assert snapshot.opportunities_created >= 1
    assert snapshot.notifications_sent >= 1
    assert snapshot.confirmations.decided >= 1


async def test_an_untouched_registry_reports_zeros(metrics: MetricsRegistry) -> None:
    """Отсутствие событий — это ноль, а не ошибка."""
    snapshot = _MetricsStatsSource(metrics).snapshot()

    assert snapshot.scans_completed == 0
    assert snapshot.confirmations.confirmation_rate is None


async def test_confirmation_rate_follows_the_recorded_statuses(
    database: Database, clock: FakeClock, metrics: MetricsRegistry
) -> None:
    """Каждая проверенная сумма попадает в свой счётчик (``CLAUDE.md`` §26-27)."""
    harness = await build_notifications(database, clock, metrics=metrics)

    confirmations = _MetricsStatsSource(metrics).snapshot().confirmations
    assert confirmations.confirmed == harness.result.confirmed_count
    assert confirmations.unconfirmed == harness.result.unconfirmed_count
    assert confirmations.partial == harness.result.partial_count


async def test_scans_are_counted_regardless_of_outcome(
    database: Database, clock: FakeClock, metrics: MetricsRegistry
) -> None:
    """Пользователь спрашивает, сколько раз сканер отработал."""
    await build_notifications(database, clock, metrics=metrics)
    before = _MetricsStatsSource(metrics).snapshot().scans_completed

    metrics.increment("level1_scans", status="failed")

    assert _MetricsStatsSource(metrics).snapshot().scans_completed == before + 1
