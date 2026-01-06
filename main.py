import os
import threading
import time
import schedule
import psycopg2
import json
import requests
import datetime
import io
import re
import base64
import random
from urllib.parse import urlparse, urljoin, quote
from telebot import TeleBot, types
from flask import Flask
from google import genai
from google.genai import types as genai_types
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# --- 1. КОНФИГУРАЦИЯ ---
load_dotenv()

ADMIN_ID = int(os.getenv("ADMIN_ID", "203473623")) 
SUPPORT_ID = 203473623 
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("APP_URL")

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)
USER_CONTEXT = {} 

# --- 2. БАЗА ДАННЫХ ---
def get_db_connection():
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        print(f"❌ DB Error: {e}")
        return None

def patch_db_schema():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_paid_rub INT DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_paid_stars INT DEFAULT 0")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS cms_login TEXT")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS cms_password TEXT")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS cms_url TEXT")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS cms_key TEXT")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS content_plan JSONB DEFAULT '[]'")
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS seo_data JSONB DEFAULT '{}'") 
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS scheduled_time TIMESTAMP")
        conn.commit()
    except Exception as e: 
        print(f"⚠️ Ошибка патчинга БД: {e}")
    finally: cur.close(); conn.close()

def init_db():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance INT DEFAULT 0,
            tariff TEXT DEFAULT 'Нет тарифа',
            tariff_expires TIMESTAMP,
            gens_left INT DEFAULT 2,
            is_admin BOOLEAN DEFAULT FALSE,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_paid_rub INT DEFAULT 0,
            total_paid_stars INT DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            type TEXT DEFAULT 'site',
            url TEXT,
            info JSONB DEFAULT '{}', 
            knowledge_base JSONB DEFAULT '[]', 
            keywords TEXT,
            cms_url TEXT,
            cms_login TEXT,
            cms_password TEXT,
            cms_key TEXT,
            platform TEXT,
            frequency INT DEFAULT 0,
            content_plan JSONB DEFAULT '[]',
            progress JSONB DEFAULT '{"info_done": false, "analysis_done": false, "upload_done": false, "competitors_done": false, "settings_done": false}', 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id SERIAL PRIMARY KEY,
            project_id INT,
            title TEXT,
            content TEXT,
            seo_data JSONB DEFAULT '{}',
            status TEXT DEFAULT 'draft',
            rewrite_count INT DEFAULT 0,
            published_url TEXT,
            scheduled_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            amount INT,
            currency TEXT,
            tariff_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        INSERT INTO users (user_id, is_admin, tariff, gens_left) 
        VALUES (%s, TRUE, 'GOD_MODE', 9999) 
        ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE, tariff = 'GOD_MODE', gens_left = 9999
    """, (ADMIN_ID,))
    conn.commit(); cur.close(); conn.close()
    patch_db_schema()

def update_last_active(user_id):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET last_active = NOW() WHERE user_id = %s", (user_id,))
        conn.commit(); cur.close(); conn.close()
    except: pass

# --- 3. УТИЛИТЫ ---
def send_safe_message(chat_id, text, parse_mode='HTML', reply_markup=None):
    if not text: return
    parts = []
    chunk_size = 3500 
    while len(text) > 0:
        if len(text) > chunk_size:
            split_pos = text.rfind('\n', 0, chunk_size)
            if split_pos == -1: split_pos = chunk_size
            parts.append(text[:split_pos])
            text = text[split_pos:]
        else:
            parts.append(text)
            text = ""
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        try: bot.send_message(chat_id, part, parse_mode=parse_mode, reply_markup=markup)
        except: 
            try: bot.send_message(chat_id, part, parse_mode=None, reply_markup=markup)
            except: pass
        time.sleep(0.3)

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"Ошибка AI: {e}"

def validate_input(text, question_context):
    if text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка", "🔙 В меню"]:
        return False, "MENU_CLICK"
    try:
        prompt = f"Модератор. Вопрос: '{question_context}'. Ответ: '{text}'. Проверь на мат, спам или бессмыслицу. Если плохо - ответь BAD. Если нормально - ответь OK."
        res = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt]).text.strip()
        return ("BAD" not in res.upper()), "AI_CHECK"
    except: return True, "SKIP"

def check_site_availability(url):
    try:
        response = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        return response.status_code == 200
    except: return False

def deep_analyze_site(url):
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 Bot"})
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = soup.title.string if soup.title else "No Title"
        desc = soup.find("meta", attrs={"name": "description"})
        desc = desc["content"] if desc else "No Description"
        headers = [h.get_text().strip() for h in soup.find_all(['h1', 'h2', 'h3'])]
        raw_text = soup.get_text()[:5000].strip()
        internal_links = []
        domain = urlparse(url).netloc
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            full_url = urljoin(url, href)
            parsed_href = urlparse(full_url)
            if parsed_href.netloc == domain and not any(ext in parsed_href.path for ext in ['.jpg', '.png', '.pdf', '.css', '.js']):
                link_text = a_tag.get_text().strip()
                if link_text and len(link_text) > 3: 
                    internal_links.append({"url": full_url, "anchor": link_text})
        unique_links = {v['url']: v for v in internal_links}.values()
        top_links = list(unique_links)[:100] 
        analysis_text = f"URL: {url}\nTitle: {title}\nDesc: {desc}\nHeaders: {headers}\nContent Sample: {raw_text}"
        return analysis_text, top_links
    except Exception as e:
        return f"Ошибка доступа к сайту: {e}", []

def update_project_progress(pid, step_key):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT progress FROM projects WHERE id=%s", (pid,))
        res = cur.fetchone()
        prog = res[0] if res and res[0] else {}
        prog[step_key] = True
        cur.execute("UPDATE projects SET progress=%s WHERE id=%s", (json.dumps(prog), pid))
        conn.commit()
    except: pass
    finally: cur.close(); conn.close()

def format_html_for_chat(html_content):
    """Очищает HTML для чата"""
    text = str(html_content).replace('\\n', '\n')
    if '", "seo_title":' in text: text = text.split('", "seo_title":')[0]
    if '","seo_title":' in text: text = text.split('","seo_title":')[0]
    
    # Убираем плейсхолдеры картинок из чата
    text = re.sub(r'\[IMG:.*?\]', '', text)
    text = re.sub(r'<h[1-6]>(.*?)</h[1-6]>', r'\n\n<b>\1</b>\n', text)
    text = re.sub(r'<li>(.*?)</li>', r'• \1\n', text)
    
    soup = BeautifulSoup(text, "html.parser")
    for script in soup(["script", "style", "head", "title", "meta", "table", "style"]):
        script.decompose()
    
    clean_text = soup.get_text(separator="\n\n")
    clean_text = re.sub(r'\n\s*\n', '\n\n', clean_text).strip()
    clean_text = clean_text.strip('",}').strip()
    return clean_text

def generate_and_upload_image(api_url, login, pwd, image_prompt, alt_text):
    """Генерация (Google -> Flux) и загрузка в WP"""
    image_bytes = None
    
    # 1. Попытка Google
    try:
        response = client.models.generate_images(
            model='imagen-3.0-generate-001', 
            prompt=image_prompt,
            config=genai_types.GenerateImagesConfig(number_of_images=1)
        )
        if response.generated_images:
            image_bytes = response.generated_images[0].image.image_bytes
            print("Generated via Google")
    except Exception as e:
        print(f"Google img fail: {e}")

    # 2. Fallback на Flux
    if not image_bytes:
        try:
            seed = random.randint(1, 99999)
            safe_prompt = quote(image_prompt)
            image_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=768&seed={seed}&nologo=true"
            img_resp = requests.get(image_url, timeout=30)
            if img_resp.status_code == 200:
                image_bytes = img_resp.content
                print("Generated via Flux")
        except Exception as e:
            print(f"Flux fail: {e}")

    if not image_bytes: return None, None

    # 3. Загрузка в WP
    try:
        # Чистим URL от trailing slash
        if api_url.endswith('/'): api_url = api_url[:-1]
        
        seed = random.randint(1, 99999)
        file_name = f"img-{seed}.png"
        
        creds = f"{login}:{pwd}"
        token = base64.b64encode(creds.encode()).decode()
        headers = {
            'Authorization': 'Basic ' + token,
            'Content-Disposition': f'attachment; filename={file_name}',
            'Content-Type': 'image/png',
            'User-Agent': 'Mozilla/5.0'
        }
        
        upload_api = f"{api_url}/wp-json/wp/v2/media"
        r = requests.post(upload_api, headers=headers, data=image_bytes, timeout=60)
        
        if r.status_code == 201:
            media_id = r.json().get('id')
            source_url = r.json().get('source_url')
            # ALT
            requests.post(
                f"{upload_api}/{media_id}", 
                headers={'Authorization': 'Basic ' + token, 'Content-Type': 'application/json'}, 
                json={'alt_text': alt_text}, 
                timeout=10
            )
            return media_id, source_url
        else:
            print(f"WP Upload Fail: {r.status_code} {r.text}")
    except Exception as e:
        print(f"WP Upload Except: {e}")
    
    return None, None

# --- 4. МЕНЮ ---
def main_menu_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("➕ Новый проект", "📂 Мои проекты")
    markup.add("👤 Профиль", "💎 Тарифы")
    markup.add("🆘 Техподдержка")
    if user_id == ADMIN_ID: markup.add("⚙️ Админка")
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    update_last_active(user_id)
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (user_id, gens_left) VALUES (%s, 2) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit(); cur.close(); conn.close()
    bot.send_message(user_id, "👋 Привет! Я AI SEO Master.\nПомогу продвинуть твой сайт в топ.", reply_markup=main_menu_markup(user_id))

@bot.message_handler(func=lambda m: m.text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка", "🔙 В меню"])
def menu_handler(message):
    uid = message.from_user.id
    txt = message.text
    update_last_active(uid)

    if txt == "➕ Новый проект":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🌐 Сайт", callback_data="new_site"),
                   types.InlineKeyboardButton("📸 Инстаграм (Скоро)", callback_data="soon"),
                   types.InlineKeyboardButton("✈️ Телеграм (Скоро)", callback_data="soon"))
        bot.send_message(uid, "Выберите тип площадки:", reply_markup=markup)
    elif txt == "📂 Мои проекты":
        list_projects(uid, message.chat.id)
    elif txt == "👤 Профиль":
        show_profile(uid)
    elif txt == "💎 Тарифы":
        show_tariff_periods(uid)
    elif txt == "🆘 Техподдержка":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Написать", url=f"tg://user?id={SUPPORT_ID}"))
        bot.send_message(uid, "Напишите в поддержку, если возникли вопросы:", reply_markup=markup)
    elif txt == "⚙️ Админка" and uid == ADMIN_ID:
        show_admin_panel(uid)
    elif txt == "🔙 В меню":
        bot.send_message(uid, "Главное меню", reply_markup=main_menu_markup(uid))

@bot.callback_query_handler(func=lambda call: call.data == "soon")
def soon_alert(call): bot.answer_callback_query(call.id, "🚧 В разработке...")

# --- 5. ПРОЕКТЫ ---
def list_projects(user_id, chat_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, url FROM projects WHERE user_id = %s ORDER BY id ASC", (user_id,))
    projs = cur.fetchall()
    cur.close(); conn.close()
    if not projs:
        bot.send_message(chat_id, "У вас нет проектов.")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in projs:
        btn_text = p[1].replace("https://", "").replace("http://", "").replace("www.", "")[:30]
        markup.add(types.InlineKeyboardButton(f"🌐 {btn_text}", callback_data=f"open_proj_mgmt_{p[0]}"))
    bot.send_message(chat_id, "Ваши проекты:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "new_site")
def new_site_start(call):
    msg = bot.send_message(call.message.chat.id, "🔗 Введите URL сайта (обязательно с http:// или https://):")
    bot.register_next_step_handler(msg, check_url_step)

def check_url_step(message):
    url = message.text.strip()
    if not url.startswith("http"):
        msg = bot.send_message(message.chat.id, "❌ Нужен URL с http://.")
        bot.register_next_step_handler(msg, check_url_step)
        return
    
    if not check_site_availability(url):
        msg = bot.send_message(message.chat.id, "❌ Сайт недоступен (не 200 OK).")
        bot.register_next_step_handler(msg, check_url_step)
        return
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO projects (user_id, type, url, info, progress) VALUES (%s, 'site', %s, '{}', '{}') RETURNING id", (message.from_user.id, url))
    pid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    USER_CONTEXT[message.from_user.id] = pid
    open_project_menu(message.chat.id, pid, mode="onboarding", new_site_url=url)

# --- ГЛАВНОЕ МЕНЮ ПРОЕКТА ---
def open_project_menu(chat_id, pid, mode="management", msg_id=None, new_site_url=None):
    conn = get_db_connection(); cur = conn.cursor()
    # ИСПРАВЛЕНИЕ: Безопасное извлечение даже если поля NULL
    cur.execute("SELECT url, keywords, progress, cms_login, cms_password FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    if not res: 
        bot.send_message(chat_id, "❌ Проект не найден.")
        return
    
    url, kw_db, progress, cms_login, cms_pass = res
    if not progress: progress = {}
    
    # Логика: Полностью ли настроен проект?
    # Считаем настроенным, если есть ключи, пройден опрос и настроена CMS
    is_fully_configured = (kw_db is not None and len(kw_db) > 5) and progress.get("info_done") and cms_login

    markup = types.InlineKeyboardMarkup(row_width=1)
    
    if mode == "onboarding":
        # ПОШАГОВЫЙ ПУТЬ
        if not progress.get("analysis_done"):
            markup.add(types.InlineKeyboardButton("📊 Анализ сайта", callback_data=f"sel_anz_{pid}"))
        elif not progress.get("info_done"):
            markup.add(types.InlineKeyboardButton("📝 Опрос", callback_data=f"srv_{pid}"))
        elif not progress.get("upload_done"):
            markup.add(types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upf_{pid}"))
            markup.add(types.InlineKeyboardButton("➡️ Пропустить", callback_data=f"skip_upl_{pid}"))
        elif not progress.get("competitors_done"):
             markup.add(types.InlineKeyboardButton("🔗 Анализ конкурентов", callback_data=f"comp_start_{pid}"))
        else:
            if not kw_db:
                markup.add(types.InlineKeyboardButton("🔑 Создать ключи", callback_data=f"kw_ask_count_{pid}"))
            elif not cms_login:
                markup.add(types.InlineKeyboardButton("⚙️ Настроить сайт (CMS)", callback_data=f"cms_select_{pid}"))
            else:
                markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
                
    else:
        # ОБЫЧНЫЙ РЕЖИМ
        if is_fully_configured:
            # ЧИСТОЕ МЕНЮ: Только работа
            markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
            markup.add(types.InlineKeyboardButton("📊 Анализ сайта", callback_data=f"sel_anz_{pid}"))
            markup.add(types.InlineKeyboardButton("⚙️ Настройки проекта", callback_data=f"proj_settings_{pid}"))
        else:
            # Если не донастроен - показываем что осталось
            if not progress.get("info_done"): markup.add(types.InlineKeyboardButton("📝 Опрос", callback_data=f"srv_{pid}"))
            if not progress.get("competitors_done"): markup.add(types.InlineKeyboardButton("🔗 Конкуренты", callback_data=f"comp_start_{pid}"))
            if not kw_db: markup.add(types.InlineKeyboardButton("🔑 Ключи", callback_data=f"kw_ask_count_{pid}"))
            if not cms_login: markup.add(types.InlineKeyboardButton("⚙️ Настроить CMS", callback_data=f"cms_select_{pid}"))
            
            markup.add(types.InlineKeyboardButton("⚙️ Все настройки", callback_data=f"proj_settings_{pid}"))

    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))

    safe_url = url
    text = f"✅ Сайт добавлен!" if new_site_url else f"📂 **Проект:** {safe_url}"
    if mode == "onboarding": text += "\n⬇️ Следующий шаг:"
    
    try:
        if msg_id and not new_site_url:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
        else:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.send_message(chat_id, text.replace("*", ""), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_settings_"))
def project_settings_menu(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🔑 Ключевые слова", callback_data=f"view_kw_{pid}"))
    markup.add(types.InlineKeyboardButton("📝 Данные опроса", callback_data=f"srv_{pid}"))
    markup.add(types.InlineKeyboardButton("🔗 Конкуренты", callback_data=f"comp_start_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ Подключение CMS", callback_data=f"cms_select_{pid}"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить проект", callback_data=f"ask_del_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад к проекту", callback_data=f"open_proj_mgmt_{pid}"))
    bot.edit_message_text("⚙️ **Настройки проекта**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

# --- 6. КОНКУРЕНТЫ (ПОШАГОВО) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("comp_start_"))
def comp_start(call):
    pid = call.data.split("_")[2]
    USER_CONTEXT[call.from_user.id] = pid
    msg = bot.send_message(call.message.chat.id, "🔗 Пришлите ссылку на 1-го конкурента:")
    bot.register_next_step_handler(msg, analyze_competitor_step, pid)

def analyze_competitor_step(message, pid):
    if message.text.startswith("/"): return
    url = message.text.strip()
    if not url.startswith("http"):
        bot.send_message(message.chat.id, "❌ Нужна ссылка с http.")
        return

    msg = bot.send_message(message.chat.id, "🕵️‍♂️ Анализирую конкурента...")
    
    try:
        prompt = f"Проанализируй сайт конкурента {url}. Выдели 5 лучших ключей и дай 1 предложение мнения."
        ai_resp = get_gemini_response(prompt)
        
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
        info = cur.fetchone()[0] or {}
        clist = info.get("competitors_list", [])
        clist.append(ai_resp)
        info["competitors_list"] = clist
        cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info, ensure_ascii=False), pid))
        conn.commit(); cur.close(); conn.close()
        
        bot.delete_message(message.chat.id, msg.message_id)
        send_safe_message(message.chat.id, f"✅ **Анализ:**\n{ai_resp}")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➕ Добавить еще", callback_data=f"comp_start_{pid}"))
        markup.add(types.InlineKeyboardButton("➡️ Готово, дальше", callback_data=f"comp_finish_{pid}"))
        bot.send_message(message.chat.id, "Добавить еще конкурента?", reply_markup=markup)
    except:
        bot.send_message(message.chat.id, "Ошибка анализа.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("comp_finish_"))
def comp_finish(call):
    pid = call.data.split("_")[2]
    update_project_progress(pid, "competitors_done")
    open_project_menu(call.message.chat.id, pid, mode="onboarding", msg_id=call.message.message_id)

# --- 7. АНАЛИЗ САЙТА (3 ТИПА) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("sel_anz_"))
def select_analysis_type(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚡ Быстрый", callback_data=f"do_anz_{pid}_fast"))
    markup.add(types.InlineKeyboardButton("⚖️ Средний", callback_data=f"do_anz_{pid}_medium"))
    markup.add(types.InlineKeyboardButton("🕵️‍♂️ Глубокий", callback_data=f"do_anz_{pid}_deep"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"open_proj_mgmt_{pid}"))
    bot.edit_message_text("Выберите тип анализа:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("do_anz_"))
def perform_analysis(call):
    _, _, pid, type_ = call.data.split("_")
    bot.edit_message_text(f"⏳ Выполняю {type_} анализ...", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
    url = cur.fetchone()[0]
    
    raw_data, links = deep_analyze_site(url)
    
    prompt = f"Проведи {type_} SEO анализ сайта {url} на основе данных:\n{raw_data}\nЯзык: Русский."
    advice = get_gemini_response(prompt)
    
    send_safe_message(call.message.chat.id, f"📊 **Отчет ({type_}):**\n\n{advice}")
    update_project_progress(pid, "analysis_done")
    open_project_menu(call.message.chat.id, pid, mode="onboarding")

# --- СТРАТЕГИЯ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("strat_"))
def strategy_start(call):
    pid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT cms_login FROM projects WHERE id=%s", (pid,))
    if not cur.fetchone()[0]:
        cur.close(); conn.close()
        bot.send_message(call.message.chat.id, "⚠️ Настройте CMS в настройках проекта!")
        return
    cur.close(); conn.close()
    
    markup = types.InlineKeyboardMarkup(row_width=4)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(call.message.chat.id, "📅 Сколько статей в неделю?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("freq_"))
def save_freq_and_plan(call):
    _, pid, freq = call.data.split("_")
    bot.edit_message_text(f"📅 Составляю календарь на {freq} статей...", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info_json = res[0] or {}
    survey = info_json.get("survey", "")
    kw = res[1] or ""
    
    prompt = f"""
    Роль: SEO Маркетолог.
    Задача: Составь контент-план на неделю ({freq} статей).
    Учти сезонность, время дня.
    Ниша: {survey}. Ключи: {kw[:500]}
    
    Выведи текстом расписание.
    А в конце дай JSON список из 5 тем: ["T1", "T2", "T3", "T4", "T5"]
    """
    ai_resp = get_gemini_response(prompt)
    
    topics = []
    try:
        json_part = ai_resp.split("```json")[-1].split("```")[0].strip()
        topics = json.loads(json_part)
        display_text = ai_resp.split("```json")[0]
    except:
        display_text = ai_resp
        topics = ["Тема 1", "Тема 2", "Тема 3", "Тема 4", "Тема 5"]

    info_json["temp_topics"] = topics
    cur.execute("UPDATE projects SET frequency=%s, info=%s WHERE id=%s", (freq, json.dumps(info_json), pid))
    conn.commit(); cur.close(); conn.close()
    
    send_safe_message(call.message.chat.id, f"🗓 **Календарь:**\n\n{display_text}")
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    msg_text = "📝 **Выберите тему:**\n\n"
    for i, t in enumerate(topics):
        if i >= 5: break
        msg_text += f"{i+1}. **{t}**\n"
        markup.add(types.InlineKeyboardButton(f"Вариант {i+1}", callback_data=f"write_{pid}_topic_{i}"))
    bot.send_message(call.message.chat.id, msg_text, reply_markup=markup, parse_mode='Markdown')

# --- НАПИСАНИЕ (ЖУРНАЛЬНЫЙ СТИЛЬ) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article(call):
    parts = call.data.split("_")
    pid, idx = parts[1], int(parts[3])
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info, keywords = res[0], res[1] or ""
    internal_links = info.get('internal_links', [])
    links_text = json.dumps(internal_links[:50], ensure_ascii=False)
    topics = info.get("temp_topics", [])
    selected_topic = topics[idx] if len(topics) > idx else "SEO Article"
    main_keyword = selected_topic.split(':')[0]
    
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, f"⏳ Пишу статью (Magazine Style, 5-7 фото)...", parse_mode='Markdown')
    
    prompt = f"""
    Role: Professional Magazine Editor & SEO Expert.
    Topic: "{selected_topic}"
    Language: STRICTLY RUSSIAN (NO ENGLISH IN TEXT).
    Focus Keyword: "{main_keyword}"
    
    REQUIREMENTS:
    1. **Magazine Layout**: 
       - Use `<blockquote>` for key insights.
       - Use `<table>` where appropriate.
       - **IMAGES**: You MUST insert 5-7 image placeholders evenly distributed.
       - Format: `[IMG: specific detailed prompt for image generation in English]`
       - Example: `...text... [IMG: photo of a modern living room with wood panels] ...text...`
       - Use HTML tags like `<ul>`, `<ol>`, `<h2>`.
    2. **SEO**: 
       - Insert 3 internal links from: {links_text}
       - Outbound links: 2 authoritative links.
       - Active voice, short sentences.
    
    OUTPUT JSON:
    {{
        "html_content": "Full HTML content with [IMG:...] tags.",
        "seo_title": "Russian SEO Title",
        "meta_desc": "Russian Meta Description",
        "focus_kw": "{main_keyword}",
        "featured_img_prompt": "Cover image prompt (English)",
        "featured_img_alt": "Cover alt text (Russian)"
    }}
    """
    response_text = get_gemini_response(prompt)
    
    try:
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        article_html = data.get("html_content", "")
        seo_data = {
            "seo_title": str(data.get("seo_title", "")),
            "meta_desc": str(data.get("meta_desc", "")),
            "focus_kw": str(data.get("focus_kw", "")),
            "featured_img_prompt": str(data.get("featured_img_prompt", "")),
            "featured_img_alt": str(data.get("featured_img_alt", ""))
        }
    except:
        article_html = response_text
        seo_data = {"seo_title": selected_topic, "meta_desc": "", "focus_kw": main_keyword, "featured_img_prompt": f"Photo of {main_keyword}"}

    cur.execute("UPDATE users SET gens_left = gens_left - 1 WHERE user_id = (SELECT user_id FROM projects WHERE id=%s) AND is_admin = FALSE", (pid,))
    cur.execute("INSERT INTO articles (project_id, title, content, seo_data, status) VALUES (%s, %s, %s, %s, 'draft') RETURNING id", 
                (pid, selected_topic, article_html, json.dumps(seo_data)))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    clean_view = format_html_for_chat(article_html)
    send_safe_message(call.message.chat.id, clean_view)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("✏️ Переписать", callback_data=f"rewrite_{aid}"))
    bot.send_message(call.message.chat.id, "👇 Статья готова. Ваши действия?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve_publish(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT project_id, title, content, seo_data FROM articles WHERE id=%s", (aid,))
    row = cur.fetchone()
    pid, title, content, seo_json = row
    seo_data = seo_json if isinstance(seo_json, dict) else json.loads(seo_json or '{}')
    
    cur.execute("SELECT cms_url, cms_login, cms_password FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    if not res:
        bot.send_message(call.message.chat.id, "❌ Проект не найден.")
        return

    url, login, pwd = res
    msg = bot.send_message(call.message.chat.id, "🚀 Генерирую 5-7 картинок и публикую...")
    
    # 1. Генерация картинок в тексте
    img_matches = re.findall(r'\[IMG: (.*?)\]', content)
    final_content = content
    
    for i, prompt in enumerate(img_matches):
        media_id, source_url = generate_and_upload_image(url, login, pwd, prompt, f"{title} photo {i}")
        if source_url:
            # Журнальная верстка: чередование + обтекание
            align = "left" if i % 2 == 0 else "right"
            margin = "margin-right: 20px;" if align == "left" else "margin-left: 20px;"
            img_html = f'<div class="wp-block-image" style="float: {align}; {margin} margin-bottom: 20px; max-width: 50%;"><img src="{source_url}" alt="{title}" class="wp-image-{media_id}" /></div>'
            final_content = final_content.replace(f'[IMG: {prompt}]', img_html, 1)
        else:
            final_content = final_content.replace(f'[IMG: {prompt}]', '', 1)

    # 2. Главная картинка
    feat_media_id = None
    if seo_data.get('featured_img_prompt'):
        feat_media_id, _ = generate_and_upload_image(url, login, pwd, seo_data['featured_img_prompt'], seo_data.get('featured_img_alt', title))

    # 3. Публикация
    try:
        creds = f"{login}:{pwd}"
        token = base64.b64encode(creds.encode()).decode()
        headers = {
            'Authorization': 'Basic ' + token,
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0',
            'Cookie': 'beget=begetok'
        }
        
        meta_payload = {
            '_yoast_wpseo_title': seo_data.get('seo_title', ''),
            '_yoast_wpseo_metadesc': seo_data.get('meta_desc', ''),
            '_yoast_wpseo_focuskw': seo_data.get('focus_kw', '')
        }

        post_data = {
            'title': title,
            'content': final_content.replace("\n", "<br>"),
            'status': 'publish',
            'meta': meta_payload
        }
        if feat_media_id: post_data['featured_media'] = feat_media_id

        api_url = f"{url}/wp-json/wp/v2/posts"
        r = requests.post(api_url, headers=headers, json=post_data, timeout=60)
        
        if r.status_code == 201:
            link = r.json().get('link')
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("UPDATE articles SET status='published', published_url=%s WHERE id=%s", (link, aid))
            conn.commit(); cur.close(); conn.close()
            
            bot.delete_message(call.message.chat.id, msg.message_id)
            bot.send_message(call.message.chat.id, f"✅ **Успешно опубликовано!**\n🔗 {link}\n\nВозврат в главное меню...", parse_mode='Markdown')
            bot.send_message(call.message.chat.id, "Главное меню:", reply_markup=main_menu_markup(call.from_user.id))
        else:
            bot.send_message(call.message.chat.id, f"❌ Ошибка WP: {r.status_code}")
            
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")

# ОСТАЛЬНЫЕ ХЕНДЛЕРЫ
@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_ask_count_"))
def kw_ask_count(call):
    pid = call.data.split("_")[3]
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(types.InlineKeyboardButton("10", callback_data=f"genkw_{pid}_10"),
               types.InlineKeyboardButton("50", callback_data=f"genkw_{pid}_50"))
    bot.edit_message_text("🔢 Сколько ключей?", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("genkw_"))
def generate_keywords_action(call):
    _, pid, count = call.data.split("_")
    bot.edit_message_text(f"🧠 Генерирую ключи...", call.message.chat.id, call.message.message_id)
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0] or {}
    survey = info.get("survey", "")
    comps = json.dumps(info.get("competitors_list", []), ensure_ascii=False)
    
    prompt = f"Составь СЯ из {count} ключей. Контекст: {survey}. Конкуренты: {comps}. Формат: Кластеры."
    keywords = get_gemini_response(prompt)
    
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    send_safe_message(call.message.chat.id, keywords)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_kw_{pid}"))
    bot.send_message(call.message.chat.id, "Действия:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_kw_"))
def approve_keywords(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настроить сайт (CMS)", callback_data=f"cms_select_{pid}"))
    bot.send_message(call.message.chat.id, "✅ Ключи утверждены!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_select_"))
def cms_select_start(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("WordPress", callback_data=f"cms_setup_wp_{pid}"))
    bot.send_message(call.message.chat.id, "CMS:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_setup_wp_"))
def cms_setup_wp(call):
    pid = call.data.split("_")[3]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Дальше ➡️", callback_data=f"cms_input_url_{pid}"))
    bot.send_message(call.message.chat.id, "Инструкция: создайте пароль приложения в WP.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_input_url_"))
def cms_ask_url(call):
    pid = call.data.split("_")[3]
    msg = bot.send_message(call.message.chat.id, "1️⃣ URL сайта:")
    bot.register_next_step_handler(msg, cms_save_url, pid)

def cms_save_url(message, pid):
    url = message.text.strip().rstrip("/")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_url=%s WHERE id=%s", (url, pid))
    conn.commit(); cur.close(); conn.close()
    msg = bot.send_message(message.chat.id, "2️⃣ Логин:")
    bot.register_next_step_handler(msg, cms_save_login, pid)

def cms_save_login(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_login=%s WHERE id=%s", (message.text.strip(), pid))
    conn.commit(); cur.close(); conn.close()
    msg = bot.send_message(message.chat.id, "3️⃣ Пароль:")
    bot.register_next_step_handler(msg, cms_save_pass, pid)

def cms_save_pass(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_password=%s WHERE id=%s", (message.text.strip(), pid))
    conn.commit(); cur.close(); conn.close()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
    bot.send_message(call.message.chat.id, "✅ Настройки сохранены!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("rewrite_"))
def rewrite_once(call):
    aid = call.data.split("_")[1]
    bot.answer_callback_query(call.id, "Функция рерайта стандартная")

@bot.callback_query_handler(func=lambda call: call.data.startswith("ask_del_"))
def ask_del(call):
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE id=%s", (pid,))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(call.message.chat.id, "Удалено.")
    list_projects(call.from_user.id, call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_kw_"))
def view_kw(call):
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT keywords FROM projects WHERE id=%s", (pid,))
    kw = cur.fetchone()[0]
    send_safe_message(call.message.chat.id, f"Ключи:\n{kw}")

# ЗАПУСК
def run_scheduler():
    while True: time.sleep(60)

app = Flask(__name__)
@app.route('/')
def h(): return "Alive", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    print("🤖 Бот запущен...")
    bot.infinity_polling(skip_pending=True)
