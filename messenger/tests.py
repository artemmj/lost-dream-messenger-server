"""
Тесты push-рассылки и реестра устройств.

Что проверяем и почему именно это:
  1. контракт REST (`/api/v1/devices/`): upsert, перепривязка токена между
     аккаунтами, чужой uuid даёт 404, значение токена наружу не уходит;
  2. presence-гейт: push получает только тот, у кого нет живого WebSocket-канала,
     — это главное поведение функции, и оно должно ломаться громко;
  3. формат уведомления: title/канал по типу чата, обрезка превью и то, что все
     значения `data` — строки (HTTP v1 не-строки отвергает);
  4. разбор ответов FCM: мёртвый токен гасим, временный сбой — нет;
  5. инфраструктуру прогона: пул потоков закрывается, а сессия рабочего потока не
     мешает тестовой базе быть удалённой;
  6. менеджер пользователей: `create_user` заполняет unique `username`, иначе
     второй аккаунт вне API-регистрации падает в IntegrityError.

Сам Firebase не вызывается: `_send_multicast` — шов, его подменяем.

Запуск: `docker compose exec backend python manage.py test`.
"""

import asyncio
import threading
import time
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection, connections
from django.test import (
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from rest_framework import status
from rest_framework.test import APIClient

from config import test_runner

from . import consumers, push
from .models import Chat, DeviceToken, Membership, Message, normalize_phone
from .push import PushNotConfigured

User = get_user_model()

# FCM registration token ~150 символов из латиницы, `-` `:` `.` `_`
VALID_TOKEN = "c" * 150 + "-mdm:test.token_1"
OTHER_TOKEN = "d" * 150 + "-mdm:test.token_2"

FCM_ON = {
    "ENABLED": True,
    "PROJECT_ID": "test-project",
    "CREDENTIALS_JSON": "{}",
    "CREDENTIALS_PATH": "",
    "CHANNEL_HIGH": "mdm_messages_high",
    "CHANNEL_LOW": "mdm_messages_low",
    "ICON": "ic_notification",
    "COLOR": "#4F46E5",
    "TTL_SECONDS": 14400,
}

# Троттлинг DRF считает в Redis-кэше — в тестах он не нужен ни разу, а недоступный
# Redis превратил бы любой запрос в 500. LocMem + очистка в setUp = детерминизм.
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def make_user(phone: str):
    # `username` менеджер выводит из телефона сам — см. `UserManagerTests`.
    return User.objects.create_user(phone=phone, password="pwd12345")


def make_message(recipients, chat_type=Chat.ChatType.PRIVATE, name=""):
    """Чат с отправителем `recipients[0]`, указанными участниками и одним сообщением."""
    chat = Chat.objects.create(type=chat_type, name=name)
    for user in recipients:
        Membership.objects.create(chat=chat, user=user)
    return Message.objects.create(chat=chat, sender=recipients[0], text="Привет")


def _exception(cls):
    """
    Экземпляр настоящего исключения firebase_admin без аргументов конструктора.

    Публичный `__init__` требует http_response/response, а `_is_dead_token`
    проверяет только isinstance — поэтому создаём объект в обход инициализации.
    """
    return cls.__new__(cls)


def _dead():
    from firebase_admin import messaging

    return _exception(messaging.UnregisteredError)


def _transient():
    from firebase_admin import messaging

    return _exception(messaging.QuotaExceededError)


class _Item:
    def __init__(self, success=True, exception=None):
        self.success = success
        self.exception = exception


class _BatchResponse:
    def __init__(self, items):
        self.responses = items


class _RecordingLayer:
    """Заглушка channel layer: помнит, в какие группы что уходило."""

    def __init__(self):
        self.sends = []

    async def group_send(self, group, event):
        self.sends.append((group, event))


class _FakeRedis:
    def __init__(self, presence):
        self._presence = presence

    async def hgetall(self, key):
        return self._presence


class ChatFixtureMixin:
    """
    Один чат: отправитель + участник с живым каналом + offline-участник.

    Нужен миксином, а не наследованием от TestCase: часть проверок идёт в
    TransactionTestCase (см. `PublishNewMessageTests`).
    """

    def setUp(self):
        super().setUp()
        self.sender = make_user("70000001001")
        self.online = make_user("70000001002")
        self.offline = make_user("70000001003")
        self.message = make_message([self.sender, self.online, self.offline])

    def tearDown(self):
        super().tearDown()
        # Структурная гарантия: если тест всё же пропустит задание в пул, его
        # воркер не должен пережить тест (см. `ExecutorLifecycleTests`).
        push.shutdown_executor()


# --- Реестр устройств ---------------------------------------------------------


@override_settings(CACHES=LOCMEM_CACHE)
class DeviceApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user("70000000001")
        self.other = make_user("70000000002")
        self.api = APIClient()

    def _register(self, token=VALID_TOKEN, user=None, **extra):
        if user is not None:
            self.api.force_authenticate(user=user)
        return self.api.post(
            "/api/v1/devices/", {"token": token, **extra}, format="json"
        )

    def test_register_creates_device(self):
        response = self._register(
            user=self.user, platform="android", app_version="1.0.0"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["created"], True)
        self.assertEqual(response.data["platform"], "android")

        device = DeviceToken.objects.get(token=VALID_TOKEN)
        self.assertEqual(device.user, self.user)
        self.assertTrue(device.is_active)

    def test_platform_defaults_to_android(self):
        response = self._register(user=self.user)
        self.assertEqual(response.data["platform"], "android")

    def test_repeat_registration_is_not_duplicate(self):
        self._register(user=self.user)
        response = self._register(user=self.user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["created"], False)
        self.assertEqual(DeviceToken.objects.filter(token=VALID_TOKEN).count(), 1)

    def test_other_account_rebinds_token_and_reactivates(self):
        """
        Тот же телефон, другой аккаунт.

        Без перепривязки unique на `token` отверг бы запись 400 на входе, а без
        `is_active: True` в defaults устройство осталось бы выключенным навсегда
        после одной ошибки FCM.
        """
        device = DeviceToken.objects.create(
            user=self.user, token=VALID_TOKEN, is_active=False
        )

        response = self._register(user=self.other)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["created"], False)
        device.refresh_from_db()
        self.assertEqual(device.user, self.other)
        self.assertTrue(device.is_active)
        self.assertEqual(DeviceToken.objects.count(), 1)

    def test_invalid_token_is_rejected_before_fcm(self):
        bad_tokens = ["", "short", "with space" + "x" * 150, "a" * 600]
        for token in bad_tokens:
            response = self._register(token=token, user=self.user)
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                msg=f"принят токен {token[:24]!r}",
            )
        self.assertEqual(DeviceToken.objects.count(), 0)

    def test_whitespace_padded_token_is_accepted(self):
        """Клиент мог принести токен с обрамляющими пробелами — это не ошибка."""
        response = self._register(token=f"  {VALID_TOKEN}  ", user=self.user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(DeviceToken.objects.filter(token=VALID_TOKEN).exists())

    def test_list_hides_token_value(self):
        DeviceToken.objects.create(
            user=self.user, token=VALID_TOKEN, app_version="1.2.3"
        )
        self.api.force_authenticate(user=self.user)

        response = self.api.get("/api/v1/devices/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertNotIn("token", results[0])
        self.assertEqual(results[0]["app_version"], "1.2.3")

    def test_list_excludes_other_users_devices(self):
        DeviceToken.objects.create(user=self.other, token=VALID_TOKEN)
        self.api.force_authenticate(user=self.user)
        response = self.api.get("/api/v1/devices/")
        self.assertEqual(response.data["results"], [])

    def test_destroy_foreign_device_is_404(self):
        device = DeviceToken.objects.create(user=self.user, token=VALID_TOKEN)
        self.api.force_authenticate(user=self.other)

        response = self.api.delete(f"/api/v1/devices/{device.id}/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(DeviceToken.objects.filter(pk=device.pk).exists())

    def test_destroy_own_device(self):
        device = DeviceToken.objects.create(user=self.user, token=VALID_TOKEN)
        self.api.force_authenticate(user=self.user)

        response = self.api.delete(f"/api/v1/devices/{device.id}/")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(DeviceToken.objects.filter(pk=device.pk).exists())

    def test_revoke_deletes_only_own_and_is_idempotent(self):
        DeviceToken.objects.create(user=self.user, token=VALID_TOKEN)
        DeviceToken.objects.create(user=self.other, token=OTHER_TOKEN)
        self.api.force_authenticate(user=self.user)

        for _ in range(2):
            response = self.api.post(
                "/api/v1/devices/revoke/", {"token": VALID_TOKEN}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.assertFalse(DeviceToken.objects.filter(token=VALID_TOKEN).exists())
        self.assertTrue(DeviceToken.objects.filter(token=OTHER_TOKEN).exists())

    def test_anonymous_cannot_register(self):
        response = self.api.post(
            "/api/v1/devices/", {"token": VALID_TOKEN}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


# --- Presence-гейт ------------------------------------------------------------


@override_settings(FCM=FCM_ON, CACHES=LOCMEM_CACHE)
class PresenceGateTests(ChatFixtureMixin, TestCase):
    """Push — только offline: это единственная защита от двойной доставки."""

    def test_disabled_push_schedules_nothing(self):
        with (
            override_settings(FCM={**FCM_ON, "ENABLED": False}),
            patch.object(push, "schedule_job") as scheduled,
        ):
            result = asyncio.run(
                push.dispatch_message_push(str(self.message.id), {}, set())
            )
        self.assertEqual(result["skipped_reason"], "disabled")
        self.assertEqual(result["scheduled"], [])
        self.assertFalse(scheduled.called)

    def test_only_offline_recipients_are_scheduled(self):
        unread = {str(self.online.id): 1, str(self.offline.id): 2}

        with patch.object(push, "schedule_job") as scheduled:
            result = asyncio.run(
                push.dispatch_message_push(
                    str(self.message.id), unread, {str(self.online.id)}
                )
            )

        self.assertEqual(result["scheduled"], [str(self.offline.id)])
        self.assertIsNone(result["skipped_reason"])
        job, message_id, recipients = scheduled.call_args.args
        self.assertIs(job, push.send_message_push)
        self.assertEqual(message_id, str(self.message.id))
        self.assertEqual(recipients, {str(self.offline.id): 2})

    def test_all_online_schedules_nothing(self):
        with patch.object(push, "schedule_job") as scheduled:
            result = asyncio.run(
                push.dispatch_message_push(
                    str(self.message.id),
                    {str(self.online.id): 1},
                    {str(self.online.id)},
                )
            )
        self.assertEqual(result["skipped_reason"], "all_online")
        self.assertFalse(scheduled.called)

    def test_online_uids_are_normalized_across_types(self):
        """presence-хеш отдаёт строки, а ключи unread_by_uid — str(uuid) уже или UUID."""
        with patch.object(push, "schedule_job") as scheduled:
            result = asyncio.run(
                push.dispatch_message_push(
                    str(self.message.id),
                    {str(self.offline.id): 1},
                    {self.offline.id},  # UUID, не строка
                )
            )
        self.assertEqual(result["skipped_reason"], "all_online")
        self.assertFalse(scheduled.called)


@override_settings(FCM=FCM_ON, CACHES=LOCMEM_CACHE)
class PublishNewMessageTests(ChatFixtureMixin, TransactionTestCase):
    """
    Сквозная проверка `publish_new_message` — почему это TransactionTestCase:

    счётчики непрочитанного читаются через `database_sync_to_async`, то есть в
    другом потоке и с другим соединением БД. TestCase держит фикстуры в
    незакоммиченной транзакции основного потока, и рабочий поток их не видит:
    на Postgres это пустой результат, на sqlite — `database table is locked`.
    TransactionTestCase коммитит данные, поэтому фикстуры видны обоим соединениям.
    """

    def test_publish_sends_ws_to_all_and_push_only_offline(self):
        """ws-кадр уходит всем получателям, в push — только offline."""
        layer = _RecordingLayer()
        presence = {str(self.online.id): "2", str(self.offline.id): "0", "junk": "x"}
        seen = {}

        async def fake_dispatch(message_id, unread_by_uid, online_uids):
            seen["message_id"] = message_id
            seen["unread"] = dict(unread_by_uid)
            seen["online"] = set(online_uids)
            return {"scheduled": [], "skipped_reason": "none"}

        with (
            patch.object(consumers, "get_channel_layer", lambda: layer),
            patch.object(consumers, "get_redis", lambda: _FakeRedis(presence)),
            patch.object(consumers, "dispatch_message_push", fake_dispatch),
        ):
            asyncio.run(consumers.publish_new_message(self.message))

        groups = [group for group, _event in layer.sends]
        self.assertIn(f"user_{self.online.id}", groups)
        self.assertIn(f"user_{self.offline.id}", groups)
        # «0» и мусор в presence — не онлайн
        self.assertEqual(seen["online"], {str(self.online.id)})
        self.assertEqual(
            set(seen["unread"]), {str(self.online.id), str(self.offline.id)}
        )
        self.assertEqual(seen["message_id"], str(self.message.id))

    def test_push_failure_does_not_break_ws_delivery(self):
        layer = _RecordingLayer()

        async def boom(*args, **kwargs):
            raise RuntimeError("диспетчер лёг")

        with (
            patch.object(consumers, "get_channel_layer", lambda: layer),
            patch.object(consumers, "get_redis", lambda: _FakeRedis({})),
            patch.object(consumers, "dispatch_message_push", boom),
        ):
            asyncio.run(consumers.publish_new_message(self.message))

        self.assertTrue(layer.sends)


# --- Пул потоков --------------------------------------------------------------


@override_settings(FCM=FCM_ON, CACHES=LOCMEM_CACHE)
class ExecutorLifecycleTests(SimpleTestCase):
    """
    Пул обязан закрываться явно.

    Потоки в `ThreadPoolExecutor` не-демонские, и воркер, успевший открыть
    соединение с БД, держит его, пока жив поток: без остановки пула задание
    пережило бы тест и помешало следующему. Завершение процесса этот хук не
    страхует — там воркеров останавливает сам интерпретатор.
    """

    def tearDown(self):
        super().tearDown()
        push.shutdown_executor()

    def test_submitted_job_runs_and_pool_restarts(self):
        first = threading.Event()
        push.schedule_job(first.set)
        self.assertTrue(first.wait(5), "задание не выполнилось в пуле")
        self.assertIsNotNone(push._executor)

        push.shutdown_executor()
        self.assertIsNone(push._executor, "shutdown обязан обнулить ссылку на пул")

        # После остановки пул создаётся заново — процесс живёт не один цикл.
        second = threading.Event()
        push.schedule_job(second.set)
        self.assertTrue(second.wait(5))

    def test_failing_job_does_not_kill_the_pool(self):
        """Исключение задания гасится в `_run_job`: иначе второе не выполнится."""

        def boom():
            raise RuntimeError("внутри воркера")

        after = threading.Event()
        push.schedule_job(boom)
        push.schedule_job(after.set)
        self.assertTrue(after.wait(5))

    def test_shutdown_without_pool_is_noop(self):
        push.shutdown_executor()
        self.assertIsNone(push._executor)


# --- Завершение прогона -------------------------------------------------------


def _other_session_pids() -> list:
    """Сессии текущей базы, кроме нашей — ровно те, что мешают её удалить."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select pid from pg_stat_activity "
            "where datname = current_database() and pid <> pg_backend_pid()"
        )
        return [row[0] for row in cursor.fetchall()]


@skipUnless(
    connection.vendor == "postgresql",
    "нужен pg_stat_activity; на sqlite-overlay этот сценарий не проверить",
)
class ForeignSessionCleanupTests(TransactionTestCase):
    """
    Сессию чужого потока обязан снимать test runner.

    Механизм тот же, из-за чего `manage.py test` падал на Postgres:
    `database_sync_to_async` открывает соединение в общем потоке asgiref и не
    закрывает его (`CONN_MAX_AGE = 60`), а стандартный runner закрывает соединения
    только своего потока. Postgres на DROP видит живую сессию и отказывает.
    """

    def test_drain_terminates_session_of_another_thread(self):
        gate = threading.Event()
        holder_pids = []

        def hold_session():
            # Обращение к ORM в новом потоке открывает его собственное соединение.
            with connections["default"].cursor() as cursor:
                cursor.execute("select pg_backend_pid()")
                holder_pids.append(cursor.fetchone()[0])
            gate.wait(30)

        thread = threading.Thread(target=hold_session, name="session-holder")
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not holder_pids and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(holder_pids, "поток не открыл соединение")

            pid = holder_pids[0]
            self.assertIn(pid, _other_session_pids(), "сессия потока не видна")

            self.assertEqual(test_runner.drain_sessions(connection), [])
            self.assertNotIn(pid, _other_session_pids(), "сессия осталась висеть")
        finally:
            gate.set()
            thread.join(10)


# --- Формат уведомления -------------------------------------------------------


@override_settings(FCM=FCM_ON, CACHES=LOCMEM_CACHE)
class PushPayloadTests(TestCase):
    def setUp(self):
        self.sender = make_user("70000002001")
        self.recipient = make_user("70000002002")

    def test_private_chat_uses_sender_name_and_high_channel(self):
        message = make_message([self.sender, self.recipient])
        payload = push.build_payloads(message, 1)
        self.assertEqual(payload["title"], self.sender.full_name)
        self.assertEqual(payload["channel_id"], "mdm_messages_high")
        self.assertEqual(payload["body"], "Привет")

    def test_group_chat_uses_name_and_low_channel(self):
        message = make_message(
            [self.sender, self.recipient], chat_type=Chat.ChatType.GROUP, name="Команда"
        )
        payload = push.build_payloads(message, 1)
        self.assertEqual(payload["title"], f"Команда · {self.sender.full_name}")
        self.assertEqual(payload["channel_id"], "mdm_messages_low")

    def test_group_without_name_falls_back(self):
        message = make_message(
            [self.sender, self.recipient], chat_type=Chat.ChatType.GROUP, name=""
        )
        self.assertIn("Групповой чат", push.build_payloads(message, 1)["title"])

    def test_unread_count_appends_to_body(self):
        message = make_message([self.sender, self.recipient])
        payload = push.build_payloads(message, 4)
        self.assertTrue(payload["body"].endswith("· ещё 3"))
        self.assertEqual(payload["notification_count"], 4)

    def test_long_text_is_collapsed_and_truncated(self):
        message = make_message([self.sender, self.recipient])
        message.text = "а\nб " * 200
        body = push.build_payloads(message, 1)["body"]
        self.assertLessEqual(len(body), push.BODY_LIMIT)
        self.assertNotIn("\n", body)
        self.assertTrue(body.endswith("…"))

    def test_data_values_are_all_strings(self):
        """HTTP v1 принимает в data только строки: UUID/int уронят отправку."""
        message = make_message([self.sender, self.recipient])
        payload = push.build_payloads(message, 2)
        for key, value in payload["data"].items():
            self.assertIsInstance(value, str, msg=f"data['{key}'] не строка")
        self.assertEqual(payload["data"]["type"], "new_message")
        self.assertEqual(payload["data"]["chat_id"], str(message.chat_id))
        self.assertEqual(payload["data"]["message_id"], str(message.id))
        # tag = id сообщения: клиент точечно отменяет уведомление по нему же
        self.assertEqual(payload["tag"], str(message.id))

    def test_android_config_shape(self):
        """Проверка, что AndroidConfig/AndroidNotification собираются без ошибок."""
        message = make_message([self.sender, self.recipient])
        payload = push.build_payloads(message, 1)

        with patch("firebase_admin.messaging.send_each_for_multicast") as send:
            send.return_value = _BatchResponse([_Item(success=True)])
            with patch.object(push, "get_app", lambda: object()):
                push._send_multicast([VALID_TOKEN], payload)

        multicasted = send.call_args.args[0]
        android = multicasted.android
        self.assertEqual(android.notification.channel_id, "mdm_messages_high")
        self.assertEqual(android.notification.tag, str(message.id))
        self.assertEqual(android.priority, "high")
        self.assertEqual(multicasted.tokens, [VALID_TOKEN])
        self.assertEqual(multicasted.data, payload["data"])


# --- Разбор ответов FCM -------------------------------------------------------


@override_settings(FCM=FCM_ON, CACHES=LOCMEM_CACHE)
class SendResultsTests(TestCase):
    def setUp(self):
        self.sender = make_user("70000003001")
        self.recipient = make_user("70000003002")
        self.message = make_message([self.sender, self.recipient])
        self.device = DeviceToken.objects.create(user=self.recipient, token=VALID_TOKEN)

    def _run(self, multicast):
        with patch.object(push, "_send_multicast", side_effect=multicast) as send:
            summary = push.send_message_push(
                str(self.message.id), {str(self.recipient.id): 1}
            )
        return summary, send

    def test_dead_token_deactivated_transient_kept(self):
        """
        Мёртвый токен гасим, «квоту» — нет: деактивация после временного сбоя
        лишила бы пользователя уведомлений навсегда.

        Ошибка привязывается к позиции в переданном списке токенов, а не к
        конкретному устройству: порядок строк задаёт `Meta.ordering`, и тест с
        захардкоженным «первый токен — мой» падал бы при смене сортировки.
        """
        second = DeviceToken.objects.create(user=self.recipient, token=OTHER_TOKEN)

        def multicast(tokens, payload):
            return _BatchResponse(
                [_Item(success=False, exception=_dead())]
                + [_Item(success=False, exception=_transient()) for _ in tokens[1:]]
            )

        summary, send = self._run(multicast)
        dead_token = send.call_args.args[0][0]

        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["deactivated"], 1)
        for device in (self.device, second):
            device.refresh_from_db()
            self.assertEqual(device.is_active, device.token != dead_token)

    def test_all_success(self):
        def multicast(tokens, payload):
            return _BatchResponse([_Item(success=True) for _ in tokens])

        summary, _ = self._run(multicast)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["deactivated"], 0)

    def test_message_deleted_before_job_skips(self):
        # `delete()` обнуляет PK экземпляра, поэтому id берём до удаления.
        message_id = str(self.message.id)
        self.message.delete()
        with patch.object(push, "_send_multicast") as send:
            summary = push.send_message_push(message_id, {str(self.recipient.id): 1})
        self.assertEqual(summary["reason"], "message_gone")
        self.assertFalse(send.called)

    def test_recipient_without_tokens_skips(self):
        DeviceToken.objects.all().delete()
        summary, send = self._run(lambda tokens, payload: None)
        self.assertEqual(summary["reason"], "no_tokens")
        self.assertFalse(send.called)

    def test_ios_tokens_are_not_used(self):
        """APNs не настраивался: слать iOS-токен с AndroidConfig — гарантированная ошибка."""
        DeviceToken.objects.update(platform=DeviceToken.Platform.IOS)
        summary, send = self._run(lambda tokens, payload: None)
        self.assertEqual(summary["reason"], "no_tokens")
        self.assertFalse(send.called)

    def test_inactive_tokens_are_not_used(self):
        DeviceToken.objects.update(is_active=False)
        summary, send = self._run(lambda tokens, payload: None)
        self.assertEqual(summary["reason"], "no_tokens")
        self.assertFalse(send.called)

    def test_transport_failure_does_not_deactivate(self):
        def multicast(tokens, payload):
            raise RuntimeError("FCM недоступен")

        summary, _ = self._run(multicast)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["deactivated"], 0)
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_active)

    def test_not_configured_returns_reason_without_raising(self):
        def multicast(tokens, payload):
            raise PushNotConfigured("нет ключа")

        summary, _ = self._run(multicast)
        self.assertEqual(summary["reason"], "not_configured")
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_active)


# --- Классификация ошибок FCM -------------------------------------------------


class DeadTokenClassificationTests(SimpleTestCase):
    """
    Что считается мёртвым токеном. `messaging` не переэкспортирует
    InvalidArgumentError (он живёт в `firebase_admin.exceptions`), а в 7.7.0 его
    конструктор принимает сообщение — поэтому здесь настоящие экземпляры.
    """

    def _invalid_argument(self, message):
        from firebase_admin import exceptions

        return exceptions.InvalidArgumentError(message)

    def test_unregistered_and_sender_mismatch_are_dead(self):
        from firebase_admin import messaging

        self.assertTrue(push._is_dead_token(_dead()))
        self.assertTrue(
            push._is_dead_token(_exception(messaging.SenderIdMismatchError))
        )

    def test_invalid_argument_about_token_is_dead(self):
        self.assertTrue(
            push._is_dead_token(
                self._invalid_argument("Registration token is not valid")
            )
        )

    def test_invalid_argument_about_payload_keeps_token(self):
        """
        Тот же код приходит на испорченный payload. Гасить по нему токен — значит
        выключить уведомления всем устройствам из-за одной ошибки в формате.
        """
        self.assertFalse(
            push._is_dead_token(
                self._invalid_argument("android.notification: unknown field")
            )
        )

    def test_transient_and_programming_errors_keep_token(self):
        from firebase_admin import messaging

        self.assertFalse(push._is_dead_token(_transient()))
        self.assertFalse(push._is_dead_token(_exception(messaging.ThirdPartyAuthError)))
        self.assertFalse(push._is_dead_token(RuntimeError("сбой сети")))
        self.assertFalse(push._is_dead_token(None))


# --- POST /api/v1/notifications/test/ ----------------------------------------


@override_settings(FCM=FCM_ON, CACHES=LOCMEM_CACHE)
class PushTestEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user("70000004001")
        DeviceToken.objects.create(user=self.user, token=VALID_TOKEN)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_returns_sent_count(self):
        with patch.object(
            push,
            "_send_multicast",
            side_effect=lambda tokens, payload: _BatchResponse([_Item(success=True)]),
        ):
            response = self.api.post("/api/v1/notifications/test/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["sent"], 1)

    def test_custom_text_reaches_payload(self):
        captured = {}

        def multicast(tokens, payload):
            captured.update(payload)
            return _BatchResponse([_Item(success=True)])

        with patch.object(push, "_send_multicast", side_effect=multicast):
            response = self.api.post(
                "/api/v1/notifications/test/",
                {"title": "Проверка", "body": "Канал работает"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(captured["title"], "Проверка")
        self.assertEqual(captured["data"], {"type": "test"})

    def test_503_when_push_disabled(self):
        with override_settings(FCM={**FCM_ON, "ENABLED": False}):
            response = self.api.post("/api/v1/notifications/test/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_503_when_credentials_broken(self):
        def multicast(tokens, payload):
            raise PushNotConfigured("ключ не читается")

        with patch.object(push, "_send_multicast", side_effect=multicast):
            response = self.api.post("/api/v1/notifications/test/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("ключ не читается", response.data["detail"])

    def test_502_when_fcm_unreachable(self):
        def multicast(tokens, payload):
            raise RuntimeError("FCM не ответил")

        with patch.object(push, "_send_multicast", side_effect=multicast):
            response = self.api.post("/api/v1/notifications/test/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_no_devices_reports_reason(self):
        DeviceToken.objects.all().delete()
        with patch.object(push, "_send_multicast", side_effect=AssertionError) as send:
            response = self.api.post("/api/v1/notifications/test/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reason"], "no_tokens")
        self.assertFalse(send.called)

    def test_anonymous_forbidden(self):
        api = APIClient()
        response = api.post("/api/v1/notifications/test/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


# --- Креденшел и инициализация ------------------------------------------------


class CredentialsTests(TestCase):
    def test_json_is_preferred_over_path(self):
        with override_settings(
            FCM={**FCM_ON, "CREDENTIALS_JSON": '{"project_id": "p1"}'}
        ):
            self.assertEqual(push._credentials_payload()["project_id"], "p1")

    def test_base64_json_is_decoded(self):
        import base64

        raw = base64.b64encode(b'{"project_id": "p2"}').decode()
        with override_settings(FCM={**FCM_ON, "CREDENTIALS_JSON": raw}):
            self.assertEqual(push._credentials_payload()["project_id"], "p2")

    def test_missing_credentials_raise(self):
        with (
            override_settings(
                FCM={**FCM_ON, "CREDENTIALS_JSON": "", "CREDENTIALS_PATH": ""}
            ),
            self.assertRaises(PushNotConfigured),
        ):
            push._credentials_payload()

    def test_push_disabled_is_not_an_error(self):
        with override_settings(FCM={**FCM_ON, "ENABLED": False}):
            self.assertFalse(push.push_enabled())


# --- Менеджер пользователей ---------------------------------------------------


class UserManagerTests(TestCase):
    """
    `create_user` обязан задавать `username`.

    `username` у AbstractUser unique и без значения по умолчанию: менеджер,
    который его не заполняет, оставляет пустую строку, и второй вызов падает
    IntegrityError'ом. Регистрация через API этого не замечала (`username`
    подставляет сериалайзер), а админка, shell и фикстуры упёрлись бы сразу.
    """

    def test_second_user_without_username_is_created(self):
        first = User.objects.create_user(phone="70000009001", password="pwd12345")
        second = User.objects.create_user(phone="70000009002", password="pwd12345")
        self.assertEqual(first.username, "70000009001")
        self.assertEqual(second.username, "70000009002")

    def test_username_repeats_canonical_phone(self):
        raw = "+7 (000) 000-09-03"
        user = User.objects.create_user(phone=raw, password="pwd12345")
        # `User.save()` канонит телефон, поэтому и `username` обязан быть в той же
        # форме: «+7 (000) …» не совпал бы с логином.
        self.assertEqual(user.phone, normalize_phone(raw))
        self.assertEqual(user.username, user.phone)

    def test_explicit_username_is_kept(self):
        user = User.objects.create_user(
            phone="70000009004", password="pwd12345", username=" alice "
        )
        self.assertEqual(user.username, "alice")

    def test_superuser_gets_username_too(self):
        admin = User.objects.create_superuser(phone="70000009005", password="pwd12345")
        self.assertEqual(admin.username, "70000009005")
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
