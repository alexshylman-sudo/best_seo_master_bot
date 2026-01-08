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

ADMIN_ID = 203473623  # Вставьте ваш ID
SUPPORT_ID = 203473623 
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("APP_URL")

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)
USER_CONTEXT = {} 
UPLOAD_STATE = {} 
LINK_UPLOAD_STATE = {} 
SURVEY_STATE = {} 
TEMP_PROMPTS = {} 
TEMP_LINKS = {} 
COMPETITOR_STATE = {} 

# --- GIF DICTIONARY ---
STEP_GIFS = {
    "scan": "https://media.giphy.com/media/l3vR85PnGgmwvPspG/giphy.gif",
    "survey": "https://media.giphy.com/media/3o7TKSjRrfIPjeiVyM/giphy.gif",
    "competitors": "https://media.giphy.com/media/l0HlOaQcLJ2hHpYzg/giphy.gif",
    "links": "https://media.giphy.com/media/3o7TKIeW38L3O3pXeo/giphy.gif",
    "gallery": "https://media.giphy.com/media/l41YtZOb9EUABfje8/giphy.gif",
    "img_prompts": "https://media.giphy.com/media/d31vTpVi1LAcDvgi/giphy.gif",
    "text_prompts": "https://media.giphy.com/media/l0HlPybHMx6D3iaHO/giphy.gif",
    "cms": "https://media.giphy.com/media/3oKIPnAiaMCws8nOsE/giphy.gif",
    "article": "https://media.giphy.com/media/l0HlHFRbY9C4FtA7i/giphy.gif",
    "strategy": "https://media.giphy.com/media/3o7TKvxnBdHP2IulJP/giphy.gif",
    "done": "https://media.giphy.com/media/l0MYt5jPR6QX5pnqM/giphy.gif"
}

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
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS approved_prompts JSONB DEFAULT '[]'")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS prompt_gens_count INT DEFAULT 0")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS approved_internal_links JSONB DEFAULT '[]'")
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS approved_external_links JSONB DEFAULT '[]'")
        conn.commit()
    except Exception as e: 
        print(f"⚠️ Schema Patch Error: {e}")
    finally:
        cur.close()
        conn.close()

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
            approved_internal_links JSONB DEFAULT '[]',
            approved_external_links JSONB DEFAULT '[]',
            prompt_gens_count INT DEFAULT 0,
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
    cur.execute("""
        INSERT INTO users (user_id, is_admin, tariff, gens_left) 
        VALUES (%s, TRUE, 'GOD_MODE', 9999) 
        ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE, tariff = 'GOD_MODE', gens_left = 9999
    """, (ADMIN_ID,))
    conn.commit()
    cur.close()
    conn.close()
    patch_db_schema()

def update_last_active(user_id):
    def _update():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE users SET last_active = NOW() WHERE user_id = %s", (user_id,))
            conn.commit()
            cur.close()
            conn.close()
        except:
            pass
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

def send_step_animation(chat_id, step_key, caption=None):
    gif_url = STEP_GIFS.get(step_key)
    if gif_url:
        try:
            bot.send_animation(chat_id, gif_url, caption=caption, parse_mode='Markdown')
        except:
            if caption: bot.send_message(chat_id, caption, parse_mode='Markdown')
    elif caption:
        bot.send_message(chat_id, caption, parse_mode='Markdown')

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
                clean_part = re.sub(r'<[^>]+>', '', part) 
                bot.send_message(chat_id, clean_part, parse_mode=None, reply_markup=current_markup)
            except Exception as e2: print(f"❌ Failed to send: {e2}")
        time.sleep(0.3) 

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"AI Error: {e}"

def check_site_availability(url):
    try:
        response = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        return response.status_code == 200
    except: return False

def update_project_progress(pid, step_key):
    """Обновляет JSON progress, добавляя новый ключ как выполненный."""
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    try:
        cur.execute("SELECT progress FROM projects WHERE id=%s", (pid,))
        res = cur.fetchone()
        prog = res[0] if res and res[0] else {}
        prog[step_key] = True
        cur.execute("UPDATE projects SET progress=%s WHERE id=%s", (json.dumps(prog), pid))
        conn.commit()
    except Exception as e: print(f"DB Progress Error: {e}")
    finally:
        cur.close()
        conn.close()

