"""
Отправка push-уведомлений в Firebase Cloud Messaging (HTTP v1).

Почему это отдельный слой, а не код в `consumers.py`: обращение к FCM —
синхронный HTTP-вызов на 100–300 мс за устройство, а вызывающий его код —
асинхронный consumer, который должен уйти в `group_send` и забыть. Поэтому тяжёлая
часть уезжает в `ThreadPoolExecutor`, а из async-кода вызывается только
`dispatch_message_push()`.

Ключевое ограничение, которое надо учитывать при правке: **push шлётся только тем,
у кого нет активного WebSocket-канала** (presence-хеш `messenger:presence`).
Presence считает соединения пользователя, а не конкретного устройства, поэтому
открытая веб-вкладка подавляет push на Android — это осознанный компромисс вместо
двойной доставки, описанный в README.
"""

import base64
import datetime
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import connections

from .models import Chat, DeviceToken, Message

logger = logging.getLogger(__name__)

# Отдельное имя Firebase-приложения: `initialize_app()` без имени бьёт по
# дефолтному приложению, и любой другой код (например, будущий Firestore) не
# сможет завести свою конфигурацию.
APP_NAME = "messenger-push"

# Два потока: push не должен ни блокировать Daphne, ни занимать весь пул при
# веерной рассылке в большой группе. Очередь бесконечная — при зависшем FCM
# растёт память, но не деградация REST/WS.
MAX_WORKERS = 2

# Multicast принимает не больше 500 токенов за вызов
MAX_TOKENS_PER_SEND = 500

BODY_LIMIT = 180

PREVIEW_SPACES = " \t\r\n\u00a0"

_init_lock = threading.Lock()
_submit_lock = threading.Lock()

_app = None
_init_error = None
_executor = None


class PushNotConfigured(RuntimeError):
    """Push включён в настройках, но креденшел не прочитан/невалиден."""


def push_enabled() -> bool:
    return bool(settings.FCM.get("ENABLED"))


# --- Инициализация Firebase ---------------------------------------------------


def _credentials_payload() -> dict:
    """
    Service account key как dict.

    JSON в env предпочтительнее файла: `credentials.Certificate(путь)` отказывает
    читать файл, доступный на чтение всем (`Firebase certs should not be readable
    by the public`), а в контейнере из bind-mount почти всегда 644.
    """
    raw = (settings.FCM.get("CREDENTIALS_JSON") or "").strip()
    if raw:
        # Значение может быть и «сырым» JSON, и base64 от него: второе удобнее
        # для .env, где многострочный JSON с кавычками не разместить.
        text = raw if raw.startswith("{") else base64.b64decode(raw).decode("utf-8")
        return json.loads(text)

    path = (settings.FCM.get("CREDENTIALS_PATH") or "").strip()
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    raise PushNotConfigured(
        "PUSH_ENABLED=1, но не задан FIREBASE_CREDENTIALS_JSON или FIREBASE_CREDENTIALS_PATH"
    )


def get_app():
    """
    Ленивая инициализация один раз на процесс.

    Провал кешируется в `_init_error`: долбить `initialize_app()` на каждое
    сообщение смысла нет, а исключение наружу отдаётся вызывающему, чтобы тот
    ответил диагностикой (см. POST /notifications/test/).
    """
    global _app, _init_error

    if _app is not None:
        return _app

    with _init_lock:
        if _app is not None:
            return _app
        if _init_error is not None:
            raise PushNotConfigured(_init_error)

        import firebase_admin
        from firebase_admin import credentials

        payload = _credentials_payload()
        # project_id берём из самого ключа, если в env он не продублирован:
        # несоответствие ключа и проекта FCM отвергает с SenderIdMismatch
        project_id = settings.FCM.get("PROJECT_ID") or payload.get("project_id", "")
        options = {"projectId": project_id} if project_id else None

        try:
            _app = firebase_admin.initialize_app(
                credentials.Certificate(payload),
                options=options,
                name=APP_NAME,
            )
        except Exception as error:  # причину отдаём наружу текстом в PushNotConfigured
            _init_error = str(error)
            logger.error("Firebase push не инициализирован: %s", _init_error)
            raise PushNotConfigured(_init_error) from error

        logger.info("Firebase push готов к работе (project=%s)", project_id or "?")
        return _app


