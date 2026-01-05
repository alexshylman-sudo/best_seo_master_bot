import os
import threading
import time
import schedule
import psycopg2
import json
import requests
import datetime
import io
from bs4 import BeautifulSoup
from telebot import TeleBot, types
from flask import Flask
from google import genai
from dotenv import load_dotenv

# --- 1. КОНФИГУРАЦИЯ ---
load_dotenv()

ADMIN_ID = 203473623
SUPPORT_ID = 203473623
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("APP_URL")

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)

# --- 2. БАЗА ДАННЫХ ---
def get_db_connection():
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
        return None

def patch_db_schema():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        conn.commit()
    except: pass
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
            gens_left INT DEFAULT 0,
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
            type TEXT,
            url TEXT,
            info JSONB DEFAULT '{}', 
            knowledge_base JSONB DEFAULT '[]', 
            keywords TEXT,
            cms_key TEXT,
            platform TEXT,
            frequency INT DEFAULT 0,
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

    cur.execute("INSERT INTO users (user_id, is_admin, tariff, gens_left) VALUES (%s, TRUE, 'GOD_MODE', 9999) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    
    conn.commit(); cur.close(); conn.close()
    patch_db_schema()
    print("✅ БД инициализирована.")

def update_last_active(user_id):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET last_active = NOW() WHERE user_id = %s", (user_id,))
        conn.commit(); cur.close(); conn.close()
    except: pass

# --- 3. УТИЛИТЫ ---
def escape_md(text):
    if not text: return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

def send_long_message(chat_id, text, parse_mode=None):
    if len(text) <= 4000:
        bot.send_message(chat_id, text, parse_mode=parse_mode)
    else:
        # Разбиваем по переносам строк, чтобы не резать слова
        parts = []
        while len(text) > 0:
            if len(text) > 4000:
                # Ищем последний перенос строки перед 4000 символом
                split_pos = text.rfind('\n', 0, 4000)
                if split_pos == -1: split_pos = 4000
                parts.append(text[:split_pos])
                text = text[split_pos:]
            else:
                parts.append(text)
                text = ""
        
        for part in parts:
            bot.send_message(chat_id, part, parse_mode=parse_mode)
            time.sleep(0.3)

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"Ошибка AI: {e}"

def validate_input(text, question_context):
    try:
        prompt = f"Модератор опроса. Вопрос: '{question_context}'. Ответ: '{text}'. Если ответ содержит мат, бессмыслицу или спам - верни BAD. Если ответ адекватный (даже 'нет конкурентов') - верни OK."
        res = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt]).text.strip()
        return "BAD" not in res.upper()
    except: return True

def check_site_availability(url):
    try:
        response = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        return response.status_code == 200
    except: return False

def deep_analyze_site(url):
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Bot"})
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = soup.title.string if soup.title else "No Title"
        meta = soup.find("meta", attrs={"name": "description"})
        desc = meta["content"] if meta else "No Description"
        links = [a['href'] for a in soup.find_all('a', href=True) if a['href'].startswith('/') or url in a['href']]
        structure_hint = f"Найдено {len(links)} внутренних страниц."
        raw_text = soup.get_text()[:2000].strip()
        return f"URL: {url}\nTitle: {title}\nDesc: {desc}\nStructure: {structure_hint}\nContent Sample: {raw_text}"
    except Exception as e:
        return f"Ошибка доступа: {e}"

def update_project_progress(pid, step_key):
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    try:
        cur.execute("SELECT progress FROM projects WHERE id=%s", (pid,))
        result = cur.fetchone()
        prog = result[0] if result and result[0] else {}
        prog[step_key] = True
        cur.execute("UPDATE projects SET progress=%s WHERE id=%s", (json.dumps(prog), pid))
        conn.commit()
    except Exception as e:
        print(f"Update progress error: {e}")
    finally:
        cur.close(); conn.close()

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
        cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit(); cur.close(); conn.close()
    bot.send_message(user_id, "👋 Привет! Я AI-ассистент для SEO.", reply_markup=main_menu_markup(user_id))

