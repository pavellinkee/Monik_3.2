"""Резервное копирование базы данных.

SQLite является source of truth восстановимого состояния
(``CLAUDE.md`` §28), поэтому копируется именно она — и только она.
Виртуальное окружение, репозиторий, кэши, логи и файлы с секретами в
копию не попадают: они либо восстанавливаются установкой, либо не должны
покидать своё место (``22_SECURITY.md``).

Само снятие копии и проверка её целостности выполняются слоем базы
данных (:mod:`monik.infrastructure.db.backup`): драйвер SQLite за его
пределы не выходит (``25_PROJECT_STRUCTURE.md`` §62). Здесь остаётся
политика: когда копировать, сколько копий хранить и что показать
оператору.

Старые копии удаляются по числу хранимых: место не должно заполняться
бесконечно (``31_DATA_RETENTION.md``). Имя файла совпадает с тем, что
создаёт ``scripts/backup_db.py``, поэтому ручные и плановые копии лежат
рядом и попадают под общую ротацию.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from monik.config.sections.database import DatabaseConfig
from monik.domain.errors import DatabaseError
from monik.infrastructure.db.backup import (
    BACKUP_PREFIX,
    backup_name,
    snapshot_database,
    verify_database_file,
)
from monik.infrastructure.db.connection import Database
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields

__all__ = [
    "BACKUP_LAST_OUTCOME_KEY",
    "BACKUP_LAST_RUN_KEY",
    "BackupResult",
    "BackupService",
]

_LOGGER = get_logger("services.backup")

#: Ключи, под которыми состояние последнего запуска переживает рестарт.
BACKUP_LAST_RUN_KEY = "backup.last_run_at"
BACKUP_LAST_OUTCOME_KEY = "backup.last_outcome"


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Итог одного запуска резервного копирования."""

    created: bool
    path: pathlib.Path | None = None
    size_bytes: int = 0
    removed: int = 0
    detail: str | None = None


@runtime_checkable
class BackupStateStore(Protocol):
    """Хранилище отметок о последнем запуске (порт ``app_metadata``)."""

    async def get(self, key: str) -> str | None:
        """Значение по ключу."""
        ...

    async def set(self, key: str, value: str, *, updated_at: datetime) -> None:
        """Записать значение."""
        ...


class BackupService:
    """Создаёт и ротирует резервные копии базы."""

    def __init__(
        self,
        config: DatabaseConfig,
        *,
        database: Database,
        clock: Clock,
        state: BackupStateStore | None = None,
    ) -> None:
        self._config = config
        self._database = database
        self._clock = clock
        self._state = state

    @property
    def enabled(self) -> bool:
        """Настроено ли резервное копирование."""
        return self._config.backup_enabled and self._config.backup_directory is not None

    @property
    def directory(self) -> pathlib.Path | None:
        """Каталог копий, если он задан."""
        if self._config.backup_directory is None:
            return None
        return pathlib.Path(self._config.backup_directory)

    async def run(self) -> BackupResult:
        """Создать копию и удалить лишние.

        Ошибка копирования не поднимается наверх: резервное копирование —
        обслуживающая задача, и её сбой не должен останавливать сканер.
        Итог фиксируется в логе и в состоянии, доступном оператору.
        """
        if not self.enabled:
            return BackupResult(created=False, detail="backup is disabled")
        directory = self.directory
        if directory is None:  # pragma: no cover - защищено валидатором конфигурации
            return BackupResult(created=False, detail="backup directory is not configured")

        now = self._clock.now()
        target = directory / backup_name(now)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            await self._copy(target)
            await self._verify(target)
        except (DatabaseError, OSError) as error:
            detail = f"{type(error).__name__}: {error}"
            _LOGGER.error("database backup failed", extra=log_fields(detail=detail))
            self._discard(target)
            await self._remember(now, f"failed ({type(error).__name__})")
            return BackupResult(created=False, detail=detail)

        removed = self._rotate(directory, keep=self._config.backup_retention_copies)
        size = target.stat().st_size
        _LOGGER.info(
            "database backup created",
            extra=log_fields(size_bytes=size, removed=removed),
        )
        await self._remember(now, "success")
        return BackupResult(created=True, path=target, size_bytes=size, removed=removed)

    def copies(self) -> tuple[pathlib.Path, ...]:
        """Существующие копии, от свежих к старым."""
        directory = self.directory
        if directory is None or not directory.is_dir():
            return ()
        return tuple(
            sorted(
                (
                    item
                    for item in directory.iterdir()
                    if item.is_file() and item.name.startswith(BACKUP_PREFIX)
                ),
                reverse=True,
            )
        )

    async def last_run(self) -> tuple[str | None, str | None]:
        """Момент и итог последнего запуска."""
        if self._state is None:
            return None, None
        return (
            await self._state.get(BACKUP_LAST_RUN_KEY),
            await self._state.get(BACKUP_LAST_OUTCOME_KEY),
        )

    # --- внутреннее -------------------------------------------------------

    async def _copy(self, target: pathlib.Path) -> None:
        """Снять согласованную копию работающей базы."""
        await snapshot_database(self._database, target)

    @staticmethod
    async def _verify(target: pathlib.Path) -> None:
        """Убедиться, что копия открывается и не повреждена."""
        outcome = await verify_database_file(target)
        if outcome != "ok":
            raise DatabaseError(
                f"backup integrity check failed: {outcome}",
                code="database_corrupted",
            )

    @staticmethod
    def _discard(target: pathlib.Path) -> None:
        """Убрать незавершённую копию.

        Уборка выполняется в обработчике ошибки и сама ошибку поднять не
        должна: причина сбоя уже известна, а недоступный путь сделал бы
        сбой копирования сбоем всей задачи.
        """
        try:
            target.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - путь недоступен целиком
            pass

    def _rotate(self, directory: pathlib.Path, *, keep: int) -> int:
        """Удалить копии сверх установленного количества."""
        existing = self.copies()
        removed = 0
        for stale in existing[keep:]:
            try:
                stale.unlink()
            except OSError as error:  # pragma: no cover - гонка с внешним удалением
                _LOGGER.warning(
                    "stale backup could not be removed",
                    extra=log_fields(detail=type(error).__name__),
                )
                continue
            removed += 1
        return removed

    async def _remember(self, now: datetime, outcome: str) -> None:
        if self._state is None:
            return
        await self._state.set(BACKUP_LAST_RUN_KEY, now.isoformat(), updated_at=now)
        await self._state.set(BACKUP_LAST_OUTCOME_KEY, outcome, updated_at=now)
