"""
Test runner: освобождает тестовую базу перед её удалением.

Проблема только в teardown. `django.db.connections` — thread-local реестр, и
стандартный runner закрывает соединения того потока, в котором работает сам.
Рабочие потоки держат свои: `database_sync_to_async` считает непрочитанное в
общем потоке asgiref, а он живёт до конца процесса. Postgres не даёт удалить
базу, к которой есть хотя бы одна сессия, — отсюда
`database "test_db_messenger" is being accessed by other users`; а осколок
тестовой базы в томе затем блокирует и следующий прогон, на этапе создания.

В проде эти соединения никому не мешают: либо переиспользуются, либо догоняются
следующим `close_old_connections()` (см. `CONN_MAX_AGE`). Закрывать их в каждом
обращении к БД из WS означало бы новый handshake на каждую операцию — поэтому
чистим ровно одно место: удаление тестовой базы.
"""

import logging
import time

from django.test.runner import DiscoverRunner

logger = logging.getLogger(__name__)

# Условие «не своё»: собственный backend убивать нельзя — на нём работает этот же
# запрос, и удалять базу должен runner, а не переживать свою же сессию.
_NOT_OURS = "datname = current_database() and pid <> pg_backend_pid()"
_OTHER_PIDS_SQL = f"select pid from pg_stat_activity where {_NOT_OURS}"
_TERMINATE_SQL = (
    f"select pg_terminate_backend(pid) from pg_stat_activity where {_NOT_OURS}"
)

# `pg_terminate_backend` лишь посылает сигнал: сессия снимается асинхронно, поэтому
# DROP нужно отложить, пока список чужих сессий не опустеет.
_DRAIN_TIMEOUT_SECONDS = 5.0
_DRAIN_POLL_SECONDS = 0.05


def drain_sessions(connection, timeout: float = _DRAIN_TIMEOUT_SECONDS) -> list:
    """
    Завершить чужие сессии текущей базы и дождаться их ухода.

    Возвращает pid'ы, которые остались висеть, — пустой список в норме.
    """
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        cursor.execute(_TERMINATE_SQL)
        leftover = _other_pids(cursor)
        while leftover and time.monotonic() < deadline:
            time.sleep(_DRAIN_POLL_SECONDS)
            leftover = _other_pids(cursor)
    return leftover


def _other_pids(cursor) -> list:
    cursor.execute(_OTHER_PIDS_SQL)
    return [row[0] for row in cursor.fetchall()]


def _current_database(connection) -> str:
    with connection.cursor() as cursor:
        cursor.execute("select current_database()")
        return cursor.fetchone()[0]


class TestRunner(DiscoverRunner):
    """Стандартный runner плюс слив висящих сессий перед удалением тестовой базы."""

    def teardown_databases(self, old_config, **kwargs) -> None:
        # Форма old_config — как в django.test.utils.teardown_databases:
        # (connection, имя базы до прогона, удалять ли её).
        for connection, old_name, destroy in old_config:
            if not destroy or connection.vendor != "postgresql":
                continue
            try:
                if _current_database(connection) == old_name:
                    # Тесты идут по настоящей базе — чужие сессии не трогаем.
                    logger.warning("База %s — рабочая, сессии не освобождаем", old_name)
                    continue
                leftover = drain_sessions(connection)
            except Exception:
                # Потерянное соединение не должно хоронить отчёт о тестах.
                logger.exception("Не удалось освободить сессии базы %s", old_name)
                continue
            if leftover:
                logger.warning("База %s: сессии не закрылись: %s", old_name, leftover)
        super().teardown_databases(old_config, **kwargs)