# --- SITEMAP & PARSING UTILS ---
def parse_sitemap(url):
    links = []
    try:
        sitemap_urls = [
            url.rstrip('/') + '/sitemap.xml',
            url.rstrip('/') + '/sitemap_index.xml',
            url.rstrip('/') + '/wp-sitemap.xml'
        ]
        target_sitemap = None
        for s_url in sitemap_urls:
            try:
                r = requests.get(s_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    target_sitemap = s_url
                    break
            except: continue
        if not target_sitemap:
            return parse_html_links(url)

        def fetch_sitemap_recursive(xml_url):
            found = []
            try:
                resp = requests.get(xml_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200: return []
                root = ET.fromstring(resp.content)
                for elem in root.iter():
                    if '}' in elem.tag: elem.tag = elem.tag.split('}', 1)[1]
                for sm in root.findall('sitemap'):
                    loc = sm.find('loc')
                    if loc is not None and loc.text:
                        found.extend(fetch_sitemap_recursive(loc.text))
                for u in root.findall('url'):
                    loc = u.find('loc')
                    if loc is not None and loc.text:
                        found.append(loc.text)
            except: pass
            return found

        links = fetch_sitemap_recursive(target_sitemap)
        if not links:
            links = parse_html_links(url)
        clean_links = [l for l in list(set(links)) if not any(x in l for x in ['.jpg', '.png', '.pdf', 'wp-admin', 'feed', '.xml', 'sitemap'])]
        return clean_links
    except: return []

def parse_html_links(url):
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, 'html.parser')
        domain = urlparse(url).netloc
        local_links = []
        for a in soup.find_all('a', href=True):
            full_url = urljoin(url, a['href'])
            if urlparse(full_url).netloc == domain:
                local_links.append(full_url)
        return local_links
    except: return []

def extract_contacts_from_soup(soup):
    contacts = []
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', soup.get_text())
    if emails: contacts.append(f"Email: {emails[0]}")
    phones = re.findall(r'(?:\+7|8)[\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}', soup.get_text())
    if phones: contacts.append(f"Phone: {phones[0]}")
    address_keywords = ["г.", "ул.", "город", "проспект", "шоссе", "Address", "Location"]
    text_blocks = soup.get_text().split('\n')
    for line in text_blocks:
        if any(x in line for x in address_keywords) and len(line) < 100 and len(line) > 10:
            contacts.append(f"Address: {line.strip()}")
            break
    return "\n".join(list(set(contacts)))

def deep_analyze_site(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
        resp = requests.get(url, timeout=15, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = soup.title.string if soup.title else "No Title"
        desc = soup.find("meta", attrs={"name": "description"})
        desc = desc["content"] if desc else "No Description"
        contact_info = extract_contacts_from_soup(soup)
        if "Phone" not in contact_info:
            for a in soup.find_all('a', href=True):
                href = a['href'].lower()
                if 'contact' in href or 'kontakt' in href or 'svyaz' in href:
                    try:
                        full_contact_url = urljoin(url, a['href'])
                        c_resp = requests.get(full_contact_url, timeout=10, headers=headers)
                        c_soup = BeautifulSoup(c_resp.text, 'html.parser')
                        more_contacts = extract_contacts_from_soup(c_soup)
                        contact_info += "\n" + more_contacts
                        break
                    except: pass
        raw_text = soup.get_text()[:4000].strip()
        result_text = f"URL: {url}\nTitle: {title}\nDesc: {desc}\nREAL_CONTACTS_DATA: {contact_info}\nContent: {raw_text}"
        return result_text, []
    except Exception as e: return f"Error: {e}", []

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
    text = str(html_content).strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    if text.startswith('{') and '"html_content":' in text:
        try:
            data = json.loads(text)
            text = data.get("html_content", text)
        except:
            match = re.search(r'"html_content":\s*"(.*?)(?<!\\)"', text, re.DOTALL)
            if match: text = match.group(1).encode('utf-8').decode('unicode_escape')
            else: text = text.replace('{', '').replace('}', '').replace('"html_content":', '')
    text = text.replace('\\n', '\n')
    text = re.sub(r'```html', '', text)
    text = re.sub(r'```', '', text)
    soup = BeautifulSoup(text, "html.parser")
    for script in soup(["script", "style", "head", "meta", "title", "link"]): script.decompose()
    for header in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        new_tag = soup.new_tag("b")
        new_tag.string = f"\n\n{header.get_text().strip()}\n"
        header.replace_with(new_tag)
    for li in soup.find_all('li'): li.string = f"• {li.get_text().strip()}\n"
    for ul in soup.find_all('ul'): ul.unwrap()
    for ol in soup.find_all('ol'): ol.unwrap()
    for p in soup.find_all('p'):
        p.append('\n\n')
        p.unwrap()
    for br in soup.find_all('br'): br.replace_with("\n")
    clean_text = str(soup)
    allowed_tags = r"b|strong|i|em|u|ins|s|strike|del|a|code|pre"
    clean_text = re.sub(r'<(?!\/?({}))[^>]*>'.format(allowed_tags), '', clean_text)
    import html
    clean_text = html.unescape(clean_text)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
    return clean_text.strip()

# --- 4. IMAGE GENERATION ---
def generate_image_bytes(image_prompt, project_style="", negative_prompt=""):
    target_model = 'imagen-4.0-generate-001'
    base_negative = "exclude text, writing, letters, watermarks, signature, words, logo"
    full_negative = f"{base_negative}, {negative_prompt}" if negative_prompt else base_negative
    if project_style and len(project_style) > 5:
        final_prompt = f"{project_style}. {image_prompt}. High resolution, 8k, cinematic lighting. Exclude: {full_negative}."
    else:
        final_prompt = f"Professional photography, {image_prompt}, realistic, high resolution, 8k, cinematic lighting. Exclude: {full_negative}."
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
        file_name = f"{slugify(seo_filename)}-{random.randint(10,99)}.png" if seo_filename else f"img-{slugify(alt_text[:20])}-{random.randint(100,999)}.png"
        headers = {'Authorization': 'Basic ' + creds, 'Content-Disposition': f'attachment; filename="{file_name}"', 'Content-Type': 'image/png', 'User-Agent': 'Mozilla/5.0'}
        r = requests.post(f"{api_url}/wp-json/wp/v2/media", headers=headers, data=image_bytes, timeout=60)
        if r.status_code == 201:
            res = r.json(); media_id = res.get('id'); source_url = res.get('source_url')
            requests.post(f"{api_url}/wp-json/wp/v2/media/{media_id}", headers={'Authorization': 'Basic ' + creds}, json={'alt_text': alt_text, 'title': alt_text, 'caption': alt_text})
            return media_id, source_url, f"✅ OK ({file_name})"
        elif r.status_code == 401: return None, None, "❌ WP 401: Неверный пароль."
        elif r.status_code == 403: return None, None, "❌ WP 403: Доступ запрещен."
        else: return None, None, f"❌ WP Error {r.status_code}"
    except Exception as e:
        print(f"WP Upload Error: {e}")
        return None, None, f"❌ WP Connection Error: {e}"

# --- HELPER: ARTICLE QUALITY VALIDATION ---
def validate_article_quality(content_html, project_sitemap_links):
    errors = []
    garbage_phrases = ["Here is the article", "Sure, here is", "json", "```", "[Insert link]", "lorem ipsum"]
    for phrase in garbage_phrases:
        if phrase.lower() in str(content_html).lower()[:100]: 
            errors.append(f"Мусорный текст в начале: '{phrase}'")
        if "```" in str(content_html):
             errors.append("Остались Markdown символы (```)")
    soup = BeautifulSoup(content_html, 'html.parser')
    if not soup.find(['h2', 'h3']):
        errors.append("Нет подзаголовков (H2/H3).")
    if len(soup.get_text()) < 500:
        errors.append("Статья слишком короткая (<500 символов).")
    links_found = soup.find_all('a', href=True)
    if links_found:
        valid_urls = set()
        for link in project_sitemap_links:
            clean = link.replace("http://", "").replace("https://", "").rstrip('/')
            valid_urls.add(clean)
        for link in links_found:
            href = link['href']
            clean_href = href.replace("http://", "").replace("https://", "").rstrip('/')
            is_external = "wikipedia" in href or "google" in href
            if not is_external and clean_href not in valid_urls and len(valid_urls) > 0:
                 if clean_href.count('/') > 1:
                     errors.append(f"Подозрительная ссылка: {href}")
    return errors

# --- 5. MENUS & BOT HANDLERS ---
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
        conn.commit()
        cur.close()
        conn.close()
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
    elif txt == "📂 Мои проекты": list_projects(uid, message.chat.id)
    elif txt == "👤 Профиль": show_profile(uid)
    elif txt == "💎 Тарифы": show_tariff_periods(uid)
    elif txt == "🆘 Техподдержка":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Написать", url=f"tg://user?id={SUPPORT_ID}"))
        bot.send_message(uid, "Напишите в поддержку:", reply_markup=markup)
    elif txt == "⚙️ Админка" and uid == ADMIN_ID: show_admin_panel(uid)
    elif txt == "🔙 В меню":
        # Clear states
        if uid in UPLOAD_STATE: del UPLOAD_STATE[uid]
        if uid in SURVEY_STATE: del SURVEY_STATE[uid]
        if uid in LINK_UPLOAD_STATE: del LINK_UPLOAD_STATE[uid]
        if uid in COMPETITOR_STATE: del COMPETITOR_STATE[uid]
        bot.send_message(uid, "Главное меню", reply_markup=main_menu_markup(uid))

@bot.callback_query_handler(func=lambda call: call.data == "soon")
def soon_alert(call): 
    try: bot.answer_callback_query(call.id, "🚧 В разработке...")
    except: pass

# --- 6. PROJECTS & WIZARD DISPATCHER ---
def list_projects(user_id, chat_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, url FROM projects WHERE user_id = %s ORDER BY id ASC", (user_id,))
    projs = cur.fetchall()
    cur.close()
    conn.close()
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_mgmt_"))
def open_proj_mgmt(call, mode="management", msg_id=None, new_site_url=None):
    """
    DISPATCHER: Checks project progress. If incomplete, forces Wizard flow.
    """
    try: bot.answer_callback_query(call.id)
    except: pass
    
    # Extract ID
    if isinstance(call, types.CallbackQuery):
        pid = call.data.split("_")[3]
        uid = call.from_user.id
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
    else:
        # Fallback if called directly (not callback)
        pid = call.data.split("_")[3] if hasattr(call, 'data') else None
        uid = call.from_user.id
        chat_id = call.chat.id
        
    USER_CONTEXT[uid] = pid
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT url, progress FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close()
    conn.close()
    
    if not res: 
        bot.send_message(chat_id, "❌ Проект не найден.")
        return

    url = res[0]
    progress = res[1] or {}

    # === WIZARD CHECKPOINTS ===
    # Step 2: Scan
    if not progress.get("step2_scan_done"):
        send_resume_wizard(chat_id, pid, 2, "Сканирование сайта", f"step2_retry_{pid}")
        return
    # Step 3: Survey
    if not progress.get("step3_survey_done"):
        send_resume_wizard(chat_id, pid, 3, "Опрос (Брифинг)", f"srv_{pid}")
        return
    # Step 4: Competitors
    if not progress.get("step4_competitors_done"):
        send_resume_wizard(chat_id, pid, 4, "Анализ конкурентов", f"step4_comp_start_{pid}")
        return
    # Step 5: Links
    if not progress.get("step5_links_done"):
        send_resume_wizard(chat_id, pid, 5, "Генерация ссылок", f"step5_links_{pid}")
        return
    # Step 6: Gallery
    if not progress.get("step6_gallery_done"):
        send_resume_wizard(chat_id, pid, 6, "Галерея (Референсы)", f"step6_gallery_{pid}")
        return
    # Step 7: Img Style
    if not progress.get("step7_imgprompts_done"):
        send_resume_wizard(chat_id, pid, 7, "Генератор стиля фото", f"step7_imgprompts_{pid}")
        return
    # Step 8: Text Style
    if not progress.get("step8_textprompts_done"):
        send_resume_wizard(chat_id, pid, 8, "Текстовые настройки", f"step8_textprompts_{pid}")
        return
    # Step 9: CMS
    if not progress.get("step9_cms_done"):
        send_resume_wizard(chat_id, pid, 9, "Подключение к сайту", f"step9_cms_{pid}")
        return
    # Step 10: Article
    if not progress.get("step10_article_done"):
        send_resume_wizard(chat_id, pid, 10, "Тестовая статья", f"step10_testart_{pid}")
        return
    # Step 11: Strategy
    if not progress.get("step11_strategy_done"):
        send_resume_wizard(chat_id, pid, 11, "Стратегия и План", f"step11_strategy_{pid}")
        return

    # === DASHBOARD (ALL DONE) ===
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ Настройки проекта", callback_data=f"proj_settings_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
    
    safe_url = url.replace("https://", "").replace("http://", "").rstrip('/')
    text = f"📂 **Проект:** {safe_url}\n✅ Настройка завершена.\n\nУправляйте проектом:"
    try:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')

def send_resume_wizard(chat_id, pid, step_num, step_name, callback):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"▶️ Продолжить: Шаг {step_num}", callback_data=callback))
    markup.add(types.InlineKeyboardButton("❌ Удалить проект", callback_data=f"ask_del_{pid}"))
    bot.send_message(
        chat_id, 
        f"🚧 **Настройка не завершена!**\n\nВы остановились на:\n👉 **Шаг {step_num}. {step_name}**\n\nНужно завершить настройку.", 
        reply_markup=markup,
        parse_mode='Markdown'
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_settings_"))
def project_settings_menu(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("⚡ Написать тестовую статью", callback_data=f"test_article_{pid}"))
    markup.add(types.InlineKeyboardButton("🧠 База Знаний (Стиль)", callback_data=f"kb_menu_{pid}"))
    markup.add(types.InlineKeyboardButton("🔗 Конкуренты", callback_data=f"step4_comp_start_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ CMS (Сайт)", callback_data=f"step9_cms_{pid}"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить проект", callback_data=f"ask_del_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"open_proj_mgmt_{pid}"))
    bot.edit_message_text("⚙️ **Настройки проекта**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

# --- WIZARD FLOW IMPLEMENTATION ---

# STEP 1: NEW SITE
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
            cur.close()
            conn.close()
            if exists:
                markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
                bot.send_message(message.chat.id, f"🚫 **Этот сайт уже добавлен!**", parse_mode='Markdown', reply_markup=markup)
                return

            # STEP 2: SCAN
            send_step_animation(message.chat.id, "scan", "⏳ **Шаг 2. Сканирую сайт...**")
            sitemap_links = parse_sitemap(url)

            # SAVE PROJECT
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO projects (user_id, type, url, info, sitemap_links, progress) VALUES (%s, 'site', %s, '{}', %s, '{}') RETURNING id", (message.from_user.id, url, json.dumps(sitemap_links)))
            pid = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            
            update_project_progress(pid, "step2_scan_done") # DONE
            USER_CONTEXT[message.from_user.id] = pid

            if not sitemap_links:
                bot.send_message(message.chat.id, "⚠️ Карта сайта не найдена. Анализирую главную страницу...")
            else:
                bot.send_message(message.chat.id, f"✅ Успешно! Найдено {len(sitemap_links)} страниц.")
            
            # --- NEW: STEP 2 SITE ANALYSIS ---
            scraped_data, _ = deep_analyze_site(url)
            prompt = f"Analyze this site: {url}. Content: {scraped_data[:3000]}. Give a short SEO summary in Russian."
            analysis = get_gemini_response(prompt)
            bot.send_message(message.chat.id, f"📊 **Экспресс-анализ сайта:**\n\n{analysis}")

            # START STEP 3
            send_step_animation(message.chat.id, "survey", "📝 **Шаг 3. Опрос (Брифинг)**")
            start_survey_logic(message.chat.id, message.from_user.id, pid)

        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
            traceback.print_exc()
    threading.Thread(target=_process_url).start()

# RETRY HANDLER FOR STEP 2 (Resume)
@bot.callback_query_handler(func=lambda call: call.data.startswith("step2_retry_"))
def step2_retry(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step2_scan_done")
    send_step_animation(call.message.chat.id, "survey", "📝 **Шаг 3. Опрос**")
    start_survey_logic(call.message.chat.id, call.from_user.id, pid)

# STEP 3: SURVEY
def start_survey_logic(chat_id, user_id, pid):
    SURVEY_STATE[user_id] = {'pid': pid, 'step': 1}
    msg = bot.send_message(chat_id, "📝 **Вопрос 1/5**\nКратко опишите суть вашего сайта/бизнеса. О чем он?", parse_mode='Markdown')
    bot.register_next_step_handler(msg, survey_step_router)

def survey_step_router(message):
    uid = message.from_user.id
    if uid not in SURVEY_STATE: return
    state = SURVEY_STATE[uid]
    step = state['step']
    pid = state['pid']
    text = message.text
    if text.startswith('/'): return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0] or {}
    info[f'survey_step{step}'] = text
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
    conn.commit()
    cur.close()
    conn.close()

    if step == 1:
        SURVEY_STATE[uid]['step'] = 2
        msg = bot.send_message(message.chat.id, "📝 **Вопрос 2/5: Ваша целевая аудитория?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 2:
        SURVEY_STATE[uid]['step'] = 3
        msg = bot.send_message(message.chat.id, "📝 **Вопрос 3/5: Регион продвижения?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 3:
        SURVEY_STATE[uid]['step'] = 4
        msg = bot.send_message(message.chat.id, "📝 **Вопрос 4/5: Ваши преимущества (УТП)?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 4:
        SURVEY_STATE[uid]['step'] = 5
        msg = bot.send_message(message.chat.id, "📝 **Вопрос 5/5: Тон коммуникации (Tone of Voice)?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 5:
        del SURVEY_STATE[uid]
        update_project_progress(pid, "step3_survey_done") # DONE
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🚀 Идем дальше", callback_data=f"step4_comp_start_{pid}"))
        bot.send_message(message.chat.id, "✅ **Опрос завершен!**", reply_markup=markup)
    
# SURVEY RETRY ENTRY POINT
@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def retry_survey(call):
    pid = call.data.split("_")[-1]
    start_survey_logic(call.message.chat.id, call.from_user.id, pid)

# STEP 4: COMPETITORS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step4_comp_start_"))
def step4_comp_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "competitors", "🕵️‍♂️ **Шаг 4. Анализ конкурентов**")
    COMPETITOR_STATE[call.from_user.id] = pid
    msg = bot.send_message(call.message.chat.id, "🔗 Пришлите **URL сайта конкурента**:")
    bot.register_next_step_handler(msg, step4_analyze_comp_logic)

def step4_analyze_comp_logic(message):
    uid = message.from_user.id
    if uid not in COMPETITOR_STATE: return
    pid = COMPETITOR_STATE[uid]
    
    url = message.text.strip()
    if not url.startswith("http"):
        msg = bot.send_message(message.chat.id, "❌ Нужна ссылка с http.")
        bot.register_next_step_handler(msg, step4_analyze_comp_logic)
        return

    bot.send_message(message.chat.id, "⏳ Анализирую...")
    
    def _analyze():
        try:
            scraped_data, _ = deep_analyze_site(url)
            # --- NEW: Force Russian ---
            prompt = f"Role: SEO Expert. Analyze: {url}.\nSnippet: {scraped_data[:2000]}\nTask: Write a VERY BRIEF (2 sentences) opinion on their SEO quality. OUTPUT LANGUAGE: RUSSIAN."
            opinion = get_gemini_response(prompt)
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
            info = cur.fetchone()[0] or {}
            clist = info.get("competitors_list", [])
            clist.append({"url": url, "opinion": opinion})
            info["competitors_list"] = clist
            cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
            conn.commit()
            cur.close()
            conn.close()
            
            send_safe_message(message.chat.id, f"🧐 **Мнение ИИ:**\n{opinion}")
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("➕ Добавить еще", callback_data=f"step4_add_more_{pid}"))
            markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 5)", callback_data=f"finish_step4_{pid}"))
            bot.send_message(message.chat.id, "Что делаем дальше?", reply_markup=markup)
            
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
            
    threading.Thread(target=_analyze).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("step4_add_more_"))
def step4_add_more(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    msg = bot.send_message(call.message.chat.id, "🔗 Следующая ссылка:")
    COMPETITOR_STATE[call.from_user.id] = pid
    bot.register_next_step_handler(msg, step4_analyze_comp_logic)

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step4_"))
def finish_step4_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step4_competitors_done") # DONE
    step5_links_start(call)

# STEP 5: LINKS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step5_links_"))
def step5_links_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    if call.from_user.id in COMPETITOR_STATE: del COMPETITOR_STATE[call.from_user.id]
    
    send_step_animation(call.message.chat.id, "links", "🔗 **Шаг 5. Генератор ссылок**")
    kb_gen_internal_logic(call.message.chat.id, pid)

def kb_gen_internal_logic(chat_id, pid):
    bot.send_message(chat_id, "⏳ Генерирую ссылки для перелинковки...")
    def _scan():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
        url = cur.fetchone()[0]
        cur.close()
        conn.close()
        
        links = parse_sitemap(url)
        clean_links = [l for l in links if not any(x in l for x in ['.jpg', '.png', 'wp-admin', 'feed', '.xml'])]
        # --- NEW: Show links properly ---
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE projects SET approved_internal_links=%s WHERE id=%s", (json.dumps(clean_links[:100]), pid))
        conn.commit()
        cur.close()
        conn.close()
        
        # Display Logic
        msg = f"✅ Найдено {len(clean_links)} ссылок."
        if len(clean_links) > 0:
            if len(clean_links) <= 20:
                msg += "\n\n" + "\n".join(clean_links)
                bot.send_message(chat_id, msg)
            else:
                msg += "\n\n(Первые 20):\n" + "\n".join(clean_links[:20])
                bot.send_message(chat_id, msg)
                # Create and send text file
                file_str = "\n".join(clean_links)
                file_io = io.BytesIO(file_str.encode('utf-8'))
                file_io.name = "all_links.txt"
                bot.send_document(chat_id, file_io, caption="📂 Полный список ссылок")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 6)", callback_data=f"finish_step5_{pid}"))
        bot.send_message(chat_id, "Ссылки собраны. Продолжим?", reply_markup=markup)

    threading.Thread(target=_scan).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step5_"))
def finish_step5_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step5_links_done") # DONE
    step6_gallery_start(call)

# STEP 6: GALLERY
@bot.callback_query_handler(func=lambda call: call.data.startswith("step6_gallery_"))
def step6_gallery_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "gallery", "🖼 **Шаг 6. Галерея (Референсы)**")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Добавить фото", callback_data=f"kb_add_photo_{pid}"))
    markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 7)", callback_data=f"finish_step6_{pid}"))
    bot.send_message(call.message.chat.id, "Загрузите фото-примеры стиля или пропустите.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step6_"))
def finish_step6_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step6_gallery_done") # DONE
    step7_imgprompts_start(call)

# STEP 7: IMG PROMPTS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step7_imgprompts_"))
def step7_imgprompts_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "img_prompts", "🎨 **Шаг 7. Генератор визуального стиля**")
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT style_images FROM projects WHERE id=%s", (pid,))
    images = cur.fetchone()[0] or []
    cur.close()
    conn.close()
    
    if len(images) > 0:
        bot.send_message(call.message.chat.id, "⏳ Анализирую фото из галереи...")
        step7_auto_gen(call.message.chat.id, pid)
    else:
        bot.send_message(call.message.chat.id, "⚠️ Фото не загружены. Используем стандартный стиль.")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 8)", callback_data=f"finish_step7_{pid}"))
        bot.send_message(call.message.chat.id, "Нажмите далее.", reply_markup=markup)

