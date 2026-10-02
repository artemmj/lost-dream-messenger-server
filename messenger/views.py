from uuid import UUID

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from drf_spectacular.views import SpectacularAPIView
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.mixins import CreateModelMixin, DestroyModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .consumers import message_payload, publish_new_message, publish_to_users
from .models import Chat, DeviceToken, Membership, Message, normalize_phone
from .push import PushNotConfigured, push_enabled, send_test_push
from .readstate import unread_counts
from .serializers import (
    AddMemberSerializer,
    ChatCreateSerializer,
    ChatDetailSerializer,
    ChatListSerializer,
    ChatRenameSerializer,
    DeviceRevokeSerializer,
    DeviceTokenCreateSerializer,
    DeviceTokenSerializer,
    LoginSerializer,
    MeSerializer,
    MessageCreateSerializer,
    MessageSerializer,
    PrivateChatCreateSerializer,
    ProfileUpdateSerializer,
    RegisterResponseSerializer,
    RegisterSerializer,
    RemoveMemberSerializer,
    TestPushSerializer,
    UserSerializer,
)

User = get_user_model()


class IsAuthenticated(permissions.IsAuthenticated):
    pass


class ChatViewSet(viewsets.ModelViewSet):
    """
    Управление чатами и сообщениями.

    - list: Список чатов текущего пользователя с последним сообщением
    - retrieve: Детальная информация о чате со списком участников
    - create: Создание нового чата (создатель автоматически становится админом)
    - partial_update: Переименование GROUP-чата (только админ)
    - destroy: Удаление чата (GROUP — только админ, PRIVATE — любой участник)
    - messages: История сообщений чата с пагинацией
    - send_message: Отправка текстового сообщения в чат
    """

    permission_classes = [IsAuthenticated]
    # PUT нет сознательно: PATCH /chats/{id}/ меняет только название GROUP-чата,
    # полноценного обновления чата в приложении нет
    http_method_names = ["get", "post", "patch", "delete"]

    # Лимиты на запись строже, чем на чтение; list/retrieve остаются только на
    # глобальном user-троттле (scope None → ScopedRateThrottle пропускает запрос).
    ACTION_THROTTLE_SCOPES = {
        "send_message": "send",
        "create": "write",
        "create_private": "write",
        "add_member": "write",
        "remove_member": "write",
        "destroy": "write",
        "partial_update": "write",
        "mark_read": "read",
    }

    def get_throttles(self):
        self.throttle_scope = self.ACTION_THROTTLE_SCOPES.get(self.action)
        return super().get_throttles()

    def get_queryset(self):
        # 👈 Защита от spectular fake view
        if getattr(self, "swagger_fake_view", False):
            return Chat.objects.none()

        user = self.request.user
        return (
            Chat.objects.filter(members=user)
            .prefetch_related(
                "members",
                Prefetch(
                    "messages",
                    queryset=Message.objects.order_by("-created_at")[:1],
                    to_attr="last_msg_list",
                ),
            )
            .distinct()
        )

    def get_serializer_class(self):
        if self.action == "list":
            return ChatListSerializer
        elif self.action == "create":
            return ChatCreateSerializer
        return ChatDetailSerializer

    def list(self, request, *args, **kwargs):
        """Стандартный list + счётчики непрочитанного одной выборкой на страницу."""
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        chats = list(page) if page is not None else list(queryset)

        context = {
            **self.get_serializer_context(),
            "unread_counts": unread_counts(request.user, [c.id for c in chats]),
        }
        serializer = ChatListSerializer(chats, many=True, context=context)

        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            ChatDetailSerializer(
                serializer.instance, context={"request": request}
            ).data,
            status=status.HTTP_201_CREATED,
        )

    def perform_create(self, serializer):
        member_ids = serializer.validated_data.pop("member_ids", [])
        with transaction.atomic():
            chat = serializer.save()
            Membership.objects.create(user=self.request.user, chat=chat, is_admin=True)
            others = [uid for uid in member_ids if uid != self.request.user.id]
            if others:
                Membership.objects.bulk_create(
                    [
                        Membership(chat=chat, user_id=uid, is_admin=False)
                        for uid in others
                    ],
                    ignore_conflicts=True,
                )

    @extend_schema(
        summary="Переименовать групповой чат",
        description=(
            "PATCH /chats/{id}/ меняет только название и только у GROUP-чата; "
            "права — администратор чата. Личный чат называется по имени собеседника, "
            "поэтому для него возвращается 400. Остальные участники получают "
            "chat_renamed в личный WebSocket-канал."
        ),
        request=ChatRenameSerializer,
        responses={
            200: OpenApiResponse(
                response=ChatDetailSerializer, description="Чат с новым названием"
            ),
            400: OpenApiResponse(description="Чат не групповой или название пустое"),
            403: OpenApiResponse(
                description="Недостаточно прав (требуется роль администратора)"
            ),
        },
    )
    def partial_update(self, request, *args, **kwargs):
        # get_object() ограничен queryset'ом «чаты пользователя» — не участник получит 404
        chat = self.get_object()

        if chat.type != Chat.ChatType.GROUP:
            return Response(
                {"detail": "Переименовать можно только групповой чат."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ok, error = self._check_membership(chat, request.user, require_admin=True)
        if not ok:
            return Response({"detail": error}, status=status.HTTP_403_FORBIDDEN)

        serializer = ChatRenameSerializer(chat, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data.get("name")
        # Пустой PATCH или то же название: ничего не пишем и не рассылаем
        if not name or name == chat.name:
            return Response(
                ChatDetailSerializer(chat, context={"request": request}).data
            )

        serializer.save()
        publish_to_users(
            list(chat.members.values_list("id", flat=True)),
            {"type": "chat.renamed", "chat_id": str(chat.id), "name": name},
        )
        return Response(ChatDetailSerializer(chat, context={"request": request}).data)

    @extend_schema(
        summary="Удалить чат",
        description=(
            "Удаляет чат вместе со всеми сообщениями. GROUP-чат может удалить только "
            "администратор чата, PRIVATE-чат — любой из двух участников (админов там нет). "
            "Подключённые клиенты получают закрытие WebSocket с кодом 4004."
        ),
        responses={
            204: OpenApiResponse(description="Чат удалён"),
            403: OpenApiResponse(description="Нет прав на удаление чата"),
            404: OpenApiResponse(
                description="Чат не найден или вы не являетесь участником"
            ),
        },
    )
    def destroy(self, request, *args, **kwargs):
        chat = self.get_object()

        if chat.type == Chat.ChatType.GROUP:
            ok, error = self._check_membership(chat, request.user, require_admin=True)
            if not ok:
                return Response({"detail": error}, status=status.HTTP_403_FORBIDDEN)

        chat_id = chat.id
        member_ids = list(chat.members.values_list("id", flat=True))
        chat.delete()

        channel_layer = get_channel_layer()
        event = {"type": "chat.deleted", "chat_id": str(chat_id)}
        async_to_sync(channel_layer.group_send)(f"chat_{chat_id}", event)
        # И в личные группы: бейдж непрочитанного должен исчезнуть, даже когда
        # сокет этого чата не открыт.
        publish_to_users(member_ids, event)

        return Response(status=status.HTTP_204_NO_CONTENT)

    def _check_membership(self, chat, user, require_admin=False):
        """Проверка участия и прав в чате"""
        try:
            membership = Membership.objects.get(chat=chat, user=user)
            if require_admin and not membership.is_admin:
                return False, "Недостаточно прав (требуется роль администратора)"
            return True, None
        except Membership.DoesNotExist:
            return False, "Вы не являетесь участником этого чата"

    @extend_schema(
        summary="История сообщений чата",
        description="Возвращает список сообщений конкретного чата с пагинацией (по умолчанию 50 на страницу)",
        parameters=[
            OpenApiParameter(
                name="page",
                type=int,
                required=False,
                description="Номер страницы",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=MessageSerializer(many=True),
                description="Список сообщений",
            ),
            403: OpenApiResponse(
                description="Пользователь не является участником чата"
            ),
            404: OpenApiResponse(description="Чат не найден"),
        },
    )
    @action(detail=True, methods=["get"])
    def messages(self, request, pk: UUID = None):
        chat = self.get_object()
        if not chat.members.filter(id=request.user.id).exists():
            return Response(
                {"detail": "Вы не участник этого чата"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Новые сообщения — на первой странице; внутри страницы порядок по возрастанию
        messages = chat.messages.select_related("sender").order_by("-created_at")

        page = self.paginate_queryset(messages)
        if page is not None:
            serializer = MessageSerializer(
                page[::-1], many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)

        serializer = MessageSerializer(
            messages, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @extend_schema(
        summary="Отправить сообщение",
        description="Отправляет текстовое сообщение в указанный чат. Chat и sender определяются автоматически.",
        request=MessageCreateSerializer,
        responses={
            201: OpenApiResponse(
                response=MessageSerializer,
                description="Сообщение успешно создано",
            ),
            400: OpenApiResponse(description="Ошибка валидации текста"),
            403: OpenApiResponse(
                description="Пользователь не является участником чата"
            ),
            404: OpenApiResponse(description="Чат не найден"),
        },
    )
    @action(detail=True, methods=["post"], url_path="send")
    def send_message(self, request, pk: UUID = None):
        chat = self.get_object()
        if not chat.members.filter(id=request.user.id).exists():
            return Response(
                {"detail": "Вы не участник этого чата"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = MessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message = Message.objects.create(
            chat=chat,
            sender=request.user,
            text=serializer.validated_data["text"],
        )

        # Real-time доставка подключённым WS-клиентам (тот же формат, что у ChatConsumer)
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"chat_{chat.id}",
            {"type": "chat.message", "message": message_payload(message)},
        )

        # Уведомления — в личные группы участников (своим счётчиком непрочитанного)
        async_to_sync(publish_new_message)(message)

        return Response(
            MessageSerializer(message, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Отметить чат прочитанным",
        description=(
            "Сдвигает курсор прочтения текущего участника на «сейчас» — счётчик "
            "непрочитанного в этом чате обнуляется."
        ),
        responses={
            200: OpenApiResponse(description="Курсор обновлён"),
            404: OpenApiResponse(description="Чат не найден или вы не участник"),
        },
    )
    @action(detail=True, methods=["post"], url_path="read")
    def mark_read(self, request, pk: UUID = None):
        # get_object() уже ограничен queryset'ом «чаты пользователя» — вне чата 404
        chat = self.get_object()
        Membership.objects.filter(chat=chat, user=request.user).update(
            last_read_at=timezone.now()
        )
        # Другие вкладки/устройства этого пользователя должны убрать бейдж
        publish_to_users(
            [request.user.id],
            {"type": "chat.read", "chat": str(chat.id)},
        )
        return Response({"unread_count": 0})

    @extend_schema(
        summary="Добавить участника в чат",
        description="Добавляет пользователя в групповой чат. Требуются права администратора.",
        request=AddMemberSerializer,
        responses={
            201: OpenApiResponse(description="Пользователь успешно добавлен"),
            400: OpenApiResponse(
                description="Ошибка валидации или пользователь уже в чате"
            ),
            403: OpenApiResponse(
                description="Нет прав администратора или вы не в чате"
            ),
            404: OpenApiResponse(description="Чат не найден"),
        },
    )
    @action(detail=True, methods=["post"], url_path="add-member")
    def add_member(self, request, pk: UUID = None):
        chat = self.get_object()

        # Проверка: только админ может добавлять
        ok, error = self._check_membership(chat, request.user, require_admin=True)
        if not ok:
            return Response({"detail": error}, status=status.HTTP_403_FORBIDDEN)

        # В личный чат добавлять участников нельзя (иначе ломается поиск дубликатов)
        if chat.type == Chat.ChatType.PRIVATE:
            return Response(
                {"detail": "Нельзя добавить участника в личный чат"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AddMemberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_to_add = User.objects.get(id=serializer.validated_data["user_id"])

        # Проверка: не состоит ли уже в чате
        if Membership.objects.filter(chat=chat, user=user_to_add).exists():
            return Response(
                {"detail": "Пользователь уже является участником этого чата"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        Membership.objects.create(chat=chat, user=user_to_add, is_admin=False)

        return Response(
            {"detail": f"Пользователь {user_to_add.phone} добавлен в чат"},
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Удалить участника из чата",
        description="Удаляет пользователя из чата. Админ может удалять других, обычный участник — только себя.",
        request=RemoveMemberSerializer,
        responses={
            200: OpenApiResponse(description="Пользователь удалён из чата"),
            400: OpenApiResponse(description="Нельзя удалить создателя / ошибка"),
            403: OpenApiResponse(description="Нет прав"),
            404: OpenApiResponse(description="Чат или участник не найдены"),
        },
    )
    @action(detail=True, methods=["post"], url_path="remove-member")
    def remove_member(self, request, pk: UUID = None):
        chat = self.get_object()

        serializer = RemoveMemberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_to_remove_id = serializer.validated_data["user_id"]
        is_self_leave = str(request.user.id) == str(user_to_remove_id)

        # Если удаляет другого — нужны права админа
        if not is_self_leave:
            ok, error = self._check_membership(chat, request.user, require_admin=True)
            if not ok:
                return Response({"detail": error}, status=status.HTTP_403_FORBIDDEN)

        try:
            membership = Membership.objects.get(chat=chat, user_id=user_to_remove_id)
        except Membership.DoesNotExist:
            return Response(
                {"detail": "Пользователь не найден в этом чате"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Защита: нельзя кикнуть единственного админа
        if (
            membership.is_admin
            and Membership.objects.filter(chat=chat, is_admin=True).count() == 1
        ):
            return Response(
                {"detail": "Нельзя удалить единственного администратора чата"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        membership.delete()

        # Уведомляем WS-группу: consumer удалённого пользователя закроет его сокет (4003)
        channel_layer = get_channel_layer()
        event = {
            "type": "member.removed",
            "user_id": str(user_to_remove_id),
            "chat_id": str(chat.id),
        }
        async_to_sync(channel_layer.group_send)(f"chat_{chat.id}", event)
        # И в личный канал: бейдж чата надо снять даже при закрытом сокете чата
        publish_to_users([user_to_remove_id], event)

        # Если в чате не осталось участников — удаляем сам чат
        if not Membership.objects.filter(chat=chat).exists():
            chat.delete()
            return Response({"detail": "Чат удалён, так как не осталось участников"})

        return Response({"detail": "Пользователь удалён из чата"})

    @extend_schema(
        summary="Создать или получить личный чат",
        description=(
            "Создаёт приватный чат с указанным пользователем. "
            "Если личный чат между вами уже существует — возвращает существующий."
        ),
        request=PrivateChatCreateSerializer,
        responses={
            200: ChatDetailSerializer,
            201: ChatDetailSerializer,
            400: OpenApiResponse(description="Ошибка валидации"),
        },
    )
    @action(detail=False, methods=["post"], url_path="private")
    def create_private(self, request):
        """
        detail=False означает, что этот endpoint вызывается без ID чата:
        POST /api/v1/chats/private/
        """
        serializer = PrivateChatCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        interlocutor_id = serializer.validated_data["interlocutor_id"]

        with transaction.atomic():
            # Строки обоих пользователей блокируются в детерминированном порядке
            # (по id) — параллельные запросы той же пары проходят последовательно
            # и не могут создать два чата; одинаковый порядок исключает deadlock.
            list(
                User.objects.select_for_update()
                .filter(id__in=[request.user.id, interlocutor_id])
                .order_by("id")
            )

            # Отдельные filter() по M2M = независимые JOIN'ы: фильтр по участникам
            # в одном запросе с Count() обрезает JOIN и ломает подсчёт.
            chat = (
                Chat.objects.filter(type=Chat.ChatType.PRIVATE)
                .filter(members=request.user)
                .filter(members__id=interlocutor_id)
                .order_by("created_at")
                .first()
            )
            created = chat is None

            if created:
                chat = Chat.objects.create(type=Chat.ChatType.PRIVATE)
                Membership.objects.bulk_create(
                    [
                        Membership(chat=chat, user=request.user, is_admin=False),
                        Membership(chat=chat, user_id=interlocutor_id, is_admin=False),
                    ]
                )

        return Response(
            ChatDetailSerializer(chat, context={"request": request}).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class RegisterView(generics.CreateAPIView):
    """
    Регистрация нового пользователя.
    Возвращает JWT-токены сразу после успешной регистрации.
    """

    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    # Анонимный запрос: ScopedRateThrottle считает по IP
    throttle_scope = "register"

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        # Генерируем токены
        refresh = RefreshToken.for_user(user)
        tokens = {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }

        response_data = {
            "user": {
                "id": str(user.id),
                "username": user.username,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
            **tokens,
        }

        response_serializer = RegisterResponseSerializer(response_data)
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    # Сабкласс только чтобы задать throttle_scope: brute-force паролей — главный вектор
    throttle_scope = "auth"
    serializer_class = LoginSerializer


class RefreshView(TokenRefreshView):
    throttle_scope = "auth"


class SchemaView(SpectacularAPIView):
    # drf-spectacular генерирует схему заново на каждый запрос — отдельный лимит
    throttle_scope = "schema"


class UserSearchView(generics.ListAPIView):
    """Поиск пользователей по номеру телефона или имени"""

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
    # icontains = LIKE '%…%' без индекса: запрос дорогой, ещё и enumeration
    throttle_scope = "search"

    @extend_schema(
        summary="Поиск пользователей",
        parameters=[
            OpenApiParameter(
                name="q",
                type=str,
                required=True,
                description="Поиск по телефону или имени",
            ),
        ],
    )
    def get_queryset(self):
        query = self.request.query_params.get("q", "").strip()
        if not query:
            return User.objects.none()

        from django.db.models import Q

        # Телефон ищем по цифрам: в БД он лежит в каноническом виде, поэтому запрос
        # "+7 999" или "7-999" без нормализации не нашёл бы никого.
        digits = normalize_phone(query)
        conditions = Q(first_name__icontains=query) | Q(last_name__icontains=query)
        if digits:
            conditions |= Q(phone__icontains=digits)

        qs = User.objects.filter(conditions).exclude(id=self.request.user.id)
        return qs[:20]  # Максимум 20 результатов


class MeView(APIView):
    """
    GET   /users/me/ — профиль текущего аутентифицированного пользователя.
    PATCH /users/me/ — редактирование своего профиля, ответ — профиль целиком.
    """

    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        # Точечный лимит только на запись: PATCH принимает уникальные поля, а текст
        # ошибки валидации отвечает «занято ли» — то есть это вектор enumeration.
        # GET делается один раз на загрузку приложения, ему хватает глобального user.
        self.throttle_scope = "profile" if self.request.method == "PATCH" else None
        return super().get_throttles()

    @extend_schema(
        summary="Текущий пользователь",
        description="Возвращает профиль аутентифицированного пользователя по JWT-токену.",
        responses={200: MeSerializer},
    )
    def get(self, request):
        serializer = MeSerializer(request.user)
        return Response(serializer.data)

    @extend_schema(
        summary="Обновление своего профиля",
        description=(
            "Частичное обновление: phone, email, first_name, last_name. Непереданные "
            "поля остаются как есть. Ответ — профиль в формате GET /users/me/."
        ),
        request=ProfileUpdateSerializer,
        responses={200: MeSerializer},
    )
    def patch(self, request):
        serializer = ProfileUpdateSerializer(
            request.user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(MeSerializer(user).data)


class DeviceViewSet(
    CreateModelMixin,
    ListModelMixin,
    DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    Push-токены устройств текущего пользователя.

    - create: upsert токена (`POST /devices/`) — идемпотентен, можно слать на
      каждый вход и на каждую ротацию токена;
    - list: свои устройства без значения токена;
    - destroy: удалить своё устройство по uuid;
    - revoke: удалить по значению токена — нужно при logout, когда uuid клиента
      неизвестен (приложение после переустановки).

    PUT/PATCH нет: токен не «редактируют», его либо регистрируют заново, либо
    забывают — как `http_method_names` в ChatViewSet.
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete"]

    ACTION_THROTTLE_SCOPES = {
        "create": "devices",
        "revoke": "devices",
    }

    queryset = DeviceToken.objects.none()

    def get_throttles(self):
        self.throttle_scope = self.ACTION_THROTTLE_SCOPES.get(self.action)
        return super().get_throttles()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return DeviceToken.objects.none()
        # Пользователь видит и удаляет только свои строки: чужой uuid даёт 404,
        # а не 403 — иначе список устройств стал бы вектором enumeration.
        return DeviceToken.objects.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return DeviceTokenCreateSerializer
        if self.action == "revoke":
            return DeviceRevokeSerializer
        return DeviceTokenSerializer

    @extend_schema(
        summary="Зарегистрировать push-токен устройства",
        description=(
            "upsert по значению токена: 200 и `created: true` при первом виде, "
            "200 и `created: false` при повторе. Токен привязан к паре "
            "(приложение, устройство), поэтому вход с другого аккаунта на том же "
            "телефоне ПЕРЕПРИВЯЗЫВАЕТ строку к новому владельцу и заодно "
            "восстанавливает `is_active` после деактивации."
        ),
        request=DeviceTokenCreateSerializer,
        responses={
            200: OpenApiResponse(
                response=DeviceTokenSerializer,
                description="Устройство зарегистрировано (в ответе есть поле `created`)",
            ),
            400: OpenApiResponse(
                description="Токен не похож на FCM registration token"
            ),
        },
    )
    def create(self, request, *args, **kwargs):
        serializer = DeviceTokenCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        device, created = DeviceToken.objects.update_or_create(
            token=data["token"],
            defaults={
                "user": request.user,
                "platform": data["platform"],
                "app_version": data["app_version"],
                # Повторная регистрация = устройство снова у нас; сбрасываем
                # мягкое выключение, иначе push не придёт никогда.
                "is_active": True,
            },
        )
        return Response(
            {**DeviceTokenSerializer(device).data, "created": created},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Мои устройства",
        description="Список зарегистрированных устройств; значение токена не отдаётся.",
        responses={200: DeviceTokenSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Забыть устройство по uuid",
        responses={
            204: OpenApiResponse(description="Устройство удалено"),
            404: OpenApiResponse(description="Такое устройство не принадлежит вам"),
        },
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        summary="Забыть устройство по токену",
        description=(
            "Идемпотентно: 204 и когда строки не было (в т.ч. когда токен уже "
            "перепривязан к другому аккаунту). Удаляется только своя строка — "
            "чужое устройство этим запросом не выключить."
        ),
        request=DeviceRevokeSerializer,
        responses={
            204: OpenApiResponse(description="Устройство забыто"),
            400: OpenApiResponse(
                description="Токен не похож на FCM registration token"
            ),
        },
    )
    @action(detail=False, methods=["post"], url_path="revoke")
    def revoke(self, request):
        serializer = DeviceRevokeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        DeviceToken.objects.filter(
            user=request.user, token=serializer.validated_data["token"]
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PushTestView(APIView):
    """
    Тестовое уведомление на активные устройства текущего пользователя.

    Нужен, чтобы проверять свой пайплайн (права, канал, навигацию), не собирая
    кампанию вручную в Firebase Console. Делает реальный вызов FCM в потоке
    запроса — из-за этого лимит 5/мин, а не «дешёвый» диагностический рид.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "push_test"

    @extend_schema(
        summary="Отправить тестовый push на свои устройства",
        description=(
            "503, если push выключен (`PUSH_ENABLED=0`) или не настроен креденшел; "
            "`reason: no_tokens` — когда у пользователя нет активных устройств. "
            "Data-тип уведомления — `test`, навигации клиент по нему не строит."
        ),
        request=TestPushSerializer,
        responses={
            200: OpenApiResponse(description="Отчёт об отправке"),
            400: OpenApiResponse(description="Ошибка валидации текста"),
            502: OpenApiResponse(description="FCM не ответил"),
            503: OpenApiResponse(description="Push не включён или не настроен"),
        },
    )
    def post(self, request):
        serializer = TestPushSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not push_enabled():
            return Response(
                {"detail": "Push выключен: установите PUSH_ENABLED=1"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            summary = send_test_push(
                str(request.user.id),
                title=serializer.validated_data["title"],
                body=serializer.validated_data["body"],
            )
        except PushNotConfigured as error:
            return Response(
                {"detail": f"Push не настроен: {error}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except RuntimeError as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(summary)
