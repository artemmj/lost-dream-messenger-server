# AGENTS.md — Контекст для AI-ассистентов и инженеров

> Этот файл содержит структурную информацию о проекте для быстрого онбординга.
> Обновлять при каждом значимом изменении архитектуры.

## 🏗 Архитектура проекта

Монорепозиторий: Django-бэкенд **в корне репозитория**, Vue-фронтенд — в `frontend/`.

```
lost-dream-messenger/
├── config/                        # Django project
│   ├── settings.py                # DB, MIDDLEWARE (+ LastSeenMiddleware), CACHES (Redis), REST_FRAMEWORK (+ троттлинг), SIMPLE_JWT, SPECTACULAR, CHANNEL_LAYERS, CORS
│   ├── asgi.py                    # ASGI app: ProtocolTypeRouter (HTTP + WebSocket, AllowedHostsOriginValidator)
│   ├── urls.py                    # admin/ + api/v1/ → messenger.urls
│   └── wsgi.py                    # Fallback (в prod не используется, сервер — Daphne)
├── messenger/                     # Django app (единственное приложение)
│   ├── models.py                  # User, Chat, Membership, Message + кастомный UserManager
│   ├── serializers.py             # DRF-сериалайзеры + Swagger-аннотации
│   ├── views.py                   # ChatViewSet, RegisterView, LoginView/RefreshView/SchemaView, UserSearchView, MeView
│   ├── consumers.py               # ChatConsumer + NotificationConsumer (AsyncJsonWebsocketConsumer), message_payload(), publish_*
│   ├── activity.py                # touch_last_seen() — обновление last_seen с троттлингом 60 с
│   ├── middleware.py              # LastSeenMiddleware — активность на авторизованных REST-запросах
│   ├── ratelimit.py               # RedisWindowLimiter (Lua INCR+PEXPIRE) + лимиты WS: сообщения, подключения, кап сессий
│   ├── readstate.py               # unread_counts() / unread_counts_per_user() — непрочитанное по курсору Membership.last_read_at
│   ├── ws_auth.py                 # JWT-аутентификация для WebSocket (token из query string)
│   ├── routing.py                 # WebSocket URL patterns: ws/chat/<id>/ и ws/notifications/
│   ├── admin.py                   # Django Admin с inlines (Membership, последние сообщения)
│   ├── urls.py                    # DefaultRouter (chats) + auth/users/schema/docs
│   ├── migrations/
│   └── tests.py                   # Пусто — тестов нет
├── manage.py
├── requirements.txt               # Пины версий (prod + dev-инструменты, см. Tech Debt)
├── Dockerfile                     # Python 3.13-slim, multi-stage, непривилегированный appuser, Daphne
├── docker-compose.yml             # db (postgres:18) + redis:7 + backend + frontend (Vite dev)
├── .env / .env.example            # DB_* читаются settings.py; DJANGO_SECRET_KEY/DEBUG — НЕ читаются
└── frontend/                      # Vue 3 SPA
    ├── src/
    │   ├── assets/styles.css      # Глобальные стили (CSS variables)
    │   ├── components/            # ChatSidebar, ChatWindow, MessageBubble, NewChatModal, GroupMembersModal, ProfileModal
    │   ├── composables/           # useChatSocket (сокет чата) и useNotificationsSocket (личный канал) — WS, reconnect, отправка
    │   ├── stores/                # Pinia: auth.ts (JWT, профиль), chat.ts (чаты, сообщения, непрочитанное, presence)
    │   ├── services/api.ts        # Axios instance + interceptors (Bearer, авто-refresh при 401)
    │   ├── views/                 # LoginView, ChatView (здесь же монтируется личный WS-канал)
    │   ├── router/index.ts        # Vue Router + navigation guard (requiresAuth)
    │   ├── App.vue                # При старте догружает профиль через /users/me/
    │   └── main.ts
    ├── vite.config.ts             # Proxy /api и /ws → http://backend:8000 (Docker DNS)
    ├── nginx.conf                 # Prod: SPA fallback + proxy /api и /ws → backend:8000 + limit_req/limit_conn (429)
    ├── Dockerfile                 # node:22-alpine; targets: dev (Vite) / prod (nginx)
    └── package.json               # engines: node ^22.18 || >=24.12
```

## 🔑 Ключевые архитектурные решения