def step7_auto_gen(chat_id, pid):
    def _gen():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT style_images, info FROM projects WHERE id=%s", (pid,))
        res = cur.fetchone()
        images_b64, info = res[0] or [], res[1] or {}
        
        try:
            content_parts = []
            instruction = f"Role: AI Art Director. Context: {info.get('survey_step1', 'General')}. Analyze images. Create English Prompt describing the STYLE."
            content_parts.append(genai_types.Part.from_text(text=instruction))
            for b64_str in images_b64[:3]:
                try:
                    img_bytes = base64.b64decode(b64_str)
                    mime = "image/png" if img_bytes.startswith(b'\x89PNG') else "image/jpeg"
                    content_parts.append(genai_types.Part.from_bytes(data=img_bytes, mime_type=mime))
                except: pass
            
            response = client.models.generate_content(model="gemini-2.0-flash", contents=[genai_types.Content(parts=content_parts)])
            prompt_text = response.text.strip()
            
            cur.execute("UPDATE projects SET approved_prompts=%s WHERE id=%s", (json.dumps([prompt_text]), pid))
            conn.commit()
            bot.send_message(chat_id, f"✅ **Стиль создан:**\n`{prompt_text}`", parse_mode='Markdown')
        except Exception as e:
            bot.send_message(chat_id, f"Ошибка: {e}")
        finally:
            cur.close()
            conn.close()

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 8)", callback_data=f"finish_step7_{pid}"))
        bot.send_message(chat_id, "Стиль сохранен.", reply_markup=markup)
    threading.Thread(target=_gen).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step7_"))
