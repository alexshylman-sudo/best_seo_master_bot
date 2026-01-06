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
        unique_links = list({v['url']: v for v in internal_links}.values())[:100]
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
    text = str(html_content).replace('\\n', '\n')
    if '", "seo_title":' in text: text = text.split('", "seo_title":')[0]
    if '","seo_title":' in text: text = text.split('","seo_title":')[0]
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
    image_bytes = None
    # 1. Google (Nano Banana / Imagen)
    try:
        response = client.models.generate_images(
            model='imagen-3.0-generate-001', 
            prompt=image_prompt,
            config=genai_types.GenerateImagesConfig(number_of_images=1)
        )
        if response.generated_images:
            image_bytes = response.generated_images[0].image.image_bytes
    except Exception:
        pass # Тихо падаем на фоллбек

    # 2. Flux Fallback (с задержкой во избежание лимитов)
    if not image_bytes:
        time.sleep(1.5) # Пауза чтобы не ловить Rate Limit
        try:
            seed = random.randint(1, 99999)
            safe_prompt = quote(image_prompt)
            image_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=768&seed={seed}&nologo=true"
            img_resp = requests.get(image_url, timeout=30)
            if img_resp.status_code == 200:
                image_bytes = img_resp.content
        except Exception:
            pass

    if not image_bytes: return None, None

    # 3. Upload WP
    try:
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
            requests.post(
                f"{upload_api}/{media_id}", 
                headers={'Authorization': 'Basic ' + token, 'Content-Type': 'application/json'}, 
                json={'alt_text': alt_text}, 
                timeout=10
            )
            return media_id, source_url
    except Exception as e:
        print(f"WP Upload Error: {e}")
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

