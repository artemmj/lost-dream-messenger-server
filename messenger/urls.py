from django.urls import path, include
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from .views import (
    ChatViewSet,
    DeviceViewSet,
    LoginView,
    MeView,
    PushTestView,
    RefreshView,
    RegisterView,
    SchemaView,
    UserSearchView,
)

router = DefaultRouter()
router.register(r"chats", ChatViewSet, basename="Сhat")
# /api/v1/devices/ — push-токены устройств: create (upsert), list, destroy,
# revoke. Имя во множественном числе как у /chats/, чтобы не различать правила.
router.register(r"devices", DeviceViewSet, basename="device")

urlpatterns = [
    path("", include(router.urls)),
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="token_obtain_pair"),
    path("auth/refresh/", RefreshView.as_view(), name="token_refresh"),
    path("users/me/", MeView.as_view(), name="user-me"),
    path("users/search/", UserSearchView.as_view(), name="user-search"),
    path("notifications/test/", PushTestView.as_view(), name="push-test"),
    path("schema/", SchemaView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
