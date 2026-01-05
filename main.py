import os
import threading
import time
import schedule
import psycopg2
import json
import requests
import datetime
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

def init_db():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()

    # Удаляем старые таблицы для обновления структуры (В ПРОДАКШЕНЕ ТАК ДЕЛАТЬ АККУРАТНО)
    # cur.execute("DROP TABLE IF EXISTS projects CASCADE")
    # cur.execute("DROP TABLE IF EXISTS users CASCADE")
    # cur.execute("DROP TABLE IF EXISTS payments CASCADE")

    # 1. Таблица пользователей (добавили last_active)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance INT DEFAULT 0,
            tariff TEXT DEFAULT 'Нет тарифа',
            tariff_expires TIMESTAMP,
            gens_left INT DEFAULT 0,
            is_admin BOOLEAN DEFAULT FALSE,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 2. Таблица проектов
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
    
    # 3. Таблица статей
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

    # 4. Таблица платежей (для админки)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            amount INT,
            currency TEXT, -- 'rub' or 'stars'
            tariff_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Предустановка Админа
    cur.execute("INSERT INTO users (user_id, is_admin, tariff, gens_left) VALUES (%s, TRUE, 'GOD_MODE', 9999) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    
    conn.commit(); cur.close(); conn.close()
    print("✅ БД инициализирована.")

def update_last_active(user_id):
    """Обновляет время последней активности пользователя"""
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET last_active = NOW() WHERE user_id = %s", (user_id,))
        conn.commit(); cur.close(); conn.close()
    except: pass

# --- 3. УТИЛИТЫ ---
def escape_md(text):
    if not text: return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"Ошибка AI: {e}"

def validate_input(text):
    """Проверяет текст на адекватность через AI"""
    try:
        prompt = f"Проанализируй этот ответ пользователя на бизнес-вопрос. Ответ: '{text}'. Если это нецензурная лексика, бессмысленный набор букв/цифр или оскорбление, ответь 'BAD'. Если это нормальный ответ (даже короткий), ответь 'OK'."
        res = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt]).text.strip()
        return "BAD" not in res.upper()
    except:
        return True # Если AI сбойнул, пропускаем

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
        # Показываем выбор периода (1 уровень)
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
        msg = bot.send_message(message.chat.id, "❌ Нужен URL с http:// или https://. Попробуйте:")
        bot.register_next_step_handler(msg, check_url_step)
        return
    msg_check = bot.send_message(message.chat.id, "⏳ Проверяю доступность...")
    if not check_site_availability(url):
        bot.edit_message_text("❌ Сайт недоступен. Проверьте ссылку.", message.chat.id, msg_check.message_id)
        return
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO projects (user_id, type, url, info, progress) VALUES (%s, 'site', %s, '{}', '{}') RETURNING id", (message.from_user.id, url))
    pid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    # УДАЛЯЕМ старое сообщение проверки
    bot.delete_message(message.chat.id, msg_check.message_id)
    # Сразу открываем меню с приветствием
    open_project_menu(message.chat.id, pid, mode="onboarding", new_site_url=url)

