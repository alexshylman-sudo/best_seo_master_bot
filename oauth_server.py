"""
Pinterest OAuth 2.0 Server for Render.com
Standalone version - no local imports needed
"""

from flask import Flask, request, redirect, session, make_response
import requests
import os
import base64
import traceback
import sys
import psycopg2
from psycopg2.extras import RealDictCursor

# Загружаем .env (для локальной разработки)
try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# Pinterest OAuth настройки
PINTEREST_APP_ID = os.environ.get('PINTEREST_APP_ID')
PINTEREST_APP_SECRET = os.environ.get('PINTEREST_APP_SECRET')
REDIRECT_URI = os.environ.get('PINTEREST_REDIRECT_URI', 'http://localhost:5000/pinterest/callback')

# Database настройки
DB_HOST = os.environ.get('DB_HOST')
DB_NAME = os.environ.get('DB_NAME')
DB_USER = os.environ.get('DB_USER')
DB_PASS = os.environ.get('DB_PASS')
DB_PORT = os.environ.get('DB_PORT', '5432')

# ОТКЛЮЧАЕМ ПРОКСИ
NO_PROXY = {'http': None, 'https': None}


def get_db_connection():
    """Получить соединение с БД"""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            port=DB_PORT
        )
        return conn
    except Exception as e:
        print(f"[DB Error] {e}")
        return None


