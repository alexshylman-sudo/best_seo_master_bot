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

# Глобальное хранилище контекста (кто с каким проектом работает)
# user_id: project_id
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

def send_safe_message(chat_id, text, parse_mode='HTML', reply_markup=None):
    """
    Максимально безопасная отправка.
    Режет текст жестко, если он длинный.
    Если HTML ломается — шлет текстом.
    """
    if not text: return

    # Убираем двойные звездочки, меняем на жирный для HTML (на всякий случай)
    if parse_mode == 'HTML':
        text = text.replace("**", "") 
    
    # Разбиваем на куски по 3000 символов (безопасный лимит)
    chunk_size = 3000
    parts = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

    for i, part in enumerate(parts):
        # Клавиатуру цепляем только к последнему куску
        markup = reply_markup if i == len(parts) - 1 else None
        
        try:
            bot.send_message(chat_id, part, parse_mode=parse_mode, reply_markup=markup)
        except Exception as e:
            print(f"⚠️ Send Error (HTML): {e}")
            # Если не вышло с HTML, шлем как есть
            try:
                bot.send_message(chat_id, part, parse_mode=None, reply_markup=markup)
            except Exception as e2:
                print(f"❌ Send Error (Plain): {e2}")
        time.sleep(0.5) # Пауза, чтобы Телеграм не банил за флуд

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"Ошибка AI: {e}"

