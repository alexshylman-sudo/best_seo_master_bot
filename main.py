import os
import threading
import time
import psycopg2
import json
import requests
import datetime
import re
import base64
import random
from telebot import TeleBot, types
from flask import Flask
from google import genai
from google.genai import types as genai_types
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin

# --- 1. CONFIGURATION ---
load_dotenv()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0")) 
SUPPORT_ID = 203473623 
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("APP_URL")

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)
USER_CONTEXT = {} 
UPLOAD_STATE = {} # Состояние загрузки фото

# --- 2. DATABASE ---
def get_db_connection():
    try: return psycopg2.connect(DB_URL)
    except Exception as e: print(f"❌ DB Error: {e}"); return None

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
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS sitemap_links JSONB DEFAULT '[]'") 
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS seo_data JSONB DEFAULT '{}'") 
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS scheduled_time TIMESTAMP")
        
        # --- НОВЫЕ ПОЛЯ ДЛЯ БАЗЫ ЗНАНИЙ ---
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS style_prompt TEXT")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS style_images JSONB DEFAULT '[]'")
        
        conn.commit()
    except Exception as e: 
        print(f"⚠️ Schema Patch Error: {e}")
    finally: cur.close(); conn.close()

def init_db():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance INT DEFAULT 0,
            tariff TEXT DEFAULT 'No Tariff',
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
            style_prompt TEXT,
            style_images JSONB DEFAULT '[]',
            cms_url TEXT,
            cms_login TEXT,
            cms_password TEXT,
            cms_key TEXT,
            platform TEXT,
            frequency INT DEFAULT 0,
            content_plan JSONB DEFAULT '[]',
            sitemap_links JSONB DEFAULT '[]',
            progress JSONB DEFAULT '{}',
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
    cur.execute("INSERT INTO users (user_id, gens_left) VALUES (%s, 999) ON CONFLICT DO NOTHING", (ADMIN_ID,))
    conn.commit(); cur.close(); conn.close()
    patch_db_schema()

def update_last_active(user_id):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET last_active = NOW() WHERE user_id = %s", (user_id,))
        conn.commit(); cur.close(); conn.close()
    except: pass

# --- 3. UTILITIES ---
def slugify(text):
    if not text: return "image"
    symbols = (u"абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ",
               u"abvgdeejzijklmnoprstufhzcss_y_euaABVGDEEJZIJKLMNOPRSTUFHZCSS_Y_EUA")
    tr = {ord(a): ord(b) for a, b in zip(*symbols)}
    text = text.translate(tr)
    text = re.sub(r'[\W\s]+', '-', text).strip('-').lower()
    return text

def escape_md(text):
    if not text: return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")

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
        try: bot.send_message(chat_id, part, parse_mode=parse_mode, reply_markup=reply_markup if i == len(parts) - 1 else None)
        except: 
            try: bot.send_message(chat_id, part, parse_mode=None, reply_markup=reply_markup if i == len(parts) - 1 else None)
            except: pass
        time.sleep(0.1)

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e: return f"AI Error: {e}"

def clean_and_parse_json(text):
    text = str(text).strip()
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    clean = match.group(1) if match else text
    try: return json.loads(clean)
    except: 
        match_arr = re.search(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
        if match_arr: 
            try: return json.loads(match_arr.group(1))
            except: pass
        return None

def check_site_availability(url):
    try: return requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).status_code == 200
    except: return False

def parse_sitemap(url):
    links = []
    try:
        sitemap_url = url.rstrip('/') + '/sitemap.xml'
        resp = requests.get(sitemap_url, timeout=10)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for url_tag in root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc'):
                links.append(url_tag.text)
            if not links:
                for url_tag in root.findall('.//loc'): links.append(url_tag.text)
        if not links:
            soup = BeautifulSoup(requests.get(url, timeout=10).text, 'html.parser')
            domain = urlparse(url).netloc
            for a in soup.find_all('a', href=True):
                full = urljoin(url, a['href'])
                if urlparse(full).netloc == domain: links.append(full)
        return list(set([l for l in links if not any(x in l for x in ['.jpg', '.png', 'wp-admin', 'feed'])]))[:50]
    except: return []

