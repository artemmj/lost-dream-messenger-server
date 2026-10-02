import re

from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

# from django.contrib.auth.password_validation import validate_password
from .models import Chat, DeviceToken, Message, normalize_phone

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(required=True)
    email = serializers.EmailField(required=False)
    username = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(
        write_only=True,
        # min_length=8,
        # style={'input_type': 'password'},
    )
    password_confirm = serializers.CharField(
        write_only=True,
        # style={'input_type': 'password'},
    )

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "password",
            "password_confirm",
            "phone",
        )
        extra_kwargs = {
            "first_name": {"required": False},
            "last_name": {"required": False},
        }

    def validate_email(self, value: str) -> str:
        """Нормализация и проверка уникальности email"""
        normalized = value.lower().strip()
        if User.objects.filter(email__iexact=normalized).exists():
            raise serializers.ValidationError(
                "Пользователь с таким email уже существует."
            )
        return normalized

    def validate_phone(self, value: str) -> str:
        # Нормализуем до записи в БД: `username` по умолчанию берётся из телефона
        # (см. create), а он unique — сырое «+7 (999) …» разошлось бы с `phone`.
        # Строка без цифр отклоняется: она нормализуется в "", и в такой аккаунт
        # вошёл бы кто угодно по любому «abc».
        normalized = normalize_phone(value)
        if not normalized:
            raise serializers.ValidationError("Телефон не может быть пустым.")
        # Поле объявлено явно, поэтому DRF не подставляет к нему UniqueValidator —
        # без этой проверки дубль падал бы IntegrityError'ом (500).
        if User.objects.filter(phone=normalized).exists():
            raise serializers.ValidationError("Пользователь с таким телефоном уже существует.")
        return normalized

    def validate_username(self, value: str) -> str:
        value = value.strip()
        if value and User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError(
                "Пользователь с таким именем уже существует."
            )
        return value

    def validate(self, attrs: dict) -> dict:
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError(
                {"password_confirm": "Пароли не совпадают."}
            )
        # Проверяем через встроенный в Django password validator
        # validate_password(attrs['password'])
        return attrs

    def create(self, validated_data: dict) -> User:
        username = validated_data.get("username") or validated_data["phone"]
        user = User.objects.create_user(
            phone=validated_data["phone"],
            password=validated_data["password"],
            username=username,
            email=validated_data.get("email", ""),
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
        )
        return user


class RegisterResponseSerializer(serializers.Serializer):
    """Формат ответа при успешной регистрации"""

    user = serializers.DictField()
    access = serializers.CharField()
    refresh = serializers.CharField()


class LoginSerializer(TokenObtainPairSerializer):
    """
    Логин по телефону. Нормализация обязательна: в БД телефон лежит в каноническом
    виде (только цифры, см. normalize_phone), а аутентификация сравнивает строку как
    есть — без неё вход с "+7 999 999-99-11" дал бы 401 при верном пароле.
    """

    def validate(self, attrs: dict) -> dict:
        attrs[self.username_field] = normalize_phone(attrs[self.username_field])
        return super().validate(attrs)


class MeSerializer(serializers.ModelSerializer):
    """Сериалайзер профиля текущего пользователя. Read-only."""

    class Meta:
        model = User
        fields = ("id", "phone", "email", "first_name", "last_name", "last_seen")
        read_only_fields = fields


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """
    Обновление своего профиля (PATCH /users/me/).

    Все поля опциональны — непереданные остаются как есть. `id` и `last_seen`
    сюда не входят намеренно: их меняет сервер.
    """

    # Объявляем явно: ModelSerializer не подставил бы max_length к переопределённому полю
    phone = serializers.CharField(required=False, max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ("phone", "email", "first_name", "last_name")

    def validate_phone(self, value: str) -> str:
        # Нормализация обязательна: по «чистому» телефону пользователь входит, запись
        # с пробелами/скобками выбила бы его из входа.
        normalized = normalize_phone(value)
        if not normalized:
            raise serializers.ValidationError("Телефон не может быть пустым.")
        if self._others_exist(phone=normalized):
            raise serializers.ValidationError("Этот телефон уже занят.")
        return normalized

    def validate_email(self, value: str) -> str:
        normalized = value.lower().strip()
        if normalized and self._others_exist(email__iexact=normalized):
            raise serializers.ValidationError(
                "Пользователь с таким email уже существует."
            )
        return normalized

    def _others_exist(self, **lookup) -> bool:
        # Не перебиваем себя: PATCH с прежним телефоном/email — не конфликт
        qs = User.objects.filter(**lookup)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        return qs.exists()

    def validate_first_name(self, value: str) -> str:
        return value.strip()

    def validate_last_name(self, value: str) -> str:
        return value.strip()

    def update(self, instance, validated_data):
        phone = validated_data.get("phone")
        # Регистрация копирует телефон в username (он unique — см. RegisterSerializer).
        # Если при смене телефона оставить старое значение, новый владелец этого номера
        # упрётся при регистрации в IntegrityError на unique username.
        if phone and instance.username == instance.phone:
            validated_data["username"] = phone
        return super().update(instance, validated_data)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "phone", "email", "first_name", "last_name")
        read_only_fields = ("id",)


