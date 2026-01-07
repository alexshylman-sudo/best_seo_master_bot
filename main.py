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

ADMIN_ID = int(os.getenv("ADMIN_ID", "0")) 
SUPPORT_ID = 203473623 
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("APP_URL")

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)
USER_CONTEXT = {} 
UPLOAD_STATE = {} 

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
        time.sleep(0.1)

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
                for url_tag in root.findall('.//s:loc', ns):
                    links.append(url_tag.text)
                if not links:
                    for url_tag in root.findall('.//loc'):
                        links.append(url_tag.text)
            except: pass
        
        if not links:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            soup = BeautifulSoup(resp.text, 'html.parser')
            domain = urlparse(url).netloc
            for a in soup.find_all('a', href=True):
                full_url = urljoin(url, a['href'])
                if urlparse(full_url).netloc == domain:
                    links.append(full_url)
        
        clean_links = [l for l in list(set(links)) if not any(x in l for x in ['.jpg', '.png', 'wp-admin', 'feed'])]
        return clean_links[:100]
    except:
        return []

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

    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        try: return json.loads(text[start:end+1])
        except: pass
    
    start_list = text.find('[')
    end_list = text.rfind(']')
    if start_list != -1 and end_list != -1:
        try: return json.loads(text[start_list:end_list+1])
        except: pass

    return None

def format_html_for_chat(html_content):
    text = str(html_content).replace('\\n', '\n')
    if '"seo_title":' in text:
        text = text.split('"seo_title":')[0].rsplit(',', 1)[0].rsplit('{', 1)[0]
    
    text = re.sub(r'\}\s*$', '', text)
    text = re.sub(r'```json.*', '', text, flags=re.DOTALL)
    text = re.sub(r'```', '', text)
    text = re.sub(r'\[IMG:.*?\]', '', text)
    text = re.sub(r'<h[1-6]>(.*?)</h[1-6]>', r'\n\n<b>\1</b>\n', text)
    text = re.sub(r'<li>(.*?)</li>', r'• \1\n', text)
    
    soup = BeautifulSoup(text, "html.parser")
    for script in soup(["script", "style", "head", "title", "meta", "table", "style"]):
        script.decompose()
    
    clean_text = soup.get_text(separator="\n\n")
    return re.sub(r'\n\s*\n', '\n\n', clean_text).strip()

# --- 4. IMAGE GENERATION (TIER 1 - IMAGEN 4 FAST) ---
def generate_and_upload_image(api_url, login, pwd, image_prompt, alt_text, seo_filename, project_style=""):
    image_bytes = None
    target_model = 'imagen-4.0-fast-generate-001'
    
    if project_style and len(project_style) > 5:
        final_prompt = f"{project_style}. {image_prompt}. High resolution, 8k, cinematic lighting."
    else:
        final_prompt = f"Professional photography, {image_prompt}, realistic, high resolution, 8k, cinematic lighting"
    
    print(f"🎨 Imagen 4 Generating: {final_prompt[:60]}...")
    
    try:
        response = client.models.generate_images(
            model=target_model, 
            prompt=final_prompt,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio='16:9'
            )
        )
        if response.generated_images:
            image_bytes = response.generated_images[0].image.image_bytes
        else:
            return None, None, "⚠️ Imagen вернул пустоту (Safety)."
            
    except Exception as e:
        print(f"❌ Google Imagen Error: {e}")
        return None, None, f"❌ API Error: {e}"

    if not image_bytes: return None, None, "❌ No bytes."

    try:
        api_url = api_url.rstrip('/')
        creds = base64.b64encode(f"{login}:{pwd}".encode()).decode()
        
        if seo_filename:
            file_name = f"{slugify(seo_filename)}-{random.randint(10,99)}.png"
        else:
            file_name = f"img-{slugify(alt_text[:20])}-{random.randint(100,999)}.png"

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
        elif r.status_code == 401:
            return None, None, "❌ WP 401: Неверный пароль."
        elif r.status_code == 403:
            return None, None, "❌ WP 403: Доступ запрещен."
        else:
            return None, None, f"❌ WP Error {r.status_code}"
            
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
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT id FROM projects WHERE url LIKE %s OR url LIKE %s", (clean_check_url, clean_check_url + '/'))
            exists = cur.fetchone()
            cur.close(); conn.close()

            if exists:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
                bot.send_message(message.chat.id, f"🚫 **Этот сайт уже добавлен в базу!**\n\nПовторное добавление невозможно. Найдите его в разделе '📂 Мои проекты'.", 
                                 parse_mode='Markdown', reply_markup=markup)
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

# --- MENU ---
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