def deep_analyze_site(url):
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 Bot"})
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = soup.title.string if soup.title else "No Title"
        desc = soup.find("meta", attrs={"name": "description"})
        desc = desc["content"] if desc else "No Description"
        headers = [h.get_text().strip() for h in soup.find_all(['h1', 'h2', 'h3'])]
        raw_text = soup.get_text()[:5000].strip()
        return f"URL: {url}\nTitle: {title}\nDesc: {desc}\nContent: {raw_text}", []
    except Exception as e: return f"Error: {e}", []

def update_project_progress(pid, step_key):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT progress FROM projects WHERE id=%s", (pid,))
        prog = cur.fetchone()[0] or {}
        prog[step_key] = True
        cur.execute("UPDATE projects SET progress=%s WHERE id=%s", (json.dumps(prog), pid))
        conn.commit()
    except: pass
    finally: cur.close(); conn.close()

def format_html_for_chat(html_content):
    text = str(html_content).replace('\\n', '\n')
    text = re.sub(r'\}\s*$', '', text)
    text = re.sub(r'```json.*', '', text, flags=re.DOTALL)
    text = re.sub(r'```', '', text)
    text = re.sub(r'\[IMG:.*?\]', '', text)
    text = re.sub(r'<h[1-6]>(.*?)</h[1-6]>', r'\n\n<b>\1</b>\n', text)
    text = re.sub(r'<li>(.*?)</li>', r'• \1\n', text)
    soup = BeautifulSoup(text, "html.parser")
    for script in soup(["script", "style", "head"]): script.decompose()
    return soup.get_text(separator="\n\n").strip()

# --- 4. IMAGE GENERATION (SMART STYLE + TIER 1) ---
def generate_and_upload_image(api_url, login, pwd, image_prompt, alt_text, seo_filename, project_style=""):
    image_bytes = None
    
    # 1. Формирование промпта с учетом стиля из БАЗЫ ЗНАНИЙ
    base_prompt = f"Professional photography, {image_prompt}, 8k, realistic"
    
    if project_style and len(project_style) > 5:
        # Смешиваем стиль пользователя и тему картинки
        final_prompt = f"{project_style}. Specific subject: {image_prompt}. High resolution, cinematic lighting, photorealistic."
    else:
        # Дефолтный стиль
        final_prompt = f"{base_prompt}, high resolution, cinematic lighting"
    
    print(f"🎨 Gen: {final_prompt[:60]}...")
    
    try:
        # Tier 1 Config (без safety_settings)
        response = client.models.generate_images(
            model='imagen-4.0-fast-generate-001', 
            prompt=final_prompt,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio='16:9'
            )
        )
        if response.generated_images:
            image_bytes = response.generated_images[0].image.image_bytes
        else:
            return None, None, "⚠️ Imagen Safety Block."
    except Exception as e:
        return None, None, f"❌ API Error: {e}"

    if not image_bytes: return None, None, "❌ No bytes."

    # 2. Загрузка в WP
    try:
        api_url = api_url.rstrip('/')
        creds = base64.b64encode(f"{login}:{pwd}".encode()).decode()
        
        # SEO имя файла
        final_filename = f"{slugify(seo_filename)}-{random.randint(10,99)}.png"
        
        headers = {
            'Authorization': 'Basic ' + creds,
            'Content-Disposition': f'attachment; filename="{final_filename}"',
            'Content-Type': 'image/png'
        }
        
        r = requests.post(f"{api_url}/wp-json/wp/v2/media", headers=headers, data=image_bytes, timeout=60)
        
        if r.status_code == 201:
            res = r.json()
            requests.post(f"{api_url}/wp-json/wp/v2/media/{res['id']}", 
                          headers={'Authorization': 'Basic ' + creds}, 
                          json={'alt_text': alt_text, 'title': alt_text, 'caption': alt_text})
            return res['id'], res['source_url'], f"✅ OK: {final_filename}"
        elif r.status_code == 401: return None, None, "❌ WP 401 (Пароль)"
        elif r.status_code == 403: return None, None, "❌ WP 403 (Доступ)"
        else: return None, None, f"❌ WP {r.status_code}"
            
    except Exception as e:
        return None, None, f"❌ Conn Error: {e}"