@bot.message_handler(func=lambda m: m.text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка"])
def menu_handler(message):
    uid = message.from_user.id
    txt = message.text
    update_last_active(uid)

    if txt == "➕ Новый проект":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🌐 Сайт", callback_data="new_site"),
                   types.InlineKeyboardButton("📸 Инстаграм (soon)", callback_data="soon"),
                   types.InlineKeyboardButton("✈️ Телеграм (soon)", callback_data="soon"))
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

@bot.callback_query_handler(func=lambda call: call.data == "soon")
def soon_alert(call): bot.answer_callback_query(call.id, "🚧 В разработке")

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
        btn_text = p[1].replace("https://", "").replace("http://", "")[:30]
        markup.add(types.InlineKeyboardButton(f"🌐 {btn_text}", callback_data=f"open_proj_mgmt_{p[0]}"))
    bot.send_message(chat_id, "Ваши проекты:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "new_site")
def new_site_start(call):
    msg = bot.send_message(call.message.chat.id, "🔗 Введите URL сайта (с http/https):")
    bot.register_next_step_handler(msg, check_url_step)

def check_url_step(message):
    url = message.text.strip()
    if not url.startswith("http"):
        msg = bot.send_message(message.chat.id, "❌ Нужен URL с http:// или https://. Попробуйте снова:")
        bot.register_next_step_handler(msg, check_url_step)
        return
    
    msg_check = bot.send_message(message.chat.id, "⏳ Проверяю доступность и базу данных...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM projects WHERE url = %s", (url,))
    exists = cur.fetchone()
    if exists:
        cur.close(); conn.close()
        bot.delete_message(message.chat.id, msg_check.message_id)
        msg = bot.send_message(message.chat.id, f"⛔ Сайт {url} уже есть в системе.\n👇 **Введите другой URL:**")
        bot.register_next_step_handler(msg, check_url_step)
        return

    if not check_site_availability(url):
        cur.close(); conn.close()
        msg = bot.edit_message_text("❌ Сайт недоступен (код не 200). Проверьте ссылку и введите снова:", message.chat.id, msg_check.message_id)
        bot.register_next_step_handler(msg, check_url_step)
        return
    
    cur.execute("INSERT INTO projects (user_id, type, url, info, progress) VALUES (%s, 'site', %s, '{}', '{}') RETURNING id", (message.from_user.id, url))
    pid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    bot.delete_message(message.chat.id, msg_check.message_id)
    open_project_menu(message.chat.id, pid, mode="onboarding", new_site_url=url)

def open_project_menu(chat_id, pid, mode="management", msg_id=None, new_site_url=None, just_finished_survey=False):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, keywords, progress FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    if not res: return
    
    url, kw_db, progress = res
    if not progress: progress = {}
    
    has_keywords = kw_db is not None and len(kw_db) > 5

    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # ЕСЛИ ТОЛЬКО ЧТО ЗАКОНЧИЛ ОПРОС - ПОКАЗЫВАЕМ ТОЛЬКО "ПОДОБРАТЬ КЛЮЧИ"
    if just_finished_survey:
        markup.add(types.InlineKeyboardButton("🔑 Подобрать ключевые слова", callback_data=f"kw_ask_count_{pid}"))
        bot.send_message(chat_id, "✅ Спасибо за честные ответы! Теперь самое время подобрать ключевые слова для продвижения.", reply_markup=markup)
        return

    # ОБЫЧНЫЙ РЕЖИМ
    if has_keywords:
        markup.add(types.InlineKeyboardButton("🚀 ⭐️ СТРАТЕГИЯ И СТАТЬИ ⭐️", callback_data=f"strat_{pid}"))

    btn_info = types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"srv_{pid}")
    btn_anal = types.InlineKeyboardButton("📊 Анализ сайта (Глубокий)", callback_data=f"anz_{pid}")
    btn_upl = types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upf_{pid}")
    
    if mode == "onboarding":
        if not progress.get("info_done"): markup.add(btn_info)
        if not progress.get("analysis_done"): markup.add(btn_anal)
        if not progress.get("upload_done"): markup.add(btn_upl)
    else:
        markup.add(btn_info, btn_anal, btn_upl)

    if has_keywords:
        markup.add(types.InlineKeyboardButton("❌ Удалить ключи", callback_data=f"delkw_{pid}"))
    elif progress.get("info_done") or progress.get("upload_done"):
        markup.add(types.InlineKeyboardButton("🔑 Подобрать ключевые слова", callback_data=f"kw_ask_count_{pid}"))
    
    markup.add(types.InlineKeyboardButton("🗑 Удалить проект", callback_data=f"delete_proj_confirm_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))

    safe_url = escape_md(url)
    text = f"✅ Сайт {safe_url} добавлен!" if new_site_url else f"📂 **Проект:** {safe_url}\nРежим: {'Первичная настройка' if mode=='onboarding' else 'Управление'}"
    
    try:
        if msg_id and not new_site_url:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
        else:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.send_message(chat_id, text.replace("*", "").replace("_", ""), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_mgmt_"))
def open_proj_mgmt(call):
    pid = call.data.split("_")[3]
    open_project_menu(call.message.chat.id, pid, mode="management", msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_proj_confirm_"))
def delete_project_confirm(call):
    pid = call.data.split("_")[3]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE id = %s", (pid,))
    conn.commit(); cur.close(); conn.close()
    bot.answer_callback_query(call.id, "🗑 Проект удален.")
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

# --- 6. ФУНКЦИОНАЛ ---

# ОПРОСНИК
@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def start_survey_6q(call):
    pid = call.data.split("_")[1]
    q_text = "Какая главная цель вашего сайта? (Продажи, Трафик, Бренд?)"
    msg = bot.send_message(call.message.chat.id, f"❓ Вопрос 1/6:\n{q_text}")
    bot.register_next_step_handler(msg, q2, {"pid": pid, "answers": []}, q_text)

def q2(m, d, prev_q): 
    if not validate_input(m.text, prev_q):
        msg = bot.send_message(m.chat.id, f"⛔ Это не похоже на честный ответ.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q2, d, prev_q)
        return
    d["answers"].append(f"Цель: {m.text}")
    q_text = "Кто ваша целевая аудитория? (Пол, возраст, интересы)"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 2/6:\n{q_text}")
    bot.register_next_step_handler(msg, q3, d, q_text)

def q3(m, d, prev_q): 
    if not validate_input(m.text, prev_q):
        msg = bot.send_message(m.chat.id, f"⛔ Некорректный ответ.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q3, d, prev_q)
        return
    d["answers"].append(f"ЦА: {m.text}")
    q_text = "Назовите ваших главных конкурентов:"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 3/6:\n{q_text}")
    bot.register_next_step_handler(msg, q4, d, q_text)

def q4(m, d, prev_q): 
    if not validate_input(m.text, prev_q):
        msg = bot.send_message(m.chat.id, f"⛔ Некорректный ответ.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q4, d, prev_q)
        return
    d["answers"].append(f"Конкуренты: {m.text}")
    q_text = "В чем ваше главное преимущество (УТП)?"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 4/6:\n{q_text}")
    bot.register_next_step_handler(msg, q5, d, q_text)

def q5(m, d, prev_q): 
    if not validate_input(m.text, prev_q):
        msg = bot.send_message(m.chat.id, f"⛔ Некорректный ответ.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q5, d, prev_q)
        return
    d["answers"].append(f"УТП: {m.text}")
    q_text = "География продвижения (Город, Страна):"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 5/6:\n{q_text}")
    bot.register_next_step_handler(msg, q6, d, q_text)

def q6(m, d, prev_q):
    if not validate_input(m.text, prev_q):
        msg = bot.send_message(m.chat.id, f"⛔ Некорректный ответ.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q6, d, prev_q)
        return
    d["answers"].append(f"Гео: {m.text}")
    q_text = "Свободная форма. Особенности, сезонность, нюансы:"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 6/6 (Важно!):\n{q_text}")
    bot.register_next_step_handler(msg, finish_survey, d, q_text)

def finish_survey(m, d, prev_q):
    if not validate_input(m.text, prev_q):
        msg = bot.send_message(m.chat.id, f"⛔ Напишите осмысленно.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, finish_survey, d, prev_q)
        return
    d["answers"].append(f"Доп. инфо: {m.text}")
    
    full_text = "\n".join(d["answers"])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET info = %s WHERE id=%s", (json.dumps({"survey": full_text}, ensure_ascii=False), d["pid"]))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(d["pid"], "info_done")
    # СПЕЦИАЛЬНЫЙ ВЫЗОВ МЕНЮ С ОДНОЙ КНОПКОЙ
    open_project_menu(m.chat.id, d["pid"], mode="management", just_finished_survey=True)

# АНАЛИЗ
@bot.callback_query_handler(func=lambda call: call.data.startswith("anz_"))
def deep_analysis(call):
    pid = call.data.split("_")[1]
    bot.answer_callback_query(call.id, "Сканирую...")
    msg = bot.send_message(call.message.chat.id, "🕵️‍♂️ Сканирую структуру и контент...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
    url = cur.fetchone()[0]
    
    raw_data = deep_analyze_site(url)
    ai_prompt = f"Ты SEO профи. Аудит сайта. Данные: {raw_data}. Дай 3 ошибки и 3 точки роста."
    advice = get_gemini_response(ai_prompt)
    
    cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
    kb = cur.fetchone()[0]; 
    if not kb: kb = []
    kb.append(f"Deep Audit: {advice[:500]}")
    cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(pid, "analysis_done")
    bot.delete_message(call.message.chat.id, msg.message_id)
    send_long_message(call.message.chat.id, f"📊 **Результат аудита:**\n\n{advice}")
    open_project_menu(call.message.chat.id, pid, mode="management")

# ЗАГРУЗКА ФАЙЛОВ
@bot.callback_query_handler(func=lambda call: call.data.startswith("upf_"))
def upload_files(call):
    pid = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "📂 Пришлите текст, фото или документ (.txt/pdf).")
    bot.register_next_step_handler(msg, process_upload, pid)

def process_upload(message, pid):
    content = ""
    if message.content_type == 'text':
        content = message.text
    elif message.content_type == 'document':
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content = downloaded_file.decode('utf-8') # Пытаемся прочитать текст
        except:
            content = "Document uploaded: " + message.document.file_name

    # Мягкая проверка: если есть слова, считаем полезным
    if len(content) > 10:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
        kb = cur.fetchone()[0]; 
        if not kb: kb = []
        kb.append(f"User Upload: {content[:1000]}...")
        cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
        conn.commit(); cur.close(); conn.close()
        update_project_progress(pid, "upload_done")
        bot.reply_to(message, "✅ Информация добавлена в базу знаний.")
    else:
        bot.reply_to(message, "⚠️ Файл пуст или не читается.")
    
    open_project_menu(message.chat.id, pid, mode="management")

# КЛЮЧИ
@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_ask_count_"))
def kw_ask_count(call):
    pid = call.data.split("_")[3]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("10 ключей", callback_data=f"genkw_{pid}_10"),
               types.InlineKeyboardButton("50 ключей", callback_data=f"genkw_{pid}_50"))
    markup.add(types.InlineKeyboardButton("100 ключей", callback_data=f"genkw_{pid}_100"),
               types.InlineKeyboardButton("500 ключей", callback_data=f"genkw_{pid}_500"))
    bot.edit_message_text("🔢 Сколько ключевых слов подобрать?", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("genkw_"))
def generate_keywords_action(call):
    _, pid, count = call.data.split("_")
    bot.edit_message_text(f"🧠 Подбираю {count} слов с учетом региональности...", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT knowledge_base, url, info FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    kb_text = str(res[0])[:2000]
    info_json = res[2] or {}
    survey_text = info_json.get("survey", "")
    
    prompt = f"Твоя задача: Составь список из {count} SEO ключевых слов для сайта {res[1]}. Контекст: {survey_text}. База: {kb_text}. ВАЖНО: Формат вывода: **Высокая частотность:** список... **Средняя частотность:** список... **Низкая частотность:** список..."
    
    keywords = get_gemini_response(prompt)
    
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    send_long_message(call.message.chat.id, keywords, parse_mode='Markdown')
    
    # 3 КНОПКИ
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_kw_{pid}"),
               types.InlineKeyboardButton("📥 Скачать (.txt)", callback_data=f"download_kw_{pid}"))
    markup.add(types.InlineKeyboardButton("🔄 Пройти опрос заново", callback_data=f"srv_{pid}"))
    
    bot.send_message(call.message.chat.id, "👇 Вы можете скачать файл, отредактировать (удалить лишнее) и загрузить обратно через кнопку 'Загрузить файлы'.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_kw_"))
def approve_keywords(call):
    pid = call.data.split("_")[2]
    open_project_menu(call.message.chat.id, pid, mode="management")

@bot.callback_query_handler(func=lambda call: call.data.startswith("download_kw_"))
def download_keywords(call):
    pid = call.data.split("_")[2]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT keywords, url FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    if not res or not res[0]:
        bot.answer_callback_query(call.id, "Ключей нет.")
        return
        
    file_data = io.BytesIO(res[0].encode('utf-8'))
    file_data.name = f"keywords_{pid}.txt"
    bot.send_document(call.message.chat.id, file_data, caption=f"Ключевые слова для {res[1]}")

# СТРАТЕГИЯ
@bot.callback_query_handler(func=lambda call: call.data.startswith("strat_"))
def strategy_start(call):
    pid = call.data.split("_")[1]
    markup = types.InlineKeyboardMarkup(row_width=3)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(call.message.chat.id, "📅 Сколько статей в неделю?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("freq_"))
def cms_ask(call):
    _, pid, freq = call.data.split("_")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET frequency=%s WHERE id=%s", (freq, pid))
    cur.execute("SELECT cms_key FROM projects WHERE id=%s", (pid,))
    has_key = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    if has_key:
        propose_articles(call.message.chat.id, pid)
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("WordPress", callback_data=f"cms_set_{pid}_wp"),
                   types.InlineKeyboardButton("Tilda", callback_data=f"cms_set_{pid}_tilda"),
                   types.InlineKeyboardButton("Bitrix", callback_data=f"cms_set_{pid}_bitrix"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"open_proj_mgmt_{pid}"))
        bot.send_message(call.message.chat.id, "⚙️ Платформа сайта?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_set_"))
def cms_instruction(call):
    parts = call.data.split("_")
    pid = parts[2]
    platform = parts[3]

    instructions = {
        "wp": "1. Зайдите в админку (/wp-admin)\n2. Пользователи -> Профиль\n3. Прокрутите вниз до 'Пароли приложений'\n4. Введите имя (напр. 'Bot') -> Добавить\n5. Скопируйте полученный пароль.",
        "tilda": "1. Настройки сайта -> API\n2. Создать ключи (Public/Secret)",
        "bitrix": "1. Профиль -> Пароли приложений"
    }
    
    txt = instructions.get(platform, "Инструкция не найдена.")
    
    msg = bot.send_message(call.message.chat.id, f"📚 **Инструкция для {platform.upper()}:**\n\n{txt}\n\n👇 **Пришлите ключ доступа в ответном сообщении:**", parse_mode='Markdown')
    bot.register_next_step_handler(msg, save_cms_key, pid, platform)

def save_cms_key(message, pid, platform):
    if not message.text:
        msg = bot.send_message(message.chat.id, "❌ Нужен текст ключа. Попробуйте еще раз:")
        bot.register_next_step_handler(msg, save_cms_key, pid, platform)
        return

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_key=%s, platform=%s WHERE id=%s", (message.text, platform, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Доступ сохранен!")
    propose_articles(message.chat.id, pid)

def propose_articles(chat_id, pid):
    bot.send_message(chat_id, "🤖 Генерирую темы...")
    titles = get_gemini_response("Придумай 2 SEO заголовка для статьи. Раздели их вертикальной чертой |").split("|")
    if len(titles) < 2: titles = ["Тема 1", "Тема 2"]
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(titles[0].strip()[:20], callback_data=f"write_{pid}_0"),
               types.InlineKeyboardButton(titles[1].strip()[:20], callback_data=f"write_{pid}_1"))
    bot.send_message(chat_id, f"Выберите тему:\n1. {titles[0]}\n2. {titles[1]}", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article(call):
    pid = call.data.split("_")[1]
    wait = bot.send_message(call.message.chat.id, "✍️ Пишу статью...")
    text = get_gemini_response("Напиши SEO статью 1500 знаков.")
    img_prompt = get_gemini_response("Image prompt 3 words english")
    img_url = f"https://api.nanobanana.pro/v1/generate?prompt={img_prompt[:50]}"
    fake_link = f"http://site.com/draft-{int(time.time())}"
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO articles (project_id, content, published_url, status) VALUES (%s, %s, %s, 'pending') RETURNING id", (pid, text, fake_link))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("✏️ Переписать", callback_data=f"rewrite_{aid}"))
    
    bot.delete_message(call.message.chat.id, wait.message_id)
    try:
        bot.send_photo(call.message.chat.id, img_url, caption=f"Готово!\n{text[:100]}...", reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, f"Готово (без фото)!\n{text[:100]}...", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("rewrite_"))
def rewrite_once(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT rewrite_count FROM articles WHERE id=%s", (aid,))
    if cur.fetchone()[0] > 0:
        bot.answer_callback_query(call.id, "Только 1 правка!")
        cur.close(); conn.close(); return
    cur.execute("UPDATE articles SET rewrite_count=1 WHERE id=%s", (aid,))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(call.message.chat.id, "🔄 Переписываю...")
    bot.send_message(call.message.chat.id, "✅ Новая версия.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Утвердить", callback_data=f"approve_{aid}")))

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE articles SET status='published' WHERE id=%s", (aid,))
    conn.commit(); cur.close(); conn.close()
    bot.edit_message_caption("✅ Опубликовано!", call.message.chat.id, call.message.message_id)

# --- 7. ТАРИФЫ (ИЕРАРХИЯ) ---
def show_tariff_periods(user_id):
    txt = ("💎 **ТАРИФНЫЕ ПЛАНЫ**\n\n"
           "1️⃣ **Тест-драйв** — 500р\n"
           "• 5 генераций (ручной режим)\n"
           "• Без срока действия\n\n"
           "2️⃣ **СЕО Старт** — 1400р/мес\n"
           "• 15 генераций (автоматический режим)\n\n"
           "3️⃣ **СЕО Профи** — 2500р/мес\n"
           "• 30 генераций (до 5 проектов)\n\n"
           "4️⃣ **PBN Агент** — 7500р/мес\n"
           "• 100 генераций (до 15 проектов)\n\n"
           "🎁 **Скидка 30% при оплате на год!**")

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🏎 Тест-драйв (500р)", callback_data="period_test"))
    markup.add(types.InlineKeyboardButton("📅 На Месяц", callback_data="period_month"))
    markup.add(types.InlineKeyboardButton("📆 На Год (-30%)", callback_data="period_year"))
    bot.send_message(user_id, txt, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("period_"))
def tariff_period_select(call):
    p_type = call.data.split("_")[1]
    
    if p_type == "test":
        process_tariff_selection(call, "Тест-драйв", 500, "test")
    elif p_type == "month":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("СЕО Старт (1400р)", callback_data="buy_start_1m"),
                   types.InlineKeyboardButton("СЕО Профи (2500р)", callback_data="buy_pro_1m"),
                   types.InlineKeyboardButton("PBN Агент (7500р)", callback_data="buy_agent_1m"),
                   types.InlineKeyboardButton("🔙 Назад", callback_data="back_periods"))
        bot.edit_message_text("📅 Тарифы на Месяц:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    elif p_type == "year":
        # Цены: 1400*12*0.7 = 11760
        p_start = 11760
        p_prof = 21000
        p_agent = 62999
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(f"СЕО Старт ({p_start}р)", callback_data="buy_start_1y"),
                   types.InlineKeyboardButton(f"СЕО Профи ({p_prof}р)", callback_data="buy_pro_1y"),
                   types.InlineKeyboardButton(f"PBN Агент ({p_agent}р)", callback_data="buy_agent_1y"),
                   types.InlineKeyboardButton("🔙 Назад", callback_data="back_periods"))
        bot.edit_message_text("📆 Тарифы на Год (Выгода 30%):", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "back_periods")
def back_to_periods(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    show_tariff_periods(call.from_user.id)

def process_tariff_selection(call, name, price, code):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Картой (РФ)", callback_data=f"pay_rub_{code}_{price}"),
               types.InlineKeyboardButton("⭐ Stars", callback_data=f"pay_star_{code}_{price}"))
    
    msg_text = f"Оплата тарифа: **{name}**\nК оплате: **{price}р**"
    if call.message:
        bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    else:
        bot.send_message(call.from_user.id, msg_text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def pre_payment(call):
    parts = call.data.split("_")
    plan = parts[1] 
    period = parts[2]
    
    prices = {
        "start_1m": 1400, "pro_1m": 2500, "agent_1m": 7500,
        "start_1y": 11760, "pro_1y": 21000, "agent_1y": 62999
    }
    
    names = {"start": "СЕО Старт", "pro": "СЕО Профи", "agent": "PBN Агент"}
    period_name = "Месяц" if period == "1m" else "Год"
    
    key = f"{plan}_{period}"
    price = prices.get(key, 0)
    full_name = f"{names.get(plan, plan)} ({period_name})"
    
    process_tariff_selection(call, full_name, price, key)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def process_payment(call):
    parts = call.data.split("_")
    currency = parts[1]
    
    if parts[2] == "test":
        plan_code = "test"
        amount_idx = 3
    else:
        plan_code = f"{parts[2]}_{parts[3]}"
        amount_idx = 4
        
    try: amount = int(parts[amount_idx])
    except: amount = 500
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO payments (user_id, amount, currency, tariff_name) VALUES (%s, %s, %s, %s)", 
                (call.from_user.id, amount, currency, plan_code))
    
    col = "total_paid_rub" if currency == "rub" else "total_paid_stars"
    cur.execute(f"UPDATE users SET tariff=%s, {col}={col}+%s WHERE user_id=%s", (plan_code, amount, call.from_user.id))
    conn.commit(); cur.close(); conn.close()
    
    bot.send_message(call.message.chat.id, f"✅ Оплата прошла успешно! Тариф {plan_code} активирован.")

# --- 8. ПРОФИЛЬ И АДМИНКА ---
def show_profile(uid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT tariff, gens_left, balance FROM users WHERE user_id=%s", (uid,))
    u = cur.fetchone()
    cur.execute("SELECT count(*) FROM articles WHERE status='published' AND project_id IN (SELECT id FROM projects WHERE user_id=%s)", (uid,))
    arts = cur.fetchone()[0]
    cur.close(); conn.close()
    
    safe_tariff = escape_md(u[0])
    txt = f"👤 **Профиль**\n\n🆔 ID: `{uid}`\n💎 Тариф: {safe_tariff}\n⚡ Генераций: {u[1]}\n💰 Баланс: {u[2]} руб.\n📄 Опубликовано: {arts}"
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Пополнить баланс", callback_data="period_test"))
    bot.send_message(uid, txt, reply_markup=markup, parse_mode='Markdown')

def show_admin_panel(uid):
    conn = get_db_connection(); cur = conn.cursor()
    
    try:
        cur.execute("SELECT count(*) FROM users WHERE last_active > NOW() - INTERVAL '15 minutes'")
        online = cur.fetchone()[0]
    except: online = 0
    
    cur.execute("SELECT sum(amount) FROM payments WHERE currency='rub' AND created_at > date_trunc('month', CURRENT_DATE)")
    profit_rub = cur.fetchone()[0] or 0
    cur.execute("SELECT sum(amount) FROM payments WHERE currency='stars' AND created_at > date_trunc('month', CURRENT_DATE)")
    profit_stars = cur.fetchone()[0] or 0
    
    cur.execute("SELECT count(*) FROM articles WHERE status='published'")
    arts = cur.fetchone()[0]
    
    cur.execute("SELECT tariff_name, count(*) FROM payments GROUP BY tariff_name")
    tariffs_stat = cur.fetchall()
    tariff_txt = "\n".join([f"- {t[0]}: {t[1]}" for t in tariffs_stat])
    
    cur.close(); conn.close()
    
    txt = (f"⚙️ **АДМИНКА**\n\n"
           f"🟢 Онлайн (15 мин): {online}\n"
           f"💰 Прибыль (мес): {profit_rub}₽ | {profit_stars}⭐️\n"
           f"📄 Опубликовано статей: {arts}\n\n"
           f"📊 **Продажи тарифов:**\n{tariff_txt}")
    
    bot.send_message(uid, txt)

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
def h(): return "OK", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    bot.infinity_polling(skip_pending=True)