def update_project_info(project_id, data):
    """Обновить info проекта в БД"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            # Получаем текущий info
            cur.execute("SELECT info FROM projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
            
            if row:
                import json
                current_info = row[0] if row[0] else {}
                if isinstance(current_info, str):
                    current_info = json.loads(current_info)
                
                # Обновляем
                current_info.update(data)
                
                # Сохраняем
                cur.execute(
                    "UPDATE projects SET info = %s, step_progress = 'Создан' WHERE id = %s",
                    (json.dumps(current_info), project_id)
                )
                conn.commit()
                return True
    except Exception as e:
        print(f"[DB Update Error] {e}")
        conn.rollback()
    finally:
        conn.close()
    
    return False


def get_user_language(user_id):
    """Получить язык пользователя"""
    conn = get_db_connection()
    if not conn:
        return 'en'
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT language FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row:
                return row.get('language', 'en')
    except:
        pass
    finally:
        conn.close()
    
    return 'en'


def log_debug(message):
    """Логирование"""
    print(f"[OAuth] {message}", flush=True)


@app.after_request
def after_request(response):
    """Заголовки для каждого ответа"""
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    response.headers['ngrok-skip-browser-warning'] = 'true'
    return response


@app.route('/')
def index():
    """Главная страница"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Pinterest OAuth</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; text-align: center; }
        </style>
    </head>
    <body>
        <h1>🎨 Pinterest OAuth Server</h1>
        <p>Server is running!</p>
        <p><a href="/health">Health Check</a></p>
    </body>
    </html>
    """


@app.route('/pinterest/auth')
def pinterest_auth():
    """Инициализация OAuth"""
    user_id = request.args.get('user_id')
    project_id = request.args.get('project_id')
    
    if not user_id or not project_id:
        return "Error: Missing user_id or project_id", 400
    
    auth_url = (
        f"https://www.pinterest.com/oauth/?"
        f"client_id={PINTEREST_APP_ID}&"
        f"redirect_uri={REDIRECT_URI}&"
        f"response_type=code&"
        f"scope=boards:read,boards:write,pins:read,pins:write,user_accounts:read&"
        f"state={user_id}_{project_id}"
    )
    
    return redirect(auth_url)


@app.route('/pinterest/callback')
def pinterest_callback():
    """Callback от Pinterest"""
    
    log_debug("=== CALLBACK START ===")
    
    try:
        code = request.args.get('code')
        error = request.args.get('error')
        state = request.args.get('state')
        
        log_debug(f"Code: {bool(code)}, Error: {error}, State: {state}")
        
        # Ошибка от Pinterest
        if error:
            return make_response(f"""
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>Error</title></head>
            <body style="font-family: Arial; max-width: 600px; margin: 50px auto; text-align: center;">
                <h1>❌ Pinterest Error</h1>
                <p>{error}</p>
            </body>
            </html>
            """, 400)
        
        # Нет code или state
        if not code or not state:
            return make_response("""
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>Error</title></head>
            <body style="font-family: Arial; max-width: 600px; margin: 50px auto; text-align: center;">
                <h1>❌ Missing Parameters</h1>
                <p>Authorization code or state is missing.</p>
                <p>Please try again from the bot.</p>
            </body>
            </html>
            """, 400)
        
        # Парсим state
        try:
            parts = state.split('_')
            user_id = int(parts[0])
            project_id = int(parts[1])
        except:
            return make_response(f"""
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>Error</title></head>
            <body style="font-family: Arial; max-width: 600px; margin: 50px auto; text-align: center;">
                <h1>❌ Invalid State</h1>
                <p>State: {state}</p>
            </body>
            </html>
            """, 400)
        
        # Проверка конфигурации
        if not PINTEREST_APP_ID or not PINTEREST_APP_SECRET:
            return make_response("""
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>Config Error</title></head>
            <body style="font-family: Arial; max-width: 600px; margin: 50px auto; text-align: center;">
                <h1>❌ OAuth Not Configured</h1>
                <p>Missing PINTEREST_APP_ID or PINTEREST_APP_SECRET</p>
            </body>
            </html>
            """, 500)
        
        # Обмен code на token
        log_debug("Exchanging code for token...")
        
        token_url = "https://api.pinterest.com/v5/oauth/token"
        auth_string = f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}"
        auth_bytes = base64.b64encode(auth_string.encode()).decode()
        
        token_data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI
        }
        
        headers = {
            'Authorization': f'Basic {auth_bytes}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        try:
            response = requests.post(
                token_url,
                data=token_data,
                headers=headers,
                timeout=30,
                proxies=NO_PROXY
            )
        except requests.exceptions.RequestException as e:
            log_debug(f"Request error: {e}")
            return make_response(f"""
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>Network Error</title></head>
            <body style="font-family: Arial; max-width: 600px; margin: 50px auto; text-align: center;">
                <h1>❌ Network Error</h1>
                <p>{str(e)[:200]}</p>
            </body>
            </html>
            """, 500)
        
        log_debug(f"Token response: {response.status_code}")
        
        if response.status_code != 200:
            return make_response(f"""
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>Token Error</title></head>
            <body style="font-family: Arial; max-width: 600px; margin: 50px auto; text-align: center;">
                <h1>❌ Token Exchange Failed</h1>
                <p>Status: {response.status_code}</p>
                <pre style="background: #f5f5f5; padding: 10px; text-align: left; overflow-x: auto;">{response.text[:500]}</pre>
            </body>
            </html>
            """, 500)
        
        # Парсим токен
        token_response = response.json()
        access_token = token_response.get('access_token')
        refresh_token = token_response.get('refresh_token', '')
        expires_in = token_response.get('expires_in', 0)
        
        if not access_token:
            return make_response("""
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>Error</title></head>
            <body style="font-family: Arial; max-width: 600px; margin: 50px auto; text-align: center;">
                <h1>❌ No Access Token</h1>
            </body>
            </html>
            """, 500)
        
        log_debug(f"Token received: {access_token[:20]}...")
        
        # Получаем username
        pinterest_username = 'Connected'
        pinterest_id = ''
        
        try:
            user_response = requests.get(
                "https://api.pinterest.com/v5/user_account",
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=10,
                proxies=NO_PROXY
            )
            if user_response.status_code == 200:
                user_data = user_response.json()
                pinterest_username = user_data.get('username', 'Connected')
                pinterest_id = user_data.get('id', '')
                log_debug(f"Pinterest user: @{pinterest_username}")
        except:
            pass
        
        # Сохраняем в БД
        from datetime import datetime
        connection_date = datetime.now().strftime('%Y-%m-%d %H:%M')
        
        update_project_info(project_id, {
            'api_key': access_token,
            'refresh_token': refresh_token,
            'token_expires_in': expires_in,
            'pinterest_username': pinterest_username,
            'pinterest_id': pinterest_id,
            'connected': True,
            'oauth_completed': True,
            'connection_date': connection_date
        })
        
        log_debug(f"Project {project_id} updated")
        
        # Отправляем уведомление в Telegram
        try:
            bot_token = os.environ.get('BOT_TOKEN')
            if bot_token:
                user_lang = get_user_language(user_id)
                
                if user_lang == 'ru':
                    msg = (
                        f"✅ <b>Pinterest подключён!</b>\n\n"
                        f"📌 Аккаунт: @{pinterest_username}\n"
                        f"🎯 Проект #{project_id} готов к работе"
                    )
                    btn = "📂 Открыть проект"
                else:
                    msg = (
                        f"✅ <b>Pinterest connected!</b>\n\n"
                        f"📌 Account: @{pinterest_username}\n"
                        f"🎯 Project #{project_id} is ready"
                    )
                    btn = "📂 Open project"
                
                requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        'chat_id': user_id,
                        'text': msg,
                        'parse_mode': 'HTML',
                        'reply_markup': {
                            'inline_keyboard': [[
                                {'text': btn, 'callback_data': f'open_project_{project_id}'}
                            ]]
                        }
                    },
                    timeout=5,
                    proxies=NO_PROXY
                )
                log_debug("Telegram notification sent")
        except Exception as e:
            log_debug(f"Telegram error: {e}")
        
        log_debug("=== SUCCESS ===")
        
        # Успешная страница
        bot_username = os.environ.get('BOT_USERNAME', '')
        bot_link = f"https://t.me/{bot_username}" if bot_username else "#"
        
        return make_response(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Success!</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    max-width: 600px;
                    margin: 50px auto;
                    padding: 20px;
                    text-align: center;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                }}
                .card {{
                    background: white;
                    padding: 40px;
                    border-radius: 16px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                }}
                .icon {{ font-size: 64px; }}
                h1 {{ color: #4caf50; }}
                .info {{
                    background: #f5f5f5;
                    padding: 15px;
                    border-radius: 8px;
                    margin: 20px 0;
                }}
                .btn {{
                    display: inline-block;
                    background: #0088cc;
                    color: white;
                    padding: 15px 30px;
                    border-radius: 8px;
                    text-decoration: none;
                    margin-top: 20px;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="icon">✅</div>
                <h1>Pinterest Connected!</h1>
                <div class="info">
                    <p><b>Account:</b> @{pinterest_username}</p>
                    <p><b>Project:</b> #{project_id}</p>
                </div>
                <p>You can now create and publish pins!</p>
                <a href="{bot_link}" class="btn">📱 Return to Bot</a>
                <p style="color: #666; margin-top: 20px;">Or close this window</p>
            </div>
        </body>
        </html>
        """)
        
    except Exception as e:
        log_debug(f"CRITICAL: {e}")
        traceback.print_exc()
        return make_response(f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"><title>Error</title></head>
        <body style="font-family: Arial; max-width: 600px; margin: 50px auto; text-align: center;">
            <h1>❌ Error</h1>
            <p>{str(e)}</p>
        </body>
        </html>
        """, 500)


@app.route('/health')
def health():
    """Health check для Render"""
    db_ok = get_db_connection() is not None
    return {
        'status': 'ok',
        'database': 'connected' if db_ok else 'error',
        'pinterest_configured': bool(PINTEREST_APP_ID and PINTEREST_APP_SECRET)
    }


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting Pinterest OAuth Server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