def open_project_menu(chat_id, pid, mode="management", msg_id=None, new_site_url=None):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, keywords, progress FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    if not res: return
    
    url, kw_db, progress = res
    if not progress: progress = {}
    
    # Кнопка ключей доступна ТОЛЬКО если есть опрос или файлы
    can_gen_keys = progress.get("info_done") or progress.get("upload_done")
    has_keywords = kw_db is not None and len(kw_db) > 5

    markup = types.InlineKeyboardMarkup(row_width=1)
    
    btn_info = types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"srv_{pid}")
    btn_anal = types.InlineKeyboardButton("📊 Анализ сайта (Глубокий)", callback_data=f"anz_{pid}")
    btn_upl = types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upf_{pid}")
    
    if mode == "onboarding":
        if not progress.get("info_done"): markup.add(btn_info)
        if not progress.get("analysis_done"): markup.add(btn_anal)
        if not progress.get("upload_done"): markup.add(btn_upl)
    else:
        markup.add(btn_info, btn_anal, btn_upl)

    # Кнопки ключей
    if has_keywords:
        markup.row(types.InlineKeyboardButton("❌ Удалить ключи", callback_data=f"delkw_{pid}"),
                   types.InlineKeyboardButton("🚀 Стратегия и Статьи", callback_data=f"strat_{pid}"))
    elif can_gen_keys:
        markup.add(types.InlineKeyboardButton("🔑 Подобрать ключевые слова", callback_data=f"kw_ask_count_{pid}"))

    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))

    safe_url = escape_md(url)
    
    # Если это новый сайт, пишем приветствие, иначе стандартный заголовок
    if new_site_url:
        text = f"✅ Сайт {safe_url} добавлен!\n\n👇 Выберите действие:"
    else:
        text = f"📂 **Проект:** {safe_url}\nРежим: {'Первичная настройка' if mode=='onboarding' else 'Управление'}"
    
    try:
        if msg_id and not new_site_url: # Если не новый сайт, редактируем
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
        else:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.send_message(chat_id, text.replace("*", "").replace("_", ""), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_mgmt_"))
def open_proj_mgmt(call):
    pid = call.data.split("_")[3]
    open_project_menu(call.message.chat.id, pid, mode="management", msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Главное меню", reply_markup=main_menu_markup(call.from_user.id))

# --- 6. ФУНКЦИОНАЛ ---

# ОПРОСНИК (С ВАЛИДАЦИЕЙ)
@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def start_survey_5q(call):
    pid = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "❓ Вопрос 1/5:\nКакая главная цель вашего сайта? (Продажи, Трафик, Бренд?)")
    bot.register_next_step_handler(msg, q2, {"pid": pid, "answers": []})

def q2(m, d): 
    if not validate_input(m.text):
        msg = bot.send_message(m.chat.id, "⛔ Пожалуйста, отвечайте честно и без нецензурной лексики.\n\n❓ Вопрос 1/5:\nКакая главная цель вашего сайта?")
        bot.register_next_step_handler(msg, q2, d) # Повтор вопроса
        return
    d["answers"].append(f"Цель: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 2/5:\nКто ваша целевая аудитория? (Пол, возраст, интересы)")
    bot.register_next_step_handler(msg, q3, d)

def q3(m, d): 
    if not validate_input(m.text):
        msg = bot.send_message(m.chat.id, "⛔ Некорректный ответ.\n\n❓ Вопрос 2/5:\nКто ваша целевая аудитория?")
        bot.register_next_step_handler(msg, q3, d)
        return
    d["answers"].append(f"ЦА: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 3/5:\nНазовите ваших главных конкурентов:")
    bot.register_next_step_handler(msg, q4, d)

def q4(m, d): 
    if not validate_input(m.text):
        msg = bot.send_message(m.chat.id, "⛔ Некорректный ответ.\n\n❓ Вопрос 3/5:\nНазовите ваших главных конкурентов:")
        bot.register_next_step_handler(msg, q4, d)
        return
    d["answers"].append(f"Конкуренты: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 4/5:\nВ чем ваше главное преимущество (УТП)?")
    bot.register_next_step_handler(msg, q5, d)

def q5(m, d): 
    if not validate_input(m.text):
        msg = bot.send_message(m.chat.id, "⛔ Некорректный ответ.\n\n❓ Вопрос 4/5:\nВ чем ваше главное преимущество (УТП)?")
        bot.register_next_step_handler(msg, q5, d)
        return
    d["answers"].append(f"УТП: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 5/5:\nГеография продвижения (Город, Страна):")
    bot.register_next_step_handler(msg, finish_survey, d)

def finish_survey(m, d):
    if not validate_input(m.text):
        msg = bot.send_message(m.chat.id, "⛔ Некорректный ответ.\n\n❓ Вопрос 5/5:\nГеография продвижения (Город, Страна):")
        bot.register_next_step_handler(msg, finish_survey, d)
        return
    d["answers"].append(f"Гео: {m.text}")
    
    full_text = "\n".join(d["answers"])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET info = %s WHERE id=%s", (json.dumps({"survey": full_text}, ensure_ascii=False), d["pid"]))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(d["pid"], "info_done")
    bot.send_message(m.chat.id, "✅ Ответы сохранены!")
    open_project_menu(m.chat.id, d["pid"], mode="management")

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
    ai_prompt = f"Ты SEO профессионал. Проведи аудит сайта. Данные: {raw_data}. Дай 3 критических ошибки и 3 точки роста."
    advice = get_gemini_response(ai_prompt)
    
    cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
    kb = cur.fetchone()[0]; 
    if not kb: kb = []
    kb.append(f"Deep Audit: {advice[:500]}")
    cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(pid, "analysis_done")
    bot.delete_message(call.message.chat.id, msg.message_id)
    bot.send_message(call.message.chat.id, f"📊 **Результат аудита:**\n\n{advice}")
    open_project_menu(call.message.chat.id, pid, mode="management")

# ЗАГРУЗКА
@bot.callback_query_handler(func=lambda call: call.data.startswith("upf_"))
def upload_files(call):
    pid = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "📂 Пришлите текст, фото или PDF.")
    bot.register_next_step_handler(msg, process_upload, pid)