| Решение | Обоснование |
|---------|-------------|
| UUID PK во всех моделях | Безопасность (нет enumeration), совместимость с distributed |
| `phone` как USERNAME_FIELD | Мессенджер-ориентированная идентификация; поле `username` из AbstractUser сохранено (у суперпользователя может быть пустой строкой, при регистрации через API в пустой `username` подставляется `phone`) |
| Канонический телефон в `normalize_phone()` — только цифры | Телефон — логин, а `authenticate()` сравнивает строку точным совпадением: `+7 (999) 999-99-11`, `79999999911` и `7-999-…` — один аккаунт. Гарантию даёт `User.save()`, поэтому её не обходят ни админка, ни фикстуры; то же правило на входе и в поиске — иначе запрос с `+` не находит никого |
| Ошибка входа не проходит через refresh-retry (`AUTH_URLS` в api.ts) | 401 от `/auth/login/` означает «неверные креды», а не «протух токен»: рефреш на нём заведомо отвечает 400, а его обработчик перезагружает страницу — то есть стирать текст ошибки и «выкидывать» с `/login` |
| `/users/me/` для профиля | JWT payload содержит только `user_id` — профиль всегда догружается отдельным запросом |
| Промежуточная модель Membership | Расширяемость (роли, mute, ban); `is_admin` управляет add/remove member |
| Daphne вместо Gunicorn | Единый ASGI-сервер для HTTP + WebSocket |
| Redis Pub/Sub channel layer | `channels_redis.pubsub` устойчив к таймаутам на Docker Desktop (в отличие от BRPOP core) |
| JWT через query string в WS | WebSocket не поддерживает HTTP-заголовки при handshake |
| Vite proxy + django-cors-headers | Proxy даёт zero-CORS в dev; corsheaders установлен как страховка для прямых запросов (CORS_ALLOWED_ORIGINS: localhost:5173) |
| Pinia для state management | Нативный store для Vue 3, DevTools поддержка |
| @vueuse/core | Готовые composables (useDebounceFn) вместо самописных |
| Multi-stage Dockerfile frontend | Dev (Vite HMR) и prod (nginx) из одного Dockerfile; в compose пока используется только dev |
| `message_payload()` в consumers.py | Единый формат WS-сообщения для consumer'а и REST-broadcast — клиенты не различают источник |
| Presence через Redis-hash `messenger:presence` | Счётчик соединений на пользователя (несколько вкладок/устройств): `online` — при 0→1, `offline` — при 1→0; отдельный клиент от channel layer, ключ общий |
| `select_for_update` в `create_private` | Идемпотентность пары пользователей без unique-индекса (на M2M его не выразить); блокировка строк в порядке `id` исключает deadlock |
| `LastSeenMiddleware` читает `request.user` в response-фазе | DRF аутентифицирует запрос внутри view и сам пробрасывает пользователя в Django HttpRequest — до view там всегда аноним |
| `touch_last_seen()` с троттлингом 60 с | Активность пишется в БД не чаще раза в минуту; условие троттлинга продублировано в `UPDATE` на случай протухшего объекта в памяти |
| DRF-троттлинг + кэш в Redis, без новых зависимостей | Встроенные `AnonRateThrottle`/`UserRateThrottle`/`ScopedRateThrottle` закрывают REST; `redis-py` уже нужен для presence. `django-ratelimit` не добавляли |
| Свой `RedisWindowLimiter` для WS | DRF-троттлинг до consumer'ов не дотягивается; fixed window на Lua (`INCR`+`PEXPIRE`) — один round trip и O(1) памяти. Скользящее окно (ZSET) — переплата за точность |
| Кап одновременных WS-соединений через presence-хеш | `messenger:presence` уже считает активные соединения пользователя — отдельный счётчик не заводим |
| `MESSAGE_LIMITER` = 10/10 с, как REST-scope `send` | Иначе лимит обходится уходом в REST-fallback после закрытия сокета |
| `ScopedRateThrottle` в глобальных `DEFAULT_THROTTLE_CLASSES` | Без `throttle_scope` на view он пропускает запрос — точечный лимит добавляется одной строкой, а не переопределением `throttle_classes` в каждой вьюхе |
| Прочтение — курсор `Membership.last_read_at`, не read-receipt на сообщение | Одна строка на участника и один запрос на все чаты против `ChatRead` на пару (сообщение, пользователь); минус — нельзя показать «прочитано на таком-то сообщении», для бейджа это не нужно |
| Уведомления — отдельный личный WS-канал `ws/notifications/`, а не «вечный» сокет чата | Сокет чата живёт, пока чат открыт: сообщение в другой чат доставлять не через что. Плюс у канала есть адресат для `chat_read`/`chat_deleted`, которых у чата нет (сокет закрыт), и общий на оба канала бюджет подключений |
| Presence и `last_seen` переехали на личный канал | Иначе закрытие чата (Esc/крестик) анонсировало «offline», хотя пользователь ещё в приложении; сокет чата теперь только читает presence-хеш для `initial_presence` |
| `user_status` анонсируется во все группы чатов пользователя | У presence-события нет «дома» — точку онлайн рисуют шапки личных чатов и список участников групповых, а чатов несколько; переход 0→1 и 1→0 редок, поэтому веер по N группам дешевле отдельной индексации |

## 📦 Модели данных

- **User** (`AbstractUser` + UUID PK): `phone` (unique, USERNAME_FIELD), `email`/`first_name`/`last_name` (опциональные), `last_seen` (обновляется при подключении и отключении личного WS-канала `ws/notifications/`, при отправке сообщения в WS и на любом авторизованном REST-запросе — с троттлингом 60 с). Телефон хранится канонически — только цифры, без `+` и разделителей; гарантирует это `User.save()` через `normalize_phone()`, поэтому правило действует и в админке, и в shell. `username` при регистрации повторяет телефон.
- **Chat**: `type` (PRIVATE/GROUP), `name` (для групп), `members` M2M через Membership, ordering `-created_at`.
- **Membership**: user ↔ chat, `is_admin`, `last_read_at` (курсор прочтения, см. ниже), unique constraint `unique_user_chat` (продублирован legacy `unique_together`).
- **Message**: `chat` FK, `sender` FK, `text` (≤5000), `created_at`, `is_read` (глобальный флаг на сообщение, не per-user), index `(chat, created_at)`, ordering `created_at`.
- **Курсор прочтения** (`Membership.last_read_at`, `default=timezone.now`): непрочитанными считаются сообщения чата, созданные позже этой отметки и не от самого пользователя. Считает единственная функция `messenger/readstate.py::unread_counts(user, chat_ids)` — один запрос на весь список чатов (`Count(..., filter=Q(created_at__gt=F("last_read_at")) & ~Q(sender=user))`, условие ложится на индекс `(chat, created_at)`). `default`, а не `null=True`: миграция заполняет старые строки «сейчас», поэтому после выгрузки курса сайдбар не вспыхнет всеми архивными сообщениями, а новый участник группы стартует «прочитано». `Message.is_read` курсор не заменяет — по-прежнему глобальный флаг, галочку ✓✓ не трогает.

