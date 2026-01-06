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
from urllib.parse import urlparse, urljoin
from telebot import TeleBot, types
from flask import Flask
from google import genai
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
        cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS seo_data JSONB DEFAULT '{}'") 
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
            progress JSONB DEFAULT '{"info_done": false, "analysis_done": false, "upload_done": false, "competitors_done": false}', 
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
    """
    Анализирует контент И собирает внутренние ссылки для перелинковки.
    Возвращает (текст_анализа, список_ссылок_json)
    """
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 Bot"})
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 1. Основной контент
        title = soup.title.string if soup.title else "No Title"
        desc = soup.find("meta", attrs={"name": "description"})
        desc = desc["content"] if desc else "No Description"
        headers = [h.get_text().strip() for h in soup.find_all(['h1', 'h2', 'h3'])]
        raw_text = soup.get_text()[:5000].strip()
        
        # 2. Сбор внутренних ссылок (Sitemap)
        internal_links = []
        domain = urlparse(url).netloc
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            # Приводим к полному URL
            full_url = urljoin(url, href)
            parsed_href = urlparse(full_url)
            
            # Проверяем, что это ссылка на тот же домен и не мусор
            if parsed_href.netloc == domain and not any(ext in parsed_href.path for ext in ['.jpg', '.png', '.pdf', '.css', '.js']):
                link_text = a_tag.get_text().strip()
                if link_text and len(link_text) > 3: # Игнорируем пустые ссылки или иконки
                    internal_links.append({"url": full_url, "anchor": link_text})
        
        # Ограничиваем кол-во ссылок, чтобы не забить базу
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
        msg = bot.send_message(message.chat.id, "❌ Нужен URL с http://. Попробуйте снова:")
        bot.register_next_step_handler(msg, check_url_step)
        return
    
    msg_check = bot.send_message(message.chat.id, "⏳ Проверяю доступность и уникальность...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM projects WHERE url = %s", (url,))
    existing = cur.fetchone()
    if existing:
        cur.close(); conn.close()
        bot.delete_message(message.chat.id, msg_check.message_id)
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
        bot.send_message(message.chat.id, f"⛔ Сайт {url} уже есть в системе.", reply_markup=markup)
        return

    if not check_site_availability(url):
        cur.close(); conn.close()
        bot.delete_message(message.chat.id, msg_check.message_id)
        msg = bot.send_message(message.chat.id, "❌ Сайт недоступен (не вернул статус 200). Проверьте ссылку:")
        bot.register_next_step_handler(msg, check_url_step)
        return
    
    cur.execute("INSERT INTO projects (user_id, type, url, info, progress) VALUES (%s, 'site', %s, '{}', '{}') RETURNING id", (message.from_user.id, url))
    pid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    USER_CONTEXT[message.from_user.id] = pid
    bot.delete_message(message.chat.id, msg_check.message_id)
    open_project_menu(message.chat.id, pid, mode="onboarding", new_site_url=url)