# --- МЕНЮ ПРОЕКТА ---
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
                markup.add(types.InlineKeyboardButton("🔑 Создать ключи", callback_data=f"kw_ask_count_{pid}"))
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
    pid = call.data.split("_")[3]
    USER_CONTEXT[call.from_user.id] = pid
    open_project_menu(call.message.chat.id, pid, mode="management", msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_settings_"))
def project_settings_menu(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🔑 Ключи", callback_data=f"view_kw_{pid}"))
    markup.add(types.InlineKeyboardButton("📝 Опрос", callback_data=f"srv_{pid}"))
    markup.add(types.InlineKeyboardButton("🔗 Конкуренты", callback_data=f"comp_start_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ CMS", callback_data=f"cms_select_{pid}"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"ask_del_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"open_proj_mgmt_{pid}"))
    bot.edit_message_text("⚙️ **Настройки проекта**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

# --- ДОП. ФУНКЦИИ ---
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

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Главное меню", reply_markup=main_menu_markup(call.from_user.id))

# --- ЛОГИКА ---
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
    scraped_data, _ = deep_analyze_site(url)
    try:
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
        bot.delete_message(message.chat.id, msg.message_id)
        send_safe_message(message.chat.id, f"✅ **Анализ:**\n\n{ai_resp}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➕ Добавить еще", callback_data=f"comp_start_{pid}"))
        markup.add(types.InlineKeyboardButton("➡️ Готово, дальше", callback_data=f"comp_finish_{pid}"))
        bot.send_message(message.chat.id, "Добавить еще?", reply_markup=markup)
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("comp_finish_"))
def comp_finish(call):
    pid = call.data.split("_")[2]
    update_project_progress(pid, "competitors_done")
    open_project_menu(call.message.chat.id, pid, mode="onboarding", msg_id=call.message.message_id)

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
    prompt = f"Проведи {type_} SEO анализ сайта {url} на основе данных:\n{raw_data}\nЯзык: Русский. Дай конкретные рекомендации."
    advice = get_gemini_response(prompt)
    send_safe_message(call.message.chat.id, f"📊 **Отчет ({type_}):**\n\n{advice}")
    update_project_progress(pid, "analysis_done")
    open_project_menu(call.message.chat.id, pid, mode="onboarding")

@bot.callback_query_handler(func=lambda call: call.data.startswith("strat_"))
def strategy_start(call):
    pid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    
    # 1. Проверяем CMS
    cur.execute("SELECT cms_login, content_plan FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    if not res[0]: # cms_login is None
        bot.send_message(call.message.chat.id, "⚠️ Настройте CMS в настройках проекта!")
        return
    
    plan = res[1]
    # 2. Если план уже есть - показываем его
    if plan and len(plan) > 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Показать текущий план", callback_data=f"show_plan_{pid}"))
        markup.add(types.InlineKeyboardButton("🗑 Удалить и создать новый", callback_data=f"reset_plan_{pid}"))
        bot.send_message(call.message.chat.id, "📅 У вас уже утвержден план на эту неделю.", reply_markup=markup)
        return

    # 3. Если плана нет - выбираем частоту
    markup = types.InlineKeyboardMarkup(row_width=4)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(call.message.chat.id, "📅 Сколько статей в неделю?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("show_plan_"))
def show_current_plan(call):
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
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET content_plan='[]' WHERE id=%s", (pid,))
    conn.commit(); cur.close(); conn.close()
    strategy_start(call) # Возврат к выбору частоты

# --- НОВАЯ ЛОГИКА КАЛЕНДАРЯ (ОБНОВЛЕНО) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("freq_"))
def save_freq_and_plan(call):
    _, pid, freq = call.data.split("_")
    freq = int(freq)
    
    # 1. Расчет оставшихся дней недели
    days_map = {0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье"}
    today_idx = datetime.datetime.today().weekday()
    remaining_days = [days_map[i] for i in range(today_idx + 1, 7)] # Дни с завтрашнего
    
    # Если дней осталось меньше чем запрошено статей, планируем на оставшиеся
    actual_count = min(freq, len(remaining_days)) if remaining_days else 0
    
    if actual_count == 0:
        bot.send_message(call.message.chat.id, f"📅 Эта неделя заканчивается. План на {freq} статей будет создан в следующий понедельник.\nСейчас вы можете написать **Тестовую статью**.")
        return

    bot.edit_message_text(f"📅 Генерирую план на остаток недели ({actual_count} статей)...", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info_json = res[0] or {}
    survey = info_json.get("survey", "")
    kw = res[1] or ""
    
    # Промпт для генерации
    days_str = ", ".join(remaining_days[:actual_count])
    prompt = f"""
    Роль: SEO Маркетолог.
    Задача: Составь контент-план только на эти дни: {days_str}.
    Всего статей: {actual_count}.
    Ниша: {survey}. Ключи: {kw[:1000]}
    
    Верни ТОЛЬКО JSON массив объектов (без Markdown, без ```json):
    [
      {{"day": "Четверг", "time": "10:00", "topic": "Тема 1"}},
      {{"day": "Пятница", "time": "15:00", "topic": "Тема 2"}}
    ]
    """
    ai_resp = get_gemini_response(prompt)
    
    calendar_plan = []
    try:
        clean_json = ai_resp.replace("```json", "").replace("```", "").strip()
        calendar_plan = json.loads(clean_json)
    except:
        calendar_plan = [{"day": remaining_days[0], "time": "10:00", "topic": "Ошибка генерации"}]

    # Сохраняем план
    info_json["temp_plan"] = calendar_plan
    cur.execute("UPDATE projects SET frequency=%s, info=%s WHERE id=%s", (freq, json.dumps(info_json), pid))
    conn.commit(); cur.close(); conn.close()
    
    # Сообщение
    msg_text = "🗓 **План на остаток недели:**\n\n"
    for item in calendar_plan:
        msg_text += f"**{item['day']} {item['time']}**\n{item['topic']}\n\n"
    
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(types.InlineKeyboardButton("✅ Утвердить план", callback_data=f"approve_plan_{pid}"))
    
    # Кнопки замены (Пн, Вт...)
    short_days = {"Понедельник": "Пн", "Вторник": "Вт", "Среда": "Ср", "Четверг": "Чт", "Пятница": "Пт", "Суббота": "Сб", "Воскресенье": "Вс"}
    repl_btns = []
    for i, item in enumerate(calendar_plan):
        d_name = item.get('day', 'День')
        short = short_days.get(d_name, d_name[:2])
        repl_btns.append(types.InlineKeyboardButton(f"🔄 {short}", callback_data=f"repl_topic_{pid}_{i}"))
    markup.add(*repl_btns)
    
    bot.send_message(call.message.chat.id, msg_text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("repl_topic_"))
def replace_topic(call):
    _, _, pid, idx = call.data.split("_")
    idx = int(idx)
    bot.answer_callback_query(call.id, "🔄 Меняю тему...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info = res[0]
    keywords = res[1] or ""
    plan = info.get("temp_plan", [])
    
    if idx < len(plan):
        old_topic = plan[idx]['topic']
        prompt = f"""
        Придумай 1 новую тему статьи для блога, отличную от '{old_topic}'. 
        Контекст: {keywords[:500]}
        Верни только тему текстом.
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_plan_"))
def approve_plan(call):
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
    # Генерация 1 статьи сразу
    write_article_handler(call) 

# --- НАПИСАНИЕ СТАТЬИ (ИСПРАВЛЕНО) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article_handler(call):
    is_test = "test_article" in call.data
    pid = call.data.split("_")[2]
    idx = 0 
    if not is_test:
        idx = int(call.data.split("_")[3])
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info, keywords = res[0], res[1] or ""
    internal_links = info.get('internal_links', [])
    links_text = json.dumps(internal_links[:50], ensure_ascii=False)
    
    topic_text = "Тестовая SEO статья"
    if is_test:
        plan = info.get("content_plan", [])
        if plan: topic_text = plan[0]['topic']
        else: topic_text = f"Тренды: {keywords.split(',')[0] if keywords else 'Ремонт'}"
    else:
        # Для ручного выбора
        topics = info.get("temp_topics", [])
        if topics: topic_text = topics[idx]

    main_keyword = topic_text.split(':')[0]
    
    if is_test:
        # Используем HTML, так как Markdown падает на спецсимволах
        bot.send_message(call.message.chat.id, f"⚡ Пишу тестовую статью: <b>{topic_text}</b>...", parse_mode='HTML')
    else:
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, f"⏳ Пишу статью...", parse_mode='Markdown')
    
    prompt = f"""
    Role: Professional Magazine Editor & SEO Expert.
    Topic: "{topic_text}"
    Language: STRICTLY RUSSIAN (NO ENGLISH IN TEXT).
    Focus Keyword: "{main_keyword}"
    
    REQUIREMENTS:
    1. **Magazine Layout**: 
       - Use `<blockquote>` for key insights.
       - Use `<table>` where appropriate.
       - **IMAGES**: You MUST insert 5-7 image placeholders evenly distributed.
       - Format: `[IMG: specific detailed prompt for image generation in English]`
       - Use HTML tags like `<ul>`, `<ol>`, `<h2>`.
       - DO NOT use CSS styles like 'float: left'. Use simple paragraph structure.
    2. **SEO**: 
       - Insert 3 internal links from: {links_text}
       - Short paragraphs.
    
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
        seo_data = data
    except:
        article_html = response_text
        seo_data = {"seo_title": topic_text, "featured_img_prompt": f"Photo of {main_keyword}"}

    cur.execute("UPDATE users SET gens_left = gens_left - 1 WHERE user_id = (SELECT user_id FROM projects WHERE id=%s) AND is_admin = FALSE", (pid,))
    cur.execute("INSERT INTO articles (project_id, title, content, seo_data, status) VALUES (%s, %s, %s, %s, 'draft') RETURNING id", 
                (pid, topic_text, article_html, json.dumps(seo_data)))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    clean_view = format_html_for_chat(article_html)
    # Используем HTML для предпросмотра, чтобы избежать Markdown ошибок
    try:
        send_safe_message(call.message.chat.id, clean_view, parse_mode='HTML')
    except:
        send_safe_message(call.message.chat.id, clean_view, parse_mode=None)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("✏️ Переписать", callback_data=f"rewrite_{aid}"))
    bot.send_message(call.message.chat.id, "👇 Статья готова. Публикуем?", reply_markup=markup)

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
    
    img_matches = re.findall(r'\[IMG: (.*?)\]', content)
    final_content = content
    for i, prompt in enumerate(img_matches):
        media_id, source_url = generate_and_upload_image(url, login, pwd, prompt, f"{title} photo {i}")
        if source_url:
            # Используем стандартный WP класс без float для безопасности
            img_html = f'<figure class="wp-block-image"><img src="{source_url}" alt="{title}" class="wp-image-{media_id}"/></figure>'
            final_content = final_content.replace(f'[IMG: {prompt}]', img_html, 1)
        else:
            final_content = final_content.replace(f'[IMG: {prompt}]', '', 1)

    feat_media_id = None
    if seo_data.get('featured_img_prompt'):
        feat_media_id, _ = generate_and_upload_image(url, login, pwd, seo_data['featured_img_prompt'], seo_data.get('featured_img_alt', title))

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
            # Используем безопасное форматирование без Markdown
            bot.send_message(call.message.chat.id, f"✅ Успешно опубликовано!\n{link}\n\nВозврат в главное меню...")
            bot.send_message(call.message.chat.id, "Главное меню:", reply_markup=main_menu_markup(call.from_user.id))
        else:
            bot.send_message(call.message.chat.id, f"❌ Ошибка WP: {r.status_code}")
            
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")

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