## 🔌 REST API (префикс `/api/v1/`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/register/` | Регистрация (phone, password, password_confirm; email/name опциональны) + JWT в ответе. Телефон приводится к каноническому виду (`normalize_phone`) и в `phone`, и в `username`, который дефолтом из него заполняется; телефон без цифр → 400, уже занятый → 400 (поле объявлено явно, поэтому `UniqueValidator` навешан вручную в `validate_phone`) |
| POST | `/auth/login/` | JWT token pair (поле `phone`, не `username`). `LoginSerializer` нормализует телефон (`normalize_phone`) перед `authenticate()` — вход с `+7 (999) 999-99-11` находит аккаунт `79999999911`; неверные креды → 401 `{"detail": "No active account found with the given credentials"}` |
| POST | `/auth/refresh/` | Refresh access token |
| GET | `/chats/` | Мои чаты с `last_message`, `interlocutor` (для PRIVATE) и `unread_count` (по курсору прочтения), пагинация (50) |
| POST | `/chats/` | Создать чат (создатель — админ; для GROUP обязателен `name`, можно передать `member_ids` — участники добавляются атомарно в транзакции; ответ — формат ChatDetail) |
| GET | `/chats/{id}/` | Детали чата: `members` с флагом `is_admin` у каждого + `my_is_admin` текущего пользователя |
| PATCH | `/chats/{id}/` | Переименование GROUP-чата: тело только `{name}` (`ChatRenameSerializer`), права — админ чата (403 остальным), не GROUP → 400, пустое/то же название → ничего не пишем. Ответ — формат ChatDetail; участникам рассылается `chat_renamed` в личный канал |
| DELETE | `/chats/{id}/` | Удалить чат вместе с сообщениями: GROUP — только админ чата, PRIVATE — любой участник; остальным участникам приходит WS-закрытие 4004 |
| POST | `/chats/private/` | Создать/найти личный чат (идемпотентно: если PRIVATE-чат, где состоят оба пользователя, уже есть — вернёт его с 200, иначе создаст с 201). Внутри `transaction.atomic()` строки обоих пользователей берутся в `select_for_update` (порядок по `id`) — параллельные запросы пары не создают дубликаты |
| GET | `/chats/{id}/messages/` | История: страница 1 = **последние** 50 сообщений (сортировка `-created_at`), внутри страницы — по возрастанию времени |
| POST | `/chats/{id}/send/` | Отправить сообщение (REST fallback) + broadcast в WS-группу `chat_{id}` |
| POST | `/chats/{id}/read/` | Отметить чат прочитанным: сдвигает `Membership.last_read_at` участника на «сейчас», ответ `{"unread_count": 0}`. Не-участник → 404 (queryset списка чатов уже отфильтрован по участников) |
| POST | `/chats/{id}/add-member/` | Добавить участника (только админ, **только GROUP-чаты**) |
| POST | `/chats/{id}/remove-member/` | Удалить участника / выйти самому; нельзя удалить единственного админа; опустевший чат удаляется |
| GET | `/users/me/` | Профиль текущего пользователя (по JWT) |
| PATCH | `/users/me/` | Редактирование своего профиля: `phone`, `email`, `first_name`, `last_name` — все опциональны, непереданные поля остаются как есть; ответ — формат `GET /users/me/`. Телефон нормализуется как при регистрации и проверяется на уникальность с `exclude(pk=...)`; если `username` совпадал со старым телефоном, он меняется следом за ним (иначе новый владелец номера упрётся в unique `username` при регистрации). Смена телефона не затрагивает действующий JWT — в payload только `user_id` |
| GET | `/users/search/?q=` | Поиск по телефону/first_name/last_name, исключает себя, лимит 20. По телефону ищет подстрокой от цифр запроса (`normalize_phone`), поэтому `+7 999 …` находит аккаунт `7999…`; запрос без цифр идёт только по имени |
| GET | `/docs/` | Swagger UI |
| GET | `/schema/` | OpenAPI 3.0 schema |

Общие настройки DRF: JWT-аутентификация, `IsAuthenticated` по умолчанию, `PageNumberPagination` (PAGE_SIZE=50), DjangoFilterBackend, троттлинг (см. «🚦 Rate limiting → REST»).

`ChatViewSet.http_method_names = ["get", "post", "patch", "delete"]` — PUT отключён намеренно: полноценного «перезаписи чата» в приложении нет, `PATCH` существует только как переименование GROUP-чата (`ChatRenameSerializer` принимает одно поле `name`).

## 🚦 Rate limiting

### REST (DRF-троттлинг)

Счётчики живут в DRF-троттлинге, кэш — Redis: `CACHES` указывает на БД **1** (channel layer и presence — в БД 0). На LocMemCache лимит считался бы отдельно в каждом процессе.

`DEFAULT_THROTTLE_CLASSES` = `AnonRateThrottle` + `UserRateThrottle` + `ScopedRateThrottle`; последний пропускает запрос, если у view нет `throttle_scope`, поэтому включён глобально, а точечные лимиты задаются одной строкой на view.

| Scope | Лимит | Где задан | Ключ |
|-------|-------|-----------|------|
| `anon` | 120/min | settings | IP |
| `user` | 600/min | settings | user id |
| `auth` | 10/min | `LoginView`, `RefreshView` | IP |
| `register` | 5/min | `RegisterView` | IP |
| `send` | 60/min | `ChatViewSet.ACTION_THROTTLE_SCOPES["send_message"]` | user id |
| `read` | 120/min | `ChatViewSet.ACTION_THROTTLE_SCOPES["mark_read"]` — отметка прочтения вызывается при каждом открытии/фокусе вкладки | user id |
| `write` | 30/min | `create`, `create_private`, `add_member`, `remove_member`, `destroy`, `partial_update` | user id |
| `search` | 20/min | `UserSearchView` | user id |
| `profile` | 20/min | `MeView.get_throttles()` — только на `PATCH` (ошибка unique-валидации отвечает «занято ли», то есть это enumeration), GET остаётся на глобальном `user` | user id |
| `schema` | 30/hour | `SchemaView` | IP |

