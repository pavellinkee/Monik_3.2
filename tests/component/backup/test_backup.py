"""Резервное копирование базы данных.

Требования — ``CLAUDE.md`` §28 (SQLite как source of truth) и
``31_DATA_RETENTION.md`` (ограниченное хранение). Копия снимается с
работающей базы, проверяется на целостность и попадает под ротацию.
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from monik.config.sections.database import DatabaseConfig
from monik.infrastructure.db import Database, MigrationRunner
from monik.infrastructure.db.backup import BACKUP_PREFIX, backup_name, verify_database_file
from monik.repositories.sqlite import SqliteMetadataRepository
from monik.services.backup import (
    BACKUP_LAST_OUTCOME_KEY,
    BACKUP_LAST_RUN_KEY,
    BackupService,
)
from monik.services.observability import FakeClock
from tests import factories as f


def _config(tmp_path: pathlib.Path, **overrides: object) -> DatabaseConfig:
    settings: dict[str, object] = {
        "path": str(tmp_path / "monik.db"),
        "busy_timeout_seconds": 1.0,
        "backup_enabled": True,
        "backup_directory": str(tmp_path / "backups"),
        "backup_retention_copies": 3,
    }
    settings.update(overrides)
    return DatabaseConfig(**settings)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(f.NOW)


@pytest.fixture
async def database(tmp_path: pathlib.Path) -> AsyncIterator[Database]:
    instance = Database(_config(tmp_path))
    await instance.connect()
    await MigrationRunner(instance).upgrade()
    try:
        yield instance
    finally:
        await instance.close()


def _service(
    tmp_path: pathlib.Path,
    database: Database,
    clock: FakeClock,
    **overrides: object,
) -> BackupService:
    return BackupService(
        _config(tmp_path, **overrides),
        database=database,
        clock=clock,
        state=SqliteMetadataRepository(database),
    )


class TestCreation:
    async def test_copy_is_created_and_readable(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        result = await _service(tmp_path, database, clock).run()

        assert result.created
        assert result.path is not None
        assert result.size_bytes > 0
        assert await verify_database_file(result.path) == "ok"

    async def test_copy_contains_the_schema(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        """Копия бесполезна, если из неё нельзя восстановиться."""
        result = await _service(tmp_path, database, clock).run()
        assert result.path is not None

        restored = Database(DatabaseConfig(path=str(result.path), busy_timeout_seconds=1.0))
        await restored.connect()
        try:
            rows = await restored.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        finally:
            await restored.close()

        assert any(row["name"] == "schema_migrations" for row in rows)

    async def test_directory_is_created_when_missing(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        service = _service(tmp_path, database, clock)
        assert service.directory is not None and not service.directory.exists()

        await service.run()

        assert service.directory.is_dir()

    async def test_name_carries_the_moment_of_the_copy(self, clock: FakeClock) -> None:
        assert backup_name(clock.now()).startswith(BACKUP_PREFIX)
        assert backup_name(clock.now()).endswith(".db")


class TestContent:
    async def test_only_the_database_is_copied(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        """В копию не попадают окружение, репозиторий, логи и секреты."""
        for noise in (".venv", ".git", "logs", "__pycache__"):
            (tmp_path / noise).mkdir()
            (tmp_path / noise / "file.txt").write_text("noise")
        (tmp_path / ".env").write_text("MONIK_ZEROX_API_KEY=secret")

        service = _service(tmp_path, database, clock)
        await service.run()

        directory = service.directory
        assert directory is not None
        stored = sorted(item.name for item in directory.iterdir())
        assert stored == [backup_name(clock.now())]


class TestRotation:
    async def test_old_copies_are_removed(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        service = _service(tmp_path, database, clock, backup_retention_copies=3)

        for _ in range(5):
            await service.run()
            clock.advance(timedelta(days=7))

        assert len(service.copies()) == 3

    async def test_the_newest_copies_survive(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        service = _service(tmp_path, database, clock, backup_retention_copies=2)

        names = []
        for _ in range(4):
            names.append(backup_name(clock.now()))
            await service.run()
            clock.advance(timedelta(days=7))

        assert [item.name for item in service.copies()] == names[-1:-3:-1]

    async def test_a_manual_copy_falls_under_the_same_rotation(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        """``scripts/backup_db.py`` кладёт копии рядом и с тем же именем."""
        service = _service(tmp_path, database, clock, backup_retention_copies=1)
        directory = service.directory
        assert directory is not None
        directory.mkdir(parents=True)
        (directory / f"{BACKUP_PREFIX}20200101T000000Z.db").write_bytes(b"manual")

        result = await service.run()

        assert result.removed == 1
        assert len(service.copies()) == 1


class TestState:
    async def test_outcome_survives_a_restart(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        await _service(tmp_path, database, clock).run()

        metadata = SqliteMetadataRepository(database)
        assert await metadata.get(BACKUP_LAST_OUTCOME_KEY) == "success"
        assert await metadata.get(BACKUP_LAST_RUN_KEY) == clock.now().isoformat()

    async def test_failure_is_reported_and_does_not_raise(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        """Сбой обслуживающей задачи не должен останавливать сканер."""
        # Каталог занят файлом: создать его невозможно.
        occupied = tmp_path / "occupied"
        occupied.write_text("not a directory")
        service = _service(tmp_path, database, clock, backup_directory=str(occupied))

        result = await service.run()

        assert not result.created
        assert result.detail is not None
        outcome = await SqliteMetadataRepository(database).get(BACKUP_LAST_OUTCOME_KEY)
        assert outcome is not None and outcome.startswith("failed (")

    async def test_a_corrupted_copy_is_not_kept(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        """Повреждённый файл не должен выдаваться за резервную копию."""
        service = _service(tmp_path, database, clock)
        directory = service.directory
        assert directory is not None
        directory.mkdir(parents=True)
        target = directory / backup_name(clock.now())

        async def corrupt(_: pathlib.Path) -> None:
            target.write_bytes(b"this is not a database")

        service._copy = corrupt  # type: ignore[method-assign]

        result = await service.run()

        assert not result.created
        assert not target.exists()


class TestDisabled:
    async def test_nothing_happens_when_switched_off(
        self, tmp_path: pathlib.Path, database: Database, clock: FakeClock
    ) -> None:
        service = _service(tmp_path, database, clock, backup_enabled=False)

        result = await service.run()

        assert not result.created
        assert service.copies() == ()