def validate_input(text, question_context):
    # Проверка на нажатие кнопок меню
    if text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка", "🔙 В меню"]:
        return False, "MENU_CLICK"

    try:
        prompt = f"Модератор. Вопрос: '{question_context}'. Ответ: '{text}'. Если это мат, спам или бред - верни BAD. Если это ответ по делу - верни OK."
        res = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt]).text.strip()
        return ("BAD" not in res.upper()), "AI_CHECK"
    except: return True, "SKIP"

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
        cur.execute("INSERT INTO users (user_id, gens_left) VALUES (%s, 2) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit(); cur.close(); conn.close()
    bot.send_message(user_id, "👋 Привет! Я AI-ассистент для SEO.", reply_markup=main_menu_markup(user_id))

@bot.message_handler(func=lambda m: m.text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка", "🔙 В меню"])
def menu_handler(message):
    uid = message.from_user.id
    txt = message.text
    update_last_active(uid)

    if txt == "➕ Новый проект":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🌐 Сайт", callback_data="new_site"),
                   types.InlineKeyboardButton("📸 Инстаграм", callback_data="soon"),
                   types.InlineKeyboardButton("✈️ Телеграм", callback_data="soon"))
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
def soon_alert(call): bot.answer_callback_query(call.id, "🚧 Скоро...")

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
    
    # Устанавливаем контекст
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
    # Обновляем контекст пользователя
    USER_CONTEXT[call.from_user.id] = pid
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

# --- 6. ОПРОСНИК ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def start_survey_6q(call):
    pid = call.data.split("_")[1]
    USER_CONTEXT[call.from_user.id] = pid # Context update
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET info = '{}', keywords = NULL, progress = '{}' WHERE id = %s", (pid,))
    conn.commit(); cur.close(); conn.close()
    
    q_text = "Какая главная цель вашего сайта? (Продажи, Трафик, Бренд?)"
    msg = bot.send_message(call.message.chat.id, f"❓ Вопрос 1/6:\n{q_text}")
    bot.register_next_step_handler(msg, q2, {"pid": pid, "answers": []}, q_text)

def q2(m, d, prev_q): 
    valid, err_type = validate_input(m.text, prev_q)
    if not valid:
        msg = bot.send_message(m.chat.id, f"⛔ Пожалуйста, ответьте текстом (без кнопок меню).\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q2, d, prev_q); return
    d["answers"].append(f"Цель: {m.text}")
    q_text = "Кто ваша целевая аудитория?"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 2/6:\n{q_text}")
    bot.register_next_step_handler(msg, q3, d, q_text)

def q3(m, d, prev_q):
    valid, err_type = validate_input(m.text, prev_q)
    if not valid:
        msg = bot.send_message(m.chat.id, f"⛔ Ответьте текстом.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q3, d, prev_q); return
    d["answers"].append(f"ЦА: {m.text}")
    q_text = "Назовите ваших главных конкурентов:"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 3/6:\n{q_text}")
    bot.register_next_step_handler(msg, q4, d, q_text)

def q4(m, d, prev_q):
    valid, err_type = validate_input(m.text, prev_q)
    if not valid:
        msg = bot.send_message(m.chat.id, f"⛔ Ответьте текстом.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q4, d, prev_q); return
    d["answers"].append(f"Конкуренты: {m.text}")
    q_text = "В чем ваше главное преимущество (УТП)?"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 4/6:\n{q_text}")
    bot.register_next_step_handler(msg, q5, d, q_text)

def q5(m, d, prev_q):
    valid, err_type = validate_input(m.text, prev_q)
    if not valid:
        msg = bot.send_message(m.chat.id, f"⛔ Ответьте текстом.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q5, d, prev_q); return
    d["answers"].append(f"УТП: {m.text}")
    q_text = "География продвижения (Город, Страна):"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 5/6:\n{q_text}")
    bot.register_next_step_handler(msg, q6, d, q_text)

def q6(m, d, prev_q):
    valid, err_type = validate_input(m.text, prev_q)
    if not valid:
        msg = bot.send_message(m.chat.id, f"⛔ Ответьте текстом.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, q6, d, prev_q); return
    d["answers"].append(f"Гео: {m.text}")
    q_text = "Свободная форма. Что важно знать о бизнесе? (Особенности, сезонность):"
    msg = bot.send_message(m.chat.id, f"❓ Вопрос 6/6 (Важно!):\n{q_text}")
    bot.register_next_step_handler(msg, finish_survey, d, q_text)

def finish_survey(m, d, prev_q):
    valid, err_type = validate_input(m.text, prev_q)
    if not valid:
        msg = bot.send_message(m.chat.id, f"⛔ Ответьте текстом.\n\n❓ {prev_q}")
        bot.register_next_step_handler(msg, finish_survey, d, prev_q); return
    d["answers"].append(f"Доп. инфо: {m.text}")
    
    full_text = "\n".join(d["answers"])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET info = %s WHERE id=%s", (json.dumps({"survey": full_text}, ensure_ascii=False), d["pid"]))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(d["pid"], "info_done")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔑 Подобрать ключевые слова", callback_data=f"kw_ask_count_{d['pid']}"))
    bot.send_message(m.chat.id, "✅ Спасибо за честные ответы! Теперь давайте подберем ключевые слова.", reply_markup=markup)

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
    send_safe_message(call.message.chat.id, f"📊 **Аудит:**\n\n{advice}")
    open_project_menu(call.message.chat.id, pid, mode="management")

@bot.callback_query_handler(func=lambda call: call.data.startswith("upf_"))
def upload_files_request(call):
    pid = call.data.split("_")[1]
    USER_CONTEXT[call.from_user.id] = pid
    msg = bot.send_message(call.message.chat.id, "📂 Пришлите текст, фото или .txt файл.")
    # Мы не полагаемся ТОЛЬКО на это, у нас есть global handler
    # bot.register_next_step_handler(msg, process_upload, pid) 

# ОБЩИЙ ОБРАБОТЧИК ФАЙЛОВ (РАБОТАЕТ ВСЕГДА, ДАЖЕ ПОСЛЕ ПЕРЕЗАГРУЗКИ)
@bot.message_handler(content_types=['document', 'text', 'photo'])
def global_file_handler(message):
    # Если это текст и похоже на команду или ответ на опрос - игнорируем (пусть другие хендлеры работают)
    if message.text and (message.text.startswith("/") or message.text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка"]):
        return

    # Проверяем, есть ли активный проект у юзера
    pid = USER_CONTEXT.get(message.from_user.id)
    
    if not pid:
        if message.content_type == 'document':
            bot.reply_to(message, "⚠️ Я не знаю, к какому проекту это относится. Сначала выберите проект -> 'Загрузить файлы'.")
        return

    # Если мы здесь - значит пользователь прислал файл/текст в контексте проекта
    process_upload_content(message, pid)

def process_upload_content(message, pid):
    content = ""
    is_txt = False
    
    if message.content_type == 'text': 
        content = message.text
    elif message.content_type == 'document':
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content = downloaded_file.decode('utf-8')
            is_txt = message.document.file_name.endswith('.txt')
        except: 
            content = ""; 
            if message.content_type == 'document': bot.reply_to(message, "❌ Ошибка чтения файла.")
            return

    if not content: return

    conn = get_db_connection(); cur = conn.cursor()
    # Умная проверка через AI
    if is_txt or len(content) > 20:
        prompt = f"Это список ключевых слов? Текст: '{content[:500]}'. Ответь YES, если это список фраз. NO, если это статья."
        check = get_gemini_response(prompt)
        
        if "YES" in check.upper() or is_txt:
            cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (content, pid))
            msg_text = "✅ Ключевые слова сохранены! Кнопка 'Стратегия' доступна."
            update_project_progress(pid, "upload_done")
        else:
            cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
            kb = cur.fetchone()[0]; 
            if not kb: kb = []
            kb.append(f"Upload: {content[:500]}...")
            cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
            msg_text = "✅ Информация добавлена в базу."
            update_project_progress(pid, "upload_done")
    else:
        msg_text = "⚠️ Слишком короткий текст."

    conn.commit(); cur.close(); conn.close()
    bot.reply_to(message, msg_text)
    open_project_menu(message.chat.id, pid, mode="management")

# --- КЛЮЧИ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("kw_ask_count_"))
def kw_ask_count(call):
    pid = call.data.split("_")[3]
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("10 ключей", callback_data=f"genkw_{pid}_10"),
               types.InlineKeyboardButton("50 ключей", callback_data=f"genkw_{pid}_50"))
    markup.add(types.InlineKeyboardButton("100 ключей", callback_data=f"genkw_{pid}_100"),
               types.InlineKeyboardButton("500 ключей", callback_data=f"genkw_{pid}_500"))
    bot.edit_message_text("🔢 Сколько ключевых слов подобрать?", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("genkw_"))
def generate_keywords_action(call):
    _, pid, count = call.data.split("_")
    bot.edit_message_text(f"🧠 Подбираю {count} слов... (Это может занять минуту)", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT knowledge_base, url, info FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info_json = res[2] or {}
    survey = info_json.get("survey", "")
    kb = str(res[0])[:2000]
    
    prompt = f"Твоя задача: Составь список из {count} SEO ключевых слов для сайта {res[1]}. Контекст: {survey}. База: {kb}. Формат: **Высокая частотность:** список... **Средняя:** список..."
    keywords = get_gemini_response(prompt)
    
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    # Безопасная отправка
    send_safe_message(call.message.chat.id, keywords)
    
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_kw_{pid}"),
               types.InlineKeyboardButton("📥 Скачать (.txt)", callback_data=f"download_kw_{pid}"))
    markup.add(types.InlineKeyboardButton("🔄 Пройти опрос заново", callback_data=f"srv_{pid}"))
    
    bot.send_message(call.message.chat.id, "👇 Что делаем дальше?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_kw_"))
def approve_keywords(call):
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🚀 ⭐️ СТРАТЕГИЯ И СТАТЬИ ⭐️", callback_data=f"strat_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 В меню проекта", callback_data=f"open_proj_mgmt_{pid}"))
    bot.send_message(call.message.chat.id, "🎉 Ключи утверждены!", reply_markup=markup)

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

# --- СТРАТЕГИЯ ---
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
    links = {"wp": "1. /wp-admin -> Пользователи -> Профиль\n2. 'Пароли приложений' -> Добавить.\n3. Введите 'Bot', скопируйте пароль.\n4. Пришлите мне: **ВАШ_ЛОГИН ПАРОЛЬ** (через пробел)", 
             "tilda": "1. Настройки -> API -> Ключи.", "bitrix": "1. Профиль -> Пароли приложений."}
    msg = bot.send_message(call.message.chat.id, f"📚 **{platform.upper()}:**\n{links.get(platform)}\n\n👇 **Пришлите ключ доступа в ответном сообщении:**", parse_mode='Markdown')
    # Здесь тоже ставим глобальный контекст на всякий случай
    USER_CONTEXT[call.from_user.id] = pid
    bot.register_next_step_handler(msg, save_cms_key, pid, platform)

def save_cms_key(message, pid, platform):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_key=%s, platform=%s WHERE id=%s", (message.text, platform, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Доступ сохранен!")
    propose_articles(message.chat.id, pid)

def propose_articles(chat_id, pid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id, info, keywords, knowledge_base FROM projects WHERE id=%s", (pid,))
    proj = cur.fetchone()
    user_id = proj[0]
    
    cur.execute("SELECT gens_left, is_admin FROM users WHERE user_id=%s", (user_id,))
    u_data = cur.fetchone()
    gens_left, is_admin = u_data[0], u_data[1]
    
    if gens_left <= 0 and not is_admin:
        cur.close(); conn.close()
        bot.send_message(chat_id, "⚠️ **Лимит генераций исчерпан!** Пополните баланс.")
        return

    bot.send_message(chat_id, f"⚡ Осталось генераций: {gens_left}. Генерирую темы...")
    
    info_json = proj[1] or {}
    survey = info_json.get("survey", "")
    kw = proj[2] or "Нет ключей"
    kb = str(proj[3])[:2000]
    
    prompt = f"""
    Твоя роль: SEO стратег. 
    Сайт данные: {survey}, Ключи: {kw[:1000]}
    
    Задача: Придумай 5 тем для статей.
    ФОРМАТ ВЫВОДА СТРОГО ТАКОЙ:
    Тема 1: Заголовок
    Описание: Описание
    |
    Тема 2: Заголовок
    Описание: Описание
    """
    
    try:
        raw_text = get_gemini_response(prompt)
        topics_raw = raw_text.split("|")
        topics = []
        for t in topics_raw:
            if "Тема" in t:
                clean_t = t.strip().replace("Тема", "").replace("*", "")
                parts = clean_t.split("\n")
                title_line = parts[0].split(":")[-1].strip()
                if len(title_line) > 5:
                    desc = parts[1] if len(parts) > 1 else ""
                    topics.append({"title": title_line, "desc": desc})
        
        topics = topics[:5]
    except:
        topics = [{"title": "Article 1", "desc": ""}, {"title": "Article 2", "desc": ""}]

    info_json["temp_topics"] = topics
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info_json), pid))
    conn.commit(); cur.close(); conn.close()

    markup = types.InlineKeyboardMarkup(row_width=1)
    msg_text = "📝 **Выберите тему:**\n\n"
    
    for i, t in enumerate(topics):
        msg_text += f"{i+1}. **{t['title']}**\n_{t['desc']}_\n\n"
        markup.add(types.InlineKeyboardButton(f"Выбрать тему №{i+1}", callback_data=f"write_{pid}_topic_{i}"))
        
    bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article(call):
    parts = call.data.split("_")
    pid = parts[1]
    topic_idx = int(parts[3])
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    info = res[0]
    keywords = res[1] or ""
    
    topics = info.get("temp_topics", [])
    selected_topic = topics[topic_idx]['title'] if len(topics) > topic_idx else "Article"
    
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, f"⏳ Пишу статью: **{selected_topic}**\n(~2500 слов, Yoast SEO)...", parse_mode='Markdown')
    
    prompt = f"""
    Напиши SEO-статью на тему: "{selected_topic}".
    Ключевые слова: {keywords[:500]}...
    Форматирование: Используй HTML <b> и <i>. Без Markdown звездочек.
    В конце добавь блок SEO (Title, Description).
    """
    
    article_text = get_gemini_response(prompt)
    
    cur.execute("UPDATE users SET gens_left = gens_left - 1 WHERE user_id = (SELECT user_id FROM projects WHERE id=%s) AND is_admin = FALSE", (pid,))
    
    cur.execute("INSERT INTO articles (project_id, title, content, status) VALUES (%s, %s, %s, 'draft') RETURNING id", (pid, selected_topic, article_text))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    send_safe_message(call.message.chat.id, article_text, parse_mode='HTML')
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Публикуем", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("✏️ Переписать (1 раз)", callback_data=f"rewrite_{aid}"))
    
    bot.send_message(call.message.chat.id, "👇 Что делаем?", reply_markup=markup)

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
    new_text = get_gemini_response(f"Перепиши статью: {title}. HTML формат.")
    
    cur.execute("UPDATE articles SET content=%s, rewrite_count=1 WHERE id=%s", (new_text, aid))
    conn.commit(); cur.close(); conn.close()
    
    send_safe_message(call.message.chat.id, new_text, parse_mode='HTML')
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Публикуем", callback_data=f"approve_{aid}"))
    
    bot.send_message(call.message.chat.id, "👇 Новая версия готова.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT project_id, title, content FROM articles WHERE id=%s", (aid,))
    art = cur.fetchone()
    pid, title, content = art
    
    # ПУБЛИКАЦИЯ В WP
    success, link = publish_to_wordpress(pid, title, content, call.from_user.id)
    
    if success:
        cur.execute("UPDATE articles SET status='published', published_url=%s WHERE id=%s", (link, aid))
        conn.commit()
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, f"✅ **Опубликовано!**\n🔗 {link}", parse_mode='Markdown')
    else:
        bot.send_message(call.message.chat.id, f"❌ Ошибка публикации: {link}")
    
    cur.close(); conn.close()