Превышение → **429** + `Retry-After`, тело `{"detail": "Request was throttled. Expected available in N seconds."}` (по-английски: `LANGUAGE_CODE = "en-us"`). Фронт показывает этот `detail` как есть: `auth.ts` берёт `e.response.data.detail` при входе/регистрации, `ChatWindow.vue` — при ошибке REST-отправки (scope `send`), строкой над полем ввода (`.send-error`).

`ChatViewSet.get_throttles()` проставляет `self.throttle_scope` из `ACTION_THROTTLE_SCOPES`: `get_throttles()` вызывается из `initial()` уже после `initialize_request()`, где DRF выставляет `self.action`, поэтому действие к этому моменту известно. `list`/`retrieve`/`messages` scope не имеют — для них работает только глобальный `user`.

`NUM_PROXIES = 1` — не косметика: при дефолтном `None` `SimpleRateThrottle.get_ident()` возвращает **весь** `X-Forwarded-For` склеенный, то есть идентификатор целиком подделывается заголовком запроса. С `1` берётся последний элемент — IP, который дописал nginx (`nginx.conf` ставит `X-Forwarded-For` в блоке `/api/`). В dev Vite-proxy XFF не добавляет, поэтому `xff is None` и используется `REMOTE_ADDR` (в dev все запросы приходят с IP контейнера frontend).

`LoginView`/`RefreshView`/`SchemaView` в `views.py` — сабклассы `TokenObtainPairView`/`TokenRefreshView`/`SpectacularAPIView`, нужны чтобы задать `throttle_scope`, а `LoginView` ещё и `serializer_class = LoginSerializer` (нормализация телефона перед `authenticate()`; SimpleJWT ищет пользователя точным совпадением, а телефон в БД хранится «чистым»).

`normalize_phone()` в `models.py` — единственная реализация правила «в телефоне только цифры»: она вырезает `+`, пробелы, скобки и дефисы. Каноничность на записи даёт `User.save()` (вызывает её же и для `create_user`, и для админки, и для фикстур), на чтении — `LoginSerializer` (иначе SimpleJWT искал бы строку точным совпадением и вход с разделителями давал бы 401), `RegisterSerializer.validate_phone` (из телефона дефолтом заполняется unique `username`) и `ProfileUpdateSerializer.validate_phone`. `UserSearchView` нормализует цифры запроса — поиск `+7 999 …` иначе не нашёл бы никого.

Нормализация склеивает форматы одного номера, но не разные номера: `+7999…` и `7999…` — один аккаунт, а `8999…` — другой (замену ведущего `8` на `7` не делаем). Наружу телефон отдаётся как лежит, то есть без `+`.

### WebSocket (`messenger/ratelimit.py`)

DRF-троттлинг до Channels-consumer'ов не дотягивается, поэтому лимиты свои: `RedisWindowLimiter` — фиксированное окно на Redis, один вызов Lua-скрипта `INCR` + `PEXPIRE` (атомарно), O(1) памяти и один round trip на событие. Компромисс — всплеск до 2× лимита на границе окна; скользящее окно (ZSET) было бы переплатой за точность. Ключи: `messenger:rl:conn:{user_id}` и `messenger:rl:msg:{user_id}`, БД 0 (та же, что presence и channel layer).

| Лимит | Значение | Ключ | Где | Превышение |
|-------|----------|------|-----|-----------|
| `MESSAGE_LIMITER` | 10 / 10 с | `messenger:rl:msg:{user_id}` | `receive_json()` | `{error: ...}` в тот же сокет, соединение живое |
| `CONNECT_LIMITER` | 20 / 60 с | `messenger:rl:conn:{user_id}` | `connect()` | close **4029**, клиент не reconnect'ится |
| `MAX_CONNECTIONS_PER_USER` | 5 | presence-хеш `messenger:presence` | `connect()` | close **4009** |

`MESSAGE_LIMITER` намеренно того же порядка, что REST-scope `send` (60/min) — иначе лимит обходится уходом в REST-fallback. Считается **до** валидации текста, чтобы мусором его не выжигать; при превышении дорогие части (запись в БД, broadcast всей группе) просто не выполняются, поэтому сокет не закрываем — эскалация потребовала бы reconnect-пластинки в UI ради выгоды, которой нет.

`CONNECT_LIMITER` и кап одновременных соединений проверяются в `connect()` **до** `accept()`. У сокета чата они только читают состояние (presence-счётчик ведёт канал уведомлений, поэтому отказ в сокете чата ничего не сдвигает); в канале уведомлений отказ происходит до инкремента — иначе отказ с откатом пришлось бы синхронизировать с `disconnect()`, который декрементит сам. Кап читается из уже существующего presence-счётчика (`HGET`), отдельного учёта не заводим; гонка «два соединения прошли кап» для ограничения неважна.

Daphne дополнительно ограничивает размер кадра: `--websocket-max-message-size 16384` в команде сервиса `backend` (дефолт 1 MiB). nginx в prod держит `limit_req` на `/api/` и `limit_conn` на `/ws/` (см. «🐳 Инфраструктура»).

## 🔌 WebSocket API

| Endpoint | Protocol | Description |
|----------|----------|-------------|
| `ws/chat/<uuid>/?token=<jwt>` | WS | Real-time одного чата (regex допускает только hex+дефисы) |
| `ws/notifications/?token=<jwt>` | WS | Личный канал пользователя: уведомления по всем его чатам + presence/`last_seen` |

**События чата (сервер → клиент), канал `ws/chat/`:**