def open_project_menu(chat_id, pid, mode="management", msg_id=None, new_site_url=None):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, keywords, progress FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    if not res: return
    
    url, kw_db, progress = res
    if not progress: progress = {}
    has_keywords = kw_db is not None and len(kw_db) > 20

    markup = types.InlineKeyboardMarkup(row_width=1)
    
    if mode == "onboarding":
        if not progress.get("analysis_done"):
            markup.add(types.InlineKeyboardButton("📊 Анализ сайта (Глубокий)", callback_data=f"anz_{pid}"))
        elif not progress.get("info_done"):
            markup.add(types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"srv_{pid}"))
        elif not progress.get("competitors_done"):
            markup.add(types.InlineKeyboardButton("🔗 Добавить ссылки на конкурентов", callback_data=f"addcomp_{pid}"))
        elif not progress.get("upload_done"):
            markup.add(types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upf_{pid}"))
            markup.add(types.InlineKeyboardButton("➡️ Пропустить / Далее", callback_data=f"skip_upl_{pid}"))
        else:
            if not has_keywords:
                markup.add(types.InlineKeyboardButton("🔑 Создать ключевые слова", callback_data=f"kw_ask_count_{pid}"))
            else:
                markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
                markup.add(types.InlineKeyboardButton("⚙️ Настройки сайта (CMS)", callback_data=f"cms_select_{pid}"))
    else:
        if has_keywords:
            markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
        
        markup.add(types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"srv_{pid}"))
        markup.add(types.InlineKeyboardButton("🔗 Добавить ссылки на конкурентов", callback_data=f"addcomp_{pid}"))
        markup.add(types.InlineKeyboardButton("📊 Анализ сайта (Глубокий)", callback_data=f"anz_{pid}"))
        markup.add(types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upf_{pid}"))
    
        if has_keywords:
            markup.add(types.InlineKeyboardButton("❌ Удалить ключи", callback_data=f"delkw_{pid}"))
        elif progress.get("info_done"):
            markup.add(types.InlineKeyboardButton("🔑 Создать ключевые слова", callback_data=f"kw_ask_count_{pid}"))
        
        markup.add(types.InlineKeyboardButton("⚙️ Настройки сайта (CMS)", callback_data=f"cms_select_{pid}"))
        markup.add(types.InlineKeyboardButton("🗑 Удалить проект", callback_data=f"ask_del_{pid}"))

    if mode == "management" or has_keywords:
        markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))

    safe_url = url
    text = f"✅ Сайт {safe_url} успешно добавлен!" if new_site_url else f"📂 **Проект:** {safe_url}"
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("skip_upl_"))
def skip_upload_step(call):
    pid = call.data.split("_")[2]
    update_project_progress(pid, "upload_done")
    open_project_menu(call.message.chat.id, pid, mode="onboarding", msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ask_del_"))
def ask_delete_project(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ ДА, Удалить", callback_data=f"delete_proj_confirm_{pid}"))
    markup.add(types.InlineKeyboardButton("❌ НЕТ, Отмена", callback_data=f"open_proj_mgmt_{pid}"))
    
    bot.edit_message_text(
        "⚠️ **Вы точно хотите удалить проект?**\n\nВаши статьи, созданные ранее, **останутся** на вашем сайте. Бот просто забудет этот проект.", 
        call.message.chat.id, 
        call.message.message_id, 
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_proj_confirm_"))
def delete_project_confirm(call):
    pid = call.data.split("_")[3]
    cur = None; conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("DELETE FROM projects WHERE id = %s", (pid,))
        conn.commit()
        bot.answer_callback_query(call.id, "🗑 Проект удален.")
    except Exception as e:
        print(f"Delete Error: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка удаления")
    finally:
        if cur: cur.close()
        if conn: conn.close()
    
    list_projects(call.from_user.id, call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delkw_"))
def delete_keywords(call):
    pid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET keywords = NULL WHERE id = %s", (pid,))
    conn.commit(); cur.close(); conn.close()
    bot.answer_callback_query(call.id, "✅ Ключи удалены.")
    open_project_menu(call.message.chat.id, pid, mode="management", msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Главное меню", reply_markup=main_menu_markup(call.from_user.id))

# --- 6. КОНКУРЕНТЫ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("addcomp_"))
def add_competitors_start(call):
    pid = call.data.split("_")[1]
    USER_CONTEXT[call.from_user.id] = pid
    msg = bot.send_message(call.message.chat.id, "🔗 Пришлите ссылки на сайты конкурентов (можно несколько, через запятую или пробел):")
    bot.register_next_step_handler(msg, save_competitors, pid)

def save_competitors(message, pid):
    if message.text in ["➕ Новый проект", "📂 Мои проекты", "🔙 В меню"]: return
    
    links = message.text.strip()
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0] or {}
    info["competitors"] = links
    
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(pid, "competitors_done")
    bot.send_message(message.chat.id, "✅ Конкуренты сохранены!")
    open_project_menu(message.chat.id, pid, mode="onboarding")


# --- 7. ОПРОСНИК (5 вопросов) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def start_survey_6q(call):
    pid = call.data.split("_")[1]
    USER_CONTEXT[call.from_user.id] = pid
    
    # Очищаем
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET info = '{}', keywords = NULL WHERE id = %s", (pid,))
    conn.commit(); cur.close(); conn.close()
    
    msg = bot.send_message(call.message.chat.id, "❓ Вопрос 1/5:\nКакая главная цель вашего сайта? (Продажи, Трафик, Бренд?)")
    bot.register_next_step_handler(msg, q2, {"pid": pid, "answers": []}, "Цель")

def q2(m, d, prev_q): 
    valid, err = validate_input(m.text, prev_q)
    if not valid:
        bot.send_message(m.chat.id, f"⛔ Пожалуйста, ответьте текстом корректно.\n\n❓ {prev_q}"); bot.register_next_step_handler(m, q2, d, prev_q); return
    d["answers"].append(f"Цель: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 2/5:\nКто ваша целевая аудитория?")
    bot.register_next_step_handler(msg, q3, d, "ЦА")

def q3(m, d, prev_q): 
    valid, err = validate_input(m.text, prev_q)
    if not valid: bot.send_message(m.chat.id, f"⛔ Некорректный ввод.\n\n❓ {prev_q}"); bot.register_next_step_handler(m, q3, d, prev_q); return
    d["answers"].append(f"ЦА: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 3/5:\nВ чем ваше главное преимущество (УТП)?")
    bot.register_next_step_handler(msg, q4, d, "УТП")

def q4(m, d, prev_q):
    valid, err = validate_input(m.text, prev_q)
    if not valid: bot.send_message(m.chat.id, f"⛔ Некорректный ввод.\n\n❓ {prev_q}"); bot.register_next_step_handler(m, q4, d, prev_q); return
    d["answers"].append(f"УТП: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 4/5:\nГеография продвижения (Город, Страна):")
    bot.register_next_step_handler(msg, q5, d, "Гео")

def q5(m, d, prev_q):
    valid, err = validate_input(m.text, prev_q)
    if not valid: bot.send_message(m.chat.id, f"⛔ Некорректный ввод.\n\n❓ {prev_q}"); bot.register_next_step_handler(m, q5, d, prev_q); return
    d["answers"].append(f"Гео: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 5/5 (Важно!):\nСвободная форма. Что важно знать о бизнесе?")
    bot.register_next_step_handler(msg, finish_survey, d, "Инфо")

def finish_survey(m, d, prev_q):
    valid, err = validate_input(m.text, prev_q)
    if not valid: bot.send_message(m.chat.id, f"⛔ Некорректный ввод.\n\n❓ {prev_q}"); bot.register_next_step_handler(m, finish_survey, d, prev_q); return
    d["answers"].append(f"Доп. инфо: {m.text}")
    
    full_text = "\n".join(d["answers"])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (d["pid"],))
    old_info = cur.fetchone()[0] or {}
    old_info["survey"] = full_text
    
    cur.execute("UPDATE projects SET info = %s WHERE id=%s", (json.dumps(old_info, ensure_ascii=False), d["pid"]))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(d["pid"], "info_done")
    
    bot.send_message(m.chat.id, "✅ Опрос пройден!")
    open_project_menu(m.chat.id, d['pid'], mode="onboarding")

@bot.callback_query_handler(func=lambda call: call.data.startswith("anz_"))
def deep_analysis(call):
    pid = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "🕵️‍♂️ Сканирую сайт (Title, Desc, Content)...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
    url = cur.fetchone()[0]
    
    # NEW: Анализ с парсингом ссылок
    raw_data, links = deep_analyze_site(url)
    
    # Сохраняем ссылки для перелинковки
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0] or {}
    info['internal_links'] = links
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info, ensure_ascii=False), pid))
    
    prompt = f"""
    Проведи SEO-анализ данных сайта:
    {raw_data}
    Напиши отчет: 1. Юзабилити (UX) 2. Ошибки SEO 3. Советы по улучшению
    Формат: Кратко, по делу.
    """
    advice = get_gemini_response(prompt)
    
    cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
    kb = cur.fetchone()[0] or []
    kb.append(f"Deep Analysis: {advice[:1000]}")
    cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(pid, "analysis_done")
    bot.delete_message(call.message.chat.id, msg.message_id)
    send_safe_message(call.message.chat.id, f"📊 **Результат анализа:**\n\n{advice}")
    open_project_menu(call.message.chat.id, pid, mode="onboarding")

