# Pinterest OAuth Server for Render.com

## Быстрый деплой

### 1. Создай репозиторий на GitHub
Загрузи эти файлы в новый репозиторий.

### 2. На Render.com
1. Dashboard → **New** → **Web Service**
2. Подключи GitHub репозиторий
3. Настройки:
   - **Name:** `pinterest-oauth` (или любое)
   - **Region:** Frankfurt (EU) - ближе к тебе
   - **Branch:** `main`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn oauth_server:app`

### 3. Environment Variables
Добавь переменные (Environment → Add Environment Variable):

```
PINTEREST_APP_ID=твой_app_id
PINTEREST_APP_SECRET=твой_app_secret
PINTEREST_REDIRECT_URI=https://твой-сервис.onrender.com/pinterest/callback
BOT_TOKEN=токен_бота
BOT_USERNAME=username_бота_без_собаки
DB_HOST=хост_бд
DB_NAME=имя_бд
DB_USER=пользователь_бд
DB_PASS=пароль_бд
DB_PORT=5432
```

### 4. После деплоя
1. Скопируй URL сервиса (например: `https://pinterest-oauth.onrender.com`)
2. Обнови **PINTEREST_REDIRECT_URI** на Render:
   ```
   https://pinterest-oauth.onrender.com/pinterest/callback
   ```
3. Обнови **Redirect URI** в Pinterest Developer Console на тот же URL
4. Обнови **PINTEREST_REDIRECT_URI** в `.env` локального бота

### 5. Проверка
Открой в браузере:
```
https://твой-сервис.onrender.com/health
```

Должен показать:
```json
{"status": "ok", "database": "connected", "pinterest_configured": true}
```

## Файлы

- `oauth_server.py` - основной сервер
- `requirements.txt` - зависимости Python
- `render.yaml` - конфиг для Render (опционально)
- `README.md` - эта инструкция

## Важно

- Render бесплатный тариф "засыпает" через 15 минут неактивности
- Первый запрос после сна занимает ~30 секунд
- Для продакшена лучше платный тариф ($7/мес) или свой VPS