| type | Payload | Описание |
|------|---------|----------|
| *(нет поля type)* | `{id, chat, sender: {id, phone, first_name, last_name}, text, created_at, is_read}` | Новое сообщение (идентичный формат из WS-отправки и REST-broadcast) |
| `user_status` | `{user_id, status: "online"\|"offline"}` | Статус пользователя (фронт хранит в `onlineUsers`: точка в шапке PRIVATE-чата, счётчик «в сети» в шапке GROUP и точки в модалке участников) |
| `messages_read` | `{reader_id}` | Кто-то прочитал сообщения (приходит и самому читателю — клиент обязан фильтровать по `reader_id != my_id`) |
| `initial_presence` | `{user_ids: [...]}` | При подключении: id участников чата, которые сейчас онлайн (отправляется только подключившемуся клиенту; состав не зависит от типа чата, поэтому для GROUP это готовый снимок всего состава) |
| `{error: "..."}` | — | Сервер отклонил событие: пустое/слишком длинное/некорректный формат сообщение или анти-флуд (`MESSAGE_LIMITER`). Соединение при этом остаётся открытым |

**События клиент → сервер:**

| type | Payload | Описание |
|------|---------|----------|
| *(json)* | `{text: "..."}` | Отправка сообщения; membership перепроверяется при каждой отправке, затем анти-флуд (`MESSAGE_LIMITER`), `last_seen` отправителя обновляется с троттлингом 60 с |

**Поведение при подключении:** проверка JWT → отказ анонимам (4001) → лимит частоты подключений (`CONNECT_LIMITER`, отказ 4029) → кап одновременных соединений (presence-счётчик, отказ 4009) → проверка membership → отказ не-участникам (4003) → `group_add("chat_{id}")` + `accept` → отметка чужих непрочитанных как прочитанных (глобальный `Message.is_read`, для галочки ✓✓) + broadcast `messages_read` → отправка `initial_presence` подключившемуся клиенту.

**Поведение при отключении:** только `group_discard`. Presence и `last_seen` сокет чата не трогает — см. ниже (закрытие чата через Esc/крестик не «выключает» пользователя).

**События личного канала (сервер → клиент), канал `ws/notifications/`:**

| type | Payload | Описание |
|------|---------|----------|
| `new_message` | `{chat, message: {...как в чате...}, unread_count}` | Сообщение в **любом** чате пользователя. `unread_count` считает сервер по курсору получателя — клиент своё не хранит |
| `chat_read` | `{chat}` | Курсор прочтения сдвинут (POST `/chats/{id}/read/`) — убрать бейдж в других вкладках/устройствах |
| `chat_deleted` | `{chat}` | Чат удалён — убрать из списка, даже когда его сокет был закрыт |
| `chat_renamed` | `{chat, name}` | Название GROUP-чата изменено (PATCH `/chats/{id}/`) — обновить заголовок в списке и в шапке |
| `member_removed` | `{chat}` | Пользователя удалили из чата — убрать из списка |

Отправлять в этот канал нечего: `receive()` — no-op, только читает.

**Поведение при подключении (личный канал):** JWT (4001) → `CONNECT_LIMITER` (4029) → кап соединений (4009) → `accept` → `last_seen` → presence `HINCRBY messenger:presence {user_id} 1`, и **только при счётчике 1** — `user_status: online` во **все** группы чатов пользователя. Кап и лимит общие с сокетом чата: бюджет `messenger:rl:conn:{user_id}` и счётчик presence одни на оба канала, то есть «вкладка с открытым чатом» = 2 соединения из 5.

**Поведение при отключении (личный канал):** presence `HINCRBY -1`; при счётчике 0 — `last_seen` и `user_status: offline` во все группы чатов. Отказ до `accept` счётчик не трогает.

**Уведомления о новом сообщении** рассылаются из `publish_new_message(message)` — её вызывают обе точки создания сообщения (`ChatConsumer.receive_json` и REST `POST /chats/{id}/send/`). Получатели — участники, кроме отправителя, каждому в свою группу `user_{uid}`; счётчик на каждого свой, поэтому это N адресных `group_send`, а не один broadcast в группу чата, и считает его одна агрегатная выборка `unread_counts_per_user`.

**Удаление участника:** REST `remove-member` шлёт `member.removed` в группу чата (consumer удалённого закрывает его сокет с 4003) и в его личный канал (фронт убирает чат из списка).

**Удаление чата:** REST `DELETE /chats/{id}/` шлёт `chat.deleted` в группу чата (все её сокеты закрываются с 4004) и в личные каналы участников.

**Переименование чата:** REST `PATCH /chats/{id}/` шлёт `chat.renamed` **только** в личные каналы участников — в группу чата ничего не идёт, потому что закрывать там нечего, названия берутся из элемента списка чатов, а личный канал открыт всё время, пока приложение запущено. Поэтому у `ChatConsumer` обработчика `chat_renamed` нет и не нужно.

**Коды закрытия:**

| Code | Причина |
|------|---------|
| 4001 | Невалидный/отсутствующий JWT (оба канала) |
| 4003 | Пользователь не участник чата (в т.ч. удалён из чата во время сессии) — сокет чата |
| 4004 | Чат удалён — сокет чата |
| 4009 | Превышен кап одновременных соединений (`MAX_CONNECTIONS_PER_USER`) |
| 4029 | Превышена частота подключений (`CONNECT_LIMITER`) за окно |

Фронт не reconnect'ится ни на один из этих кодов — при лимитах переподключение только продлевает бан (счётчик пополняется каждым новым handshake).

## 🖥 Frontend (Vue 3 SPA)