@bot.callback_query_handler(func=lambda call: call.data.startswith("upf_"))
def upload_files(call):
    pid = call.data.split("_")[1]
    USER_CONTEXT[call.from_user.id] = pid
    bot.send_message(call.message.chat.id, "📂 Пришлите текст, фото или .txt файл для анализа бизнеса или списка ключей.")

@bot.message_handler(content_types=['document', 'text', 'photo'])
def global_file_handler(message):
    if message.text and message.text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка", "🔙 В меню"]:
        menu_handler(message)
        return
    if message.text and message.text.startswith("/"):
        return

    uid = message.from_user.id
    pid = USER_CONTEXT.get(uid)
    
    if not pid:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT id, url FROM projects WHERE user_id = %s ORDER BY id DESC LIMIT 1", (uid,))
        res = cur.fetchone()
        cur.close(); conn.close()
        if res:
            pid = res[0]
            USER_CONTEXT[uid] = pid
            bot.reply_to(message, f"🔄 Контекст восстановлен. Работаем с проектом: {res[1]}")
        else:
            if message.content_type == 'document':
                bot.reply_to(message, "⚠️ Нет активных проектов. Создайте новый.")
            return

    content = ""
    is_txt = False
    
    if message.content_type == 'text': 
        content = message.text
    elif message.content_type == 'document':
        msg_loading = bot.send_message(message.chat.id, "⏳ Читаю и анализирую файл...", parse_mode='Markdown')
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            try: content = downloaded_file.decode('utf-8')
            except UnicodeDecodeError: content = downloaded_file.decode('cp1251') 
            filename = message.document.file_name or ""
            is_txt = filename.lower().endswith('.txt')
            bot.delete_message(message.chat.id, msg_loading.message_id)
        except Exception as e: 
            bot.delete_message(message.chat.id, msg_loading.message_id)
            bot.reply_to(message, f"⚠️ Ошибка чтения файла: {e}\nУбедитесь, что это текстовый файл (.txt).")
            return

    if not content: return

    conn = get_db_connection(); cur = conn.cursor()
    
    if is_txt or len(content) > 10:
        msg_ai = bot.send_message(message.chat.id, "🧠 AI анализирует контент...")
        try:
            check = get_gemini_response(f"Проанализируй текст: '{content[:500]}...'. Это похоже на список ключевых слов (SEO keys)? Ответь ТОЛЬКО 'ДА' или 'НЕТ'.")
            bot.delete_message(message.chat.id, msg_ai.message_id)
            
            if "ДА" in check.upper():
                cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (content, pid))
                msg_text = "✅ Файл распознан как Ключевые слова! Доступ к стратегии открыт."
                update_project_progress(pid, "upload_done")
            else:
                cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
                kb = cur.fetchone()[0] or []
                kb.append(f"File/Text Upload: {content[:2000]}...")
                cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
                msg_text = "✅ Информация сохранена в Базу Знаний проекта."
                update_project_progress(pid, "upload_done")
        except:
            bot.delete_message(message.chat.id, msg_ai.message_id)
            msg_text = "⚠️ Ошибка AI анализа."
    else: 
        msg_text = "⚠️ Слишком короткое сообщение для анализа."

    conn.commit(); cur.close(); conn.close()
    bot.reply_to(message, msg_text)
    open_project_menu(message.chat.id, pid, mode="onboarding")

