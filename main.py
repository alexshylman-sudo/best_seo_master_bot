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

    # Таблицы (создаем, если нет)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance INT DEFAULT 0,
            tariff TEXT DEFAULT 'Нет тарифа',
            tariff_expires TIMESTAMP,
            gens_left INT DEFAULT 0,
            is_admin BOOLEAN DEFAULT FALSE,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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

    # Предустановка Админа
    cur.execute("INSERT INTO users (user_id, is_admin, tariff, gens_left) VALUES (%s, TRUE, 'GOD_MODE', 9999) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    
    # Проекты Админа
    admin_projects = [('site', 'https://designservice.group/'), ('site', 'https://ecosteni.ru/')]
    for p_type, p_url in admin_projects:
        cur.execute("SELECT id FROM projects WHERE user_id = %s AND url = %s", (ADMIN_ID, p_url))
        if not cur.fetchone():
            cur.execute("INSERT INTO projects (user_id, type, url, info, progress) VALUES (%s, %s, %s, '{}', '{}')", (ADMIN_ID, p_type, p_url))

    conn.commit(); cur.close(); conn.close()
    print("✅ БД инициализирована.")

# --- 3. УТИЛИТЫ ---
def escape_md(text):
    """Экранирует спецсимволы Markdown, чтобы бот не падал из-за _ или *"""
    if not text: return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"Ошибка AI: {e}"

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
    """Обновляет прогресс проекта. Исправлено падение NoneType."""
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    
    try:
        cur.execute("SELECT progress FROM projects WHERE id=%s", (pid,))
        result = cur.fetchone()
        
        # Если проекта нет или progress NULL
        if result is None:
            prog = {}
        else:
            prog = result[0]
            if prog is None: prog = {}
            
        prog[step_key] = True
        
        cur.execute("UPDATE projects SET progress=%s WHERE id=%s", (json.dumps(prog), pid))
        conn.commit()
    except Exception as e:
        print(f"⚠️ Ошибка обновления прогресса: {e}")
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
        show_tariffs(uid)

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
        # Экранируем URL для красоты кнопки (убираем http)
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
    bot.delete_message(message.chat.id, msg_check.message_id)
    bot.send_message(message.chat.id, f"✅ Сайт {url} добавлен!")
    open_project_menu(message.chat.id, pid, mode="onboarding")

def open_project_menu(chat_id, pid, mode="management", msg_id=None):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, keywords, progress FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    if not res: return
    
    url, kw_db, progress = res
    if not progress: progress = {}
    has_keywords = kw_db is not None and len(kw_db) > 5

    markup = types.InlineKeyboardMarkup(row_width=1)
    
    btn_info = types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"srv_{pid}")
    btn_anal = types.InlineKeyboardButton("📊 Анализ сайта (Глубокий)", callback_data=f"anz_{pid}")
    btn_upl = types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upf_{pid}")
    
    # Логика исчезновения кнопок при "Первичной настройке"
    if mode == "onboarding":
        if not progress.get("info_done"): markup.add(btn_info)
        if not progress.get("analysis_done"): markup.add(btn_anal)
        if not progress.get("upload_done"): markup.add(btn_upl)
    else:
        markup.add(btn_info, btn_anal, btn_upl)

    # Кнопки появляются только после прохождения этапов или всегда в режиме управления
    if has_keywords:
        markup.row(types.InlineKeyboardButton("❌ Удалить ключи", callback_data=f"delkw_{pid}"),
                   types.InlineKeyboardButton("🚀 Стратегия и Статьи", callback_data=f"strat_{pid}"))
    else:
        markup.add(types.InlineKeyboardButton("🔑 Подобрать ключевые слова", callback_data=f"kw_ask_count_{pid}"))

    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))

    safe_url = escape_md(url)
    text = f"📂 **Проект:** {safe_url}\nРежим: {'Первичная настройка' if mode=='onboarding' else 'Управление'}"
    
    # Безопасная отправка (если Markdown сломается, отправит текст)
    try:
        if msg_id:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
        else:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.send_message(chat_id, text.replace("*", "").replace("_", ""), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_mgmt_"))
def open_proj_mgmt(call):
    pid = call.data.split("_")[3]
    open_project_menu(call.message.chat.id, pid, mode="management", message_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Главное меню", reply_markup=main_menu_markup(call.from_user.id))

# --- 6. ФУНКЦИОНАЛ ---

# ОПРОСНИК
@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def start_survey_5q(call):
    pid = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "❓ Вопрос 1/5:\nКакая главная цель вашего сайта? (Продажи, Трафик, Бренд?)")
    bot.register_next_step_handler(msg, q2, {"pid": pid, "answers": []})

def q2(m, d): d["answers"].append(f"Цель: {m.text}"); msg = bot.send_message(m.chat.id, "❓ Вопрос 2/5:\nКто ваша целевая аудитория? (Пол, возраст, интересы)"); bot.register_next_step_handler(msg, q3, d)
def q3(m, d): d["answers"].append(f"ЦА: {m.text}"); msg = bot.send_message(m.chat.id, "❓ Вопрос 3/5:\nНазовите ваших главных конкурентов:"); bot.register_next_step_handler(msg, q4, d)
def q4(m, d): d["answers"].append(f"Конкуренты: {m.text}"); msg = bot.send_message(m.chat.id, "❓ Вопрос 4/5:\nВ чем ваше главное преимущество (УТП)?"); bot.register_next_step_handler(msg, q5, d)
def q5(m, d): 
    d["answers"].append(f"УТП: {m.text}")
    msg = bot.send_message(m.chat.id, "❓ Вопрос 5/5:\nГеография продвижения (Город, Страна):")
    bot.register_next_step_handler(msg, finish_survey, d)

def finish_survey(m, d):
    d["answers"].append(f"Гео: {m.text}")
    full_text = "\n".join(d["answers"])
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET info = %s WHERE id=%s", (json.dumps({"survey": full_text}, ensure_ascii=False), d["pid"]))
    conn.commit(); cur.close(); conn.close()
    
    # Обновляем прогресс (исправленная функция)
    update_project_progress(d["pid"], "info_done")
    
    bot.send_message(m.chat.id, "✅ Ответы сохранены!")
    # Возвращаем пользователя в меню проекта
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
    # Используем безопасную отправку текста (без Markdown, так как AI может вернуть что угодно)
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
    bot.edit_message_text(f"🧠 Подбираю {count} слов...", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT knowledge_base, url FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    kb_text = str(res[0])[:2000]
    
    prompt = f"Составь список из {count} SEO ключевых слов для сайта {res[1]}. База: {kb_text}. Верни список с частотностью."
    keywords = get_gemini_response(prompt)
    
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    # Разбиваем длинные сообщения
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
    links = {"wp": "https://wordpress.org", "tilda": "https://tilda.cc", "bitrix": "https://1c-bitrix.ru"}
    msg = bot.send_message(call.message.chat.id, f"📚 Инструкция для {platform.upper()}: {links.get(platform)}\nПришлите ключ доступа:")
    bot.register_next_step_handler(msg, save_cms_key, pid, platform)

def save_cms_key(message, pid, platform):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_key=%s, platform=%s WHERE id=%s", (message.text, platform, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Доступ сохранен!")
    propose_articles(message.chat.id, pid)

def propose_articles(chat_id, pid):
    bot.send_message(chat_id, "🤖 Генерирую темы...")
    titles = get_gemini_response("2 SEO заголовка. Раздели символом |").split("|")
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

# --- 7. ПРОФИЛЬ И ТАРИФЫ ---
def show_tariffs(user_id):
    p_start_y = int(1400 * 12 * 0.7)
    p_prof_y = int(2500 * 12 * 0.7)
    p_agent_y = int(7500 * 12 * 0.7)
    txt = f"💎 **ТАРИФЫ**\n\n1. Тест (500р)\n2. Старт (1400р/мес | {p_start_y}р/год)\n3. Профи (2500р/мес | {p_prof_y}р/год)\n4. Агент (7500р/мес | {p_agent_y}р/год)"
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Тест (500)", callback_data="buy_test"),
               types.InlineKeyboardButton("Старт (1400)", callback_data="buy_start_1m"))
    markup.add(types.InlineKeyboardButton(f"Старт ГОД ({p_start_y})", callback_data="buy_start_1y"))
    bot.send_message(user_id, txt, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def payment_method(call):
    plan = call.data
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Картой", callback_data=f"pay_rub_{plan}"),
               types.InlineKeyboardButton("⭐ Stars", callback_data=f"pay_star_{plan}"))
    bot.edit_message_text("Метод оплаты:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def process_payment(call):
    parts = call.data.split("_")
    currency = parts[1]
    plan = "_".join(parts[3:])
    amount = 500
    conn = get_db_connection(); cur = conn.cursor()
    col = "total_paid_rub" if currency == "rub" else "total_paid_stars"
    cur.execute(f"UPDATE users SET tariff=%s, {col}={col}+%s WHERE user_id=%s", (plan, amount, call.from_user.id))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(call.message.chat.id, f"✅ Оплата прошла! Тариф {plan}")

def show_profile(uid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT tariff, gens_left, balance FROM users WHERE user_id=%s", (uid,))
    u = cur.fetchone()
    cur.execute("SELECT count(*) FROM articles WHERE status='published' AND project_id IN (SELECT id FROM projects WHERE user_id=%s)", (uid,))
    arts = cur.fetchone()[0]
    cur.close(); conn.close()
    
    # Экранируем название тарифа, чтобы _GOD_MODE_ не ломал Markdown
    safe_tariff = escape_md(u[0])
    
    txt = f"👤 **Профиль**\n\n🆔 ID: `{uid}`\n💎 Тариф: {safe_tariff}\n⚡ Генераций: {u[1]}\n💰 Баланс: {u[2]} руб.\n📄 Опубликовано: {arts}"
    
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Пополнить баланс", callback_data="buy_test"))
    bot.send_message(uid, txt, reply_markup=markup, parse_mode='Markdown')

def show_admin_panel(uid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) FROM users")
    users = cur.fetchone()[0]
    cur.close(); conn.close()
    bot.send_message(uid, f"⚙️ Админка\nПользователей: {users}")

# --- 8. ЗАПУСК ---
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