- **auth.ts (Pinia)**: `UserProfile` (формат `/users/me/`) и `ProfileUpdatePayload` типизированы и экспортируются; login/register сохраняют токены в localStorage и грузят профиль через `/users/me/`; `getUserFromToken()` при перезагрузке восстанавливает из JWT только `id` (payload не содержит имени/телефона), полный профиль догружает `App.vue` в `onMounted`. `updateProfile(payload)` — `PATCH /users/me/`, при успехе заменяет `user` ответом сервера и возвращает true, при ошибке собирает текст (`detail`, иначе первое поле из DRF-овских `{field: ["..."]}`) в `error` и возвращает false. Logout — только очистка localStorage.
- **chat.ts (Pinia)**: `loadChats()`, `selectChat()` (первая страница сообщений + `loadChatDetails()` + `markRead()`), `markRead(chatId)` (`POST /chats/{id}/read/`, при успехе обнуляет бейдж; при ошибке счётчик остаётся серверным), `setUnread(chatId, n)` (если чата в списке нет — нас только что добавили: перечитываем `loadChats()`), `applyNewMessage(message, unreadCount)` (превью + бейдж; если этот чат открыт на видимой вкладке — сразу подтверждаем прочтение через `markRead`, иначе ставим серверный счётчик), `loadOlderMessages()` (prepend следующей страницы; состояние `messagesPage`/`hasMoreMessages`/`isLoadingHistory`; guard от смены чата во время запроса), `reloadMessages()` (перечитывание первой страницы после WS-reconnect: если вернулась полная страница (`MESSAGES_PAGE_SIZE = 50`) — история перезагружается целиком, иначе новые сообщения добираются в хвост, а у уже известных обновляется `is_read`), `addMessage()` (дедупликация по id + обновление превью в sidebar), `markAllRead()`, `removeChat()` (чат исчез для нас — удаление/вылет), `deleteChat(chatId)` (`DELETE /chats/{id}/` и `removeChat()` сразу, не дожидаясь WS: сервер сам разнесёт `chat.deleted` остальным; ошибку не глотает — текст `detail` нужен кнопке), `applyRename(chatId, name)` (локальное применение нового названия — ответ PATCH или событие `chat_renamed`; чата в списке нет → `loadChats()`; заодно правит `currentChatDetails.name`), `renameChat(chatId, name)` (`PATCH /chats/{id}/ {name}` → `applyRename` из ответа сервера), `closeChat()` (только снять выделение и очистить окно: крестик в шапке и Esc; WS закрывается сам через watch на `selectedChatId`; используется и при logout). Presence: `onlineUsers` (Set id) + `setUserStatus()`/`setInitialPresence()`; `selectChat()` очищает набор перед открытием чата — иначе до `initial_presence` показывались бы статусы участников предыдущего чата. Состояние WS: `wsStatus` (`WsStatus`, тип экспортируется и используется в useChatSocket) + `setWsStatus()` — пишет ChatWindow, отображает ChatSidebar; сбрасывается в `disconnected` в `closeChat()`. `ChatListItem.unread_count` приходит из REST и обновляется из личного канала.
- **api.ts**: axios с Bearer-interceptor; при 401 — один retry через `/auth/refresh/`, при неудаче — очистка токенов и редирект на `/login`. Из retry исключены сами auth-эндпоинты (`AUTH_URLS` + `isAuthRequest()` по `original.url`): 401 от `/auth/login/` — это «неверные креды», а не «протух токен», и попытка рефреша на них гасила бы ошибку входа перезагрузкой страницы. Редирект при неудачном рефреше пропускается, если мы уже на `/login` (перезагрузка стёрла бы текст ошибки).
- **useChatSocket.ts**: подключение по `chatId` (watch, immediate), reconnect через фиксированные 2 c, кроме кодов из `NO_RECONNECT_CODES` (4001/4003/4004 — доступ, 4009/4029 — WS-лимиты: переподключение только продлевает бан), `sendMessage()` возвращает false если сокет не открыт — вызывающий код уходит в REST fallback. Колбэки: `onMessage`, `onUserStatus`, `onMessagesRead`, `onInitialPresence`, `onError(message)` (сервер отклонил событие, сокет жив), `onClose(code)`, `onReconnect()`. Все хэндлеры сокета проверяют `ws !== socket` — события соединения, закрытого при смене чата, игнорируются. Флаг `hadConnection` отличает reconnect от первого подключения (сбрасывается в watch на `chatId`; считается установленным и после неудачного соединения, поэтому пропущенные сообщения добираются и при первом подключении со второй попытки).
- **useNotificationsSocket.ts**: личный канал `ws/notifications/`, монтируется в `ChatView.vue`. Только читает: `new_message`/`chat_read`/`chat_deleted`/`chat_renamed`/`member_removed` → колбэки в store (`chat_renamed` тащит ещё и `name`). Reconnect через фиксированные 2 c, кроме `NO_RECONNECT_CODES` (общий набор экспортирует `useChatSocket`); при 4001 (истёкший JWT) канал молчит до момента, когда вкладка снова станет видимой — `visibilitychange` поднимает соединение со свежим токеном из localStorage. Обрыв после сна/4001 — тот же путь. Хэндлеры проверяют `ws !== socket`.
- **ChatView.vue**: монтирует сайдбар + окно чата, в `onMounted` — `loadChats()`, личный канал уведомлений и `visibilitychange`: возврат во вкладку с выбранным чатом = `markRead()` (всё пришедшее в отсутствие считается прочитанным).
- **ChatWindow.vue**: отправка (WS → fallback REST `POST /send/`, при ошибке текст возвращается в input, а `detail` ответа — в строку `.send-error` над полем ввода, туда же уходит и 429 от REST-scope `send`); автоскролл по id **последнего** сообщения (догрузка истории его не сбрасывает); infinite scroll вверх (`scrollTop < 100` → `loadOlderMessages()` + якорь `scrollTop += Δ scrollHeight`); шапка: название чата — для GROUP и админа это `.chat-title-btn`, клик по которому открывает инлайн-редактор `.chat-rename-input` (Enter — `chatStore.renameChat()`, Esc — отмена; оба через `@keydown.*.stop`, чтобы Esc не ушёл в window-хэндлер и не закрыл чат; пустое имя и `detail` отказа 400/403/429 уходят в ту же строку под шапкой), presence под названием: точка «в сети»/«не в сети» собеседника (PRIVATE, `interlocutorOnline`) или счётчик участников в сети (GROUP, `onlineMembersCount` — участники из `currentChatDetails.members`, которых нет в `onlineUsers`, плюс `.presence-dot`/`.presence-dot.online` вместо инлайн-стиля), кнопка «Участники (N)» (GROUP), кнопка «Удалить чат» (`canDeleteChat`: GROUP — только при `my_is_admin` из деталей чата, PRIVATE — всегда) и крестик `×` справа; статус **WS-соединения** пробрасывается в store (`setWsStatus`) и показывается в сайдбаре; обработка `messages_read` с фильтром по `readerId`; `CLOSE_NOTICES` — плашка по коду закрытия: 4003/4004 (`FORGET_CHAT_CODES`) убирают чат из списка через `removeChat()` и показываются в empty-state, 4009/4029 оставляют чат выбранным (участник-то остался) и показываются баннером `.connection-notice` под шапкой; `onError` пишет текст отказа в `sendError`; по `onReconnect` — `reloadMessages()` + `loadChats()` (прокрутку к новым сообщениям выполняет watcher по `lastMessageId`); удаление чата — двухшаговое (`confirmDelete`: первый клик взводит кнопку и показывает `.header-notice` с текстом подтверждения, второй вызывает `chatStore.deleteChat()`; туда же уходит `detail` отказа — 403 не-админа и 429 от scope `write`; состояние сбрасывается в watcher по смене `selectedChatId`). Esc закрывает текущий чат (`closeChat()`), но уступает модалкам: если в DOM висит `.modal-overlay`, клавиша не трогает ничего — состояние модалок живёт в соседних компонентах, а их собственные обработчики навешаны на тот же `window`.
- **ChatSidebar.vue**: список чатов (имя + превью последнего сообщения + круглый бейдж непрочитанного в углу пункта, `99+` на больших числах; при `unread_count > 0` имя подсвечивается и утолщается), кнопка «+ Новый чат», в шапке — «Профиль» и «Выйти». Индикатор состояния **WS-соединения** текущего чата (точка + «на связи» / «подключение...» / «нет соединения») — второй строкой **под** приветствием (в один ряд с именем и кнопками шапки не влезает), отображается только когда чат выбран.
- **GroupMembersModal.vue**: список участников (бейджи «админ»/«вы» + presence-точка `.member-presence` у каждого, над списком — строка «N в сети из M», `onlineCount`/`others` без самого себя, как в шапке); админ — debounced-поиск и добавление (`POST add-member/`), удаление любого (`POST remove-member/`); любой участник — «Выйти» (выход из чата, при опустевшем чате сервер его удаляет). Присутствие берётся из `chatStore.onlineUsers` — снимок того чата, сокет которого сейчас открыт, поэтому у добавленного после подключения участника точка будет «не в сети» до перезахода в чат (`user_status` анонсируется только на переходах 0→1/1→0). Esc и клик по оверлею закрывают модалку, чат под ней остаётся открытым.
- **NewChatModal.vue**: режимы «Личный / Групповой». Личный: debounced-поиск (300 мс) → `POST /chats/private/` → обновление списка + **автовыбор** чата. Групповой: название + мультивыбор пользователей из поиска (chips) → `POST /chats/ {type, name, member_ids}` → автовыбор. Esc и клик по оверлею закрывают модалку.
- **ProfileModal.vue**: модалка своего профиля, открывается кнопкой «Профиль» в шапке сайдбара. Поля — имя, фамилия, email, телефон; подставляются из `auth.user` при каждом открытии (`watch` на `isOpen`), пустые email/имя отправляются как `""` и означают «очистить». Отправка — `auth.updateProfile()` (PATCH `/users/me/`), по успеху модалка закрывается и имя в шапке сайдбара обновляется сами собой (`user` в store заменяется ответом сервера). Телефон — логин, поэтому под полем висит `.form-hint` про вход новым номером. Ошибку показываем локально (копируем `auth.error`, либо своя проверка на пустой телефон до запроса), Esc и клик по оверлею закрывают.
- **LoginView.vue**: вход и регистрация в одной форме. Email/имя/фамилия необязательны — пустые значения вырезаются из payload перед `POST /auth/register/` (бэкенд трактует `""` как невалидный email). У всех полей проставлены `name` + `autocomplete` (`username` / `current-password` на входе, `new-password` на регистрации): без них браузер подставляет в логин сохранённую учётку от другой записи, и вход даёт 401 при верном пароле (пароли в БД хешируются, поэтому `name` ничего не «светит»).
- Роуты: `/login`, `/` (requiresAuth), catch-all → `/login`.