# --- CMS HANDLERS ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_select_"))
def cms_start_setup(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Отмена", callback_data=f"proj_settings_{pid}"))
    
    msg = bot.send_message(call.message.chat.id, 
                           "🔐 **Настройка WordPress**\n\n1. Убедитесь, что у вас включены 'Application Passwords'.\n2. Введите **Логин** администратора:", 
                           reply_markup=markup, parse_mode='Markdown')
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

# --- KNOWLEDGE BASE HANDLERS (UPDATED) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_menu_"))
def kb_menu(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_prompt, style_images FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    style_text = res[0] if res and res[0] else "Не задан"
    images = res[1] if res and res[1] else []
    
    msg = f"🧠 **База Знаний (Стиль)**\n\n📝 **Промпт:**\n_{escape_md(style_text)}_\n\n🖼 **Фото:** {len(images)}/30 загружено."
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 Изменить Стиль (Текст)", callback_data=f"kb_set_text_{pid}"))
    markup.add(types.InlineKeyboardButton(f"🖼 Добавить фото ({len(images)}/30)", callback_data=f"kb_add_photo_{pid}"))
    if images:
        markup.add(types.InlineKeyboardButton("📂 Галерея / Удаление", callback_data=f"kb_gallery_{pid}"))
        markup.add(types.InlineKeyboardButton("🗑 Очистить все фото", callback_data=f"kb_clear_photos_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
    
    bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_set_text_"))
def kb_set_text(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[3]
    msg = bot.send_message(call.message.chat.id, "📝 Опишите идеальный стиль картинок (промпт).\nПример: *Реалистичные фото, теплый свет, современный стиль, пастельные тона.*")
    bot.register_next_step_handler(msg, save_kb_text, pid)

def save_kb_text(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET style_prompt=%s WHERE id=%s", (message.text, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Стиль сохранен! Теперь генератор будет его использовать.")
    kb_menu_wrapper(message.chat.id, pid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_add_photo_"))
def kb_add_photo(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[3]
    UPLOAD_STATE[call.from_user.id] = pid
    bot.send_message(call.message.chat.id, "🖼 Отправьте фото (JPG/PNG) до 1МБ.\nМожно отправить несколько сразу (как альбом).")

@bot.message_handler(content_types=['photo', 'document'])
def handle_photo_upload(message):
    uid = message.from_user.id
    if uid not in UPLOAD_STATE: return 
    
    def _save_photo():
        try:
            pid = UPLOAD_STATE[uid]
            conn = get_db_connection()
            if not conn: return
            cur = conn.cursor()

            # БЛОКИРУЕМ СТРОКУ (FOR UPDATE) ЧТОБЫ ПОТОКИ ЖДАЛИ ДРУГ ДРУГА
            cur.execute("SELECT style_images FROM projects WHERE id=%s FOR UPDATE", (pid,))
            res = cur.fetchone()
            images = res[0] or []

            # --- ПРОВЕРКА ЛИМИТА ---
            if len(images) >= 30:
                cur.close(); conn.close()
                markup_limit = types.InlineKeyboardMarkup()
                markup_limit.add(types.InlineKeyboardButton("🔙 В меню проекта", callback_data=f"kb_menu_{pid}"))
                bot.send_message(message.chat.id, "⚠️ Лимит превышен! Максимум 30 фото.", reply_markup=markup_limit)
                return 
            # -----------------------

            file_info = None
            file_name_display = f"photo_{random.randint(1000,9999)}.jpg" # Default fallback
            
            # --- ОПРЕДЕЛЕНИЕ ИМЕНИ ФАЙЛА ---
            if message.document:
                # Если это документ - берем реальное имя файла
                if message.document.mime_type in ['image/jpeg', 'image/png']:
                    file_info = bot.get_file(message.document.file_id)
                    file_name_display = message.document.file_name
                else:
                    cur.close(); conn.close(); return
            elif message.photo:
                # Если это сжатое фото - генерируем имя
                file_info = bot.get_file(message.photo[-1].file_id)
                # Телеграм не хранит оригинальное имя для Compressed photos
                file_name_display = f"photo_{int(time.time())}_{random.randint(10,99)}.jpg"
            else:
                 cur.close(); conn.close(); return
            # -------------------------------

            if file_info.file_size > 1048576:
                cur.close(); conn.close(); return

            downloaded_file = bot.download_file(file_info.file_path)
            b64_img = base64.b64encode(downloaded_file).decode('utf-8')
            
            images.append(b64_img)
            cur.execute("UPDATE projects SET style_images=%s WHERE id=%s", (json.dumps(images), pid))
            conn.commit()
            
            current_count = len(images)
            cur.close(); conn.close()
            
            # --- КЛАВИАТУРА ПОСЛЕ ЗАГРУЗКИ ---
            markup = types.InlineKeyboardMarkup()
            if current_count < 30:
                markup.add(types.InlineKeyboardButton("➕ Добавить еще", callback_data=f"kb_add_photo_{pid}"))
            markup.add(types.InlineKeyboardButton("🔙 В меню проекта", callback_data=f"kb_menu_{pid}"))
            
            bot.reply_to(message, f"✅ Фото №{current_count} сохранено ({file_name_display})", reply_markup=markup)
            
        except Exception as e:
            print(f"Upload Error: {e}")

    threading.Thread(target=_save_photo).start()

# --- NEW: GALLERY & DELETE (FIXED) ---
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
    btns = []
    for i in range(len(images)):
        btns.append(types.InlineKeyboardButton(f"Фото {i+1}", callback_data=f"kb_view_{pid}_{i}"))
    
    markup.add(*btns)
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"kb_menu_{pid}"))
    
    msg_text = f"📁 **Галерея ({len(images)} фото)**\n\nНажмите на кнопку, чтобы увидеть фото и удалить его."
    
    try:
        # Попытка отредактировать сообщение (если это текст)
        bot.edit_message_text(
            text=msg_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode='Markdown'
        )
    except Exception:
        # Если редактирование не удалось (например, предыдущее сообщение было фото),
        # удаляем старое и отправляем новое.
        try:
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except:
            pass
        
        bot.send_message(
            chat_id=call.message.chat.id,
            text=msg_text,
            reply_markup=markup,
            parse_mode='Markdown'
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_view_"))
def kb_view_photo(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    parts = call.data.split("_")
    pid, idx = parts[2], int(parts[3])
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT style_images FROM projects WHERE id=%s", (pid,))
    images = cur.fetchone()[0] or []
    cur.close(); conn.close()
    
    if idx >= len(images):
        bot.send_message(call.message.chat.id, "❌ Фото уже удалено.")
        kb_gallery(call) # Refresh
        return

    b64_data = images[idx]
    img_bytes = base64.b64decode(b64_data)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑 Удалить это фото", callback_data=f"kb_del_{pid}_{idx}"))
    markup.add(types.InlineKeyboardButton("🔙 В галерею", callback_data=f"kb_gallery_{pid}"))
    
    try:
        bot.send_photo(call.message.chat.id, img_bytes, caption=f"🖼 Фото #{idx+1}", reply_markup=markup)
    except Exception as e:
        bot.send_message(call.message.chat.id, "❌ Ошибка отображения фото.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_del_"))
def kb_delete_single(call):
    try: bot.answer_callback_query(call.id, "Удалено")
    except: pass
    parts = call.data.split("_")
    pid, idx = parts[2], int(parts[3])
    
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    
    # Блокировка для безопасного удаления
    cur.execute("SELECT style_images FROM projects WHERE id=%s FOR UPDATE", (pid,))
    images = cur.fetchone()[0] or []
    
    if idx < len(images):
        del images[idx]
        cur.execute("UPDATE projects SET style_images=%s WHERE id=%s", (json.dumps(images), pid))
        conn.commit()
        # Попытка удалить фото перед возвратом в галерею
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, f"✅ Фото удалено. Осталось: {len(images)}")
    else:
        conn.rollback()
        bot.send_message(call.message.chat.id, "❌ Фото уже не существует.")
        
    cur.close(); conn.close()
    
    # Возвращаем в галерею
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
    cur.execute("SELECT style_prompt, style_images FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    style_text = res[0] if res and res[0] else "Не задан"
    images = res[1] if res and res[1] else []
    msg = f"🧠 **База Знаний (Стиль)**\n\n📝 **Промпт:**\n_{escape_md(style_text)}_\n\n🖼 **Фото:** {len(images)}/30 загружено."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 Изменить Стиль", callback_data=f"kb_set_text_{pid}"),
               types.InlineKeyboardButton(f"🖼 Добавить фото", callback_data=f"kb_add_photo_{pid}"))
    if images:
        markup.add(types.InlineKeyboardButton("📂 Галерея / Удаление", callback_data=f"kb_gallery_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"proj_settings_{pid}"))
    bot.send_message(chat_id, msg, reply_markup=markup, parse_mode='Markdown')

# --- UTILS (PROFILE) ---
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
    try: bot.answer_callback_query(call.id)
    except: pass
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
    try: bot.answer_callback_query(call.id)
    except: pass
    show_tariff_periods(call.from_user.id)

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
    try: bot.answer_callback_query(call.id)
    except: pass
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
    msg = bot.send_message(call.message.chat.id, "🔑 Вставьте список ключевых слов (столбиком):")
    bot.register_next_step_handler(msg, kw_save_step, pid)

def kw_save_step(message, pid):
    kw_text = message.text
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET keywords=%s WHERE id=%s", (kw_text, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Ключи сохранены!")
    open_project_menu(message.chat.id, pid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_kw_"))
def view_kw(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT keywords FROM projects WHERE id=%s", (pid,))
    kw = cur.fetchone()[0]
    send_safe_message(call.message.chat.id, f"Ключи:\n{kw}")

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Главное меню", reply_markup=main_menu_markup(call.from_user.id))

# --- LOGIC ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("comp_start_"))
def comp_start(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    USER_CONTEXT[call.from_user.id] = pid
    msg = bot.send_message(call.message.chat.id, "🔗 Пришлите ссылку на 1-го конкурента:")
    bot.register_next_step_handler(msg, analyze_competitor_step, pid)

def analyze_competitor_step(message, pid):
    def _process_comp():
        try:
            if message.text.startswith("/"): return
            url = message.text.strip()
            if not url.startswith("http"):
                bot.send_message(message.chat.id, "❌ Нужна ссылка с http.")
                return
            msg = bot.send_message(message.chat.id, "🕵️‍♂️ Анализирую конкурента... (это может занять 15-30 сек)")
            
            scraped_data, _ = deep_analyze_site(url)
            
            prompt = f"""
            Проанализируй сайт конкурента: {url}.
            Текст сайта: {scraped_data[:4000]}
            
            ЗАДАЧА:
            1. Оценка сайта (1-10) и краткое мнение (1-2 предложения, просто и по делу).
            2. Оптимизация: Хорошо/Плохо? (понятным языком).
            3. Инфо о продукте: Достаточно?
            4. Ключевые слова: Выпиши 5 лучших SEO-ключей (БЕЗ гео).
            """
            ai_resp = get_gemini_response(prompt)
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
            info = cur.fetchone()[0] or {}
            clist = info.get("competitors_list", [])
            clist.append(ai_resp)
            info["competitors_list"] = clist
            cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info, ensure_ascii=False), pid))
            conn.commit(); cur.close(); conn.close()
            
            try: bot.delete_message(message.chat.id, msg.message_id)
            except: pass
            
            send_safe_message(message.chat.id, f"✅ **Анализ:**\n\n{ai_resp}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("➕ Добавить еще", callback_data=f"comp_start_{pid}"))
            markup.add(types.InlineKeyboardButton("➡️ Готово, дальше", callback_data=f"comp_finish_{pid}"))
            bot.send_message(message.chat.id, "Добавить еще?", reply_markup=markup)
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка анализа конкурента: {e}")

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
    bot.edit_message_text("Выберите тип анализа:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("do_anz_"))
def perform_analysis(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    _, _, pid, type_ = call.data.split("_")
    bot.edit_message_text(f"⏳ Выполняю {type_} анализ...", call.message.chat.id, call.message.message_id)
    
    def _run_analysis():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
            url = cur.fetchone()[0]
            cur.close(); conn.close()
            
            raw_data, links = deep_analyze_site(url)
            prompt = f"Проведи {type_} SEO анализ сайта {url} на основе данных:\n{raw_data}\nЯзык: Русский. Дай конкретные рекомендации."
            advice = get_gemini_response(prompt)
            
            send_safe_message(call.message.chat.id, f"📊 **Отчет ({type_}):**\n\n{advice}")
            update_project_progress(pid, "analysis_done")
            open_project_menu(call.message.chat.id, pid, mode="onboarding")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка анализа: {e}")

    threading.Thread(target=_run_analysis).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("strat_"))
def strategy_start(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT cms_login, content_plan FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    if not res[0]:
        bot.send_message(call.message.chat.id, "⚠️ Настройте CMS в настройках проекта!")
        return
    
    plan = res[1]
    if plan and len(plan) > 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Показать текущий план", callback_data=f"show_plan_{pid}"))
        markup.add(types.InlineKeyboardButton("🗑 Удалить и создать новый", callback_data=f"reset_plan_{pid}"))
        bot.send_message(call.message.chat.id, "📅 У вас уже утвержден план на эту неделю.", reply_markup=markup)
        return

    markup = types.InlineKeyboardMarkup(row_width=4)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(call.message.chat.id, "📅 Сколько статей в неделю?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("show_plan_"))
def show_current_plan(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT content_plan FROM projects WHERE id=%s", (pid,))
    plan = cur.fetchone()[0] or []
    cur.close(); conn.close()
    
    msg = "🗓 **Ваш текущий план:**\n\n"
    for item in plan:
        msg += f"**{item['day']} {item['time']}**\n{item['topic']}\n\n"
    
    bot.send_message(call.message.chat.id, msg, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("reset_plan_"))
def reset_plan(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET content_plan='[]' WHERE id=%s", (pid,))
    conn.commit(); cur.close(); conn.close()
    strategy_start_helper(call, pid)

def strategy_start_helper(call, pid):
    markup = types.InlineKeyboardMarkup(row_width=4)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(call.message.chat.id, "📅 Сколько статей в неделю?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("freq_"))
def save_freq_and_plan(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    _, pid, freq = call.data.split("_")
    freq = int(freq)
    
    days_map = {0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье"}
    today_idx = datetime.datetime.today().weekday()
    remaining_days = [days_map[i] for i in range(today_idx + 1, 7)] 
    
    actual_count = min(freq, len(remaining_days)) if remaining_days else 0
    
    if actual_count == 0:
        bot.send_message(call.message.chat.id, f"📅 Эта неделя заканчивается. План на {freq} статей будет создан в следующий понедельник.\nСейчас вы можете написать **Тестовую статью**.")
        return

    bot.edit_message_text(f"📅 Генерирую план на остаток недели ({actual_count} статей)...", call.message.chat.id, call.message.message_id)
    
    def _gen_plan():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info_json = res[0] or {}
            survey = info_json.get("survey", "")
            kw = res[1] or ""
            
            days_str = ", ".join(remaining_days[:actual_count])
            prompt = f"""
            Роль: SEO Маркетолог.
            Задача: Составь контент-план только на эти дни: {days_str}.
            Всего статей: {actual_count}.
            Ниша: {survey}. Ключи: {kw[:1000]}
            
            Верни ТОЛЬКО JSON массив объектов (без Markdown):
            [
            {{"day": "Четверг", "time": "10:00", "topic": "Тема 1"}},
            {{"day": "Пятница", "time": "15:00", "topic": "Тема 2"}}
            ]
            """
            ai_resp = get_gemini_response(prompt)
            
            calendar_plan = clean_and_parse_json(ai_resp)
            if not calendar_plan:
                calendar_plan = [{"day": remaining_days[0], "time": "10:00", "topic": "Ошибка генерации, попробуйте сбросить"}]

            info_json["temp_plan"] = calendar_plan
            cur.execute("UPDATE projects SET frequency=%s, info=%s WHERE id=%s", (freq, json.dumps(info_json), pid))
            conn.commit(); cur.close(); conn.close()
            
            msg_text = "🗓 **План на остаток недели:**\n\n"
            for item in calendar_plan:
                msg_text += f"**{item['day']} {item['time']}**\n{item['topic']}\n\n"
            
            markup = types.InlineKeyboardMarkup(row_width=3)
            markup.add(types.InlineKeyboardButton("✅ Утвердить план", callback_data=f"approve_plan_{pid}"))
            
            short_days = {"Понедельник": "Пн", "Вторник": "Вт", "Среда": "Ср", "Четверг": "Чт", "Пятница": "Пт", "Суббота": "Сб", "Воскресенье": "Вс"}
            repl_btns = []
            for i, item in enumerate(calendar_plan):
                d_name = item.get('day', 'День')
                short = short_days.get(d_name, d_name[:2])
                repl_btns.append(types.InlineKeyboardButton(f"🔄 {short}", callback_data=f"repl_topic_{pid}_{i}"))
            markup.add(*repl_btns)
            
            bot.send_message(call.message.chat.id, msg_text, reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка генерации плана: {e}")

    threading.Thread(target=_gen_plan).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("repl_topic_"))
def replace_topic(call):
    try: bot.answer_callback_query(call.id, "🔄 Меняю тему...")
    except: pass
    
    _, _, pid, idx = call.data.split("_")
    idx = int(idx)
    
    def _repl_topic():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info = res[0]
            keywords = res[1] or ""
            plan = info.get("temp_plan", [])
            
            if idx < len(plan):
                old_topic = plan[idx]['topic']
                prompt = f"""
                Задача: Придумай 1 новую тему статьи для блога, отличную от '{old_topic}'. 
                Контекст ниши: {keywords[:500]}
                Верни ТОЛЬКО тему текстом (без кавычек).
                """
                new_topic = get_gemini_response(prompt).strip().replace('"', '')
                plan[idx]['topic'] = new_topic
                
                info["temp_plan"] = plan
                cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
                conn.commit()
            
            cur.close(); conn.close()
            
            msg_text = "🗓 **Обновленный план:**\n\n"
            for item in plan:
                msg_text += f"**{item['day']} {item['time']}**\n{item['topic']}\n\n"
                
            markup = types.InlineKeyboardMarkup(row_width=3)
            markup.add(types.InlineKeyboardButton("✅ Утвердить план", callback_data=f"approve_plan_{pid}"))
            
            short_days = {"Понедельник": "Пн", "Вторник": "Вт", "Среда": "Ср", "Четверг": "Чт", "Пятница": "Пт", "Суббота": "Сб", "Воскресенье": "Вс"}
            repl_btns = []
            for i, item in enumerate(plan):
                d_name = item.get('day', 'День')
                short = short_days.get(d_name, d_name[:2])
                repl_btns.append(types.InlineKeyboardButton(f"🔄 {short}", callback_data=f"repl_topic_{pid}_{i}"))
            markup.add(*repl_btns)
            
            bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка замены темы: {e}")

    threading.Thread(target=_repl_topic).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_plan_"))
def approve_plan(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0]
    plan = info.get("temp_plan", [])
    
    cur.execute("UPDATE projects SET content_plan=%s WHERE id=%s", (json.dumps(plan), pid))
    conn.commit(); cur.close(); conn.close()
    
    bot.edit_message_text(f"✅ План утвержден! На эту неделю запланировано {len(plan)} статей.\n\nКак только статья будет опубликована, я пришлю уведомление.", 
                          call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("test_article_"))
def test_article_start(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT gens_left FROM users WHERE user_id=%s", (call.from_user.id,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    if res and res[0] <= 0:
        bot.send_message(call.message.chat.id, "⚠️ У вас закончились бесплатные генерации (лимит 2). Пожалуйста, пополните баланс для продолжения.")
        return

    pid = call.data.split("_")[2]
    propose_test_topics(call.message.chat.id, pid)

def propose_test_topics(chat_id, pid):
    bot.send_message(chat_id, "⏳ Генерирую 5 тем для тестовой статьи (на основе ключей и базы знаний)...")
    
    def _gen_topics():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT info, keywords, style_prompt FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info = res[0] or {}
            kw = res[1] or ""
            style = res[2] or ""
            
            prompt = f"""
            Придумай 5 вирусных заголовков для статьи в блог.
            Ниша сайта (из опроса): {info.get('survey', 'Общая тема')}. 
            SEO Ключевые слова: {kw[:500]}
            Стиль проекта: {style}
            Язык: Русский.
            
            Строго верни ТОЛЬКО JSON массив строк, например:
            ["Как выбрать...", "ТОП 10 ошибок...", "Секреты..."]
            """
            
            raw_response = get_gemini_response(prompt)
            if "AI Error" in raw_response:
                bot.send_message(chat_id, f"⚠️ Ошибка ИИ:\n{raw_response}")
                cur.close(); conn.close()
                return

            topics = clean_and_parse_json(raw_response)
            if not topics:
                bot.send_message(chat_id, "⚠️ ИИ вернул пустой ответ или неверный формат. Попробуйте еще раз.")
                cur.close(); conn.close()
                return
            
            info["temp_topics"] = topics
            cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
            conn.commit(); cur.close(); conn.close()
            
            markup = types.InlineKeyboardMarkup(row_width=1)
            msg_text = "📝 **Выберите тему для теста:**\n\n"
            for i, t in enumerate(topics[:5]):
                msg_text += f"{i+1}. {t}\n"
                markup.add(types.InlineKeyboardButton(f"Вариант {i+1}", callback_data=f"write_{pid}_topic_{i}"))
                
            bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            bot.send_message(chat_id, f"❌ Ошибка генерации тем: {e}")

    threading.Thread(target=_gen_topics).start()

# --- WRITE ARTICLE HANDLER (FIXED) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article_handler(call):
    try: bot.answer_callback_query(call.id, "Пишу статью...")
    except: pass
    
    parts = call.data.split("_")
    pid, idx = parts[1], int(parts[3])
    
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, f"⏳ Начинаю писать статью... Это займет около минуты.", parse_mode='Markdown')
    
    def _write_art():
        try:
            conn = get_db_connection(); cur = conn.cursor()
            # 1. ЗАПРОС КЛЮЧЕЙ И СТИЛЯ
            cur.execute("SELECT info, keywords, sitemap_links, style_prompt FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            
            info = res[0]
            keywords_raw = res[1] or ""
            
            # --- FIX: SAFE SITEMAP LOADING ---
            sitemap_data = res[2]
            if isinstance(sitemap_data, list):
                sitemap_list = sitemap_data
            elif isinstance(sitemap_data, str):
                try: sitemap_list = json.loads(sitemap_data)
                except: sitemap_list = []
            else:
                sitemap_list = []
            # ---------------------------------
            
            style_prompt = res[3] or "" # Стиль из базы знаний
            
            links_text = "\n".join(sitemap_list[:30]) if sitemap_list else "No internal links found."
            
            topics = info.get("temp_topics", [])
            topic_text = topics[idx] if len(topics) > idx else "SEO Article"
            
            current_year = datetime.datetime.now().year
            
            cur.execute("UPDATE users SET gens_left = gens_left - 1 WHERE user_id = (SELECT user_id FROM projects WHERE id=%s) AND is_admin = FALSE", (pid,))
            conn.commit()
            
            # 2. ПРОМПТ С КЛЮЧАМИ ИЗ БАЗЫ
            prompt = f"""
            Role: Professional Magazine Editor & Yoast SEO Expert.
            Topic: "{topic_text}"
            Length: 2000-2500 words.
            Style: Magazine Layout (Use HTML <blockquote>, <table>, <ul>).
            Current Year: {current_year}.
            Style/Tone Prompt: {style_prompt}
            
            IMPORTANT: WRITE STRICTLY IN RUSSIAN LANGUAGE.
            
            SEO SEMANTIC CORE (Integrate these keywords naturally into the text):
            {keywords_raw}
            
            MANDATORY YOAST SEO RULES (GREEN BULLET):
            1. **Focus Keyword**: Pick ONE main keyword from the list above that best fits the topic. Use it in the Title, first paragraph, and subheadings.
            2. **Keyphrase Density**: Use the focus keyword 0.5-2% of the text length.
            3. **Subheadings**: Include focus keyword in 50% of H2 and H3 tags.
            4. **Internal Linking**: You MUST insert 2-3 links to other pages from this list:
            {links_text}
            (Insert them naturally in context using <a href="...">anchor</a>).
            5. **Readability**: Short paragraphs. Use transition words.
            6. **Images**: Insert 5 [IMG: description containing keyword] placeholders.
            7. **Meta Description**: Max 155 characters. Must contain keyword.
            8. **Title**: Max 60 chars. Start with Keyword.
            
            OUTPUT JSON ONLY:
            {{
                "html_content": "Full HTML content with [IMG:...] tags.",
                "seo_title": "SEO Title (Max 60 chars)",
                "meta_desc": "Meta Description (Max 155 chars)",
                "focus_kw": "Selected Focus Keyword",
                "featured_img_prompt": "Photorealistic image of {topic_text}, interior design style"
            }}
            """
            response_text = get_gemini_response(prompt)
            
            data = clean_and_parse_json(response_text)
            
            if data:
                article_html = data.get("html_content", "")
                seo_data = data
            else:
                article_html = response_text
                seo_data = {"seo_title": topic_text, "featured_img_prompt": f"Photo of {topic_text}"}

            cur.execute("INSERT INTO articles (project_id, title, content, seo_data, status, rewrite_count) VALUES (%s, %s, %s, %s, 'draft', 0) RETURNING id", 
                        (pid, topic_text, article_html, json.dumps(seo_data)))
            aid = cur.fetchone()[0]
            conn.commit(); cur.close(); conn.close()
            
            clean_view = format_html_for_chat(article_html)
            try:
                send_safe_message(call.message.chat.id, clean_view, parse_mode='HTML')
            except:
                send_safe_message(call.message.chat.id, clean_view, parse_mode=None)
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"pre_approve_{aid}"),
                    types.InlineKeyboardButton("✏️ Переписать (1/1)", callback_data=f"rewrite_{aid}"))
            bot.send_message(call.message.chat.id, "👇 Статья готова. Утверждаем или переписываем?", reply_markup=markup)
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка написания статьи: {e}")

    threading.Thread(target=_write_art).start()

# --- REWRITE LOGIC (FIXED) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("rewrite_"))
def rewrite_article(call):
    try: bot.answer_callback_query(call.id, "Переписываю...")
    except: pass
    aid = call.data.split("_")[1]
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT rewrite_count, project_id, title, seo_data FROM articles WHERE id=%s", (aid,))
    row = cur.fetchone()
    
    if not row:
        cur.close(); conn.close()
        return

    count, pid, title, seo_json = row
    if count >= 1:
        cur.close(); conn.close()
        bot.send_message(call.message.chat.id, "⚠️ Вы уже использовали попытку переписать статью.")
        return
    
    cur.execute("UPDATE articles SET rewrite_count = rewrite_count + 1 WHERE id=%s", (aid,))
    conn.commit()
    
    bot.send_message(call.message.chat.id, "⏳ Переписываю статью (это займет около минуты)...")
    
    def _do_rewrite():
        try:
            # Re-fetch data for context
            cur.execute("SELECT info, keywords, sitemap_links, style_prompt FROM projects WHERE id=%s", (pid,))
            proj = cur.fetchone()
            keywords_raw = proj[1] or ""
            style_prompt = proj[3] or ""
            
            # --- FIX: SAFE SITEMAP LOADING ---
            sitemap_data = proj[2]
            if isinstance(sitemap_data, list):
                sitemap_list = sitemap_data
            elif isinstance(sitemap_data, str):
                try: sitemap_list = json.loads(sitemap_data)
                except: sitemap_list = []
            else:
                sitemap_list = []
            # ---------------------------------
            
            links_text = "\n".join(sitemap_list[:30]) if sitemap_list else "No internal links found."
            
            current_year = datetime.datetime.now().year
            
            prompt = f"""
            TASK: REWRITE this article completely. Make it more engaging, human-like, and professional.
            Topic: "{title}"
            Length: 2000-2500 words.
            Style: Magazine Layout.
            Current Year: {current_year}.
            Style Prompt: {style_prompt}
            
            KEEP SEO OPTIMIZATION:
            Keywords: {keywords_raw}
            Yoast Rules: Focus keyword in title, headers, first paragraph.
            Internal Links: {links_text}
            
            OUTPUT JSON ONLY (Same format):
            {{
                "html_content": "Full HTML content...",
                "seo_title": "...",
                "meta_desc": "...",
                "focus_kw": "...",
                "featured_img_prompt": "..."
            }}
            """
            
            response_text = get_gemini_response(prompt)
            data = clean_and_parse_json(response_text)
            
            if data:
                article_html = data.get("html_content", "")
                seo_data = data
            else:
                article_html = response_text
                seo_data = {"seo_title": title, "featured_img_prompt": f"Photo of {title}"}
            
            cur.execute("UPDATE articles SET content=%s, seo_data=%s WHERE id=%s", 
                        (article_html, json.dumps(seo_data), aid))
            conn.commit(); cur.close(); conn.close()

            clean_view = format_html_for_chat(article_html)
            try:
                send_safe_message(call.message.chat.id, clean_view, parse_mode='HTML')
            except:
                send_safe_message(call.message.chat.id, clean_view, parse_mode=None)
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"pre_approve_{aid}"))
            bot.send_message(call.message.chat.id, "👇 Новая версия готова. Утверждаем?", reply_markup=markup)
            
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка рерайта: {e}")

    threading.Thread(target=_do_rewrite).start()

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
    markup.add(types.InlineKeyboardButton("🚀 Публикуем", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("🔙 В меню проекта", callback_data=f"open_proj_mgmt_{pid}"))
    
    bot.send_message(call.message.chat.id, "✅ Статья утверждена.\n\nПубликуем её на сайт с картинками или просто сохраняем в проекте?", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve_publish(call):
    try: bot.answer_callback_query(call.id, "Публикую...")
    except: pass
    
    aid = call.data.split("_")[1]
    bot.send_message(call.message.chat.id, "🚀 Начинаю генерацию картинок и публикацию... (Это может занять 2-3 минуты)")
    
    def _pub_process():
        conn = get_db_connection(); cur = conn.cursor()
        try:
            cur.execute("SELECT project_id, title, content, seo_data FROM articles WHERE id=%s", (aid,))
            row = cur.fetchone()
            pid, title, content, seo_json = row
            seo_data = seo_json if isinstance(seo_json, dict) else json.loads(seo_json or '{}')
            
            cur.execute("SELECT cms_url, cms_login, cms_password, style_prompt FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            
            if not res:
                bot.send_message(call.message.chat.id, "❌ Проект не найден.")
                cur.close(); conn.close(); return

            url, login, pwd, project_style = res
            
            debug_report = []
            focus_kw = seo_data.get('focus_kw', 'seo-article')
            
            img_matches = re.findall(r'\[IMG: (.*?)\]', content)
            final_content = content
            
            if img_matches:
                debug_report.append(f"🔎 Найдено {len(img_matches)} тегов [IMG].")
                
            for i, prompt in enumerate(img_matches):
                seo_filename = f"{focus_kw}-{i+1}"
                media_id, source_url, msg = generate_and_upload_image(url, login, pwd, prompt, f"{focus_kw} {i}", seo_filename, project_style)
                
                debug_report.append(f"🖼 Картинка {i+1}: {msg}")
                
                if source_url:
                    img_html = f'<figure class="wp-block-image"><img src="{source_url}" alt="{focus_kw}" title="{focus_kw}" class="wp-image-{media_id}"/></figure>'
                    final_content = final_content.replace(f'[IMG: {prompt}]', img_html, 1)
                else:
                    final_content = final_content.replace(f'[IMG: {prompt}]', '', 1)

            feat_media_id = None
            if seo_data.get('featured_img_prompt'):
                seo_filename_cover = f"{focus_kw}-main"
                feat_media_id, _, feat_msg = generate_and_upload_image(url, login, pwd, seo_data['featured_img_prompt'], focus_kw, seo_filename_cover, project_style)
                debug_report.append(f"🎨 Обложка: {feat_msg}")

            error_found = any("❌" in x or "⚠️" in x for x in debug_report)
            if error_found:
                report_text = "\n".join(debug_report)
                try: bot.send_message(call.message.chat.id, f"📋 **Отчет по медиа:**\n{report_text}", parse_mode='Markdown')
                except: pass

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
                    '_yoast_wpseo_title': seo_data.get('seo_title', title),
                    '_yoast_wpseo_metadesc': seo_data.get('meta_desc', ''),
                    '_yoast_wpseo_focuskw': focus_kw
                }
                post_data = {
                    'title': seo_data.get('seo_title', title),
                    'content': final_content.replace("\n", "<br>"),
                    'status': 'publish',
                    'meta': meta_payload
                }
                if feat_media_id: post_data['featured_media'] = feat_media_id

                api_url = f"{url}/wp-json/wp/v2/posts"
                r = requests.post(api_url, headers=headers, json=post_data, timeout=60)
                
                if r.status_code == 201:
                    link = r.json().get('link')
                    cur.execute("UPDATE articles SET status='published', published_url=%s WHERE id=%s", (link, aid))
                    
                    cur.execute("SELECT gens_left FROM users WHERE user_id=%s", (call.from_user.id,))
                    left = cur.fetchone()[0]
                    conn.commit(); cur.close(); conn.close()
                    
                    try: bot.delete_message(call.message.chat.id, call.message.message_id) 
                    except: pass
                    
                    success_gif = "https://ecosteni.ru/wp-content/uploads/2026/01/202601071222.gif"
                    
                    markup_final = types.InlineKeyboardMarkup()
                    markup_final.add(types.InlineKeyboardButton("🔙 В меню проекта", callback_data=f"open_proj_mgmt_{pid}"))

                    try:
                        bot.send_animation(call.message.chat.id, success_gif, caption=f"✅ Успешно! Ключ: {focus_kw}\n🔗 {link}\n\n⚡ Осталось генераций: {left}", reply_markup=markup_final)
                    except:
                        bot.send_message(call.message.chat.id, f"✅ Успешно! Ключ: {focus_kw}\n🔗 {link}\n\n⚡ Осталось генераций: {left}", reply_markup=markup_final)
                        
                else:
                    conn.close()
                    bot.send_message(call.message.chat.id, f"❌ Ошибка WP Публикации: {r.status_code} - {r.text[:100]}")
                    
            except Exception as e:
                if conn: conn.close()
                bot.send_message(call.message.chat.id, f"❌ Ошибка соединения: {e}")
        except Exception as e:
             if conn: conn.close()
             bot.send_message(call.message.chat.id, f"❌ Критическая ошибка в процессе публикации: {e}")

    threading.Thread(target=_pub_process).start()

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
    print("🤖 Бот запущен (Test Article & Lock Fixed)...")
    bot.infinity_polling(skip_pending=True)