# --- Сборка уведомления -------------------------------------------------------


def _preview(text: str) -> str:
    """
    Превью в статус-бар: одна строка, без переносов и длинных хвостов.

    Обрезка по символам, а не по байтам: лимит FCM на полезную нагрузку считают
    по байтам, но 180 символов кириллицы (720 байт в UTF-8) в 4 КБ заведомо
    влезают даже вместе с data-блоком.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= BODY_LIMIT:
        return collapsed
    return collapsed[: BODY_LIMIT - 1].rstrip() + "…"


def _display_name(user) -> str:
    return getattr(user, "full_name", "") or "Без имени"


def _channel_for(chat_type: str) -> str:
    """
    Личный чат — высокий приоритет, групповой — низкий.

    В группе сообщений больше, и все они с channel_high выжигают внимание
    пользователя; важный состав в личном чате при этом не теряется.
    """
    high_default = "mdm_messages_high"
    low_default = "mdm_messages_low"
    channels = settings.FCM
    if chat_type == Chat.ChatType.PRIVATE:
        return channels.get("CHANNEL_HIGH") or high_default
    return channels.get("CHANNEL_LOW") or low_default


def build_payloads(message, unread_count: int) -> dict:
    """
    Заготовка одного уведомления: тексты, канал, data для навигации клиента.

    Формат `data` — контракт с клиентом (Flutter-парсер push-нагрузки). Ключи
    snake_case и все значения строки: FCM переводит values в строки сам, а
    нестроковое значение (UUID, int) HTTP v1 отвергает.
    """
    chat = message.chat
    sender_name = _display_name(message.sender)
    body = _preview(message.text)
    if unread_count > 1:
        body = f"{body} · ещё {unread_count - 1}"

    if chat.type == Chat.ChatType.PRIVATE:
        title = sender_name
    else:
        title = f"{chat.name or 'Групповой чат'} · {sender_name}"

    return {
        "title": title,
        "body": body,
        "channel_id": _channel_for(chat.type),
        # tag по id сообщения: SDK на устройстве не склеивает разные сообщения,
        # и клиент может точечно отменить уведомление по этому же ключу.
        "tag": str(message.id),
        "notification_count": max(unread_count, 1),
        "data": {
            "type": "new_message",
            "chat_id": str(chat.id),
            "message_id": str(message.id),
            "sender_name": sender_name,
            "unread_count": str(unread_count),
        },
    }


def _send_multicast(tokens: list, payload: dict):
    """
    Один вызов HTTP v1 на все устройства одного получателя.

    Вынесено в отдельную функцию как шов для тестов: здесь boundary с внешним
    сервисом, дальше — только разбор BatchResponse.
    """
    from firebase_admin import messaging

    response = messaging.send_each_for_multicast(
        messaging.MulticastMessage(
            tokens=tokens,
            data=payload["data"],
            notification=messaging.Notification(
                title=payload["title"],
                body=payload["body"],
            ),
            android=messaging.AndroidConfig(
                # high: сообщение в мессенджере — не «фоновая синхронизация»,
                # и Doze не должен откладывать его на окно обслуживания.
                priority="high",
                ttl=datetime.timedelta(seconds=int(settings.FCM["TTL_SECONDS"])),
                notification=messaging.AndroidNotification(
                    channel_id=payload["channel_id"],
                    tag=payload["tag"],
                    icon=settings.FCM.get("ICON") or None,
                    color=settings.FCM.get("COLOR") or None,
                    # Звук/вибрация берутся из канала (channel_id), поэтому
                    # default_* только подтверждают это, а не задают свои.
                    default_sound=True,
                    default_vibrate_timings=True,
                    notification_count=payload.get("notification_count"),
                ),
            ),
        ),
        app=get_app(),
    )
    return response


# --- Разбор ответов FCM -------------------------------------------------------


def _is_dead_token(exception) -> bool:
    """
    Токен мёртв: устройство разлогинено в Firebase, ключ от другого проекта или
    FCM не знает такой токен. Повторять такие запросы бессмысленно — токен гасим.

    `QuotaExceededError`/`ThirdPartyAuthError`/`InternalError` сюда не входят
    нарочно: там токен жив, а проблема на стороне FCM, и деактивация лишила бы
    пользователя уведомлений после временного сбоя.

    С `INVALID_ARGUMENT` осторожничаем: тем же кодом FCM отвечает и на испорченный
    payload (regression в формате сообщения ≠ мёртвый токен). Поэтому по нему гасим
    только если текст ошибки явно про токен — иначе одна кривая правка выключила бы
    уведомления на всех устройствах разом, а вернули бы их только перерегистрацией.
    """
    from firebase_admin import exceptions, messaging

    if isinstance(
        exception, (messaging.UnregisteredError, messaging.SenderIdMismatchError)
    ):
        return True
    if isinstance(exception, exceptions.InvalidArgumentError):
        text = str(exception).lower()
        return "token" in text or "registration" in text
    return False


def deactivate_tokens(tokens: list) -> int:
    if not tokens:
        return 0
    updated = DeviceToken.objects.filter(token__in=tokens, is_active=True).update(
        is_active=False
    )
    logger.info("Деактивировано push-токенов: %d", updated)
    return updated


def _active_tokens_by_user(user_ids: list) -> dict:
    """
    Активные android-токены пользователей: `{user_id: [token, …]}`.

    Только `android`: iOS-токены (APNs) в этом проекте не настраивались, и слать
    им AndroidConfig — гарантированная ошибка на каждое устройство.
    """
    grouped: dict = {}
    rows = DeviceToken.objects.filter(
        user_id__in=user_ids,
        is_active=True,
        platform=DeviceToken.Platform.ANDROID,
    ).values_list("user_id", "token")
    for user_id, token in rows:
        grouped.setdefault(str(user_id), []).append(token)
    return grouped


# --- Публичные точки входа ----------------------------------------------------


def send_message_push(message_id: str, unread_by_uid: dict) -> dict:
    """
    Push о новом сообщении получателям, у которых нет живого WebSocket-канала.

    Синхронная — вызывается из пула потоков. Сообщение перечитывается по id, а не
    передаётся объектом: за время очереди чат могли удалить, и один `get()` с
    `select_related` дешевле, чем держать объект (и его связи) между потоками.
    """
    summary = {"sent": 0, "failed": 0, "deactivated": 0, "recipients": 0}

    message = (
        Message.objects.select_related("chat", "sender").filter(id=message_id).first()
    )
    if message is None:
        summary["reason"] = "message_gone"
        return summary

    tokens_by_uid = _active_tokens_by_user(list(unread_by_uid))
    if not tokens_by_uid:
        summary["reason"] = "no_tokens"
        return summary

    for user_id, tokens in tokens_by_uid.items():
        summary["recipients"] += 1
        payload = build_payloads(message, int(unread_by_uid.get(user_id) or 1))
        try:
            response = _send_multicast(tokens[:MAX_TOKENS_PER_SEND], payload)
        except PushNotConfigured:
            summary["reason"] = "not_configured"
            return summary
        except Exception as error:  # noqa: BLE001 - сбой push не должен ронять job
            summary["failed"] += len(tokens)
            logger.warning("FCM multicast для %s не выполнен: %s", user_id, error)
            continue

        dead = []
        for index, item in enumerate(response.responses):
            if item.success:
                summary["sent"] += 1
                continue
            summary["failed"] += 1
            if item.exception is not None and _is_dead_token(item.exception):
                dead.append(tokens[index])
            else:
                logger.warning(
                    "FCM отклонил токен %s…: %s",
                    tokens[index][:12],
                    item.exception,
                )
        if dead:
            summary["deactivated"] += deactivate_tokens(dead)

    return summary


def send_test_push(user_id: str, title: str = "", body: str = "") -> dict:
    """
    Тестовое уведомление на активные устройства пользователя.

    Нужен не «погонять Firebase Console», а проверить свой пайплайн: маршрута,
    канал и presence-гейт — без ручной сборки сообщения. Идёт в канал
    `mdm_messages_high`, с data-типом `test` (клиент на него не строит навигацию).
    """
    summary = {"sent": 0, "failed": 0, "deactivated": 0}

    tokens_by_uid = _active_tokens_by_user([user_id])
    tokens = tokens_by_uid.get(str(user_id), [])
    if not tokens:
        summary["reason"] = "no_tokens"
        return summary

    payload = {
        "title": title or "Lost Dream Messenger",
        "body": body or "Тестовое уведомление: канал и права работают",
        "channel_id": settings.FCM.get("CHANNEL_HIGH") or "mdm_messages_high",
        "tag": f"test-{datetime.datetime.now(tz=datetime.UTC).timestamp():.0f}",
        "notification_count": 1,
        "data": {"type": "test"},
    }
    try:
        response = _send_multicast(tokens[:MAX_TOKENS_PER_SEND], payload)
    except PushNotConfigured:
        raise
    except Exception as error:
        raise RuntimeError(f"FCM не ответил: {error}") from error

    dead = []
    for index, item in enumerate(response.responses):
        if item.success:
            summary["sent"] += 1
        else:
            summary["failed"] += 1
            if item.exception is not None and _is_dead_token(item.exception):
                dead.append(tokens[index])
    if dead:
        summary["deactivated"] = deactivate_tokens(dead)
    return summary


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _submit_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=MAX_WORKERS, thread_name_prefix="push"
                )
    return _executor


def _run_job(job, *args):
    """
    Обёртка потока: Django-соединения в чужом потоке надо закрывать самому.

    Закрываем через `connections.close_all()` в `finally`, а не
    `close_old_connections()`: второе с `CONN_MAX_AGE = 60` свежее соединение не
    трогает, поэтому воркер после задания оставлял бы живую сессию Postgres — два
    простаивающих соединения на процесс, а в тестах такая сессия ещё и мешает
    удалить тестовую базу (её догоняет страховка из `config.test_runner`).
    Цена — одно новое соединение на задание, а это один handshake на фоне
    100–300 мс самого вызова FCM.
    """
    try:
        job(*args)
    except Exception:  # job уже должен глотать своё — сюда попадает только баг
        logger.exception("Push-задача упала")
    finally:
        connections.close_all()


def schedule_job(job, *args) -> None:
    """Огнестрельно: вызывающий код не ждёт ответа FCM."""
    future = _get_executor().submit(_run_job, job, *args)
    future.add_done_callback(_log_failure)


def shutdown_executor(wait: bool = True) -> None:
    """
    Долить очередь и закрыть потоки пула; при следующем `schedule_job` пул
    создастся заново.

    Вызывается из `tearDown` тестов: потоки пула не-демонские, и воркер, успевший
    сходить в БД, держит своё соединение, пока жив поток, — задание могло бы
    пережить тест и повлиять на следующий. Процессу этот хук не нужен:
    `concurrent.futures` останавливает воркеры на выходе интерпретатора сам.
    """
    global _executor

    with _submit_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait)
        logger.info("Push-пул потоков остановлен")


def _log_failure(future) -> None:
    if future.exception() is not None:
        logger.error("Push-задача завершилась исключением: %s", future.exception())


async def dispatch_message_push(
    message_id: str,
    unread_by_uid: dict,
    online_uids,
) -> dict:
    """
    Единственная точка входа из async-кода (consumers/views).

    `online_uids` — пользователи с presence-счётчиком > 0: у них живой личный
    канал, уведомление рисует клиент, push не нужен. Возвращает, кому именно
    ушло задание (для логов и тестов); сама отправка — в пуле потоков.
    """
    if not push_enabled():
        return {"scheduled": [], "skipped_reason": "disabled"}

    online = {str(uid) for uid in online_uids}
    offline = {
        str(uid): count
        for uid, count in unread_by_uid.items()
        if str(uid) not in online
    }
    if not offline:
        return {"scheduled": [], "skipped_reason": "all_online"}

    schedule_job(send_message_push, str(message_id), offline)
    return {"scheduled": sorted(offline), "skipped_reason": None}