## 🐳 Инфраструктура

- **docker-compose.yml**: сервисы `db` (postgres:18, порт на хосте **5434**, healthcheck), `redis` (7-alpine, healthcheck, порт 6379), `backend` (build из корневого Dockerfile; команда: migrate → collectstatic → daphne с `--websocket-max-message-size 16384`; bind-mount `.:/app`; volume `media_data`), `frontend` (target `dev`, bind-mount исходников для HMR). **Сервис называется `backend`, не `web`.**
- **Dockerfile (backend)**: python:3.13-slim, multi-stage (pip `--prefix=/install`), непривилегированный `appuser`.
- **Dockerfile (frontend)**: node:22-alpine, `npm ci`; target `dev` — Vite с `--host 0.0.0.0`; target `prod` — билд + nginx с `nginx.conf` (upstream `backend:8000`).
- **nginx.conf (prod)**: `limit_req` на `/api/` (30 r/s на IP, burst 60 nodelay) и `limit_conn 10` на `/ws/`; обе зоны — `$binary_remote_addr`, ответы 429. Работает только на prod-таргете, в compose поднят `dev`.
- **`.dockerignore`** (корневой, для образа backend): исключает `.git`, `.env*`, `.venv/`, кэши (`.ruff_cache/`, `.pytest_cache/`, `.mypy_cache/`), `frontend/` (у фронтенда собственный build-контекст `./frontend`), `node_modules/`, `media/`, `staticfiles/`.
- Статика: WhiteNoise (`CompressedManifestStaticFilesStorage`), `collectstatic` выполняется в команде compose.
- Swagger UI + drf-spectacular с JWT security scheme и persistAuthorization.