def process_upload(message, pid):
    content = message.text if message.text else "File/Photo content"
    check = get_gemini_response(f"Это полезно для SEO? Если нет ответь МУСОР. Текст: {content[:500]}")
    
    if "МУСОР" in check.upper():
        bot.reply_to(message, "⚠️ Это не полезная информация.")
    else:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
        kb = cur.fetchone()[0]; 
        if not kb: kb = []
        kb.append(f"User Upload: {check}")
        cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
        conn.commit(); cur.close(); conn.close()
        update_project_progress(pid, "upload_done")
        bot.reply_to(message, "✅ Добавлено в базу знаний.")
    open_project_menu(message.chat.id, pid, mode="management")

# КЛЮЧИ (С ИСПРАВЛЕННЫМ ФОРМАТОМ)
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
    
    prompt = f"""
    Твоя задача: Составь список из {count} SEO ключевых слов для сайта {res[1]}.
    Контекст из опроса: {survey_text}
    База знаний: {kb_text}
    
    ВАЖНО: Формат вывода должен быть СТРОГО таким (без лишних описаний и болтовни):
    
    **Высокая частотность:**
    - слово 1
    - слово 2
    
    **Средняя частотность:**
    - слово 3
    - слово 4
    
    **Низкая частотность:**
    - слово 5
    - слово 6
    
    Учти региональность из опроса.
    """
    
    keywords = get_gemini_response(prompt)
    
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    if len(keywords) > 4000:
        bot.send_message(call.message.chat.id, keywords[:4000])
        bot.send_message(call.message.chat.id, keywords[4000:])
    else:
        bot.send_message(call.message.chat.id, keywords)
    open_project_menu(call.message.chat.id, pid, mode="management")

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
        bot.send_message(call.message.chat.id, "⚙️ Платформа сайта?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_set_"))