# --- 5. MENUS ---
def main_menu_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("➕ Новый проект", "📂 Мои проекты")
    markup.add("👤 Профиль", "💎 Тарифы")
    if user_id == ADMIN_ID: markup.add("⚙️ Админка")
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    init_db()
    bot.send_message(message.chat.id, "👋 AI SEO Master (Tier 1 + Knowledge Base Fixed).", reply_markup=main_menu_markup(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == "➕ Новый проект")
def new_project(message):
    msg = bot.send_message(message.chat.id, "🔗 Введите URL сайта:")
    bot.register_next_step_handler(msg, save_url)

def save_url(message):
    url = message.text.strip()
    if not url.startswith("http"): 
        bot.send_message(message.chat.id, "❌ Нужен http/https")
        return
    links = parse_sitemap(url)
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO projects (user_id, url, info, sitemap_links) VALUES (%s, %s, '{}', %s) RETURNING id", 
                (message.from_user.id, url, json.dumps(links)))
    pid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настроить CMS (WP)", callback_data=f"cms_select_{pid}"))
    bot.send_message(message.chat.id, f"✅ Проект {url} создан!", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "📂 Мои проекты")
def my_projects(message):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, url FROM projects WHERE user_id=%s", (message.from_user.id,))
    projs = cur.fetchall()
    cur.close(); conn.close()
    
    markup = types.InlineKeyboardMarkup()
    for p in projs:
        markup.add(types.InlineKeyboardButton(p[1], callback_data=f"open_proj_mgmt_{p[0]}"))
    bot.send_message(message.chat.id, "Ваши проекты:", reply_markup=markup)

# --- CMS SETTINGS ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_select_"))
def cms_select(call):
    pid = call.data.split("_")[2]
    msg = bot.send_message(call.message.chat.id, "Введите логин и пароль приложения WP через пробел:\n`admin xxxx yyyy zzzz`")
    bot.register_next_step_handler(msg, save_cms, pid)

def save_cms(message, pid):
    try:
        login, pwd = message.text.split(" ", 1)
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE projects SET cms_login=%s, cms_password=%s WHERE id=%s", (login, pwd, pid))
        conn.commit(); cur.close(); conn.close()
        bot.send_message(message.chat.id, "✅ CMS сохранена.")
        open_project_menu(message.chat.id, pid)
    except:
        bot.send_message(message.chat.id, "❌ Ошибка формата.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_mgmt_"))
def open_proj(call):
    open_project_menu(call.message.chat.id, call.data.split("_")[3])

def open_project_menu(chat_id, pid):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚡ Написать статью", callback_data=f"test_article_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ Настройки проекта", callback_data=f"proj_settings_{pid}"))
    bot.send_message(chat_id, "Управление проектом:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_settings_"))