## 🔧 Команды разработки

```bash
# Поднять всё
docker compose up --build -d

# Миграции / админка / shell (сервис — backend!)
docker compose exec backend python manage.py makemigrations
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py createsuperuser
docker compose exec backend python manage.py shell
docker compose logs -f

# Frontend локально (вне Docker): proxy ожидает хост `backend`,
# поэтому npm run dev имеет смысл только внутри compose
cd frontend && npm ci && npm run dev        # Vite
cd frontend && npm run type-check           # vue-tsc
cd frontend && npm run build                # type-check + vite build

# Линт бэкенда (ruff установлен в requirements)
ruff check .

# Полный сброс БД
docker compose down -v && docker compose up --build -d
```

## 🚧 В планах (приоритет по убыванию)

### Phase 2: Features
- [x] Визуальный онлайн-статус собеседника (presence в Redis + `initial_presence` + точка в шапке PRIVATE-чата)
- [x] Создание групповых чатов из UI (режим «Групповой» в NewChatModal, `member_ids` в `POST /chats/`)
- [x] Добавление/удаление участников группового чата из UI (GroupMembersModal, выход из чата, live-закрытие сокета удалённого участника)
- [x] Пагинация сообщений в UI (infinite scroll вверх с якорем позиции)
- [x] Перечитывание истории после WS-reconnect
- [x] Счётчик непрочитанного в списке чатов (курсор `Membership.last_read_at`, `unread_count` в REST, личный WS-канал `ws/notifications/`, бейдж в сайдбаре)
- [x] Удаление чата из UI (кнопка в шапке окна: GROUP — только админ по `my_is_admin`, PRIVATE — любой участник; подтверждение вторым кликом, бэкенд `DELETE /chats/{id}/` + WS-закрытие 4004 и `chat.deleted` в личные каналы были готовы)
- [x] Переименование GROUP-чата (`PATCH /chats/{id}/` + `ChatRenameSerializer`, инлайн-редактор в заголовке окна для админа, `chat_renamed` в личный канал)
- [x] Онлайн-статус участников в групповых чатах (счётчик «в сети» в шапке + точки в `GroupMembersModal`; данные были и так — `initial_presence` отдаёт всех онлайн-участников, `user_status` анонсируется во все группы чатов, поэтому правка чисто презентационная)
- [ ] Typing indicators («печатает...»)
- [ ] Загрузка файлов и изображений (MEDIA_* в settings заданы, но media не раздаётся)
- [ ] Message editing / deletion
- [ ] Push notifications

### Phase 3: Quality & Ops
- [ ] pytest + factory_boy + coverage > 80% (tests.py сейчас пустой)
- [ ] Vitest для frontend unit-тестов
- [ ] ESLint/Prettier + pre-commit hooks (ruff для backend уже есть)
- [ ] CI/CD (GitHub Actions)
- [x] Rate limiting REST (DRF-троттлинг: глобальные anon/user + scope'ы auth/register/send/write/search/schema, счётчики в Redis)
- [x] Rate limiting WebSocket (`RedisWindowLimiter`: анти-флуд сообщений, частота подключений, кап на одновременные соединения; коды 4009/4029)
- [x] `limit_req`/`limit_conn` в nginx (`frontend/nginx.conf`: `/api/` 30 r/s + burst 60, `/ws/` до 10 соединений на IP, ответы — 429)
- [ ] Prod-обвязка: `frontend` в compose на таргете `prod` (сейчас `dev`), SSL, настройки из env — без этого `nginx.conf` с лимитами не исполняется
- [ ] Обработка 429 на фронте (текст DRF показывается как есть, он по-английски; локализации и `Retry-After`-таймера нет)
- [ ] Logging + Sentry
- [ ] Production deploy: prod-сервис frontend в compose, SSL, настройки из env

## ⚠️ Известные ограничения / Tech Debt

1. **SECRET_KEY и DEBUG захардкожены** в `settings.py`; `DJANGO_SECRET_KEY`/`DJANGO_DEBUG` из `.env.example` **не читаются** — перед деплоем перевести на env.
2. **Rate limiting не покрывает `/admin/`** — DRF-троттлинг работает только на DRF-view'ах, Django Admin не ограничен ничем; WS-лимитеры в `ChatConsumer` есть, но nginx-слой с `limit_req`/`limit_conn` исполняется только на prod-таргете фронтенда (в compose поднят `dev`).
3. **Нет тестов** — покрытие 0% (backend + frontend).
4. **`is_read` глобальный на сообщение** — в групповом чате прочтение одним участником помечает сообщение прочитанным для всех. Частично закрыто: бейдж непрочитанного считается по per-user курсору `Membership.last_read_at`, но галочка ✓✓ по-прежнему опирается на глобальный флаг — read-receipt на пару (сообщение, пользователь) не заводили.
5. **Валидация пароля отключена** — `validate_password` и `min_length` в RegisterSerializer закомментированы; `AUTH_PASSWORD_VALIDATORS` в DRF не применяются автоматически.
6. **requirements.txt**: gunicorn не используется (сервер — Daphne), ruff — dev-инструмент в prod-образе.
7. **Concurrent 401** — interceptor в api.ts не блокирует параллельные refresh-запросы (ротация refresh-токенов не включена, поэтому не критично).
8. **`create_private` полагается на `select_for_update`** — защита от дубликатов работает только на Postgres; на SQLite (например, в будущих тестах) запрос упадёт с `NotSupportedError`.