class MessageSerializer(serializers.ModelSerializer):
    """Сериалайзер для чтения сообщений (история чата)"""

    sender = UserSerializer(read_only=True)

    class Meta:
        model = Message
        fields = ("id", "chat", "sender", "text", "created_at", "is_read")
        read_only_fields = ("id", "chat", "sender", "created_at")


class ChatListSerializer(serializers.ModelSerializer):
    """Сериалайзер для списка чатов (нужно показать последнее сообщение и собеседника)"""

    last_message = serializers.SerializerMethodField()
    interlocutor = serializers.SerializerMethodField()  # Для личных чатов
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Chat
        fields = (
            "id",
            "type",
            "name",
            "created_at",
            "last_message",
            "interlocutor",
            "unread_count",
        )

    @extend_schema_field(MessageSerializer)
    def get_last_message(self, obj):
        # Предварительная выборка (prefetch) должна быть сделана во ViewSet
        if hasattr(obj, "last_msg_list") and obj.last_msg_list:
            return MessageSerializer(obj.last_msg_list[0]).data
        return None

    @extend_schema_field(serializers.IntegerField())
    def get_unread_count(self, obj):
        # Счётчики считает вьюха одной агрегатной выборкой на всю страницу и
        # кладёт в context: аннотировать сам queryset нельзя, там уже .distinct()
        # вместе с Prefetch — вторая join-агрегация размножила бы строки.
        return self.context.get("unread_counts", {}).get(str(obj.id), 0)

    @extend_schema_field(UserSerializer)
    def get_interlocutor(self, obj):
        if obj.type == Chat.ChatType.PRIVATE:
            request = self.context.get("request")
            if request and request.user.is_authenticated:
                # Ищем второго участника
                for member in obj.members.all():
                    if member != request.user:
                        return UserSerializer(member).data
        return None


class ChatMemberSerializer(UserSerializer):
    """Участник чата с флагом администратора (для Swagger-схемы)"""

    is_admin = serializers.BooleanField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ("is_admin",)


class ChatDetailSerializer(serializers.ModelSerializer):
    members = serializers.SerializerMethodField()
    my_is_admin = serializers.SerializerMethodField()

    class Meta:
        model = Chat
        fields = ("id", "type", "name", "members", "my_is_admin", "created_at")

    @extend_schema_field(ChatMemberSerializer(many=True))
    def get_members(self, obj):
        admin_flags = {m.user_id: m.is_admin for m in obj.membership_set.all()}
        result = []
        for member in obj.members.all():
            data = UserSerializer(member).data
            data["is_admin"] = admin_flags.get(member.id, False)
            result.append(data)
        return result

    @extend_schema_field(serializers.BooleanField())
    def get_my_is_admin(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            membership = obj.membership_set.filter(user=request.user).first()
            return bool(membership and membership.is_admin)
        return False


class ChatCreateSerializer(serializers.ModelSerializer):
    """
    Создание чата. Для GROUP можно сразу передать участников (member_ids) —
    создатель автоматически становится админом.
    """

    member_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="UUID пользователей, которых добавить в чат (для групповых чатов)",
    )

    class Meta:
        model = Chat
        fields = ("id", "type", "name", "member_ids")

    def validate(self, attrs: dict) -> dict:
        if attrs.get("type") == Chat.ChatType.GROUP:
            if not attrs.get("name", "").strip():
                raise serializers.ValidationError(
                    {"name": "Групповой чат должен иметь название."}
                )
        elif attrs.get("member_ids"):
            raise serializers.ValidationError(
                {"member_ids": "Участников можно добавлять только в групповой чат."}
            )
        return attrs

    def validate_member_ids(self, value):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        if value:
            found = User.objects.filter(id__in=value).count()
            if found != len(set(value)):
                raise serializers.ValidationError(
                    "Один или несколько пользователей не найдены."
                )
        return list(set(value))