def cms_instruction(call):
    _, pid, platform = call.data.split("_")
    links = {"wp": "https://wordpress.org/documentation/article/application-passwords/", 
             "tilda": "https://help-ru.tilda.cc/api", 
             "bitrix": "https://dev.1c-bitrix.ru/learning/course/index.php?COURSE_ID=43&LESSON_ID=3533"}
    
    msg = bot.send_message(call.message.chat.id, f"📚 Инструкция для {platform.upper()}:\n{links.get(platform)}\n\n👇 **Пришлите ключ доступа в ответном сообщении:**")
    # Исправлена логика регистрации шага
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
    """Показывает 1 уровень: Тест / Месяц / Год"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🏎 Тест-драйв (500р)", callback_data="period_test"))
    markup.add(types.InlineKeyboardButton("📅 На Месяц", callback_data="period_month"))
    markup.add(types.InlineKeyboardButton("📆 На Год (-30%)", callback_data="period_year"))
    bot.send_message(user_id, "💎 Выберите период оплаты:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("period_"))
def tariff_period_select(call):
    p_type = call.data.split("_")[1]
    
    if p_type == "test":
        # Сразу к оплате
        process_tariff_selection(call, "Тест-драйв", 500)
    elif p_type == "month":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("Старт (1400р)", callback_data="buy_start_1m"),
                   types.InlineKeyboardButton("Профи (2500р)", callback_data="buy_pro_1m"),
                   types.InlineKeyboardButton("Агент (7500р)", callback_data="buy_agent_1m"),
                   types.InlineKeyboardButton("🔙 Назад", callback_data="back_periods"))
        bot.edit_message_text("📅 Тарифы на Месяц:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    elif p_type == "year":
        # Цены: 1400*12*0.7 = 11760
        p_start = 11760
        p_prof = 21000
        p_agent = 62999
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(f"Старт Год ({p_start}р)", callback_data="buy_start_1y"),
                   types.InlineKeyboardButton(f"Профи Год ({p_prof}р)", callback_data="buy_pro_1y"),
                   types.InlineKeyboardButton(f"Агент Год ({p_agent}р)", callback_data="buy_agent_1y"),
                   types.InlineKeyboardButton("🔙 Назад", callback_data="back_periods"))
        bot.edit_message_text("📆 Тарифы на Год (Выгода 30%):", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "back_periods")
def back_to_periods(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    show_tariff_periods(call.from_user.id)

def process_tariff_selection(call, name, price, code="test"):
    # Показываем методы оплаты
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
    # buy_start_1m
    parts = call.data.split("_")
    plan = parts[1] # start
    period = parts[2] # 1m
    
    prices = {
        "start_1m": 1400, "pro_1m": 2500, "agent_1m": 7500,
        "start_1y": 11760, "pro_1y": 21000, "agent_1y": 62999
    }
    key = f"{plan}_{period}"
    price = prices.get(key, 0)
    name = f"{plan.upper()} ({period})"
    
    process_tariff_selection(call, name, price, key)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def process_payment(call):
    # pay_rub_start_1m_1400
    parts = call.data.split("_")
    currency = parts[1]
    plan_code = f"{parts[2]}_{parts[3]}" if len(parts) > 4 else "test"
    try:
        amount = int(parts[-1])
    except: amount = 500
    
    # Запись платежа
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO payments (user_id, amount, currency, tariff_name) VALUES (%s, %s, %s, %s)", 
                (call.from_user.id, amount, currency, plan_code))
    
    # Обновление юзера
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
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Пополнить баланс", callback_data="period_test")) # Ведет на выбор тарифа
    bot.send_message(uid, txt, reply_markup=markup, parse_mode='Markdown')

def show_admin_panel(uid):
    conn = get_db_connection(); cur = conn.cursor()
    
    # 1. Онлайн (активные за 15 мин)
    cur.execute("SELECT count(*) FROM users WHERE last_active > NOW() - INTERVAL '15 minutes'")
    online = cur.fetchone()[0]
    
    # 2. Прибыль за месяц
    cur.execute("SELECT sum(amount) FROM payments WHERE currency='rub' AND created_at > date_trunc('month', CURRENT_DATE)")
    profit_rub = cur.fetchone()[0] or 0
    cur.execute("SELECT sum(amount) FROM payments WHERE currency='stars' AND created_at > date_trunc('month', CURRENT_DATE)")
    profit_stars = cur.fetchone()[0] or 0
    
    # 3. Статьи
    cur.execute("SELECT count(*) FROM articles WHERE status='published'")
    arts = cur.fetchone()[0]
    
    # 4. Тарифы
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
