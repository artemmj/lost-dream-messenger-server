import logging

import redis.asyncio as aioredis
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from .activity import touch_last_seen
from .models import Membership, Message
from .push import dispatch_message_push
from .ratelimit import (
    CONNECT_LIMITER,
    MAX_CONNECTIONS_PER_USER,
    MESSAGE_LIMITER,
)
from .readstate import unread_counts_per_user
from .ws_auth import get_user_from_scope

User = get_user_model()

logger = logging.getLogger(__name__)

PRESENCE_KEY = "messenger:presence"

_redis_client = None


def get_redis() -> aioredis.Redis:
    """Ленивый singleton Redis-клиента для presence-реестра."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            f"redis://{settings.REDIS_HOST}:6379/0",
            decode_responses=True,
        )
    return _redis_client


def _connection_count(value) -> int:
    """Presence-значение как число: мусор в хеше не должен ронять рассылку."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def online_user_ids() -> set:
    """
    Пользователи с активным личным каналом — тем push не нужен.

    Тот же presence-хеш, что даёт `initial_presence` и cap соединений, поэтому
    отдельного учёта не заводим. Сбой Redis отдаёт пустой набор сознательно:
    «никто не онлайн» означает доставить push всем (клиент отфильтрует дубликат по
    id сообщения), а «все онлайн» означало бы молчание у всех.
    """
    try:
        presence = await get_redis().hgetall(PRESENCE_KEY)
    except Exception as error:  # noqa: BLE001 - граница с внешним сервисом
        logger.warning("Presence недоступен, push уйдёт всем: %s", error)
        return set()
    return {uid for uid, count in presence.items() if _connection_count(count) > 0}


