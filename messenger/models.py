import uuid
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone

from config import settings


def normalize_phone(value: str) -> str:
    """
    Канонический вид телефона: только цифры, без `+` и разделителей.
    Используется и при записи, и при входе — `phone` ищется точным совпадением.
    """
    return "".join(c for c in value if c.isdigit())


class UserManager(BaseUserManager):
    """
    Кастомный менеджер пользователей.
    Убирает обязательность email для create_user и create_superuser.
    Телефон используется как основной идентификатор (USERNAME_FIELD).
    """

    def create_user(self, phone, password=None, **extra_fields):
        """
        Создаёт обычного пользователя.
        phone — обязателен, password — обязателен.
        Все остальные поля (email, first_name, last_name) — опциональны.
        """
        if not phone:
            raise ValueError("Телефон обязателен")

        # Канонический вид телефону задаёт User.save()

        # email может быть None или пустой строкой — нормализуем
        email = extra_fields.pop("email", None)
        if email:
            email = self.normalize_email(email)

        user = self.model(
            phone=phone,
            email=email or "",
            **extra_fields,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone, password=None, **extra_fields):
        """
        Создаёт суперпользователя.
        Требует ТОЛЬКО телефон и пароль.
        Автоматически ставит is_staff=True, is_superuser=True.
        """
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Суперпользователь должен иметь is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Суперпользователь должен иметь is_superuser=True")

        return self.create_user(phone, password, **extra_fields)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone = models.CharField(max_length=20, unique=True, verbose_name="Телефон")
    email = models.EmailField(blank=True, default="", verbose_name="Email")
    first_name = models.CharField(
        max_length=150, blank=True, default="", verbose_name="Имя"
    )
    last_name = models.CharField(
        max_length=150, blank=True, default="", verbose_name="Фамилия"
    )
    last_seen = models.DateTimeField(
        null=True, blank=True, verbose_name="Последний визит"
    )

    # Переопределяем username_field и manager
    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = []  # ← Пустой! createsuperuser спросит ТОЛЬКО phone + password

    objects = UserManager()

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"

    def save(self, *args, **kwargs):
        # Нормализуем на сохранении, а не только в менеджере: телефон — логин, и
        # канонический вид должен обеспечиваться и для админки, и для shell, и для
        # фикстур, иначе вход перестанет находить аккаунт.
        self.phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.first_name or self.phone

    @property
    def full_name(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return name or self.phone


class Chat(models.Model):
    class ChatType(models.TextChoices):
        PRIVATE = "PRIVATE", "Личный"
        GROUP = "GROUP", "Групповой"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(
        max_length=10, choices=ChatType.choices, default=ChatType.PRIVATE
    )
    name = models.CharField(
        max_length=255, blank=True, help_text="Название для групповых чатов"
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through="Membership", related_name="chats"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_type_display()} chat {self.id}"


class Membership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_admin = models.BooleanField(default=False)
    # Курсор прочтения: непрочитанными считаются сообщения чата, созданные позже.
    # default вместо null=True — миграция заполняет существующие строки «сейчас»,
    # иначе сайдбар после выгрузки курса вспыхнет всеми старыми сообщениями.
    last_read_at = models.DateTimeField(
        default=timezone.now, verbose_name="Прочитано до"
    )

    class Meta:
        unique_together = ("user", "chat")
        constraints = [
            # Уникальность участника в чате (на уровне БД)
            models.UniqueConstraint(fields=["user", "chat"], name="unique_user_chat")
        ]

    def __str__(self):
        return f"{self.user.username} in {self.chat.id}"


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_messages"
    )
    text = models.TextField(max_length=5000)  # Ограничение на текст
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(
        default=False, db_index=True
    )  # Пока просто флаг, без усложнений

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["chat", "created_at"]
            ),  # Для быстрой пагинации сообщений в чате
        ]

    def __str__(self):
        return f"Message from {self.sender.username} at {self.created_at}"