def finish_step7_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step7_imgprompts_done") # DONE
    step8_textprompts_start(call)

# STEP 8: TEXT PROMPTS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step8_textprompts_"))
def step8_textprompts_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "text_prompts", "📝 **Шаг 8. Текстовые настройки**")
    bot.send_message(call.message.chat.id, "⏳ Подбираю Negative Prompt...")
    kb_auto_style_gen_logic(call.message.chat.id, pid)

def kb_auto_style_gen_logic(chat_id, pid):
    def _gen_style():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
        info = cur.fetchone()[0] or {}
        niche = info.get("survey_step1", "General Website")
        
        prompt = f"""
        Act as an AI Prompter. Topic: {niche}.
        Task: Create GENERAL style modifiers.
        Rules: Output ONLY two strings separated by '|||'.
        1. Positive prompt. 2. Negative prompt. English only.
        """
        resp = get_gemini_response(prompt)
        try:
            parts = resp.split('|||')
            if len(parts) == 2:
                pos = parts[0].strip()
                neg = parts[1].strip()
                cur.execute("UPDATE projects SET style_prompt=%s, style_negative_prompt=%s WHERE id=%s", (pos, neg, pid))
                conn.commit()
                msg = f"✅ **Настройки сохранены!**\nPos: {pos}\nNeg: {neg}"
            else:
                msg = "⚠️ Использованы стандартные настройки."
        except:
            msg = "⚠️ Ошибка генерации."
        finally:
            cur.close()
            conn.close()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 9)", callback_data=f"finish_step8_{pid}"))
        bot.send_message(chat_id, msg, reply_markup=markup)
    threading.Thread(target=_gen_style).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step8_"))