def message_payload(msg: Message) -> dict:
    """Единый формат сообщения для WS-рассылки (используется consumer'ом и REST)."""
    return {
        "id": str(msg.id),
        "chat": str(msg.chat_id),
        "sender": {
            "id": str(msg.sender.id),
            "phone": msg.sender.phone,
            "first_name": msg.sender.first_name,
            "last_name": msg.sender.last_name,
        },
        "text": msg.text,
        "created_at": msg.created_at.isoformat(),
        "is_read": msg.is_read,
    }


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket consumer для чата.
    Подключение: ws://host/ws/chat/<uuid>/?token=<jwt>
    """

    async def connect(self):
        self.chat_id = self.scope["url_route"]["kwargs"]["chat_id"]
        self.group_name = f"chat_{self.chat_id}"
        self.user = await get_user_from_scope(self.scope)

        # Отказ анонимам
        if isinstance(self.user, AnonymousUser):
            await self.close(code=4001)
            return

        # Лимиты подключения — до accept. Счётчик presence ведёт NotificationConsumer,
        # здесь он только читается, поэтому отказ ничего не сдвигает. Redis проверяем
        # раньше запроса к БД: при reconnect-шторме проверка участия — тоже дорогая часть.
        redis_client = get_redis()
        user_key = str(self.user.id)
        if (
            await CONNECT_LIMITER.hit(redis_client, f"messenger:rl:conn:{user_key}")
            > CONNECT_LIMITER.limit
        ):
            await self.close(code=4029)
            return

        # Общий кап на активные сокеты пользователя (канал уведомлений + сокеты чатов):
        # берём из presence-хеша, отдельный счётчик не заводим. Гонка «два соединения
        # прошли кап» для ограничения неважна.
        connections = int(await redis_client.hget(PRESENCE_KEY, user_key) or 0)
        if connections >= MAX_CONNECTIONS_PER_USER:
            await self.close(code=4009)
            return

        # Проверка участия в чате
        is_member = await self._check_membership()
        if not is_member:
            await self.close(code=4003)
            return

        # Присоединяемся к группе
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # помечаем сообщения как прочитанные
        had_unread = await self._mark_messages_read()
        if had_unread:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "messages.read",
                    "reader_id": str(self.user.id),
                },
            )

        # Presence и last_seen ведёт NotificationConsumer — сокет чата открывается
        # и закрывается при выборе чата, и на его закрытии пользователь ещё в приложении.

        # Подключившемуся клиенту — снимок: кто из участников чата уже онлайн
        await self.send_json(
            {
                "type": "initial_presence",
                "user_ids": await self._online_member_ids(),
            }
        )

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        """Обработка входящего сообщения от клиента"""
        # Перепроверка участия: пользователя могли удалить из чата после connect
        if not await self._check_membership():
            await self.close(code=4003)
            return

        # Анти-флуд: считаем каждый кадр, а не только валидный текст — иначе
        # мусором лимит обходится. Дорогие части (запись в БД и broadcast всей
        # группе) ниже просто не выполняются, поэтому соединение держим.
        hits = await MESSAGE_LIMITER.hit(
            get_redis(), f"messenger:rl:msg:{self.user.id}"
        )
        if hits > MESSAGE_LIMITER.limit:
            await self.send_json(
                {"error": "Слишком много сообщений, подождите немного"}
            )
            return

        # Массив или строка вместо объекта упала бы на .get() с трейсбеком в лог
        if not isinstance(content, dict):
            await self.send_json({"error": "Некорректный формат сообщения"})
            return

        text = content.get("text", "").strip()
        if not text:
            await self.send_json({"error": "Сообщение не может быть пустым"})
            return

        if len(text) > 5000:
            await self.send_json({"error": "Сообщение слишком длинное (макс. 5000)"})
            return

        # Сохраняем в БД
        message = await self._save_message(text)

        # Отправка сообщения — реальная активность, а не только факт подключения
        await self._touch_last_seen()

        # Broadcast всем в группе
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat.message",
                "message": message_payload(message),
            },
        )

        # Уведомления участникам — в их личные группы: счётчик непрочитанного
        # у каждого свой, поэтому один broadcast на всех не подходит.
        await publish_new_message(message)

    # --- Handlers для group_send ---

    async def chat_message(self, event):
        """Получение broadcast-сообщения и отправка клиенту"""
        await self.send_json(event["message"])

    async def user_status(self, event):
        """Получение обновления статуса пользователя"""
        await self.send_json(
            {
                "type": "user_status",
                "user_id": event["user_id"],
                "status": event["status"],
            }
        )

    async def messages_read(self, event):
        """Уведомление о прочтении сообщений"""
        await self.send_json(
            {
                "type": "messages_read",
                "reader_id": event["reader_id"],
            }
        )

    async def member_removed(self, event):
        """Участник удалён из чата: закрываем сокет удалённого пользователя."""
        if event.get("user_id") == str(self.user.id):
            await self.close(code=4003)

    async def chat_deleted(self, event):
        """Чат удалён: закрываем сокеты всех участников."""
        await self.close(code=4004)

    # --- DB operations (sync → async safe) ---

    @database_sync_to_async
    def _check_membership(self):
        return Membership.objects.filter(chat_id=self.chat_id, user=self.user).exists()

    @database_sync_to_async
    def _save_message(self, text: str) -> Message:
        return Message.objects.create(chat_id=self.chat_id, sender=self.user, text=text)

    @database_sync_to_async
    def _mark_messages_read(self):
        updated = (
            Message.objects.filter(
                chat_id=self.chat_id,
                is_read=False,
            )
            .exclude(sender=self.user)
            .update(is_read=True)
        )
        return updated > 0

    @database_sync_to_async
    def _touch_last_seen(self):
        touch_last_seen(self.user)

    @database_sync_to_async
    def _member_ids(self) -> list:
        return [
            str(uid)
            for uid in Membership.objects.filter(chat_id=self.chat_id).values_list(
                "user_id", flat=True
            )
        ]

    async def _online_member_ids(self) -> list:
        presence = await get_redis().hgetall(PRESENCE_KEY)
        members = await self._member_ids()
        return [uid for uid in members if int(presence.get(uid, 0)) > 0]


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """
    Личный канал пользователя: ws://host/ws/notifications/?token=<jwt>

    Нужен потому, что сокет чата живёт только пока чат открыт: без этого канала
    сообщение в другой чат не о чём доставлять, пока пользователь его не выбрал.
    Здесь же ведётся presence и last_seen — этот сокет открыт всё время, пока
    приложение запущено, поэтому закрытие чата (Esc/крестик) не «выключает»
    пользователя.
    """

    async def connect(self):
        self.user = await get_user_from_scope(self.scope)

        if isinstance(self.user, AnonymousUser):
            await self.close(code=4001)
            return

        # Те же лимиты и тот же бюджет подключений, что у сокета чата: кап на
        # соединения считается по presence-хешу, а его заполняют именно здесь.
        redis_client = get_redis()
        user_key = str(self.user.id)
        if (
            await CONNECT_LIMITER.hit(redis_client, f"messenger:rl:conn:{user_key}")
            > CONNECT_LIMITER.limit
        ):
            await self.close(code=4029)
            return

        connections = int(await redis_client.hget(PRESENCE_KEY, user_key) or 0)
        if connections >= MAX_CONNECTIONS_PER_USER:
            await self.close(code=4009)
            return

        self.group_name = f"user_{user_key}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        await self._update_last_seen()

        # Online анонсируется только при первом соединении (несколько вкладок/
        # устройств одного пользователя).
        if await self._presence_enter() == 1:
            await self._announce_status("online")

    async def disconnect(self, close_code):
        # Offline — только когда закрылось последнее соединение пользователя.
        # Если отказали до accept, счётчик не трогался и _presence_leave вернёт 1.
        if await self._presence_leave() == 0:
            await self._update_last_seen()
            await self._announce_status("offline")

        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, *args, **kwargs):
        """Клиент здесь ничего не отправляет: канал односторонний."""

    # --- Handlers для group_send ---

    async def new_message(self, event):
        await self.send_json(
            {
                "type": "new_message",
                "chat": event["chat"],
                "message": event["message"],
                "unread_count": event["unread_count"],
            }
        )

    async def chat_read(self, event):
        """Чат прочитан в другой вкладке/устройстве — сбрасываем бейдж и здесь."""
        await self.send_json({"type": "chat_read", "chat": event["chat"]})

    async def messages_read(self, event):
        await self.send_json({"type": "messages_read", "reader_id": event["reader_id"]})

    async def chat_deleted(self, event):
        await self.send_json({"type": "chat_deleted", "chat": event["chat_id"]})

    async def chat_renamed(self, event):
        """Название изменили — обновляем заголовок, даже когда сокет чата закрыт."""
        await self.send_json(
            {
                "type": "chat_renamed",
                "chat": event["chat_id"],
                "name": event["name"],
            }
        )

    async def member_removed(self, event):
        await self.send_json({"type": "member_removed", "chat": event["chat_id"]})

    # --- Presence и активность ---

    async def _presence_enter(self) -> int:
        self._presence_entered = True
        count = await get_redis().hincrby(PRESENCE_KEY, str(self.user.id), 1)
        return int(count)

    async def _presence_leave(self) -> int:
        # Отказ до accept (4001/4009/4029): счётчик не поднимали — и не опускаем,
        # иначе такое соединение «выключит» пользователя, который его не включал.
        if not getattr(self, "_presence_entered", False):
            return 1
        uid = str(self.user.id)
        r = get_redis()
        count = int(await r.hincrby(PRESENCE_KEY, uid, -1))
        if count <= 0:
            await r.hdel(PRESENCE_KEY, uid)
            return 0
        return count

    @database_sync_to_async
    def _chat_group_names(self) -> list:
        return [
            f"chat_{cid}"
            for cid in Membership.objects.filter(user=self.user).values_list(
                "chat_id", flat=True
            )
        ]

    async def _announce_status(self, status: str):
        """
        Статус пользователя — в группы его чатов: точка онлайн рисуется в шапке
        личного чата тем, кто в нём состоит.
        """
        for group in await self._chat_group_names():
            await self.channel_layer.group_send(
                group,
                {"type": "user.status", "user_id": str(self.user.id), "status": status},
            )

    @database_sync_to_async
    def _update_last_seen(self):
        now = timezone.now()
        User.objects.filter(id=self.user.id).update(last_seen=now)
        # Синхронизируем кэш в памяти: иначе троттлинг в touch_last_seen
        # не сработает и каждое соединение будет дёргать БД впустую
        self.user.last_seen = now


async def publish_new_message(message: Message) -> None:
    """
    Уведомление о новом сообщении — каждому получателю в его личную группу.
    Счётчик непрочитанного серверный: клиенту не нужно хранить свой курсор.

    Здесь же подключается push: тем, до кого живой канал не дотянулся. Порядок
    «presence → group_send → push» выбран нарочно: если участник отключится, пока
    мы рассылаем, он получит и ws-кадр в уже мёртвый сокет, и push. Дубликат
    дешевле, чем молчание, а клиент дедуплицирует по id сообщения.
    """
    layer = get_channel_layer()
    payload = message_payload(message)
    counts = await recipient_unread(message)
    online = await online_user_ids()

    for uid, unread in counts.items():
        await layer.group_send(
            f"user_{uid}",
            {
                "type": "new.message",
                "chat": payload["chat"],
                "message": payload,
                "unread_count": unread,
            },
        )

    try:
        await dispatch_message_push(str(message.id), counts, online)
    except Exception as error:  # noqa: BLE001 - push не должен ломать ws-рассылку
        logger.warning("Push-диспетчер не отработал: %s", error)


@database_sync_to_async
def recipient_unread(message: Message) -> dict:
    return unread_counts_per_user(message.chat_id, exclude_user_id=message.sender_id)


def publish_to_users(user_ids, event: dict) -> None:
    """
    Синхронная веерная отправка в личные группы — для REST-вьюх (там channel layer
    доступна только через async_to_sync).
    """
    layer = get_channel_layer()
    for uid in user_ids:
        async_to_sync(layer.group_send)(f"user_{uid}", event)