def publish_to_wordpress(pid, title, content, user_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, cms_key FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    if not res or not res[1]: return False, "Нет ключа доступа."
    
    site_url, app_key = res
    if site_url.endswith('/'): site_url = site_url[:-1]
    api_url = f"{site_url}/wp-json/wp/v2/posts"
    
    try:
        # Пытаемся распарсить ЛОГИН ПАРОЛЬ
        parts = app_key.split(' ', 1)
        if len(parts) < 2: return False, "Неверный формат ключа (нужен ЛОГИН ПАРОЛЬ)."
        
        creds = f"{parts[0]}:{parts[1]}"
        token = base64.b64encode(creds.encode()).decode()
        headers = {'Authorization': 'Basic ' + token}
        post = {'title': title, 'content': content, 'status': 'publish'}
        
        r = requests.post(api_url, headers=headers, json=post)
        if r.status_code == 201: return True, r.json().get('link')
        return False, f"Code {r.status_code}: {r.text[:100]}"
    except Exception as e: return False, str(e)

# --- 7. ТАРИФЫ (Без изменений) ---
def show_tariff_periods(user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🏎 Тест-драйв (500р)", callback_data="period_test"))
    markup.add(types.InlineKeyboardButton("📅 На Месяц", callback_data="period_month"))
    markup.add(types.InlineKeyboardButton("📆 На Год (-30%)", callback_data="period_year"))
    bot.send_message(user_id, "💎 Выберите период:", reply_markup=markup)

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
    process_tariff_selection(call, f"{parts[1]}", 1000, f"{parts[1]}_{parts[2]}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def process_payment(call):
    bot.send_message(call.message.chat.id, "✅ Оплата прошла успешно!")

# --- 8. ПРОФИЛЬ ---
def show_profile(uid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT tariff, gens_left, balance FROM users WHERE user_id=%s", (uid,))
    u = cur.fetchone()
    cur.close(); conn.close()
    bot.send_message(uid, f"👤 Тариф: {u[0]}\n⚡ Генераций: {u[1]}")

def show_admin_panel(uid):
    bot.send_message(uid, "⚙️ Админка")

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