def finish_step8_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step8_textprompts_done") # DONE
    step9_cms_start(call)

# STEP 9: CMS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step9_cms_"))
def step9_cms_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "cms", "🔐 **Шаг 9. Подключение к сайту**")
    
    markup = types.InlineKeyboardMarkup(); 
    markup.add(types.InlineKeyboardButton("🚫 Пропустить (Я настрою позже)", callback_data=f"skip_cms_{pid}"))
    
    msg = bot.send_message(call.message.chat.id, "1. Введите **Логин** администратора WordPress:", reply_markup=markup, parse_mode='Markdown')
    bot.register_next_step_handler(msg, step9_cms_login, pid)

def step9_cms_login(message, pid):
    if message.text.startswith("/"): return
    login = message.text.strip()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_login=%s WHERE id=%s", (login, pid))
    conn.commit()
    cur.close()
    conn.close()
    msg = bot.send_message(message.chat.id, "🔑 2. Теперь введите **Пароль приложения** (Application Password):")
    bot.register_next_step_handler(msg, step9_cms_pass, pid)

def step9_cms_pass(message, pid):
    pwd = message.text.strip()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_password=%s WHERE id=%s", (pwd, pid))
    cur.execute("UPDATE projects SET cms_url=url WHERE id=%s AND cms_url IS NULL", (pid,))
    conn.commit()
    cur.close()
    conn.close()
    
    update_project_progress(pid, "step9_cms_done") # DONE
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 10)", callback_data=f"step10_testart_{pid}"))
    bot.send_message(message.chat.id, "✅ CMS данные сохранены!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("skip_cms_"))
def skip_cms_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step9_cms_done") # DONE (Skipped)
    step10_testart_start(call)

# STEP 10: ARTICLE
@bot.callback_query_handler(func=lambda call: call.data.startswith("step10_testart_"))
def step10_testart_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "article", "⚡ **Шаг 10. Тестовая статья**")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✍️ Написать статью", callback_data=f"test_article_{pid}")) 
    markup.add(types.InlineKeyboardButton("➡️ Пропустить (Шаг 11)", callback_data=f"finish_step10_{pid}"))
    bot.send_message(call.message.chat.id, "Напишем первую статью?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step10_"))
