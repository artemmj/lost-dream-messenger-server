from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import (
    BaseUserCreationForm,
    SetUnusablePasswordMixin,
)
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm

from .models import DeviceToken, User, Chat, Membership, Message


class UserChangeForm(BaseUserChangeForm):
    """
    Штатная `UserChangeForm` привязана к `django.contrib.auth.models.User` — в
    Django `Meta.model` задан конкретным классом, а у нас эта модель swap-нута в
    `messenger.User`. Меняем привязку: иначе в форме поле `username` обязано, а
    `phone` (наш `USERNAME_FIELD`, то есть логин) отсутствует вовсе — в админке
    его нельзя ни посмотреть, ни исправить.
    """

    class Meta(BaseUserChangeForm.Meta):
        model = User


class UserAdminCreationForm(SetUnusablePasswordMixin, BaseUserCreationForm):
    """
    Создание пользователя в админке.

    Штатная `AdminUserCreationForm` — та же привязка к `auth.User`, поэтому на
    любом POST падает `Manager isn't available; 'auth.User' has been swapped for
    'messenger.User'` (`clean_username` дёргает `objects` swap-нутой модели).
    Базис берём `BaseUserCreationForm` — тот же, что у Django, только с нашей
    моделью и логином-телефоном вместо `username`.
    """

    # Переключатель «парольная авторизация включена/отключена» — как в штатной
    # форме админки; миксин по нему решает, set_password или set_unusable_password.
    usable_password = SetUnusablePasswordMixin.create_usable_password_field()

    # `username` в форме не участвует: его выводит `User.save()` из телефона.
    class Meta:
        model = User
        fields = ("phone",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Обязательность паролей решает миксин (`validate_passwords`), а не поле:
        # при отключённой пароль-авторизации оба пустые — корректно. Как в Django.
        self.fields["password1"].required = False
        self.fields["password2"].required = False


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    autocomplete_fields = ["user"]


class RecentMessagesInline(admin.TabularInline):
    model = Message
    verbose_name_plural = "Последние сообщения"
    fields = ("sender", "short_text_display", "created_at", "is_read")
    readonly_fields = ("sender", "short_text_display", "created_at", "is_read")
    extra = 0
    max_num = 0
    can_delete = False
    ordering = ("-created_at",)

    def get_queryset(self, request):
        """НЕ применяем срез здесь — Django добавит фильтр по chat_id позже."""
        return super().get_queryset(request).order_by("-created_at")

    def get_formset(self, request, obj=None, **kwargs):
        """Ограничиваем queryset ПОСЛЕ того как Django применил фильтр по parent."""
        formset = super().get_formset(request, obj, **kwargs)
        if obj:
            # obj — это Chat instance, к которому привязан inline
            formset.queryset = Message.objects.filter(chat=obj).order_by("-created_at")[
                :7
            ]
        return formset

    @admin.display(description="Текст")
    def short_text_display(self, obj):
        text = obj.text or ""
        return text[:80] + "..." if len(text) > 80 else text


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("id", "username", "email", "phone")
    form = UserChangeForm
    add_form = UserAdminCreationForm
    # `phone` первым и в списке, и в поиске: `username` хранит канонический
    # телефон, поэтому поиск по нему не находит номер, введённый с «+» или
    # пробелами, — а в админке ищут именно по телефону.
    fieldsets = (
        (None, {"fields": ("phone", "username", "password")}),
        ("Личные данные", {"fields": ("first_name", "last_name", "email")}),
        (
            "Права доступа",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Даты", {"fields": ("last_login", "last_seen", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("phone", "usable_password", "password1", "password2"),
            },
        ),
    )
    search_fields = ("phone", "username", "first_name", "last_name", "email")


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "name", "created_at", "members_count")
    list_filter = ("type", "created_at")
    search_fields = ("name", "members__phone")
    inlines = [MembershipInline, RecentMessagesInline]  # ← Добавили inline

    @admin.display(description="Участников")
    def members_count(self, obj):
        return obj.members.count()


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "chat", "sender", "short_text", "created_at", "is_read")
    list_filter = ("is_read", "created_at")
    search_fields = ("text", "sender__phone")
    readonly_fields = ("chat", "sender", "created_at")

    @admin.display(description="Текст")
    def short_text(self, obj):
        text = obj.text or ""
        return text[:50] + "..." if len(text) > 50 else text


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    """
    Диагностика «почему пользователю не пришёл push»: есть ли активный токен,
    с какой платформы, когда он последний раз перепривязывался.
    """

    list_display = ("short_token", "user", "platform", "is_active", "last_seen_at")
    list_filter = ("platform", "is_active", "created_at")
    search_fields = ("token", "user__phone", "user__first_name", "user__last_name")
    # Значение токена — секрет доставки: наружу (в список) идёт только префикс,
    # а в форме оно readonly, чтобы «починить опечатку» нельзя было — пусть
    # устройство зарегистрируется само.
    readonly_fields = ("token", "created_at", "last_seen_at")
    actions = ("deactivate_devices", "activate_devices")

    @admin.display(description="Токен", ordering="token")
    def short_token(self, obj):
        return f"{obj.token[:16]}…"

    @admin.action(description="Пометить неактивными")
    def deactivate_devices(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Неактивны: {updated}")

    @admin.action(description="Вернуть в рассылку")
    def activate_devices(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Активны: {updated}")