class ChatRenameSerializer(serializers.ModelSerializer):
    """
    Переименование GROUP-чата: только название, остальные поля недоступны.
    Права (админ чата) и тип чата проверяются во вьюхе — здесь только валидация.
    """

    class Meta:
        model = Chat
        fields = ("name",)

    def validate_name(self, value: str) -> str:
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Название чата не может быть пустым.")
        return name


class MessageCreateSerializer(serializers.Serializer):
    """
    Сериалайзер для отправки сообщения.
    Принимает только текст, chat и sender определяются автоматически.
    """

    text = serializers.CharField(
        max_length=5000,
        min_length=1,
        help_text="Текст сообщения (1–5000 символов)",
    )

    def validate_text(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Сообщение не может быть пустым.")
        return value


class AddMemberSerializer(serializers.Serializer):
    """Сериалайзер для добавления участника в чат"""

    user_id = serializers.UUIDField(
        help_text="UUID пользователя, которого нужно добавить в чат"
    )

    def validate_user_id(self, value):
        if not User.objects.filter(id=value).exists():
            raise serializers.ValidationError("Пользователь с таким ID не найден.")
        return value


class RemoveMemberSerializer(serializers.Serializer):
    """Сериалайзер для удаления участника из чата"""

    user_id = serializers.UUIDField(
        help_text="UUID пользователя, которого нужно удалить из чата"
    )


class PrivateChatCreateSerializer(serializers.Serializer):
    """
    Сериалайзер для создания/получения личного чата.
    Если личный чат с этим пользователем уже существует — вернёт его.
    """

    interlocutor_id = serializers.UUIDField(
        help_text="UUID пользователя, с которым нужно создать личный чат"
    )

    def validate_interlocutor_id(self, value):
        request = self.context.get("request")
        if request and request.user.id == value:
            raise serializers.ValidationError(
                "Нельзя создать личный чат с самим собой."
            )
        if not User.objects.filter(id=value).exists():
            raise serializers.ValidationError("Пользователь с таким ID не найден.")
        return value


# --- Push-токены устройств ----------------------------------------------------

# FCM registration token — длинная строка из латиницы и `-` `.` `:` `=` `_`.
# `\Z` вместо `$`: у `$` есть сюрприз — он пропускает завершающий перевод строки.
FCM_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:=\-]{20,512}\Z")


def validate_fcm_token(value: str) -> str:
    """
    Формальная проверка токена до обращения к FCM.

    Мусор дешевле отклонить на входе 400: на невалидный формат FCM отвечает
    `InvalidArgumentError`, а наш разбор таких ответов деактивирует токен — то
    есть опечатка в клиенте выглядела бы как «устройство само отвалилось».
    """
    token = (value or "").strip()
    if not FCM_TOKEN_RE.match(token):
        raise serializers.ValidationError(
            "Ожидается FCM registration token: 20–512 символов без пробелов."
        )
    return token


class DeviceTokenSerializer(serializers.ModelSerializer):
    """
    Формат ответа про устройство.

    Само значение `token` не отдаётся никогда: это секрет доставки, и его утечка
    через список устройств позволяет чужому коду слать пользователю push.
    """

    class Meta:
        model = DeviceToken
        fields = (
            "id",
            "platform",
            "is_active",
            "app_version",
            "created_at",
            "last_seen_at",
        )
        read_only_fields = fields


class DeviceTokenCreateSerializer(serializers.Serializer):
    """Регистрация (upsert) токена текущего пользователя."""

    token = serializers.CharField(
        max_length=512,
        help_text="FCM registration token устройства",
    )
    platform = serializers.ChoiceField(
        choices=DeviceToken.Platform.choices,
        default=DeviceToken.Platform.ANDROID,
        required=False,
    )
    app_version = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=32,
        default="",
        help_text="Версия клиента — для диагностики «пришло/не пришло»",
    )

    def validate_token(self, value):
        return validate_fcm_token(value)

    def validate_app_version(self, value):
        return (value or "").strip()


class DeviceRevokeSerializer(serializers.Serializer):
    """«Забудь это устройство» — по значению токена, когда uuid неизвестен."""

    token = serializers.CharField(max_length=512)

    def validate_token(self, value):
        return validate_fcm_token(value)


class TestPushSerializer(serializers.Serializer):
    """Параметры тестового уведомления; оба поля опциональны — дефолты в push.py."""

    title = serializers.CharField(
        required=False, allow_blank=True, max_length=80, default=""
    )
    body = serializers.CharField(
        required=False, allow_blank=True, max_length=200, default=""
    )