# --- КЛЮЧИ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_ask_count_"))
def kw_ask_count(call):
    pid = call.data.split("_")[3]
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(types.InlineKeyboardButton("10", callback_data=f"genkw_{pid}_10"),
               types.InlineKeyboardButton("50", callback_data=f"genkw_{pid}_50"),
               types.InlineKeyboardButton("100", callback_data=f"genkw_{pid}_100"))
    markup.add(types.InlineKeyboardButton("200", callback_data=f"genkw_{pid}_200"),
               types.InlineKeyboardButton("300", callback_data=f"genkw_{pid}_300"),
               types.InlineKeyboardButton("500", callback_data=f"genkw_{pid}_500"))
    bot.edit_message_text("🔢 Выберите количество ключевых слов:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("genkw_"))
def generate_keywords_action(call):
    _, pid, count = call.data.split("_")
    bot.edit_message_text(f"🧠 AI составляет ядро из {count} запросов...", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT knowledge_base, url, info FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info_json = res[2] or {}
    survey = info_json.get("survey", "")
    competitors = info_json.get("competitors", "Не указаны")
    kb = str(res[0])[:3000] 
    
    # NEW: Промпт для кластеризации
    prompt = f"""
    Роль: SEO Эксперт.
    Задача: Составь Семантическое Ядро (СЯ) из {count} ключевых слов для сайта {res[1]}.
    
    Контекст: {survey}
    Конкуренты: {competitors}
    База знаний: {kb}
    
    СТРОГОЕ ТРЕБОВАНИЕ К ВЫВОДУ:
    Сгруппируй слова по КЛАСТЕРАМ.
    Укажи примерную частотность (ВЧ, СЧ, НЧ) и Интент (Коммерческий/Инфо).
    
    Формат:
    ## Кластер: [Название]
    * [Ключевое слово] (ВЧ/СЧ, Коммерческий)
    * [Ключевое слово] ...
    
    Без лишнего текста.
    """
    keywords = get_gemini_response(prompt)
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    send_safe_message(call.message.chat.id, keywords)
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_kw_{pid}"),
               types.InlineKeyboardButton("📥 Скачать (.txt)", callback_data=f"download_kw_{pid}"))
    markup.add(types.InlineKeyboardButton("🔄 Пройти опрос заново", callback_data=f"srv_{pid}"))
    markup.add(types.InlineKeyboardButton("🔄 Другое количество", callback_data=f"kw_ask_count_{pid}"))
    bot.send_message(call.message.chat.id, "👇 Действия:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_kw_"))
