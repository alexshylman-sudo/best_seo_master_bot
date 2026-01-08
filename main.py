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
import traceback 
from urllib.parse import urlparse, urljoin, quote
from telebot import TeleBot, types
from flask import Flask
from google import genai
from google.genai import types as genai_types
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import xml.etree.ElementTree as ET

# --- 1. CONFIGURATION ---
load_dotenv()

# ВАЖНО: Укажите здесь ваш ID как администратора для доступа к панели
ADMIN_ID = 203473623 
SUPPORT_ID = 203473623 
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("APP_URL")

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)
USER_CONTEXT = {} 
UPLOAD_STATE = {} 
SURVEY_STATE = {} 
TEMP_PROMPTS = {} # Для хранения неутвержденных промптов

# --- 2. DATABASE ---
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
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS sitemap_links JSONB DEFAULT '[]'") 
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS seo_data JSONB DEFAULT '{}'") 
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS scheduled_time TIMESTAMP")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS style_prompt TEXT")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS style_negative_prompt TEXT")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS style_images JSONB DEFAULT '[]'")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS approved_prompts JSONB DEFAULT '[]'") # NEW
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
            style_negative_prompt TEXT,
            style_images JSONB DEFAULT '[]',
            approved_prompts JSONB DEFAULT '[]',
            cms_url TEXT,
            cms_login TEXT,
            cms_password TEXT,
            cms_key TEXT,
            platform TEXT,
            frequency INT DEFAULT 0,
            content_plan JSONB DEFAULT '[]',
            sitemap_links JSONB DEFAULT '[]',
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
    def _update():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("UPDATE users SET last_active = NOW() WHERE user_id = %s", (user_id,))
            conn.commit(); cur.close(); conn.close()
        except: pass
    threading.Thread(target=_update).start()

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
    MAX_LENGTH = 3800 
    parts = []
    while len(text) > 0:
        if len(text) > MAX_LENGTH:
            split_pos = text.rfind('\n', 0, MAX_LENGTH)
            if split_pos == -1: split_pos = text.rfind(' ', 0, MAX_LENGTH)
            if split_pos == -1: split_pos = MAX_LENGTH
            parts.append(text[:split_pos])
            text = text[split_pos:]
        else:
            parts.append(text)
            text = ""
    for i, part in enumerate(parts):
        is_last = (i == len(parts) - 1)
        current_markup = reply_markup if is_last else None
        try:
            bot.send_message(chat_id, part, parse_mode=parse_mode, reply_markup=current_markup)
        except Exception as e:
            try:
                # Fallback to plain text if HTML fails
                clean_part = re.sub(r'<[^>]+>', '', part) 
                bot.send_message(chat_id, clean_part, parse_mode=None, reply_markup=current_markup)
            except: pass
        time.sleep(0.3) 

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"AI Error: {e}"

def validate_input(text, question_context):
    if text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка", "🔙 В меню"]:
        return False, "MENU_CLICK"
    try:
        prompt = f"Moderator. Question: '{question_context}'. Answer: '{text}'. Check for spam. If bad respond BAD. If ok respond OK."
        res = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt]).text.strip()
        return ("BAD" not in res.upper()), "AI_CHECK"
    except: return True, "SKIP"

def check_site_availability(url):
    try:
        response = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        return response.status_code == 200
    except: return False

def parse_sitemap(url):
    links = []
    try:
        sitemap_url = url.rstrip('/') + '/sitemap.xml'
        resp = requests.get(sitemap_url, timeout=10)
        if resp.status_code == 200:
            try:
                root = ET.fromstring(resp.content)
                ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
                for url_tag in root.findall('.//s:loc', ns): links.append(url_tag.text)
                if not links:
                    for url_tag in root.findall('.//loc'): links.append(url_tag.text)
            except: pass
        if not links:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            soup = BeautifulSoup(resp.text, 'html.parser')
            domain = urlparse(url).netloc
            for a in soup.find_all('a', href=True):
                full_url = urljoin(url, a['href'])
                if urlparse(full_url).netloc == domain: links.append(full_url)
        clean_links = [l for l in list(set(links)) if not any(x in l for x in ['.jpg', '.png', 'wp-admin', 'feed'])]
        return clean_links[:100]
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
    except Exception as e:
        return f"Error: {e}", []

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

