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

    # Админу даем 2 теста + GOD_MODE
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
        parts = []
        while len(text) > 0:
            if len(text) > 4000:
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
        prompt = f"Модератор. Вопрос: '{question_context}'. Ответ: '{text}'. Если мат/спам - BAD. Если адекватно - OK."
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
        raw_text = soup.get_text()[:2000].strip()
        return f"URL: {url}\nTitle: {title}\nDesc: {desc}\nText: {raw_text}"
    except Exception as e:
        return f"Ошибка доступа: {e}"

def update_project_progress(pid, step_key):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT progress FROM projects WHERE id=%s", (pid,))
        result = cur.fetchone()
        prog = result[0] if result and result[0] else {}
        prog[step_key] = True
        cur.execute("UPDATE projects SET progress=%s WHERE id=%s", (json.dumps(prog), pid))
        conn.commit()
    except Exception as e:
        print(f"Progress error: {e}")
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
        # Даем 2 бесплатных генерации при регистрации
        cur.execute("INSERT INTO users (user_id, gens_left) VALUES (%s, 2) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit(); cur.close(); conn.close()
    bot.send_message(user_id, "👋 Привет! Я AI-ассистент для SEO.", reply_markup=main_menu_markup(user_id))

@bot.message_handler(func=lambda m: m.text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка"])
def menu_handler(message):
    uid = message.from_user.id
    txt = message.text
    update_last_active(uid)

    if txt == "➕ Новый проект":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🌐 Сайт", callback_data="new_site"))
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
        msg = bot.send_message(message.chat.id, "❌ Нужен URL с http://. Попробуйте снова:")
        bot.register_next_step_handler(msg, check_url_step)
        return
    
    msg_check = bot.send_message(message.chat.id, "⏳ Проверяю...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM projects WHERE url = %s", (url,))
    if cur.fetchone():
        cur.close(); conn.close()
        bot.delete_message(message.chat.id, msg_check.message_id)
        msg = bot.send_message(message.chat.id, f"⛔ Сайт {url} уже есть в системе.\n👇 **Введите другой URL:**")
        bot.register_next_step_handler(msg, check_url_step)
        return

    if not check_site_availability(url):
        cur.close(); conn.close()
        msg = bot.edit_message_text("❌ Сайт недоступен. Проверьте ссылку:", message.chat.id, msg_check.message_id)
        bot.register_next_step_handler(msg, check_url_step)
        return
    
    cur.execute("INSERT INTO projects (user_id, type, url, info, progress) VALUES (%s, 'site', %s, '{}', '{}') RETURNING id", (message.from_user.id, url))
    pid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
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
    
    has_keywords = kw_db is not None and len(kw_db) > 5

    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # 1. ГЛАВНАЯ КНОПКА
    if has_keywords:
        markup.add(types.InlineKeyboardButton("🚀 ⭐️ СТРАТЕГИЯ И СТАТЬИ ⭐️", callback_data=f"strat_{pid}"))

    # 2. КНОПКИ ЭТАПОВ
    btn_info = types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"srv_{pid}")
    btn_anal = types.InlineKeyboardButton("📊 Анализ сайта (Глубокий)", callback_data=f"anz_{pid}")
    btn_upl = types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upf_{pid}")
    
    if mode == "onboarding":
        if not progress.get("info_done"): markup.add(btn_info)
        if not progress.get("analysis_done"): markup.add(btn_anal)
        if not progress.get("upload_done"): markup.add(btn_upl)
    else:
        markup.add(btn_info, btn_anal, btn_upl)

    # 3. КЛЮЧИ (Логика отображения)
    # Если ключи есть -> Кнопка удаления
    # Если ключей нет, но пройден опрос/загрузка -> Кнопка подбора
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

# --- 6. ОПРОСНИК И ФАЙЛЫ ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def start_survey_6q(call):
    pid = call.data.split("_")[1]
    q_text = "Какая главная цель вашего сайта? (Продажи, Трафик, Бренд?)"
    msg = bot.send_message(call.message.chat.id, f"❓ Вопрос 1/6:\n{q_text}")
    bot.register_next_step_handler(msg, q2, {"pid": pid, "answers": []}, q_text)

def q2(m, d, prev_q): 
    if not validate_input(m.text, prev_q):
        msg = bot.send_message(m.chat.id, f"⛔ Попробуйте точнее.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q2, d, prev_q); return
    d["answers"].append(f"Цель: {m.text}")
    q_text = "Кто ваша целевая аудитория?"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 2/6:\n{q_text}")
    bot.register_next_step_handler(msg, q3, d, q_text)

def q3(m, d, prev_q):
    d["answers"].append(f"ЦА: {m.text}")
    q_text = "Назовите ваших главных конкурентов:"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 3/6:\n{q_text}")
    bot.register_next_step_handler(msg, q4, d, q_text)

def q4(m, d, prev_q):
    d["answers"].append(f"Конкуренты: {m.text}")
    q_text = "В чем ваше главное преимущество (УТП)?"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 4/6:\n{q_text}")
    bot.register_next_step_handler(msg, q5, d, q_text)

def q5(m, d, prev_q):
    d["answers"].append(f"УТП: {m.text}")
    q_text = "География продвижения (Город, Страна):"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 5/6:\n{q_text}")
    bot.register_next_step_handler(msg, q6, d, q_text)

def q6(m, d, prev_q):
    d["answers"].append(f"Гео: {m.text}")
    q_text = "Свободная форма. Что важно знать о бизнесе? (Особенности, сезонность):"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 6/6 (Важно!):\n{q_text}")
    bot.register_next_step_handler(msg, finish_survey, d, q_text)

def finish_survey(m, d, prev_q):
    d["answers"].append(f"Доп. инфо: {m.text}")
    full_text = "\n".join(d["answers"])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET info = %s WHERE id=%s", (json.dumps({"survey": full_text}, ensure_ascii=False), d["pid"]))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(d["pid"], "info_done")
    
    # ПОКАЗЫВАЕМ ТОЛЬКО КНОПКУ ПОДБОРА КЛЮЧЕЙ
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔑 Подобрать ключевые слова", callback_data=f"kw_ask_count_{d['pid']}"))
    
    bot.send_message(m.chat.id, "✅ Спасибо за честные ответы! Теперь давайте подберем ключевые слова для продвижения.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("anz_"))
def deep_analysis(call):
    pid = call.data.split("_")[1]
    bot.answer_callback_query(call.id, "Сканирую...")
    msg = bot.send_message(call.message.chat.id, "🕵️‍♂️ Сканирую...")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
    url = cur.fetchone()[0]
    raw_data = deep_analyze_site(url)
    advice = get_gemini_response(f"Ты SEO профи. Аудит сайта: {raw_data}. Дай 3 ошибки и 3 точки роста.")
    
    cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
    kb = cur.fetchone()[0]; 
    if not kb: kb = []
    kb.append(f"Deep Audit: {advice[:500]}")
    cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(pid, "analysis_done")
    bot.delete_message(call.message.chat.id, msg.message_id)
    send_long_message(call.message.chat.id, f"📊 **Аудит:**\n\n{advice}")
    open_project_menu(call.message.chat.id, pid, mode="management")

@bot.callback_query_handler(func=lambda call: call.data.startswith("upf_"))
def upload_files(call):
    pid = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "📂 Пришлите текст, фото или документ (.txt/pdf).")
    bot.register_next_step_handler(msg, process_upload, pid)

def process_upload(message, pid):
    content = ""
    if message.content_type == 'text': content = message.text
    elif message.content_type == 'document':
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content = downloaded_file.decode('utf-8')
        except: content = ""

    # Умная проверка контента
    ai_check = get_gemini_response(f"Проанализируй этот текст: '{content[:1000]}'. Это список ключевых слов/фраз для SEO? Ответь YES, если это похоже на список ключей. Если это просто текст статьи или описание, ответь TEXT. Если мусор, ответь NO.")
    
    conn = get_db_connection(); cur = conn.cursor()
    
    if "YES" in ai_check.upper():
        cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (content, pid))
        msg_text = "✅ Отлично! Я распознал файл как список ключевых слов и сохранил их."
        update_project_progress(pid, "upload_done")
    elif "TEXT" in ai_check.upper():
        cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
        kb = cur.fetchone()[0]; 
        if not kb: kb = []
        kb.append(f"Upload: {content[:500]}...")
        cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
        msg_text = "✅ Информация добавлена в базу знаний проекта."
        update_project_progress(pid, "upload_done")
    else:
        msg_text = "⚠️ Файл не содержит полезной информации или не читается."

    conn.commit(); cur.close(); conn.close()
    bot.reply_to(message, msg_text)
    open_project_menu(message.chat.id, pid, mode="management")

# КЛЮЧИ И СТРАТЕГИЯ
@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_ask_count_"))
def kw_ask_count(call):
    pid = call.data.split("_")[3]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("10 ключей", callback_data=f"genkw_{pid}_10"),
               types.InlineKeyboardButton("50 ключей", callback_data=f"genkw_{pid}_50"))
    bot.edit_message_text("🔢 Сколько ключевых слов подобрать?", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("genkw_"))
