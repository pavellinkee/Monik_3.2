"""Снятие согласованной копии базы средствами SQLite.

Драйвер SQLite используется только в слое базы данных
(``25_PROJECT_STRUCTURE.md`` §62), поэтому низкоуровневая часть
резервного копирования живёт здесь, а политика — расписание, ротация,
состояние — в :mod:`monik.services.backup`.

Копия снимается запросом ``VACUUM INTO``: это единственный безопасный
способ скопировать работающую базу. Обычное копирование файла может
захватить незавершённую транзакцию и дать повреждённый результат.
"""

from __future__ import annotations

import pathlib
from datetime import datetime

import aiosqlite

from monik.domain.errors import DatabaseError
from monik.infrastructure.db.connection import Database

__all__ = ["BACKUP_PREFIX", "backup_name", "snapshot_database", "verify_database_file"]

#: Префикс имени файла копии. По нему находятся копии этой базы — и
#: сделанные по расписанию, и созданные вручную ``scripts/backup_db.py``.
BACKUP_PREFIX = "monik-"


def backup_name(now: datetime) -> str:
    """Имя файла резервной копии."""
    return f"{BACKUP_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}.db"


async def snapshot_database(database: Database, target: pathlib.Path) -> None:
    """Записать согласованную копию базы в ``target``.

    Существующий файл удаляется: ``VACUUM INTO`` отказывается писать
    поверх.
    """
    if target.exists():
        target.unlink()
    await database.execute("VACUUM INTO ?", (str(target),))


async def verify_database_file(path: pathlib.Path) -> str:
    """Проверить целостность файла базы.

    Возвращает результат ``PRAGMA integrity_check``: ``ok`` означает
    исправную копию. Копия, которую нельзя открыть, бесполезна, поэтому
    проверка обязательна (``16_DATABASE.md``).

    Ошибки драйвера за пределы слоя базы не выходят: повреждённый файл —
    это ``DatabaseError``, а не ``sqlite3.DatabaseError``
    (``25_PROJECT_STRUCTURE.md`` §62).
    """
    if not path.is_file() or path.stat().st_size == 0:
        raise DatabaseError(
            f"database file {path.name} does not exist or is empty",
            code="database_corrupted",
        )
    try:
        async with (
            aiosqlite.connect(path) as connection,
            connection.execute("PRAGMA integrity_check") as cursor,
        ):
            row = await cursor.fetchone()
    except (aiosqlite.Error, OSError) as exc:
        raise DatabaseError(
            f"database file {path.name} cannot be opened: {type(exc).__name__}",
            code="database_corrupted",
        ) from exc
    return str(row[0]) if row else "unknown"