def clean_and_parse_json(text):
    text = str(text).strip()
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try: return json.loads(match.group(1))
        except: pass
    match_list = re.search(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
    if match_list:
        try: return json.loads(match_list.group(1))
        except: pass
    start = text.find('{'); end = text.rfind('}')
    if start != -1 and end != -1:
        try: return json.loads(text[start:end+1])
        except: pass
    start_list = text.find('['); end_list = text.rfind(']')
    if start_list != -1 and end_list != -1:
        try: return json.loads(text[start_list:end_list+1])
        except: pass
    return None

def format_html_for_chat(html_content):
    # Агрессивная очистка от JSON-мусора и форматирование
    text = str(html_content).strip()
    
    # 1. Если текст начинается с JSON-обертки (```json ... ```), убираем её
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    
    # 2. Если это JSON объект, пытаемся достать html_content
    match = re.search(r'"html_content":\s*"(.*?)(?<!\\)"', text, re.DOTALL)
    if match:
        text = match.group(1).encode('utf-8').decode('unicode_escape')
    elif text.startswith('{') and text.endswith('}'):
         try:
             js = json.loads(text)
             text = js.get("html_content", text)
         except: pass

    # 3. Чистка HTML
    soup = BeautifulSoup(text, "html.parser")
    for script in soup(["script", "style", "head", "title", "meta"]): script.decompose()

    # Заголовки
    for header in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        new_tag = soup.new_tag("b")
        new_tag.string = header.get_text().strip()
        header.replace_with(new_tag)
        new_tag.insert_before("\n\n")
        new_tag.insert_after("\n")

    # Списки
    for li in soup.find_all('li'):
        li.string = f"• {li.get_text().strip()}"
        li.insert_after("\n")
        li.unwrap()

    # Параграфы
    for p in soup.find_all('p'):
        p.insert_after("\n\n")
        p.unwrap()

    # Br
    for br in soup.find_all('br'):
        br.replace_with("\n")

    clean_text = str(soup)
    allowed_tags = r"b|strong|i|em|u|ins|s|strike|del|a|code|pre"
    clean_text = re.sub(r'<(?!\/?({}))[^>]*>'.format(allowed_tags), '', clean_text)
    
    import html
    clean_text = html.unescape(clean_text)
    return re.sub(r'\n\s*\n', '\n\n', clean_text).strip()

# --- 4. IMAGE GENERATION LOGIC ---
def generate_image_bytes(image_prompt, project_style="", negative_prompt=""):
    target_model = 'imagen-4.0-generate-001'
    base_negative = "exclude text, writing, letters, watermarks, signature, words, logo"
    full_negative = f"{base_negative}, {negative_prompt}" if negative_prompt else base_negative
    
    if project_style and len(project_style) > 5:
        final_prompt = f"{project_style}. {image_prompt}. High resolution, 8k, cinematic lighting. Exclude: {full_negative}."
    else:
        final_prompt = f"Professional photography, {image_prompt}, realistic, high resolution, 8k, cinematic lighting. Exclude: {full_negative}."
    
    print(f"🎨 Generating Preview: {final_prompt[:60]}...")
    try:
        response = client.models.generate_images(
            model=target_model, prompt=final_prompt,
            config=genai_types.GenerateImagesConfig(number_of_images=1, aspect_ratio='16:9')
        )
        if response.generated_images: return response.generated_images[0].image.image_bytes
        else: return None
    except Exception as e:
        print(f"Gen Error: {e}")
        return None

def generate_and_upload_image(api_url, login, pwd, image_prompt, alt_text, seo_filename, project_style="", negative_prompt=""):
    image_bytes = generate_image_bytes(image_prompt, project_style, negative_prompt)
    if not image_bytes: return None, None, "❌ No bytes."

    try:
        api_url = api_url.rstrip('/')
        creds = base64.b64encode(f"{login}:{pwd}".encode()).decode()
        if seo_filename: file_name = f"{slugify(seo_filename)}-{random.randint(10,99)}.png"
        else: file_name = f"img-{slugify(alt_text[:20])}-{random.randint(100,999)}.png"

        headers = {
            'Authorization': 'Basic ' + creds,
            'Content-Disposition': f'attachment; filename="{file_name}"',
            'Content-Type': 'image/png',
            'User-Agent': 'Mozilla/5.0'
        }
        r = requests.post(f"{api_url}/wp-json/wp/v2/media", headers=headers, data=image_bytes, timeout=60)
        
        if r.status_code == 201:
            res = r.json()
            media_id = res.get('id')
            source_url = res.get('source_url')
            requests.post(
                f"{api_url}/wp-json/wp/v2/media/{media_id}", 
                headers={'Authorization': 'Basic ' + creds}, 
                json={'alt_text': alt_text, 'title': alt_text, 'caption': alt_text}
            )
            return media_id, source_url, f"✅ OK ({file_name})"
        elif r.status_code == 401: return None, None, "❌ WP 401: Неверный пароль."
        elif r.status_code == 403: return None, None, "❌ WP 403: Доступ запрещен."
        else: return None, None, f"❌ WP Error {r.status_code}"
    except Exception as e:
        print(f"WP Upload Error: {e}")
        return None, None, f"❌ WP Connection Error: {e}"

# --- 5. MENUS ---
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
    bot.send_message(user_id, "👋 Привет! Я AI SEO Master (Tier 1 + Knowledge Base).\nПомогу продвинуть твой сайт в топ.", reply_markup=main_menu_markup(user_id))

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
        bot.send_message(uid, "Напишите в поддержку:", reply_markup=markup)
    elif txt == "⚙️ Админка" and uid == ADMIN_ID:
        show_admin_panel(uid)
    elif txt == "🔙 В меню":
        if uid in UPLOAD_STATE: del UPLOAD_STATE[uid]
        if uid in SURVEY_STATE: del SURVEY_STATE[uid]
        bot.send_message(uid, "Главное меню", reply_markup=main_menu_markup(uid))

@bot.callback_query_handler(func=lambda call: call.data == "soon")
def soon_alert(call): 
    try: bot.answer_callback_query(call.id, "🚧 В разработке...")
    except: pass

# --- 6. PROJECTS ---
def list_projects(user_id, chat_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, url FROM projects WHERE user_id = %s ORDER BY id ASC", (user_id,))
    projs = cur.fetchall()
    cur.close(); conn.close()
    if not projs:
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
        bot.send_message(chat_id, "📂 У вас пока нет проектов.", reply_markup=markup)
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in projs:
        btn_text = p[1].replace("https://", "").replace("http://", "").replace("www.", "")[:30]
        markup.add(types.InlineKeyboardButton(f"🌐 {btn_text}", callback_data=f"open_proj_mgmt_{p[0]}"))
    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
    bot.send_message(chat_id, "Ваши проекты:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "new_site")
def new_site_start(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    msg = bot.send_message(call.message.chat.id, "🔗 Введите URL сайта (обязательно с http:// или https://):")
    bot.register_next_step_handler(msg, check_url_step)

def check_url_step(message):
    def _process_url():
        try:
            url = message.text.strip()
            if not url.startswith("http"):
                msg = bot.send_message(message.chat.id, "❌ Нужен URL с http://.")
                bot.register_next_step_handler(msg, check_url_step)
                return
            
            clean_check_url = url.rstrip('/')
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT id FROM projects WHERE url LIKE %s OR url LIKE %s", (clean_check_url, clean_check_url + '/'))
            exists = cur.fetchone()
            cur.close(); conn.close()

            if exists:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
                bot.send_message(message.chat.id, f"🚫 **Этот сайт уже добавлен в базу!**\n\nПовторное добавление невозможно.", parse_mode='Markdown', reply_markup=markup)
                return

            tmp_msg = bot.send_message(message.chat.id, "🔎 Проверяю доступность сайта...")
            if not check_site_availability(url):
                try: bot.delete_message(message.chat.id, tmp_msg.message_id)
                except: pass
                msg = bot.send_message(message.chat.id, "❌ Сайт недоступен (не 200 OK). Проверьте ссылку.")
                bot.register_next_step_handler(msg, check_url_step)
                return
            
            sitemap_links = parse_sitemap(url)
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("INSERT INTO projects (user_id, type, url, info, sitemap_links, progress) VALUES (%s, 'site', %s, '{}', %s, '{}') RETURNING id", 
                        (message.from_user.id, url, json.dumps(sitemap_links)))
            pid = cur.fetchone()[0]
            conn.commit(); cur.close(); conn.close()
            
            try: bot.delete_message(message.chat.id, tmp_msg.message_id)
            except: pass
            USER_CONTEXT[message.from_user.id] = pid
            open_project_menu(message.chat.id, pid, mode="onboarding", new_site_url=url)
            
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Произошла ошибка при добавлении: {e}")
    threading.Thread(target=_process_url).start()

# --- MENU LOGIC ---
def open_project_menu(chat_id, pid, mode="management", msg_id=None, new_site_url=None):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, keywords, progress, cms_login, cms_password FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    if not res: 
        bot.send_message(chat_id, "❌ Проект не найден.")
        return
    url, kw_db, progress, cms_login, cms_pass = res
    if not progress: progress = {}
    is_fully_configured = (kw_db is not None and len(kw_db) > 5) and progress.get("info_done") and cms_login

    markup = types.InlineKeyboardMarkup(row_width=1)
    if mode == "onboarding":
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
                markup.add(types.InlineKeyboardButton("🔑 Добавить КЛЮЧИ", callback_data=f"kw_ask_count_{pid}"))
            elif not cms_login:
                markup.add(types.InlineKeyboardButton("⚙️ Настроить сайт (CMS)", callback_data=f"cms_select_{pid}"))
            else:
                markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
    else:
        if is_fully_configured:
            markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
            markup.add(types.InlineKeyboardButton("📊 Анализ сайта", callback_data=f"sel_anz_{pid}"))
            markup.add(types.InlineKeyboardButton("⚡ Написать тестовую статью", callback_data=f"test_article_{pid}"))
            markup.add(types.InlineKeyboardButton("⚙️ Настройки проекта", callback_data=f"proj_settings_{pid}"))
        else:
            markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ (Настроить)", callback_data=f"strat_{pid}"))
            markup.add(types.InlineKeyboardButton("⚙️ Продолжить настройку", callback_data=f"proj_settings_{pid}"))

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

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_mgmt_"))
def open_proj_mgmt(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[3]
    USER_CONTEXT[call.from_user.id] = pid
    open_project_menu(call.message.chat.id, pid, mode="management", msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_settings_"))
def project_settings_menu(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("⚡ Написать тестовую статью", callback_data=f"test_article_{pid}"))
    markup.add(types.InlineKeyboardButton("🧠 База Знаний (Стиль)", callback_data=f"kb_menu_{pid}"))
    markup.add(types.InlineKeyboardButton("🔑 Ключевые слова", callback_data=f"view_kw_{pid}"))
    markup.add(types.InlineKeyboardButton("📝 Опрос", callback_data=f"srv_{pid}"))
    markup.add(types.InlineKeyboardButton("🔗 Конкуренты", callback_data=f"comp_start_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ CMS (Сайт)", callback_data=f"cms_select_{pid}"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить проект", callback_data=f"ask_del_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"open_proj_mgmt_{pid}"))
    bot.edit_message_text("⚙️ **Настройки проекта**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

# --- CMS & KB HANDLERS (Omitted for brevity, kept from previous stable logic) ---
# ... (CMS handlers, KB handlers, Image Upload handlers - same as before) ...
@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_select_"))
def cms_start_setup(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Отмена", callback_data=f"proj_settings_{pid}"))
    msg = bot.send_message(call.message.chat.id, "🔐 **Настройка WordPress**\n\n1. Убедитесь, что у вас включены 'Application Passwords'.\n2. Введите **Логин** администратора:", reply_markup=markup, parse_mode='Markdown')
    bot.register_next_step_handler(msg, cms_save_login_step, pid)

def cms_save_login_step(message, pid):
    if message.text.startswith("/"): return
    login = message.text.strip()
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_login=%s WHERE id=%s", (login, pid))
    conn.commit(); cur.close(); conn.close()
    msg = bot.send_message(message.chat.id, "🔑 Теперь введите **Пароль приложения** (Application Password):")
    bot.register_next_step_handler(msg, cms_save_password_step, pid)

def cms_save_password_step(message, pid):
    pwd = message.text.strip()
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_password=%s WHERE id=%s", (pwd, pid))
    cur.execute("UPDATE projects SET cms_url=url WHERE id=%s AND cms_url IS NULL", (pid,))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ CMS данные сохранены! Теперь можно публиковать статьи.")
    open_project_menu(message.chat.id, pid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_menu_"))
def kb_menu(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_prompt, style_images, style_negative_prompt, approved_prompts FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    style_text = res[0] or "Не задан"; images = res[1] or []; neg = res[2] or "Не задан"; app_p = res[3] or []
    msg = (f"🧠 **База Знаний**\n🎨: {escape_md(style_text)}\n🚫: {escape_md(neg)}\n🖼: {len(images)}\n✅: {len(app_p)}")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎨 Генератор промптов", callback_data=f"kb_prompt_gen_menu_{pid}"))
    markup.add(types.InlineKeyboardButton("🎨 Промпт", callback_data=f"kb_set_text_{pid}"),
               types.InlineKeyboardButton("🚫 Анти-промпт", callback_data=f"kb_set_negative_{pid}"),
               types.InlineKeyboardButton("🖼 Добавить фото", callback_data=f"kb_add_photo_{pid}"))
    if images: markup.add(types.InlineKeyboardButton("📂 Галерея", callback_data=f"kb_gallery_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
    bot.send_message(chat_id, msg, reply_markup=markup, parse_mode='Markdown')

# --- PROMPT GENERATOR (NEW FEATURE) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_prompt_gen_menu_"))
def kb_prompt_gen_menu(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[4]
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎲 Сгенерировать вариант", callback_data=f"kb_gen_new_prompt_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"kb_menu_{pid}"))
    
    bot.edit_message_text("🎨 **Генератор промптов**\n\nЯ проанализирую ваши загруженные фото и создам идеальное текстовое описание стиля. Вы сможете утвердить его, и я буду использовать его для всех будущих статей.", 
                          call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_gen_new_prompt_"))
def kb_gen_new_prompt(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[4]
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_images, info FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    images_b64 = res[0] or []
    info = res[1] or {}
    
    if len(images_b64) < 1:
        bot.send_message(call.message.chat.id, "⚠️ Сначала загрузите хотя бы 1 фото в галерею!")
        return

    bot.send_message(call.message.chat.id, "⏳ Анализирую ваши фото и придумываю стиль...")
    
    try:
        context = f"Niche: {info.get('survey_step1')}. Audience: {info.get('survey_step2')}."
        text_prompt = f"Based on this context: {context}, write a highly detailed, photorealistic image generation prompt (in English) for a header image. Focus on lighting, texture, and professional look. Output JUST the prompt."
        prompt_text = get_gemini_response(text_prompt)
    except Exception as e:
        print(f"Gen Error: {e}")
        prompt_text = "Modern interior design, wall panels, cinematic lighting, 8k resolution, photorealistic."

    bot.send_message(call.message.chat.id, f"🎨 Генерирую превью по промпту:\n\n`{prompt_text}`", parse_mode='Markdown')
    
    img_bytes = generate_image_bytes(prompt_text)
    
    if img_bytes:
        prompt_id = f"{pid}_{int(time.time())}"
        TEMP_PROMPTS[prompt_id] = prompt_text
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Сохранить", callback_data=f"kb_approve_p_{prompt_id}"),
                   types.InlineKeyboardButton("❌ Нет, другой", callback_data=f"kb_gen_new_prompt_{pid}"))
        bot.send_photo(call.message.chat.id, img_bytes, caption="Нравится такой стиль?", reply_markup=markup)
    else:
        bot.send_message(call.message.chat.id, "❌ Не удалось сгенерировать превью.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_approve_p_"))
def kb_approve_prompt(call):
    try: bot.answer_callback_query(call.id, "Сохранено!")
    except: pass
    prompt_id = call.data.split("p_")[1]
    pid = prompt_id.split("_")[0]
    
    if prompt_id in TEMP_PROMPTS:
        text = TEMP_PROMPTS[prompt_id]
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT approved_prompts FROM projects WHERE id=%s", (pid,))
        res = cur.fetchone()
        current_list = res[0] or []
        current_list.append(text)
        cur.execute("UPDATE projects SET approved_prompts=%s WHERE id=%s", (json.dumps(current_list), pid))
        conn.commit(); cur.close(); conn.close()
        del TEMP_PROMPTS[prompt_id]
        bot.send_message(call.message.chat.id, "✅ Промпт добавлен в базу! Теперь я буду использовать его в статьях.")
        kb_menu_wrapper(call.message.chat.id, pid)
    else:
        bot.send_message(call.message.chat.id, "⚠️ Промпт устарел.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_set_text_"))
def kb_set_text(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[3]
    msg = bot.send_message(call.message.chat.id, "📝 Опишите идеальный стиль картинок (промпт).")
    bot.register_next_step_handler(msg, save_kb_text, pid)

def save_kb_text(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET style_prompt=%s WHERE id=%s", (message.text, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Стиль сохранен!")
    kb_menu_wrapper(message.chat.id, pid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_set_negative_"))
def kb_set_negative(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[3]
    msg = bot.send_message(call.message.chat.id, "🚫 Напишите, чего НЕ должно быть на фото (Anti-prompt).")
    bot.register_next_step_handler(msg, save_kb_negative, pid)

def save_kb_negative(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET style_negative_prompt=%s WHERE id=%s", (message.text, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Анти-промпт сохранен!")
    kb_menu_wrapper(message.chat.id, pid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_add_photo_"))
def kb_add_photo(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[3]
    UPLOAD_STATE[call.from_user.id] = pid
    bot.send_message(call.message.chat.id, "🖼 Отправьте фото (JPG/PNG).")

@bot.message_handler(content_types=['photo', 'document'])
def handle_photo_upload(message):
    uid = message.from_user.id
    if uid not in UPLOAD_STATE: return 
    def _save_photo():
        try:
            pid = UPLOAD_STATE[uid]
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT style_images FROM projects WHERE id=%s FOR UPDATE", (pid,))
            images = cur.fetchone()[0] or []
            if len(images) >= 30:
                cur.close(); conn.close()
                bot.send_message(message.chat.id, "⚠️ Лимит 30 фото.")
                return 
            file_info = bot.get_file(message.photo[-1].file_id) if message.photo else bot.get_file(message.document.file_id)
            if file_info.file_size > 1048576: cur.close(); conn.close(); return
            downloaded_file = bot.download_file(file_info.file_path)
            b64_img = base64.b64encode(downloaded_file).decode('utf-8')
            images.append(b64_img)
            cur.execute("UPDATE projects SET style_images=%s WHERE id=%s", (json.dumps(images), pid))
            conn.commit(); cur.close(); conn.close()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("➕ Еще", callback_data=f"kb_add_photo_{pid}"),
                       types.InlineKeyboardButton("🔙 Меню", callback_data=f"kb_menu_{pid}"))
            bot.reply_to(message, f"✅ Фото №{len(images)} сохранено", reply_markup=markup)
        except Exception as e: print(f"Upload Error: {e}")
    threading.Thread(target=_save_photo).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_gallery_"))
def kb_gallery(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_images FROM projects WHERE id=%s", (pid,))
    images = cur.fetchone()[0] or []
    cur.close(); conn.close()
    markup = types.InlineKeyboardMarkup(row_width=3)
    btns = [types.InlineKeyboardButton(f"Фото {i+1}", callback_data=f"kb_view_{pid}_{i}") for i in range(len(images))]
    markup.add(*btns)
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"kb_menu_{pid}"))
    try: bot.edit_message_text(f"📁 **Галерея ({len(images)} фото)**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, f"📁 **Галерея ({len(images)} фото)**", reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_view_"))
def kb_view_photo(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    parts = call.data.split("_"); pid, idx = parts[2], int(parts[3])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_images FROM projects WHERE id=%s", (pid,))
    images = cur.fetchone()[0] or []
    cur.close(); conn.close()
    if idx >= len(images): kb_gallery(call); return
    img_bytes = base64.b64decode(images[idx])
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"kb_del_{pid}_{idx}"),
               types.InlineKeyboardButton("🔙 Галерея", callback_data=f"kb_gallery_{pid}"))
    try: bot.send_photo(call.message.chat.id, img_bytes, caption=f"🖼 Фото #{idx+1}", reply_markup=markup)
    except: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_del_"))
def kb_delete_single(call):
    try: bot.answer_callback_query(call.id, "Удалено")
    except: pass
    parts = call.data.split("_"); pid, idx = parts[2], int(parts[3])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_images FROM projects WHERE id=%s FOR UPDATE", (pid,))
    images = cur.fetchone()[0] or []
    if idx < len(images):
        del images[idx]
        cur.execute("UPDATE projects SET style_images=%s WHERE id=%s", (json.dumps(images), pid))
        conn.commit()
    cur.close(); conn.close()
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass
    kb_gallery(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_clear_photos_"))
def kb_clear_photos(call):
    try: bot.answer_callback_query(call.id, "Фото удалены.")
    except: pass
    pid = call.data.split("_")[3]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET style_images='[]' WHERE id=%s", (pid,))
    conn.commit(); cur.close(); conn.close()
    kb_menu(call)

def kb_menu_wrapper(chat_id, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_prompt, style_images, style_negative_prompt FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    style_text = res[0] or "Не задан"; images = res[1] or []; neg = res[2] or "Не задан"
    msg = (f"🧠 **База Знаний**\n🎨: {escape_md(style_text)}\n🚫: {escape_md(neg)}\n🖼: {len(images)}")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎨 Промпт", callback_data=f"kb_set_text_{pid}"),
               types.InlineKeyboardButton("🚫 Анти-промпт", callback_data=f"kb_set_negative_{pid}"),
               types.InlineKeyboardButton("🖼 Добавить фото", callback_data=f"kb_add_photo_{pid}"))
    if images: markup.add(types.InlineKeyboardButton("📂 Галерея", callback_data=f"kb_gallery_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
    bot.send_message(chat_id, msg, reply_markup=markup, parse_mode='Markdown')

# --- UTILS (PROFILE & ADMIN) ---
def show_profile(uid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT tariff, gens_left, balance, joined_at, total_paid_rub FROM users WHERE user_id=%s", (uid,))
    u = cur.fetchone()
    cur.execute("SELECT count(*) FROM projects WHERE user_id=%s", (uid,))
    projs = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM articles WHERE status='published' AND project_id IN (SELECT id FROM projects WHERE user_id=%s)", (uid,))
    arts = cur.fetchone()[0]
    cur.close(); conn.close()
    txt = (f"👤 **Профиль**\nID: `{uid}`\n📅 Рег: {u[3].strftime('%Y-%m-%d')}\n💎 Тариф: {u[0]}\n⚡ Генераций: {u[1]}\n💰 Расход: {u[4]}р\n📂 Проектов: {projs}\n📄 Статей: {arts}")
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Пополнить баланс", callback_data="period_test"))
    bot.send_message(uid, txt, reply_markup=markup, parse_mode='Markdown')

def show_tariff_periods(user_id):
    txt = ("💎 **ТАРИФЫ**\n1️⃣ **Тест-драйв** — 500р (5 ген)\n2️⃣ **Старт** — 1400р/мес (15 ген)\n3️⃣ **Профи** — 2500р/мес (30 ген)\n4️⃣ **Агент** — 7500р/мес (100 ген)")
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # GIF
    gif_url = "https://ecosteni.ru/wp-content/uploads/2026/01/202601080242.gif"
    
    markup.add(types.InlineKeyboardButton("🏎 Тест (500р)", callback_data="period_test"),
               types.InlineKeyboardButton("📅 Месяц", callback_data="period_month"),
               types.InlineKeyboardButton("📆 Год", callback_data="period_year"))
    try:
        bot.send_animation(user_id, gif_url, caption=txt, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.send_message(user_id, txt, reply_markup=markup, parse_mode='Markdown')

def show_admin_panel(uid):
    if uid != ADMIN_ID: return

    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()

    try:
        # 1. Посетители за сегодня (DAU)
        cur.execute("SELECT count(DISTINCT user_id) FROM users WHERE last_active >= CURRENT_DATE")
        dau = cur.fetchone()[0]

        # 2. Прибыль за месяц (с 1 числа)
        cur.execute("SELECT sum(amount) FROM payments WHERE created_at >= date_trunc('month', CURRENT_DATE) AND currency='rub'")
        profit_rub = cur.fetchone()[0] or 0
        cur.execute("SELECT sum(amount) FROM payments WHERE created_at >= date_trunc('month', CURRENT_DATE) AND currency='stars'")
        profit_stars = cur.fetchone()[0] or 0

        # 3. Опубликовано статей за сегодня
        cur.execute("SELECT count(*) FROM articles WHERE status='published' AND published_url IS NOT NULL AND created_at >= CURRENT_DATE")
        articles_today = cur.fetchone()[0]

        # 4. Новые пользователи (Сегодня / Месяц)
        cur.execute("SELECT count(*) FROM users WHERE joined_at >= CURRENT_DATE")
        new_today = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM users WHERE joined_at >= date_trunc('month', CURRENT_DATE)")
        new_month = cur.fetchone()[0]

        # 5. Проекты (Всего)
        cur.execute("SELECT count(*) FROM projects")
        total_projects = cur.fetchone()[0]

        # 6. Тарифы (Статистика покупок за все время)
        cur.execute("SELECT tariff_name, count(*) FROM payments GROUP BY tariff_name ORDER BY count(*) DESC")
        tariff_stats = cur.fetchall()
        tariff_text = "\n".join([f"• {t[0]}: {t[1]} шт." for t in tariff_stats]) if tariff_stats else "Нет продаж"

        text = (
            f"⚙️ **АДМИН ПАНЕЛЬ**\n\n"
            f"👥 **Активность:**\n"
            f"• Заходили сегодня: {dau}\n"
            f"• Новых сегодня: {new_today}\n"
            f"• Новых за месяц: {new_month}\n\n"
            f"💰 **Финансы (Месяц):**\n"
            f"• Рубли: {profit_rub}₽\n"
            f"• Звезды: {profit_stars}⭐️\n\n"
            f"📄 **Контент:**\n"
            f"• Статей за сегодня: {articles_today}\n"
            f"• Всего проектов: {total_projects}\n\n"
            f"📊 **Продажи тарифов:**\n{tariff_text}"
        )
        
        bot.send_message(uid, text, parse_mode='Markdown')
    except Exception as e:
        bot.send_message(uid, f"Ошибка админки: {e}")
    finally:
        cur.close(); conn.close()

# --- PAYMENTS & HANDLERS ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("period_"))
def tariff_period_select(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    p_type = call.data.split("_")[1]
    if p_type == "test": process_tariff_selection(call, "Тест-драйв", 500, "test")
    elif p_type == "month":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("Старт (1400р)", callback_data="buy_start_1m"),
                   types.InlineKeyboardButton("Профи (2500р)", callback_data="buy_pro_1m"),
                   types.InlineKeyboardButton("Агент (7500р)", callback_data="buy_agent_1m"),
                   types.InlineKeyboardButton("🔙 Назад", callback_data="back_periods"))
        bot.edit_message_text("📅 Тарифы на Месяц:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    elif p_type == "year":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("Старт (11760р)", callback_data="buy_start_1y"),
                   types.InlineKeyboardButton("Профи (21000р)", callback_data="buy_pro_1y"),
                   types.InlineKeyboardButton("Агент (62999р)", callback_data="buy_agent_1y"),
                   types.InlineKeyboardButton("🔙 Назад", callback_data="back_periods"))
        bot.edit_message_text("📆 Тарифы на Год:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "back_periods")
def back_to_periods(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    show_tariff_periods(call.from_user.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass

def process_tariff_selection(call, name, price, code):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Картой (РФ)", callback_data=f"pay_rub_{code}_{price}"),
               types.InlineKeyboardButton("⭐ Stars", callback_data=f"pay_star_{code}_{price}"))
    bot.edit_message_text(f"Оплата: {name} ({price}р)", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def pre_payment(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    parts = call.data.split("_")
    tariff_code, period = parts[1], parts[2]
    price = 0; name = ""
    if tariff_code == "start": price = 1400 if period == "1m" else 11760; name = "СЕО Старт"
    elif tariff_code == "pro": price = 2500 if period == "1m" else 21000; name = "СЕО Профи"
    elif tariff_code == "agent": price = 7500 if period == "1m" else 62999; name = "PBN Агент"
    process_tariff_selection(call, name, price, f"{tariff_code}_{period}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def process_payment(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    parts = call.data.split("_"); currency = parts[1]; amount = int(parts[3])
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("ask_del_"))
def ask_del(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[-1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE id=%s", (pid,))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(call.message.chat.id, "Удалено.")
    list_projects(call.from_user.id, call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_ask_count_"))
def kw_ask_count(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[3]
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("50", callback_data=f"kw_gen_{pid}_50"),
               types.InlineKeyboardButton("100", callback_data=f"kw_gen_{pid}_100"),
               types.InlineKeyboardButton("200", callback_data=f"kw_gen_{pid}_200"),
               types.InlineKeyboardButton("500", callback_data=f"kw_gen_{pid}_500"),
               types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
    bot.send_message(call.message.chat.id, "🔑 Выберите количество ключевых слов:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_gen_"))
def kw_gen_handler(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    parts = call.data.split("_"); pid = parts[2]; count = parts[3]
    bot.send_message(call.message.chat.id, f"⏳ Генерирую {count} ключевых слов...")
    def _gen_keywords():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone(); info = res[0] or {}
            context_str = f"Site: {info.get('survey_step1','')}\nAudience: {info.get('survey_step2','')}\nRegion: {info.get('survey_step3','')}\nUSP: {info.get('survey_step4','')}"
            prompt = f"Act as SEO Expert. Context:\n{context_str}\nTask: Generate {count} keywords clustered by intent (Cluster Name:\n- keyword). Lang: Russian."
            ai_resp = get_gemini_response(prompt)
            cur.execute("UPDATE projects SET keywords=%s WHERE id=%s", (ai_resp, pid))
            conn.commit(); cur.close(); conn.close()
            send_safe_message(call.message.chat.id, f"🔑 **Ядро ({count} шт):**\n\n{ai_resp}", parse_mode=None)
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"kw_approve_{pid}"),
                       types.InlineKeyboardButton("📄 Скачать .txt", callback_data=f"kw_download_{pid}"),
                       types.InlineKeyboardButton("🔄 Заново", callback_data=f"srv_{pid}"),
                       types.InlineKeyboardButton("🔢 Изменить кол-во", callback_data=f"kw_ask_count_{pid}"))
            bot.send_message(call.message.chat.id, "👇 Действия:", reply_markup=markup)
        except Exception as e: bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")
    threading.Thread(target=_gen_keywords).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_approve_"))
def kw_approve(call):
    try: bot.answer_callback_query(call.id, "Сохранено!")
    except: pass
    pid = call.data.split("_")[2]
    open_project_menu(call.message.chat.id, pid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_download_"))
def kw_download(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT keywords FROM projects WHERE id=%s", (pid,))
    kw_text = cur.fetchone()[0] or "No keywords"
    cur.close(); conn.close()
    file_data = io.BytesIO(kw_text.encode('utf-8')); file_data.name = "keywords.txt"
    bot.send_document(call.message.chat.id, file_data, caption="📂 Семантическое ядро")

# --- SURVEY ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def start_survey_handler(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[1]
    SURVEY_STATE[call.from_user.id] = {'pid': pid, 'step': 1}
    msg = bot.send_message(call.message.chat.id, "📝 **Опрос (1/4)**\nОпишите суть сайта/бизнеса:", parse_mode='Markdown')
    bot.register_next_step_handler(msg, survey_step_router)

def survey_step_router(message):
    uid = message.from_user.id
    if uid not in SURVEY_STATE: return
    state = SURVEY_STATE[uid]; step = state['step']; pid = state['pid']; text = message.text
    if text.startswith('/'): return
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0] or {}
    info[f'survey_step{step}'] = text
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
    conn.commit(); cur.close(); conn.close()
    
    if step == 1:
        SURVEY_STATE[uid]['step'] = 2
        msg = bot.send_message(message.chat.id, "📝 **(2/4) Целевая аудитория?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 2:
        SURVEY_STATE[uid]['step'] = 3
        msg = bot.send_message(message.chat.id, "📝 **(3/4) Регион продвижения?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 3:
        SURVEY_STATE[uid]['step'] = 4
        msg = bot.send_message(message.chat.id, "📝 **(4/4) Сильные стороны (УТП)?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 4:
        del SURVEY_STATE[uid]
        update_project_progress(pid, "info_done")
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(types.InlineKeyboardButton("50", callback_data=f"kw_gen_{pid}_50"),
                   types.InlineKeyboardButton("100", callback_data=f"kw_gen_{pid}_100"),
                   types.InlineKeyboardButton("200", callback_data=f"kw_gen_{pid}_200"),
                   types.InlineKeyboardButton("500", callback_data=f"kw_gen_{pid}_500"))
        bot.send_message(message.chat.id, "✅ Опрос завершен! Генерируем ключи:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_kw_"))
def view_kw(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT keywords FROM projects WHERE id=%s", (pid,))
    kw = cur.fetchone()[0]
    if not kw or len(str(kw).strip()) < 5:
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📝 Пройти опрос", callback_data=f"srv_{pid}"),
                                                  types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
        bot.send_message(call.message.chat.id, "Нет ключей. Пройдите опрос.", reply_markup=markup)
    else:
        if len(kw) > 3000:
            markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📄 Скачать", callback_data=f"kw_download_{pid}"),
                       types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
            bot.send_message(call.message.chat.id, f"Ключи (отрывок):\n{kw[:500]}...", reply_markup=markup)
        else: send_safe_message(call.message.chat.id, f"Ключи:\n{kw}")

# --- OTHER HANDLERS (COMPETITORS, ANALYSIS, ETC - Same as before) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("comp_start_"))
def comp_start(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]; USER_CONTEXT[call.from_user.id] = pid
    msg = bot.send_message(call.message.chat.id, "🔗 Ссылка на конкурента:")
    bot.register_next_step_handler(msg, analyze_competitor_step, pid)

def analyze_competitor_step(message, pid):
    def _process_comp():
        try:
            if message.text.startswith("/"): return
            url = message.text.strip()
            msg = bot.send_message(message.chat.id, "🕵️‍♂️ Анализирую...")
            scraped_data, _ = deep_analyze_site(url)
            prompt = f"Analyze competitor {url}. Content: {scraped_data[:4000]}. Give score (1-10), SEO critique, Keywords."
            ai_resp = get_gemini_response(prompt)
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
            info = cur.fetchone()[0] or {}; clist = info.get("competitors_list", [])
            clist.append(ai_resp); info["competitors_list"] = clist
            cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info, ensure_ascii=False), pid))
            conn.commit(); cur.close(); conn.close()
            try: bot.delete_message(message.chat.id, msg.message_id)
            except: pass
            send_safe_message(message.chat.id, f"✅ **Анализ:**\n\n{ai_resp}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("➕ Еще", callback_data=f"comp_start_{pid}"),
                       types.InlineKeyboardButton("➡️ Готово", callback_data=f"comp_finish_{pid}"))
            bot.send_message(message.chat.id, "Дальше?", reply_markup=markup)
        except: pass
    threading.Thread(target=_process_comp).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("comp_finish_"))
def comp_finish(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    update_project_progress(pid, "competitors_done")
    open_project_menu(call.message.chat.id, pid, mode="onboarding", msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("sel_anz_"))
def select_analysis_type(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚡ Быстрый", callback_data=f"do_anz_{pid}_fast"))
    markup.add(types.InlineKeyboardButton("⚖️ Средний", callback_data=f"do_anz_{pid}_medium"))
    markup.add(types.InlineKeyboardButton("🕵️‍♂️ Глубокий", callback_data=f"do_anz_{pid}_deep"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"open_proj_mgmt_{pid}"))
    bot.edit_message_text("Тип анализа:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("do_anz_"))
def perform_analysis(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    _, _, pid, type_ = call.data.split("_")
    bot.edit_message_text(f"⏳ Анализ ({type_})...", call.message.chat.id, call.message.message_id)
    def _run_analysis():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
            url = cur.fetchone()[0]; cur.close(); conn.close()
            raw_data, _ = deep_analyze_site(url)
            prompt = f"SEO audit ({type_}) for {url}. Data: {raw_data}. Lang: Russian."
            advice = get_gemini_response(prompt)
            send_safe_message(call.message.chat.id, f"📊 **Отчет:**\n\n{advice}")
            update_project_progress(pid, "analysis_done")
            open_project_menu(call.message.chat.id, pid, mode="onboarding")
        except: pass
    threading.Thread(target=_run_analysis).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("test_article_"))
def test_article_start(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT gens_left FROM users WHERE user_id=%s", (call.from_user.id,))
    res = cur.fetchone(); cur.close(); conn.close()
    if res and res[0] <= 0:
        bot.send_message(call.message.chat.id, "⚠️ Нет генераций. Пополните баланс.")
        return
    pid = call.data.split("_")[2]
    bot.send_message(call.message.chat.id, "⏳ Генерирую 5 тем...")
    def _gen_topics():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT info, keywords, style_prompt FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone(); info = res[0] or {}; kw = res[1] or ""; style = res[2] or ""
            prompt = f"5 viral blog topics for {info.get('survey_step1', 'site')}. SEO Keywords: {kw[:500]}. Style: {style}. Return strictly JSON array of strings."
            raw_response = get_gemini_response(prompt)
            topics = clean_and_parse_json(raw_response)
            if not topics: return
            info["temp_topics"] = topics
            cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
            conn.commit(); cur.close(); conn.close()
            markup = types.InlineKeyboardMarkup(row_width=1)
            msg_text = "📝 **Выберите тему:**\n\n"
            for i, t in enumerate(topics[:5]):
                msg_text += f"{i+1}. {t}\n"
                markup.add(types.InlineKeyboardButton(f"Вариант {i+1}", callback_data=f"write_{pid}_topic_{i}"))
            bot.send_message(call.message.chat.id, msg_text, reply_markup=markup, parse_mode='Markdown')
        except: pass
    threading.Thread(target=_gen_topics).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article_handler(call):
    try: bot.answer_callback_query(call.id, "Пишу...")
    except: pass
    parts = call.data.split("_"); pid, idx = parts[1], int(parts[3])
    
    # GIF
    gif_url = "https://ecosteni.ru/wp-content/uploads/2026/01/202601080219.gif"
    caption = "⏳ Начинаю писать статью... Это займет около минуты."
    try: bot.send_animation(call.message.chat.id, gif_url, caption=caption)
    except: bot.send_message(call.message.chat.id, caption)
    
    def _write_art():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT info, keywords, sitemap_links, style_prompt, approved_prompts FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info = res[0]; kw = res[1] or ""; sitemap = res[2]; style = res[3] or ""; app_p = res[4] or []
            
            sitemap_list = sitemap if isinstance(sitemap, list) else (json.loads(sitemap) if isinstance(sitemap, str) else [])
            links_text = "\n".join(sitemap_list[:30])
            topic = info.get("temp_topics", [])[idx]
            
            # Form context for Gemini
            survey_context = f"Niche: {info.get('survey_step1')}\nAudience: {info.get('survey_step2')}\nRegion: {info.get('survey_step3')}\nUSP: {info.get('survey_step4')}"
            
            # Use Approved Prompts if available
            visual_guide = f"Approved Prompts to Adapt: {app_p}" if app_p else f"Style Guide: {style}"

            cur.execute("UPDATE users SET gens_left = gens_left - 1 WHERE user_id = (SELECT user_id FROM projects WHERE id=%s) AND is_admin = FALSE", (pid,))
            conn.commit()
            
            prompt = f"""
            STRICTLY RUSSIAN LANGUAGE (Русский язык).
            Role: SEO Expert. Topic: "{topic}". Length: 2000 words.
            CONTEXT: {survey_context}
            VISUALS: {visual_guide}
            KEYWORDS: {kw[:1000]}
            LINKS: {links_text}
            
            INSTRUCTIONS:
            1. Write a high-quality article in Russian.
            2. Use [IMG: description] tags. Adapt descriptions from VISUALS to fit the context.
            3. Follow Yoast SEO: Keyphrase in Intro, Subheadings, Meta.
            
            OUTPUT JSON: {{ "html_content": "...", "seo_title": "...", "meta_desc": "...", "focus_kw": "...", "featured_img_prompt": "..." }}
            """
            resp = get_gemini_response(prompt)
            data = clean_and_parse_json(resp)
            if data: html = data.get("html_content",""); seo = data
            else: html = resp; seo = {"seo_title": topic}
            
            cur.execute("INSERT INTO articles (project_id, title, content, seo_data, status, rewrite_count) VALUES (%s, %s, %s, %s, 'draft', 0) RETURNING id", (pid, topic, html, json.dumps(seo)))
            aid = cur.fetchone()[0]
            conn.commit(); cur.close(); conn.close()
            
            # Send cleaned text
            send_safe_message(call.message.chat.id, format_html_for_chat(html), parse_mode='HTML')
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"pre_approve_{aid}"),
                       types.InlineKeyboardButton("✏️ Переписать", callback_data=f"rewrite_{aid}"))
            bot.send_message(call.message.chat.id, "👇 Действия:", reply_markup=markup)
        except Exception as e: bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")
    threading.Thread(target=_write_art).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("pre_approve_"))
def pre_approve_check(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    aid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT project_id FROM articles WHERE id=%s", (aid,))
    pid = cur.fetchone()[0]
    cur.close(); conn.close()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🚀 Опубликовать", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("🔙 В меню", callback_data=f"open_proj_mgmt_{pid}"))
    bot.send_message(call.message.chat.id, "✅ Утверждено. Публикуем?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("rewrite_"))
def rewrite_article(call):
    try: bot.answer_callback_query(call.id, "Переписываю...")
    except: pass
    aid = call.data.split("_")[1]
    
    # Check limits
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT rewrite_count, project_id, title, seo_data FROM articles WHERE id=%s", (aid,))
    row = cur.fetchone(); count, pid, title, seo = row
    if count >= 1:
        cur.close(); conn.close()
        bot.send_message(call.message.chat.id, "⚠️ Лимит рерайтов исчерпан.")
        return
    cur.execute("UPDATE articles SET rewrite_count = rewrite_count + 1 WHERE id=%s", (aid,))
    conn.commit()
    
    # GIF for Rewrite
    gif_url = "https://ecosteni.ru/wp-content/uploads/2026/01/202601080219.gif"
    try: bot.send_animation(call.message.chat.id, gif_url, caption="⏳ Переписываю статью...")
    except: bot.send_message(call.message.chat.id, "⏳ Переписываю статью...")

    # Fetch context again
    cur.execute("SELECT info, keywords, style_prompt, approved_prompts FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info = res[0]; style = res[2]; app_p = res[3]
    cur.close(); conn.close()

    def _do_rewrite():
        try:
            survey_context = f"Niche: {info.get('survey_step1')}\nUSP: {info.get('survey_step4')}"
            visual_guide = f"Approved Prompts: {app_p}" if app_p else f"Style: {style}"
            
            prompt = f"""
            STRICTLY RUSSIAN LANGUAGE (Русский язык).
            TASK: Rewrite article "{title}". Make it more engaging.
            CONTEXT: {survey_context}
            VISUALS: {visual_guide}
            Keep Yoast SEO rules.
            Output JSON: {{ "html_content": "...", "seo_title": "...", "meta_desc": "...", "focus_kw": "...", "featured_img_prompt": "..." }}
            """
            resp = get_gemini_response(prompt)
            data = clean_and_parse_json(resp)
            if data: html = data.get("html_content",""); seo_new = data
            else: html = resp; seo_new = {"seo_title": title}
            
            conn2 = get_db_connection(); cur2 = conn2.cursor()
            cur2.execute("UPDATE articles SET content=%s, seo_data=%s WHERE id=%s", (html, json.dumps(seo_new), aid))
            conn2.commit(); cur2.close(); conn2.close()
            
            send_safe_message(call.message.chat.id, format_html_for_chat(html), parse_mode='HTML')
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"pre_approve_{aid}"))
            bot.send_message(call.message.chat.id, "👇 Новая версия:", reply_markup=markup)
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")
            
    threading.Thread(target=_do_rewrite).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve_publish(call):
    try: bot.answer_callback_query(call.id, "Публикую...")
    except: pass
    aid = call.data.split("_")[1]
    bot.send_message(call.message.chat.id, "🚀 Публикация и генерация фото...")
    def _pub():
        conn = get_db_connection(); cur = conn.cursor()
        try:
            cur.execute("SELECT project_id, title, content, seo_data FROM articles WHERE id=%s", (aid,))
            pid, title, content, seo = cur.fetchone()
            cur.execute("SELECT cms_url, cms_login, cms_password, style_prompt, style_negative_prompt FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            if not res: return
            url, login, pwd, style, neg = res
            seo_data = seo if isinstance(seo, dict) else json.loads(seo)
            
            # Images
            img_matches = re.findall(r'\[IMG: (.*?)\]', content)
            final_content = content
            focus_kw = seo_data.get('focus_kw', 'seo')
            
            for i, p in enumerate(img_matches):
                mid, src, _ = generate_and_upload_image(url, login, pwd, p, focus_kw, f"{focus_kw}-{i}", style, neg)
                if src: final_content = final_content.replace(f'[IMG: {p}]', f'<img src="{src}" class="wp-image-{mid}"/>', 1)
                else: final_content = final_content.replace(f'[IMG: {p}]', '', 1)
            
            feat_id = None
            if seo_data.get('featured_img_prompt'):
                feat_id, _, _ = generate_and_upload_image(url, login, pwd, seo_data['featured_img_prompt'], focus_kw, f"{focus_kw}-main", style, neg)

            # WP Post
            creds = base64.b64encode(f"{login}:{pwd}".encode()).decode()
            post_data = {
                'title': seo_data.get('seo_title', title),
                'content': final_content,
                'status': 'publish',
                'featured_media': feat_id
            }
            r = requests.post(f"{url}/wp-json/wp/v2/posts", headers={'Authorization': 'Basic '+creds}, json=post_data)
            if r.status_code == 201:
                link = r.json().get('link')
                cur.execute("UPDATE articles SET status='published', published_url=%s WHERE id=%s", (link, aid))
                cur.execute("SELECT gens_left FROM users WHERE user_id=%s", (call.from_user.id,))
                left = cur.fetchone()[0]
                conn.commit()
                
                # Success GIF
                success_gif = "https://ecosteni.ru/wp-content/uploads/2026/01/202601080228.gif"
                
                markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Меню", callback_data=f"open_proj_mgmt_{pid}"))
                try:
                    bot.send_animation(call.message.chat.id, success_gif, caption=f"✅ Опубликовано: {link}\n⚡ Осталось: {left}", reply_markup=markup)
                except:
                    bot.send_message(call.message.chat.id, f"✅ Опубликовано: {link}\n⚡ Осталось: {left}", reply_markup=markup)
            else: bot.send_message(call.message.chat.id, f"❌ Ошибка WP: {r.text[:100]}")
        except Exception as e: print(e)
        finally: cur.close(); conn.close()
    threading.Thread(target=_pub).start()

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
    print("🤖 Бот запущен (Stable Image Gen & Safe MSG)...")
    bot.infinity_polling(skip_pending=True)
