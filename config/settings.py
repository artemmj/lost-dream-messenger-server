import os
from pathlib import Path
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "django-insecure-b-lzir18=x4tvtyg())e*dd02@q=05_9!(coe4q*#+v97%_7jd"
DEBUG = True
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "10.0.2.2",
    "backend",  # Docker internal DNS
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "messenger",
]

AUTH_USER_MODEL = "messenger.User"
ASGI_APPLICATION = "config.asgi.application"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # После AuthenticationMiddleware: читает request.user уже после отработки view
    "messenger.middleware.LastSeenMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "db_messenger"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "postgres"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {
            "connect_timeout": 10,
        },
    }
}

# Runner сливает висящие сессии тестовой базы перед её удалением: их держат
# рабочие потоки (asgiref, push-пул), а Django закрывает только своё — иначе
# `manage.py test` падает на «is being accessed by other users».
TEST_RUNNER = "config.test_runner.TestRunner"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")

# DRF-троттлинг хранит счётчики в кэше: LocMemCache дал бы отдельный лимит на
# каждый процесс, поэтому считаем в Redis (БД 1 — отдельно от channel layer и presence).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"redis://{REDIS_HOST}:6379/1",
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        # Без throttle_scope на view пропускает запрос — поэтому можно включить глобально
        "rest_framework.throttling.ScopedRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "120/min",
        "user": "600/min",
        # Точечные scope'ы (ScopedRateThrottle) — см. messenger/views.py и urls.py
        "auth": "10/min",  # login/refresh: анонимные, ключ — IP
        "register": "5/min",
        "send": "60/min",
        # Отметка прочтения вызывается при каждом открытии/фокусе чата
        "read": "120/min",
        "write": "30/min",
        "search": "20/min",
        # PATCH /users/me/: уникальные поля, ошибка валидации = enumeration
        "profile": "20/min",
        "schema": "30/hour",
        # Регистрация push-токена — событие редкое (вход, ротация токена). 30/мин
        # с запасом, но закрывает и спам, и перебор uuid на DELETE /devices/<id>/.
        "devices": "30/min",
        # Тестовый push: сам ход дорогой (обращение к FCM), а не диагностический
        # запрос на каждый чих.
        "push_test": "5/min",
    },
    # Один прокси (nginx): без этого DRF берёт весь X-Forwarded-For целиком,
    # а он подделывается заголовком запроса.
    "NUM_PROXIES": 1,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Messenger API",
    "DESCRIPTION": "API мессенджера на Django + DRF",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,  # Не показывать эндпоинт /api/schema/ в самом Swagger UI
    "COMPONENT_SPLIT_REQUEST": True,  # Разделять read/write сериалайзеры в документации
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "persistAuthorization": True,  # 👈 Сохранять токен после перезагрузки страницы
        "displayOperationId": False,
    },
    # Автоматическая аутентификация в Swagger через JWT
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "jwtAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            }
        }
    },
    "SECURITY": [{"jwtAuth": []}],
}

# WhiteNoise кэширует и сжимает статику
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.pubsub.RedisPubSubChannelLayer",
        "CONFIG": {
            "hosts": [f"redis://{REDIS_HOST}:6379/0"],
            "capacity": 1500,
            "expiry": 10,
        },
    },
}

# Push-уведомления через Firebase Cloud Messaging (HTTP v1).
#
# ENABLED=0 по умолчанию: локальный `docker compose up` поднимается без ключей,
# push просто не отправляется (с одной WARN в лог). Без этого бэкенд требовал бы
# Firebase-проекта для запуска, а это лишний блок для любой правки вне push.
#
# Креденшел админ-SDK — service account key из Firebase Console:
#   FIREBASE_CREDENTIALS_JSON — сам JSON или base64 от него (для compose:
#     значение можно положить в .env, файл в контейнер не монтируется);
#   FIREBASE_CREDENTIALS_PATH — путь к файлу (вариант для прода с mounted secret).
# JSON-вариант предпочтительнее: `credentials.Certificate(путь)` отказывается
# читать файл с правами лучше 600 («readable by the public»), а в контейнере это
# почти всегда 644.
#
# CHANNEL_HIGH/CHANNEL_LOW — те же id, что обязан создать клиент своими
# Android-каналами (flutter_local_notifications). Расхождение — и системное
# уведомление, показанное самим SDK при убитом приложении, попадёт в канал-заглушку
# «прочие» без нужного звука и приоритета.
FCM = {
    "ENABLED": os.environ.get("PUSH_ENABLED", "0") == "1",
    "PROJECT_ID": os.environ.get("FIREBASE_PROJECT_ID", ""),
    "CREDENTIALS_JSON": os.environ.get("FIREBASE_CREDENTIALS_JSON", ""),
    "CREDENTIALS_PATH": os.environ.get("FIREBASE_CREDENTIALS_PATH", ""),
    "CHANNEL_HIGH": os.environ.get("PUSH_CHANNEL_HIGH", "mdm_messages_high"),
    "CHANNEL_LOW": os.environ.get("PUSH_CHANNEL_LOW", "mdm_messages_low"),
    # Иконка в статус-баре: монохромный drawable из android/app/src/main/res/drawable
    "ICON": os.environ.get("PUSH_ICON", "ic_notification"),
    "COLOR": os.environ.get("PUSH_COLOR", "#4F46E5"),
    # «Просроченное» уведомление о сообщении, прилетевшее через сутки, — шум:
    # непрочитанное и так дотянется через REST при следующем открытии.
    "TTL_SECONDS": int(os.environ.get("PUSH_TTL_SECONDS", "14400")),
}

CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",  # Vite dev server
    "http://127.0.0.1:5173",
]
# Для WebSocket тоже нужно разрешить
CORS_ALLOW_CREDENTIALS = True