def generate_keywords_action(call):
    _, pid, count = call.data.split("_")
    bot.edit_message_text(f"🧠 Подбираю {count} слов...", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT knowledge_base, url, info FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info_json = res[2] or {}
    survey = info_json.get("survey", "")
    kb = str(res[0])[:2000]
    
    prompt = f"Составь список из {count} SEO ключей для {res[1]}. Контекст: {survey}. База: {kb}. Формат: **Высокая частотность:** список... **Средняя:** список..."
    keywords = get_gemini_response(prompt)
    
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    send_long_message(call.message.chat.id, keywords, parse_mode='Markdown')
    
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_kw_{pid}"),
               types.InlineKeyboardButton("📥 Скачать (.txt)", callback_data=f"download_kw_{pid}"))
    markup.add(types.InlineKeyboardButton("🔄 Пройти опрос заново", callback_data=f"srv_{pid}"))
    
    bot.send_message(call.message.chat.id, "👇 Скачайте файл, отредактируйте и загрузите обратно через 'Загрузить файлы', если нужно.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_kw_"))
def approve_keywords(call):
    pid = call.data.split("_")[2]
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🚀 ⭐️ СТРАТЕГИЯ И СТАТЬИ ⭐️", callback_data=f"strat_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 В меню проекта", callback_data=f"open_proj_mgmt_{pid}"))
    
    bot.send_message(call.message.chat.id, "🎉 Поздравляю! Семантическое ядро готово. Переходим к продвижению!", reply_markup=markup)

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
        bot.send_document(call.message.chat.id, file, caption=f"Ключи для {res[1]}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("strat_"))
def strategy_start(call):
    pid = call.data.split("_")[1]
    markup = types.InlineKeyboardMarkup(row_width=3)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(call.message.chat.id, "📅 Сколько статей в неделю генерировать?", reply_markup=markup)

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
    pid, platform = parts[2], parts[3]
    links = {"wp": "1. /wp-admin -> Пользователи -> Профиль\n2. 'Пароли приложений' -> Добавить.", 
             "tilda": "1. Настройки -> API -> Ключи.", "bitrix": "1. Профиль -> Пароли приложений."}
    msg = bot.send_message(call.message.chat.id, f"📚 **{platform.upper()}:**\n{links.get(platform)}\n\n👇 **Пришлите ключ:**", parse_mode='Markdown')
    bot.register_next_step_handler(msg, save_cms_key, pid, platform)

def save_cms_key(message, pid, platform):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_key=%s, platform=%s WHERE id=%s", (message.text, platform, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Доступ сохранен!")
    propose_articles(message.chat.id, pid)