def approve_keywords(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🚀 ⭐️ СТРАТЕГИЯ И СТАТЬИ ⭐️", callback_data=f"strat_{pid}"))
    bot.send_message(call.message.chat.id, "✅ Ключи утверждены! Переходим к стратегии.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("download_kw_"))
def download_keywords(call):
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT keywords, url FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    if res and res[0]:
        file = io.BytesIO(res[0].encode('utf-8'))
        file.name = f"keywords_{pid}.txt"
        bot.send_document(call.message.chat.id, file, caption=f"Семантика для {res[1]}")

# --- CMS НАСТРОЙКИ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_select_"))
def cms_select_start(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("WordPress", callback_data=f"cms_setup_wp_{pid}"))
    markup.add(types.InlineKeyboardButton("Tilda (В разработке)", callback_data="soon"))
    markup.add(types.InlineKeyboardButton("Bitrix (В разработке)", callback_data="soon"))
    bot.send_message(call.message.chat.id, "Выберите вашу CMS:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_setup_wp_"))
def cms_setup_wp(call):
    pid = call.data.split("_")[3]
    msg = bot.send_message(call.message.chat.id, 
                           "1️⃣ Введите **URL админки**\nПример: `https://mysite.com` (без /wp-admin)", 
                           parse_mode='Markdown')
    bot.register_next_step_handler(msg, cms_save_url, pid)

def cms_save_url(message, pid):
    url = message.text.strip().rstrip("/")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_url=%s WHERE id=%s", (url, pid))
    conn.commit(); cur.close(); conn.close()
    msg = bot.send_message(message.chat.id, "2️⃣ Введите **Логин** администратора WP:")
    bot.register_next_step_handler(msg, cms_save_login, pid)

def cms_save_login(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_login=%s WHERE id=%s", (message.text.strip(), pid))
    conn.commit(); cur.close(); conn.close()
    msg = bot.send_message(message.chat.id, "3️⃣ Введите **Пароль приложения** (Application Password).")
    bot.register_next_step_handler(msg, cms_save_pass, pid)

def cms_save_pass(message, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_password=%s WHERE id=%s", (message.text.strip(), pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Настройки WordPress сохранены!")
    open_project_menu(message.chat.id, pid, "management")

# --- СТРАТЕГИЯ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("strat_"))
def strategy_start(call):
    pid = call.data.split("_")[1]
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT cms_login, frequency FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cms_ok = res[0]
    freq = res[1]
    
    if not cms_ok:
        cur.close(); conn.close()
        bot.send_message(call.message.chat.id, "⚠️ Сначала настройте CMS (Логин/Пароль)!")
        cms_select_start(call) 
        return
    
    if freq and freq > 0:
        cur.close(); conn.close()
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Да, изменить", callback_data=f"change_strat_{pid}"))
        markup.add(types.InlineKeyboardButton("❌ Нет, оставить", callback_data=f"keep_strat_{pid}"))
        bot.send_message(call.message.chat.id, f"📅 У вас уже выбрана стратегия: **{freq} публикации в неделю**.\nХотите изменить стратегию?", reply_markup=markup, parse_mode='Markdown')
        return

    cur.close(); conn.close()
    show_freq_selection(call.message.chat.id, pid)

def show_freq_selection(chat_id, pid):
    markup = types.InlineKeyboardMarkup(row_width=4)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(chat_id, "📅 Выберите частоту публикаций (статей в неделю):", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("change_strat_"))
def change_strategy_yes(call):
    pid = call.data.split("_")[2]
    show_freq_selection(call.message.chat.id, pid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("keep_strat_"))
def change_strategy_no(call):
    pid = call.data.split("_")[2]
    propose_articles(call.message.chat.id, pid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("freq_"))
def save_freq_and_gen_topics(call):
    _, pid, freq = call.data.split("_")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET frequency=%s WHERE id=%s", (freq, pid))
    conn.commit(); cur.close(); conn.close()
    propose_articles(call.message.chat.id, pid)

def propose_articles(chat_id, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id, info, keywords FROM projects WHERE id=%s", (pid,))
    proj = cur.fetchone()
    user_id = proj[0]
    
    cur.execute("SELECT gens_left, is_admin FROM users WHERE user_id=%s", (user_id,))
    u_data = cur.fetchone()
    if u_data[0] <= 0 and not u_data[1]:
        cur.close(); conn.close()
        bot.send_message(chat_id, "⚠️ **Лимит генераций исчерпан!** Пополните баланс.", parse_mode='Markdown')
        return

    bot.send_message(chat_id, f"⚡ Осталось генераций: {u_data[0]}. Генерирую 5 тем (ВЧ)...")
    
    info_json = proj[1] or {}
    survey = info_json.get("survey", "")
    competitors = info_json.get("competitors", "")
    kw = proj[2] or "Общие"
    
    prompt = f"""
    Роль: SEO Стратег. 
    Контекст: {survey}
    Ключи: {kw[:1000]}
    
    Задача: Придумай 5 вирусных SEO тем для блога, используя Высокочастотные (ВЧ) ключи.
    ФОРМАТ ВЫВОДА (Строго):
    1. **Заголовок**
    Краткое описание...
    |
    2. **Заголовок**
    ...
    """
    
    try:
        raw_text = get_gemini_response(prompt)
        topics_raw = raw_text.split("|")
        topics = []
        for t in topics_raw:
            clean = t.replace("*", "").strip()
            lines = clean.split("\n")
            header = lines[0]
            if header and header[0].isdigit(): 
                header = header.split(".", 1)[-1].strip()
            if len(header) > 3: topics.append(header)
        topics = topics[:5]
    except: 
        topics = ["Ошибка генерации тем"]

    info_json["temp_topics"] = topics
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info_json), pid))
    conn.commit(); cur.close(); conn.close()

    markup = types.InlineKeyboardMarkup(row_width=1)
    msg_text = "📝 **Выберите тему для статьи:**\n\n"
    for i, t in enumerate(topics):
        msg_text += f"{i+1}. **{t}**\n"
        markup.add(types.InlineKeyboardButton(f"Вариант {i+1}", callback_data=f"write_{pid}_topic_{i}"))
        
    bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article(call):
    parts = call.data.split("_")
    pid, idx = parts[1], int(parts[3])
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info, keywords = res[0], res[1] or ""
    
    # NEW: Достаем внутренние ссылки
    internal_links = info.get('internal_links', [])
    links_text = json.dumps(internal_links[:50], ensure_ascii=False) # Берем топ 50
    
    topics = info.get("temp_topics", [])
    selected_topic = topics[idx] if len(topics) > idx else "SEO Article"
    
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, f"⏳ Пишу статью (~2500 слов) с учетом Yoast SEO...", parse_mode='Markdown')
    
    # NEW: Мощный промпт с перелинковкой и Yoast правилами
    prompt = f"""
    Роль: SEO-копирайтер уровня Pro.
    Тема: "{selected_topic}".
    Ключи (Приоритет ВЧ): {keywords[:1000]}...
    
    ВНУТРЕННИЕ ССЫЛКИ САЙТА (Для перелинковки):
    {links_text}
    
    ИНСТРУКЦИЯ (YOAST SEO GREEN LIGHT):
    1. Напиши статью на 2000+ слов. Используй только HTML теги (h2, h3, p, ul, li).
    2. Ключевая фраза должна быть в первом абзаце и в одном из H2.
    3. Предложения короткие (макс 20 слов). Пассивный залог < 10%.
    4. ОБЯЗАТЕЛЬНО: Вставь 3-5 ссылок из списка "Внутренние ссылки" в текст контекстно. (Тег <a href="...">анкор</a>).
    5. Структура: Введение, 4-6 разделов H2 (внутри H3), Заключение.
    
    ФОРМАТ ОТВЕТА (JSON):
    {{
        "html_content": "Полный HTML код статьи...",
        "seo_title": "Заголовок для сниппета (ключ в начале)",
        "meta_desc": "Мета-описание (с призывом к действию)",
        "focus_kw": "Главное ключевое слово"
    }}
    """
    response_text = get_gemini_response(prompt)
    
    try:
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        article_html = data.get("html_content", "")
        seo_data = {
            "seo_title": data.get("seo_title", ""),
            "meta_desc": data.get("meta_desc", ""),
            "focus_kw": data.get("focus_kw", "")
        }
    except:
        article_html = response_text
        seo_data = {"seo_title": selected_topic, "meta_desc": "", "focus_kw": ""}

    cur.execute("UPDATE users SET gens_left = gens_left - 1 WHERE user_id = (SELECT user_id FROM projects WHERE id=%s) AND is_admin = FALSE", (pid,))
    cur.execute("INSERT INTO articles (project_id, title, content, seo_data, status) VALUES (%s, %s, %s, %s, 'draft') RETURNING id", 
                (pid, selected_topic, article_html, json.dumps(seo_data)))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    send_safe_message(call.message.chat.id, article_html, parse_mode='HTML')
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("✏️ Переписать (1 раз)", callback_data=f"rewrite_{aid}"))
    bot.send_message(call.message.chat.id, "👇 Статья готова. Ваши действия?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("rewrite_"))
def rewrite_once(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT rewrite_count, title FROM articles WHERE id=%s", (aid,))
    res = cur.fetchone()
    
    if res[0] > 0:
        bot.answer_callback_query(call.id, "⛔ Лимит переписываний (1 раз) исчерпан!")
        cur.close(); conn.close(); return
        
    bot.edit_message_text("🔄 Переписываю...", call.message.chat.id, call.message.message_id)
    prompt = f"Перепиши статью '{res[1]}' в другом стиле, сохраняя HTML теги. Верни только HTML контент."
    text = get_gemini_response(prompt)
    
    cur.execute("UPDATE articles SET content=%s, rewrite_count=1 WHERE id=%s", (text, aid))
    conn.commit(); cur.close(); conn.close()
    
    send_safe_message(call.message.chat.id, text, parse_mode='HTML')
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{aid}"))
    bot.send_message(call.message.chat.id, "👇 Обновленная версия.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve_publish(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT project_id, title, content, seo_data FROM articles WHERE id=%s", (aid,))
    row = cur.fetchone()
    pid, title, content, seo_json = row
    seo_data = seo_json if seo_json else {}
    
    cur.execute("SELECT cms_url, cms_login, cms_password FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    url, login, pwd = res[0], res[1], res[2]
    formatted_content = content.replace("\n", "<br>")
    
    if url.endswith('/'): url = url[:-1]
    api_url = f"{url}/wp-json/wp/v2/posts"
    
    msg = bot.send_message(call.message.chat.id, "🚀 Публикую на сайт...")
    
    try:
        creds = f"{login}:{pwd}"
        token = base64.b64encode(creds.encode()).decode()
        
        headers = {
            'Authorization': 'Basic ' + token,
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Cookie': 'beget=begetok'
        }
        
        meta_payload = {
            '_yoast_wpseo_title': seo_data.get('seo_title', title),
            '_yoast_wpseo_metadesc': seo_data.get('meta_desc', ''),
            '_yoast_wpseo_focuskw': seo_data.get('focus_kw', '')
        }

        post_data = {
            'title': title,
            'content': formatted_content,
            'status': 'publish',
            'meta': meta_payload 
        }
        
        r = requests.post(api_url, headers=headers, json=post_data, timeout=20)
        
        if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
             bot.delete_message(call.message.chat.id, msg.message_id)
             bot.send_message(call.message.chat.id, "❌ Хостинг продолжает блокировать бота.")
             return

        if r.status_code == 201:
            link = r.json().get('link')
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("UPDATE articles SET status='published', published_url=%s WHERE id=%s", (link, aid))
            conn.commit(); cur.close(); conn.close()
            
            bot.delete_message(call.message.chat.id, msg.message_id)
            succ_msg = f"✅ **Успешно опубликовано!**\n🔗 {link}\n\n"
            succ_msg += f"🔑 Фокус: {seo_data.get('focus_kw')}\n"
            succ_msg += f"🏷 SEO Title: {seo_data.get('seo_title')}"
            bot.send_message(call.message.chat.id, succ_msg, parse_mode='Markdown')
        else:
            bot.delete_message(call.message.chat.id, msg.message_id)
            try:
                err_json = r.json()
                err_msg = err_json.get('message', r.text[:200])
                err_code = err_json.get('code', r.status_code)
            except:
                err_msg = r.text[:200]
                err_code = r.status_code

            err_text = f"❌ Ошибка WP ({err_code}): {err_msg}"
            if r.status_code == 401: err_text += "\n\nПроверьте Логин и Пароль приложения!"
            bot.send_message(call.message.chat.id, err_text)
            
    except Exception as e:
        bot.delete_message(call.message.chat.id, msg.message_id)
        bot.send_message(call.message.chat.id, f"❌ Ошибка соединения: {e}")

# --- 7. ТАРИФЫ ---
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
    tariff_code = parts[1] # start, pro, agent
    period = parts[2] # 1m, 1y
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

# --- 8. ПРОФИЛЬ ---
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

# --- 9. ЗАПУСК ---
def keep_alive():
    while True:
        time.sleep(14 * 60)
        if APP_URL:
            try: requests.get(APP_URL); print("Ping sent")
            except: pass

def run_scheduler():
    schedule.every().day.at("10:00").do(lambda: None) 
    threading.Thread(target=keep_alive, daemon=True).start()
    while True: schedule.run_pending(); time.sleep(60)

app = Flask(__name__)
@app.route('/')
def h(): return "AI SEO Master Alive", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    print("🤖 Бот запущен...")
    bot.infinity_polling(skip_pending=True)