def finish_step10_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step10_article_done") # DONE
    step11_strategy_start(call)

# STEP 11: STRATEGY
@bot.callback_query_handler(func=lambda call: call.data.startswith("step11_strategy_"))
def step11_strategy_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "strategy", "🚀 **Шаг 11. Стратегия и Контент-план**")
    strategy_start_helper(call, pid)

def strategy_start_helper(call, pid):
    markup = types.InlineKeyboardMarkup(row_width=4)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(call.message.chat.id, "📅 **Контент-план**\nСколько статей в неделю вы хотите публиковать?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("freq_"))
def save_freq_and_plan(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    _, pid, freq = call.data.split("_")
    freq = int(freq)
    
    bot.edit_message_text(f"📅 Генерирую план...", call.message.chat.id, call.message.message_id)
    def _gen_plan():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info_json = res[0] or {}
            kw = res[1] or ""
            prompt = f"Role: SEO Expert. Create Content Plan for {freq} articles. Topic: {info_json.get('survey_step1','')}. Output JSON: [{{'day':'Mon','time':'10:00','topic':'...'}}]"
            ai_resp = get_gemini_response(prompt)
            calendar_plan = clean_and_parse_json(ai_resp)
            if not calendar_plan: calendar_plan = [{"day": "Monday", "time": "10:00", "topic": "Intro Article"}]
            info_json["temp_plan"] = calendar_plan
            cur.execute("UPDATE projects SET frequency=%s, info=%s WHERE id=%s", (freq, json.dumps(info_json), pid))
            conn.commit()
            cur.close()
            conn.close()
            
            msg_text = "🗓 **План:**\n\n"
            for item in calendar_plan: msg_text += f"**{item['day']}**: {item['topic']}\n"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Утвердить план", callback_data=f"approve_plan_{pid}"))
            bot.send_message(call.message.chat.id, msg_text, reply_markup=markup, parse_mode='Markdown')
        except Exception as e: bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")
    threading.Thread(target=_gen_plan).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_plan_"))
def approve_plan(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0]
    plan = info.get("temp_plan", [])
    cur.execute("UPDATE projects SET content_plan=%s WHERE id=%s", (json.dumps(plan), pid))
    conn.commit()
    cur.close()
    conn.close()
    
    update_project_progress(pid, "step11_strategy_done") # DONE - FINISH
    
    send_step_animation(call.message.chat.id, "done", "🎉 **Поздравляю! Настройка завершена!**")
    open_project_menu(call, mode="management") # Re-opens menu which now shows Dashboard

# --- SAFE DELETE HANDLER ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("ask_del_"))
def ask_del_confirmation(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑 Да, удалить из бота", callback_data=f"confirm_del_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Нет, оставить", callback_data=f"proj_settings_{pid}"))
    bot.edit_message_text("⚠️ **Удаление проекта**\n\nПроект удалится из бота. Статьи на сайте останутся.", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_del_"))
def delete_project_finally(call):
    try: bot.answer_callback_query(call.id, "Удаляю..."); 
    except: pass
    pid = call.data.split("_")[-1]
    conn = None
    try:
        conn = get_db_connection()
        if not conn: return
        cur = conn.cursor()
        cur.execute("DELETE FROM articles WHERE project_id=%s", (pid,))
        cur.execute("DELETE FROM projects WHERE id=%s", (pid,))
        conn.commit()
        cur.close()
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, "✅ **Проект удален.**")
        list_projects(call.from_user.id, call.message.chat.id)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")
    finally:
        if conn: conn.close()

# --- REUSED KB HANDLERS (SHORTENED FOR BREVITY, BUT FUNCTIONAL) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_add_photo_"))
def kb_add_photo(call):
    pid = call.data.split("_")[3]
    UPLOAD_STATE[call.from_user.id] = pid
    bot.send_message(call.message.chat.id, "🖼 Отправьте фото.")

@bot.message_handler(content_types=['photo'])
def handle_photo_upload(message):
    uid = message.from_user.id
    if uid not in UPLOAD_STATE: return 
    pid = UPLOAD_STATE[uid]
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        b64 = base64.b64encode(downloaded).decode('utf-8')
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT style_images FROM projects WHERE id=%s", (pid,))
        imgs = cur.fetchone()[0] or []
        imgs.append(b64)
        cur.execute("UPDATE projects SET style_images=%s WHERE id=%s", (json.dumps(imgs), pid))
        conn.commit()
        cur.close()
        conn.close()
        # --- NEW: Show reaction with Next Button ---
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 7)", callback_data=f"finish_step6_{pid}"))
        bot.reply_to(message, f"✅ Фото загружено ({len(imgs)}/30).", reply_markup=markup)
    except: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("test_article_"))
def test_article_start_wrapper(call):
    pid = call.data.split("_")[2]
    propose_test_topics(call.message.chat.id, pid)

def propose_test_topics(chat_id, pid):
    bot.send_message(chat_id, "⏳ Генерирую темы...")
    def _gen():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info = res[0] or {}
            kw = res[1] or ""
            
            # IMPROVED PROMPT
            prompt = (
                f"Role: SEO Expert. Task: Generate 5 viral blog article titles for this niche: "
                f"{info.get('survey_step1', 'General')}. "
                f"Keywords context: {str(kw)[:200]}. "
                f"Language: Russian. "
                f"Strict Output Format: JSON Array of Strings ONLY. Example: [\"Title 1\", \"Title 2\"]"
            )
            
            resp = get_gemini_response(prompt)
            topics = clean_and_parse_json(resp)
            
            # FALLBACK
            if not topics or not isinstance(topics, list):
                topics = ["Почему важно SEO?", "Как продвинуть сайт?", "Тренды 2025 года", "Секреты оптимизации", "Ошибки новичков"]
            
            # Store temp topics
            info["temp_topics"] = topics
            cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
            conn.commit()
            cur.close()
            conn.close()
            
            markup = types.InlineKeyboardMarkup(row_width=1)
            for i, t in enumerate(topics[:5]):
                # --- FIX 400 ERROR: STRING SANITIZATION ---
                if isinstance(t, dict): t = list(t.values())[0] # Handle dict case
                btn_text = str(t).replace('"', '').replace("'", "").strip()
                if not btn_text: btn_text = f"Вариант {i+1}"
                if len(btn_text) > 60: btn_text = btn_text[:57] + "..." # Truncate for button limit
                markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"write_{pid}_topic_{i}"))
                
            bot.send_message(chat_id, "Выберите тему:", reply_markup=markup)
        except Exception as e: bot.send_message(chat_id, f"Error: {e}")
    threading.Thread(target=_gen).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article_handler(call):
    try: bot.answer_callback_query(call.id, "Пишу..."); 
    except: pass
    parts = call.data.split("_")
    pid = parts[1]
    idx = int(parts[3])
    bot.delete_message(call.message.chat.id, call.message.message_id)
    send_step_animation(call.message.chat.id, "article", "⏳ Пишу статью...")
    
    def _write():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT info, approved_internal_links, approved_external_links FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info = res[0]
            
            topics = info.get("temp_topics", [])
            title = topics[idx]
            
            prompt = f"Write SEO article about '{title}'. Yoast SEO optimized. JSON format: {{'html_content':'...', 'seo_data': {{...}}}}"
            resp = get_gemini_response(prompt)
            data = clean_and_parse_json(resp)
            content = data.get('html_content', resp) if data else resp
            seo = data if data else {}
            
            cur.execute("INSERT INTO articles (project_id, title, content, seo_data, status) VALUES (%s, %s, %s, %s, 'draft') RETURNING id", (pid, title, content, json.dumps(seo)))
            aid = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            
            # Auto-mark step 10 as done if this is the wizard flow
            update_project_progress(pid, "step10_article_done")
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"pre_approve_{aid}"))
            send_safe_message(call.message.chat.id, format_html_for_chat(content), reply_markup=markup)
        except Exception as e: bot.send_message(call.message.chat.id, f"Error: {e}")
    threading.Thread(target=_write).start()

# --- START SERVER ---
app = Flask(__name__)
@app.route('/')
def h(): return "Alive", 200

def run_scheduler():
    while True:
        try:
            schedule.run_pending()
            if APP_URL: requests.get(APP_URL, timeout=10)
        except: pass
        time.sleep(600)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000, use_reloader=False), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    print("🤖 Бот запущен...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"Polling Error: {e}")
            time.sleep(5)