def propose_articles(chat_id, pid):
    # ПРОВЕРКА ЛИМИТОВ
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id, info, keywords, knowledge_base FROM projects WHERE id=%s", (pid,))
    proj = cur.fetchone()
    user_id = proj[0]
    
    cur.execute("SELECT gens_left, is_admin FROM users WHERE user_id=%s", (user_id,))
    u_data = cur.fetchone()
    gens_left, is_admin = u_data[0], u_data[1]
    
    if gens_left <= 0 and not is_admin:
        cur.close(); conn.close()
        bot.send_message(chat_id, "⚠️ **Лимит генераций исчерпан!** Пополните баланс в меню 'Тарифы'.")
        return

    bot.send_message(chat_id, f"⚡ Осталось генераций: {gens_left}. Генерирую темы на основе ваших ключей...")
    
    # КОНТЕКСТ ДЛЯ AI (ЧТОБЫ НЕ БЫЛО КОФЕВАРОК)
    info_json = proj[1] or {}
    survey = info_json.get("survey", "Нет данных")
    kw = proj[2] or "Нет ключей"
    kb = str(proj[3])[:1000]
    
    prompt = f"""
    Твоя роль: SEO стратег.
    Проект:
    - Опрос: {survey}
    - Ключи: {kw[:500]}...
    - База: {kb}
    
    Задача: Придумай 5 тем для статей. Верни их списком, разделенным символом | (вертикальная черта).
    Пример: Тема 1 | Тема 2 | Тема 3
    Темы должны быть строго по тематике сайта!
    """
    
    try:
        titles_raw = get_gemini_response(prompt)
        titles = titles_raw.split("|")
        # Очистка от лишних пробелов и символов
        titles = [t.strip().replace("*", "") for t in titles if len(t) > 3][:5] 
    except:
        titles = ["Ошибка генерации. Попробуйте еще раз."]

    markup = types.InlineKeyboardMarkup(row_width=1)
    msg_text = "📝 **Выберите тему для статьи:**\n\n"
    
    for i, title in enumerate(titles):
        msg_text += f"{i+1}. {title}\n"
        # Передаем только индекс в callback, чтобы не перегружать
        markup.add(types.InlineKeyboardButton(f"Выбрать тему №{i+1}", callback_data=f"write_{pid}_{i}"))
        
    # Сохраняем темы во временное хранилище (можно в файл, но проще в глобал словарь для MVP, или просто перегенерировать текст потом)
    # Для надежности в MVP мы просто передадим номер, а при генерации скажем "Напиши статью на тему №Х из списка: [Список]"
    # Но так как список в памяти не хранится между вызовами в serverless,
    # мы схитрим: передадим первые 20 символов темы в callback или (лучше) просто сгенерируем статью "по теме проекта", это будет Topic X.
    # ЛУЧШИЙ ВАРИАНТ ДЛЯ STATELESS: Запишем темы в БД в поле progress или info временно? Нет, сложно.
    # ПРОСТОЙ ВАРИАНТ: Передаем обрезанный заголовок в callback (до 30 байт)
    
    # ПЕРЕДЕЛЫВАЕМ КНОПКИ ЧТОБЫ РАБОТАЛО ЖЕЛЕЗНО
    markup = types.InlineKeyboardMarkup(row_width=1)
    for i, title in enumerate(titles):
        # Храним полный текст темы в базе не будем, просто передадим индекс, 
        # а в write_article попросим AI "Сгенерируй статью на тему, которая подходит под эти ключи, вариант номер {i+1}" - это рискованно.
        # Давайте запишем выбранные темы в info
        markup.add(types.InlineKeyboardButton(f"Тема {i+1}", callback_data=f"write_{pid}_topic_{i}"))
    
    # Сохраняем сгенерированные темы в info проекта, чтобы потом достать
    info_json["temp_topics"] = titles
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info_json), pid))
    conn.commit(); cur.close(); conn.close()
    
    bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article(call):
    # write_PID_topic_INDEX
    parts = call.data.split("_")
    pid = parts[1]
    topic_idx = int(parts[3])
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0]
    topics = info.get("temp_topics", [])
    selected_topic = topics[topic_idx] if len(topics) > topic_idx else "SEO Article"
    
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, f"⏳ Пишу статью на тему: **{selected_topic}**\nЭто займет около 30 секунд...", parse_mode='Markdown')
    
    # ГЕНЕРАЦИЯ
    article_text = get_gemini_response(f"Напиши SEO статью (1500 знаков) на тему: '{selected_topic}'. Используй html теги <b> и <i> для форматирования.")
    
    # Списываем лимит
    cur.execute("UPDATE users SET gens_left = gens_left - 1 WHERE user_id = (SELECT user_id FROM projects WHERE id=%s) AND is_admin = FALSE", (pid,))
    
    # Сохраняем как черновик
    cur.execute("INSERT INTO articles (project_id, title, content, status) VALUES (%s, %s, %s, 'draft') RETURNING id", (pid, selected_topic, article_text))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    # ОТПРАВЛЯЕМ ТЕКСТ В ЧАТ
    send_long_message(call.message.chat.id, article_text, parse_mode='HTML')
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Публикуем на сайт", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("✏️ Переписать (1 раз)", callback_data=f"rewrite_{aid}"))
    
    bot.send_message(call.message.chat.id, "👇 Что делаем с этой статьей?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("rewrite_"))
def rewrite_once(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT rewrite_count, title FROM articles WHERE id=%s", (aid,))
    res = cur.fetchone()
    rc, title = res[0], res[1]
    
    if rc > 0:
        bot.answer_callback_query(call.id, "⛔ Только 1 правка!")
        cur.close(); conn.close(); return
        
    bot.send_message(call.message.chat.id, "🔄 Переписываю...")
    new_text = get_gemini_response(f"Перепиши эту статью в другом стиле: {title}")
    
    cur.execute("UPDATE articles SET content=%s, rewrite_count=1 WHERE id=%s", (new_text, aid))
    conn.commit(); cur.close(); conn.close()
    
    send_long_message(call.message.chat.id, new_text)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Публикуем эту версию", callback_data=f"approve_{aid}"))
    
    bot.send_message(call.message.chat.id, "👇 Новая версия готова.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE articles SET status='published' WHERE id=%s", (aid,))
    conn.commit(); cur.close(); conn.close()
    
    # Имитация публикации
    fake_url = f"https://yoursite.com/blog/article-{aid}"
    bot.edit_message_text(f"✅ **Статья успешно опубликована!**\n🔗 {fake_url}", call.message.chat.id, call.message.message_id, parse_mode='Markdown')

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
    plan, period = parts[1], parts[2]
    
    prices = {"start_1m": 1400, "pro_1m": 2500, "agent_1m": 7500, "start_1y": 11760, "pro_1y": 21000, "agent_1y": 62999}
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
    # Начисляем генерации при покупке
    gens_add = 5 if plan_code == 'test' else 15 # Упрощенно
    cur.execute(f"UPDATE users SET tariff=%s, gens_left=gens_left+%s, {col}={col}+%s WHERE user_id=%s", (plan_code, gens_add, amount, call.from_user.id))
    conn.commit(); cur.close(); conn.close()
    
    bot.send_message(call.message.chat.id, f"✅ Оплата прошла! Тариф {plan_code} активирован. (+{gens_add} ген.)")

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
    try: cur.execute("SELECT count(*) FROM users WHERE last_active > NOW() - INTERVAL '15 minutes'")
    except: pass
    online = cur.fetchone()[0] if cur.description else 0
    
    cur.execute("SELECT sum(amount) FROM payments WHERE currency='rub'")
    rub = cur.fetchone()[0] or 0
    cur.execute("SELECT count(*) FROM articles")
    arts = cur.fetchone()[0]
    
    cur.close(); conn.close()
    bot.send_message(uid, f"⚙️ **АДМИНКА**\n\n🟢 Онлайн: {online}\n💰 Всего заработано: {rub}₽\n📄 Статей: {arts}")

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