def project_settings_menu(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🧠 База Знаний (Стиль)", callback_data=f"kb_menu_{pid}"))
    markup.add(types.InlineKeyboardButton("🔑 Ключевые слова", callback_data=f"add_kw_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ CMS Данные", callback_data=f"cms_select_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"open_proj_mgmt_{pid}"))
    bot.edit_message_text("⚙️ **Настройки проекта**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_kw_"))
def add_kw_handler(call):
    pid = call.data.split("_")[2]
    msg = bot.send_message(call.message.chat.id, "Отправьте список ключевых слов одним сообщением:")
    bot.register_next_step_handler(msg, save_keywords, pid)

def save_keywords(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET keywords=%s WHERE id=%s", (message.text, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Ключи сохранены.")
    open_project_menu(message.chat.id, pid)

# --- KNOWLEDGE BASE HANDLERS (FIXED) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_menu_"))
def kb_menu(call):
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_prompt, style_images FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    style_text = res[0] if res and res[0] else "Не задан"
    images = res[1] if res and res[1] else []
    
    msg = f"🧠 **База Знаний (Стиль)**\n\n📝 **Промпт:**\n_{escape_md(style_text)}_\n\n🖼 **Фото:** {len(images)}/30 загружено."
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 Изменить Стиль", callback_data=f"kb_set_text_{pid}"))
    markup.add(types.InlineKeyboardButton(f"🖼 Добавить фото", callback_data=f"kb_add_photo_{pid}"))
    if images:
        markup.add(types.InlineKeyboardButton("🗑 Очистить фото", callback_data=f"kb_clear_photos_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
    
    bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_set_text_"))
def kb_set_text(call):
    pid = call.data.split("_")[3]
    msg = bot.send_message(call.message.chat.id, "📝 Опишите стиль картинок (напр: 'Теплый свет, лофт, реализм').")
    bot.register_next_step_handler(msg, save_kb_text, pid)

def save_kb_text(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET style_prompt=%s WHERE id=%s", (message.text, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Стиль сохранен.")
    kb_menu_wrapper(message.chat.id, pid) # Возврат в меню
    
@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_add_photo_"))
def kb_add_photo(call):
    pid = call.data.split("_")[3]
    
    # Проверка текущего количества
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_images FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    current_count = len(res[0]) if res and res[0] else 0
    if current_count >= 30:
        bot.send_message(call.message.chat.id, "❌ Лимит 30 фото достигнут. Очистите базу.")
        return

    UPLOAD_STATE[call.from_user.id] = pid
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад / Отмена", callback_data=f"kb_menu_{pid}"))
    
    bot.send_message(call.message.chat.id, 
                     f"🖼 **Загрузка референсов ({current_count}/30)**\n\n"
                     "Правила:\n• Формат: **JPG/PNG**\n• Размер: **< 1 МБ**\n• Можно слать альбомом.\n"
                     "• Бот назовет файл image_X.jpg", 
                     reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(content_types=['photo', 'document'])
def handle_photo_upload(message):
    uid = message.from_user.id
    if uid not in UPLOAD_STATE: return 
    
    pid = UPLOAD_STATE[uid]
    file_id = None
    file_name = ""
    file_size = 0
    mime_type = ""

    # 1. ОПРЕДЕЛЕНИЕ ТИПА
    if message.photo:
        img = message.photo[-1]
        file_id = img.file_id
        file_size = img.file_size
        mime_type = "image/jpeg"
        file_name = f"image_photo.jpg" 
    elif message.document:
        file_id = message.document.file_id
        file_size = message.document.file_size
        mime_type = message.document.mime_type
        file_name = message.document.file_name
    else: return 

    # 2. ПРОВЕРКИ
    if mime_type not in ['image/jpeg', 'image/png', 'image/jpg']:
        bot.reply_to(message, "❌ Только JPG/PNG.")
        return

    if file_size > 1048576: # 1 МБ
        bot.reply_to(message, f"❌ Файл > 1 МБ.")
        return

    # 3. СОХРАНЕНИЕ В БД (Безопасное)
    conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        
        # Блокировка для атомарности
        cur.execute("SELECT style_images FROM projects WHERE id=%s FOR UPDATE", (pid,))
        res = cur.fetchone()
        images = res[0] if res and res[0] else []
        
        if len(images) >= 30:
            bot.reply_to(message, "❌ Лимит исчерпан (30).")
            return

        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        b64_img = base64.b64encode(downloaded_file).decode('utf-8')
        
        images.append(b64_img)
        cur.execute("UPDATE projects SET style_images=%s WHERE id=%s", (json.dumps(images), pid))
        conn.commit()
        
        count = len(images)
        actual_name = f"image_{count}.jpg" if not message.document else file_name
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Готово / В меню", callback_data=f"kb_menu_{pid}"))
        
        bot.reply_to(message, f"✅ Фото **#{count}** загружено!\n📄 `{actual_name}`", reply_markup=markup, parse_mode='Markdown')

    except Exception as e:
        print(f"Upload Err: {e}")
        bot.reply_to(message, "❌ Ошибка сохранения.")
    finally:
        if conn: conn.close()

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_clear_photos_"))
def kb_clear_photos(call):
    pid = call.data.split("_")[3]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET style_images='[]' WHERE id=%s", (pid,))
    conn.commit(); cur.close(); conn.close()
    bot.answer_callback_query(call.id, "Фото удалены.")
    kb_menu(call)

def kb_menu_wrapper(chat_id, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_prompt, style_images FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    style_text = res[0] if res and res[0] else "Не задан"
    images = res[1] if res and res[1] else []
    msg = f"🧠 **База Знаний (Стиль)**\n\n📝 **Промпт:**\n_{escape_md(style_text)}_\n\n🖼 **Фото:** {len(images)}/30 загружено."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 Изменить Стиль", callback_data=f"kb_set_text_{pid}"),
               types.InlineKeyboardButton(f"🖼 Добавить фото", callback_data=f"kb_add_photo_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
    bot.send_message(chat_id, msg, reply_markup=markup, parse_mode='Markdown')

# --- ARTICLE GENERATION ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("test_article_"))
def generate_topics(call):
    pid = call.data.split("_")[2]
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, keywords FROM projects WHERE id=%s", (pid,))
    url, kw_db = cur.fetchone()
    cur.close(); conn.close()

    if not kw_db or len(kw_db) < 5:
        bot.send_message(call.message.chat.id, "⚠️ Сначала добавьте ключи в настройках!")
        return

    bot.send_message(call.message.chat.id, "⏳ Генерирую темы на основе ключей...")
    
    prompt = f"""
    Role: SEO Strategist.
    Task: Create 5 viral blog topics based on these keywords.
    Keywords: {kw_db[:2000]}
    Language: Russian.
    Output: JSON list of strings.
    """
    resp = get_gemini_response(prompt)
    topics = clean_and_parse_json(resp)
    
    if not topics:
        bot.send_message(call.message.chat.id, "❌ Ошибка генерации тем.")
        return

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0]
    info['temp_topics'] = topics
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
    conn.commit(); cur.close(); conn.close()
    
    markup = types.InlineKeyboardMarkup()
    for i, t in enumerate(topics):
        markup.add(types.InlineKeyboardButton(t[:40], callback_data=f"write_{pid}_{i}"))
    bot.send_message(call.message.chat.id, "Выберите тему:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_full_article(call):
    _, pid, idx = call.data.split("_")
    idx = int(idx)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info, keywords, sitemap_links, style_prompt FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info, keywords_raw, sitemap_json, style_prompt = res
    sitemap_list = json.loads(sitemap_json) if sitemap_json else []
    links_text = "\n".join(sitemap_list[:30])
    style = style_prompt or ""
    
    topic = info['temp_topics'][idx]
    
    bot.send_message(call.message.chat.id, f"📝 Пишу статью: {topic}...")
    
    prompt = f"""
    Role: Professional SEO Copywriter.
    Topic: "{topic}"
    Language: STRICTLY RUSSIAN.
    
    SEO CORE KEYWORDS (Must be used naturally):
    {keywords_raw}
    
    REQUIREMENTS:
    1. Focus Keyword: Pick one main keyword from the list. Use in Title, H1, first paragraph.
    2. Internal Links: Insert 2-3 links from: {links_text}
    3. Images: Insert 3 [IMG: description in English] placeholders.
    4. Structure: H2, H3, lists. High readability.
    5. Meta: Include Title and Description.
    
    OUTPUT JSON:
    {{
        "html_content": "Article HTML...",
        "seo_title": "SEO Title",
        "meta_desc": "Meta Desc",
        "focus_kw": "Main Keyword",
        "featured_img_prompt": "Cover image prompt"
    }}
    """
    
    resp = get_gemini_response(prompt)
    data = clean_and_parse_json(resp)
    
    if not data:
        bot.send_message(call.message.chat.id, "⚠️ Ошибка текста.")
        return

    cur.execute("INSERT INTO articles (project_id, title, content, seo_data, status) VALUES (%s, %s, %s, %s, 'draft') RETURNING id",
                (pid, topic, data['html_content'], json.dumps(data)))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Опубликовать", callback_data=f"pub_{aid}"))
    bot.send_message(call.message.chat.id, "Статья готова!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pub_"))
def publish_to_wp(call):
    aid = call.data.split("_")[1]
    bot.send_message(call.message.chat.id, "🚀 Публикация...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT project_id, title, content, seo_data FROM articles WHERE id=%s", (aid,))
    pid, title, content, seo_json = cur.fetchone()
    seo_data = seo_json if isinstance(seo_json, dict) else json.loads(seo_json)
    
    # Загружаем стиль из БД
    cur.execute("SELECT cms_url, cms_login, cms_password, style_prompt FROM projects WHERE id=%s", (pid,))
    url, login, pwd, project_style = cur.fetchone()
    
    focus_kw = seo_data.get('focus_kw', 'seo')
    report = []
    
    # 1. Картинки в тексте
    img_tags = re.findall(r'\[IMG: (.*?)\]', content)
    for i, p_text in enumerate(img_tags):
        seo_name = f"{slugify(focus_kw)}-{i+1}"
        # Передаем стиль в функцию генерации
        mid, src, msg = generate_and_upload_image(url, login, pwd, p_text, f"{focus_kw} {i}", seo_name, project_style)
        report.append(f"Img {i}: {msg}")
        if src:
            html = f'<figure class="wp-block-image"><img src="{src}" alt="{focus_kw}" title="{focus_kw}" class="wp-image-{mid}"/></figure>'
            content = content.replace(f'[IMG: {p_text}]', html, 1)
        else:
            content = content.replace(f'[IMG: {p_text}]', '', 1)

    # 2. Обложка
    feat_id = None
    if seo_data.get('featured_img_prompt'):
        feat_id, _, msg = generate_and_upload_image(url, login, pwd, seo_data['featured_img_prompt'], focus_kw, f"{slugify(focus_kw)}-cover", project_style)
        report.append(f"Cover: {msg}")

    if any("❌" in x for x in report):
        bot.send_message(call.message.chat.id, "\n".join(report))

    # 3. Публикация
    try:
        creds = base64.b64encode(f"{login}:{pwd}".encode()).decode()
        meta = {
            '_yoast_wpseo_focuskw': focus_kw,
            '_yoast_wpseo_title': seo_data.get('seo_title', title),
            '_yoast_wpseo_metadesc': seo_data.get('meta_desc', '')
        }
        post = {'title': seo_data.get('seo_title', title), 'content': content, 'status': 'publish', 'featured_media': feat_id, 'meta': meta}
        
        r = requests.post(f"{url}/wp-json/wp/v2/posts", headers={'Authorization': 'Basic '+creds}, json=post)
        if r.status_code == 201:
            bot.send_message(call.message.chat.id, f"✅ Опубликовано: {r.json().get('link')}")
        else:
            bot.send_message(call.message.chat.id, f"❌ Ошибка WP: {r.status_code}")
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Err: {e}")

# --- UTILS (PROFILE & PAYMENTS) ---
def show_profile(uid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT tariff, gens_left, balance, joined_at, total_paid_rub FROM users WHERE user_id=%s", (uid,))
    u = cur.fetchone()
    cur.execute("SELECT count(*) FROM projects WHERE user_id=%s", (uid,))
    projs = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM articles WHERE status='published' AND project_id IN (SELECT id FROM projects WHERE user_id=%s)", (uid,))
    arts = cur.fetchone()[0]
    cur.close(); conn.close()
    safe_tariff = escape_md(u[0])
    txt = (f"👤 **Профиль**\nID: `{uid}`\n"
           f"📅 Дата регистрации: {u[3].strftime('%Y-%m-%d')}\n"
           f"💎 Тариф: {safe_tariff}\n⚡ Генераций: {u[1]}\n"
           f"💰 Расходы: {u[4]}р\n"
           f"📂 Проектов: {projs}\n📄 Опубликовано статей: {arts}")
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Пополнить баланс", callback_data="period_test"))
    bot.send_message(uid, txt, reply_markup=markup, parse_mode='Markdown')

def show_tariff_periods(user_id):
    txt = ("💎 **ТАРИФНЫЕ ПЛАНЫ**\n\n"
           "1️⃣ **Тест-драйв** — 500р\n"
           "• 5 генераций\n\n"
           "2️⃣ **СЕО Старт** — 1400р/мес\n"
           "• 15 генераций\n"
           "• Год: 11760р\n\n"
           "3️⃣ **СЕО Профи** — 2500р/мес\n"
           "• 30 генераций\n"
           "• Год: 21000р\n\n"
           "4️⃣ **PBN Агент** — 7500р/мес\n"
           "• 100 генераций\n"
           "• Год: 62999р")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🏎 Тест-драйв (500р)", callback_data="period_test"))
    markup.add(types.InlineKeyboardButton("📅 На Месяц", callback_data="period_month"))
    markup.add(types.InlineKeyboardButton("📆 На Год (Выгодно)", callback_data="period_year"))
    bot.send_message(user_id, txt, reply_markup=markup, parse_mode='Markdown')

def show_admin_panel(uid):
    conn = get_db_connection(); cur = conn.cursor()
    try: cur.execute("SELECT count(*) FROM users WHERE last_active > NOW() - INTERVAL '15 minutes'")
    except: pass
    online = cur.fetchone()[0] if cur.description else 0
    cur.execute("SELECT sum(amount) FROM payments WHERE currency='rub'")
    rub = cur.fetchone()[0] or 0
    cur.execute("SELECT tariff_name, count(*) FROM payments GROUP BY tariff_name")
    tariffs = "\n".join([f"{r[0]}: {r[1]} шт." for r in cur.fetchall()])
    cur.close(); conn.close()
    bot.send_message(uid, f"⚙️ **АДМИНКА**\n\n🟢 Онлайн (15 мин): {online}\n💰 Прибыль: {rub}₽\n📊 Продажи:\n{tariffs}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("period_"))
def tariff_period_select(call):
    p_type = call.data.split("_")[1]
    if p_type == "test": process_tariff_selection(call, "Тест-драйв", 500, "test")
    elif p_type == "month":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("СЕО Старт (1400р)", callback_data="buy_start_1m"),
                   types.InlineKeyboardButton("СЕО Профи (2500р)", callback_data="buy_pro_1m"),
                   types.InlineKeyboardButton("PBN Агент (7500р)", callback_data="buy_agent_1m"),
                   types.InlineKeyboardButton("🔙 Назад", callback_data="back_periods"))
        bot.edit_message_text("📅 Тарифы на Месяц:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    elif p_type == "year":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("СЕО Старт (11760р)", callback_data="buy_start_1y"),
                   types.InlineKeyboardButton("СЕО Профи (21000р)", callback_data="buy_pro_1y"),
                   types.InlineKeyboardButton("PBN Агент (62999р)", callback_data="buy_agent_1y"),
                   types.InlineKeyboardButton("🔙 Назад", callback_data="back_periods"))
        bot.edit_message_text("📆 Тарифы на Год:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "back_periods")
def back_to_periods(call):
    show_tariff_periods(call.from_user.id)

def process_tariff_selection(call, name, price, code):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Картой (РФ)", callback_data=f"pay_rub_{code}_{price}"),
               types.InlineKeyboardButton("⭐ Stars", callback_data=f"pay_star_{code}_{price}"))
    bot.edit_message_text(f"Оплата: {name} ({price}р)", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def pre_payment(call):
    parts = call.data.split("_")
    tariff_code = parts[1]
    period = parts[2]
    price = 0
    name = ""
    if tariff_code == "start":
        price = 1400 if period == "1m" else 11760
        name = "СЕО Старт"
    elif tariff_code == "pro":
        price = 2500 if period == "1m" else 21000
        name = "СЕО Профи"
    elif tariff_code == "agent":
        price = 7500 if period == "1m" else 62999
        name = "PBN Агент"
    process_tariff_selection(call, name, price, f"{tariff_code}_{period}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def process_payment(call):
    parts = call.data.split("_")
    currency = parts[1] 
    amount = int(parts[3])
    gens = 5
    if amount >= 1400: gens = 15
    if amount >= 2500: gens = 30
    if amount >= 7500: gens = 100
    if amount > 10000: gens *= 12 
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + %s, gens_left = gens_left + %s, tariff=%s WHERE user_id=%s", 
                (amount, gens, "Premium", call.from_user.id))
    cur.execute("INSERT INTO payments (user_id, amount, currency, tariff_name) VALUES (%s, %s, %s, %s)",
                (call.from_user.id, amount, currency, f"Tariff {amount}"))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(call.message.chat.id, f"✅ Оплата {amount} {currency} прошла успешно! Начислено {gens} генераций.")

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
    print("🤖 Бот запущен (Full Version)...")
    bot.infinity_polling(skip_pending=True)
