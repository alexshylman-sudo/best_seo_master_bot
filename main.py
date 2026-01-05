import os
import threading
import time
import schedule
import psycopg2
import json
import requests
import datetime
import random
from bs4 import BeautifulSoup
from urllib.parse import urlparse
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
APP_URL = os.getenv("APP_URL") # URL вашего приложения на Render (например, https://bot.onrender.com)

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)

# Кэш состояний
user_states = {}

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

    # --- ВАЖНО: ЭТИ СТРОКИ УДАЛЯЮТ СТАРУЮ БАЗУ, ЧТОБЫ СОЗДАТЬ НОВУЮ С КОЛОНКОЙ PROGRESS ---
    print("⚠️ Обновляю структуру базы данных...")
    cur.execute("DROP TABLE IF EXISTS projects CASCADE")
    cur.execute("DROP TABLE IF EXISTS users CASCADE")
    cur.execute("DROP TABLE IF EXISTS articles CASCADE")
    # --------------------------------------------------------------------------------------

    # 1. Таблица пользователей
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
    
    # 2. Таблица проектов (ТУТ ТЕПЕРЬ ЕСТЬ progress)
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

    # Предустановка Админа
    cur.execute("INSERT INTO users (user_id, is_admin, tariff, gens_left) VALUES (%s, TRUE, 'GOD_MODE', 9999) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    
    # Проекты Админа (Восстанавливаем их)
    admin_projects = [('site', 'https://designservice.group/'), ('site', 'https://ecosteni.ru/')]
    for p_type, p_url in admin_projects:
        # Сначала создаем "болванку", если проекта нет
        cur.execute("SELECT id FROM projects WHERE user_id = %s AND url = %s", (ADMIN_ID, p_url))
        if not cur.fetchone():
            cur.execute("INSERT INTO projects (user_id, type, url, info, progress) VALUES (%s, %s, %s, '{}', '{}')", (ADMIN_ID, p_type, p_url))

    conn.commit()
    cur.close()
    conn.close()
    print("✅ БД успешно пересоздана с новыми колонками.")

# --- 3. УТИЛИТЫ ---
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
    # Эмуляция глубокого прохода (парсим главную + ищем ссылки)
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Bot"})
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Основной контент
        title = soup.title.string if soup.title else "No Title"
        meta = soup.find("meta", attrs={"name": "description"})
        desc = meta["content"] if meta else "No Description"
        
        # Поиск внутренних ссылок для "глубины"
        links = [a['href'] for a in soup.find_all('a', href=True) if a['href'].startswith('/') or url in a['href']]
        structure_hint = f"Найдено {len(links)} внутренних страниц."
        
        raw_text = soup.get_text()[:2000].strip()
        return f"URL: {url}\nTitle: {title}\nDesc: {desc}\nStructure: {structure_hint}\nContent Sample: {raw_text}"
    except Exception as e:
        return f"Ошибка доступа: {e}"

def update_project_progress(pid, step_key):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT progress FROM projects WHERE id=%s", (pid,))
    prog = cur.fetchone()[0]
    if not prog: prog = {}
    prog[step_key] = True
    cur.execute("UPDATE projects SET progress=%s WHERE id=%s", (json.dumps(prog), pid))
    conn.commit(); cur.close(); conn.close()

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

# --- 5. ПРОЕКТЫ И ЛОГИКА ---
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
        markup.add(types.InlineKeyboardButton(f"🌐 {p[1]}", callback_data=f"open_proj_mgmt_{p[0]}")) # mgmt = management mode
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

# УНИВЕРСАЛЬНОЕ МЕНЮ ПРОЕКТА
def open_project_menu(chat_id, pid, mode="management", msg_id=None):
    # mode="onboarding" - скрываем пройденные этапы
    # mode="management" - показываем всё
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, keywords, progress FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    if not res: return
    
    url, kw_db, progress = res
    if not progress: progress = {}
    has_keywords = kw_db is not None and len(kw_db) > 5

    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Кнопки этапов
    btn_info = types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"srv_{pid}")
    btn_anal = types.InlineKeyboardButton("📊 Анализ сайта (Глубокий)", callback_data=f"anz_{pid}")
    btn_upl = types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upf_{pid}")
    
    # Логика скрытия кнопок для НОВОГО проекта
    if mode == "onboarding":
        if not progress.get("info_done"): markup.add(btn_info)
        if not progress.get("analysis_done"): markup.add(btn_anal)
        if not progress.get("upload_done"): markup.add(btn_upl)
    else:
        # В режиме "Мои проекты" показываем всё
        markup.add(btn_info, btn_anal, btn_upl)

    # Ключевые слова
    if has_keywords:
        markup.row(types.InlineKeyboardButton("❌ Удалить ключи", callback_data=f"delkw_{pid}"),
                   types.InlineKeyboardButton("🚀 Стратегия и Статьи", callback_data=f"strat_{pid}"))
    else:
        markup.add(types.InlineKeyboardButton("🔑 Подобрать ключевые слова", callback_data=f"kw_ask_count_{pid}"))

    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))

    text = f"📂 **Проект:** {url}\nРежим: {'Первичная настройка' if mode=='onboarding' else 'Управление'}"
    if msg_id:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_mgmt_"))
def open_proj_mgmt(call):
    pid = call.data.split("_")[3]
    open_project_menu(call.message.chat.id, pid, mode="management", message_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Главное меню", reply_markup=main_menu_markup(call.from_user.id))

# --- 6. ФУНКЦИОНАЛ ПРОЕКТА ---

# A. ОПРОСНИК (5 вопросов)
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
    # Сохраняем в Info
    cur.execute("UPDATE projects SET info = %s WHERE id=%s", (json.dumps({"survey": full_text}, ensure_ascii=False), d["pid"]))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(d["pid"], "info_done")
    bot.send_message(m.chat.id, "✅ Ответы сохранены!")
    # Определяем режим возврата. Если это была настройка, вернемся в onboarding
    open_project_menu(m.chat.id, d["pid"], mode="management") # Для простоты возвращаем в полный режим, чтобы видеть результат

# B. ГЛУБОКИЙ АНАЛИЗ
@bot.callback_query_handler(func=lambda call: call.data.startswith("anz_"))
def deep_analysis(call):
    pid = call.data.split("_")[1]
    bot.answer_callback_query(call.id, "Начинаю сканирование страниц...")
    msg = bot.send_message(call.message.chat.id, "🕵️‍♂️ Сканирую структуру сайта и контент. Это может занять до 30 секунд...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
    url = cur.fetchone()[0]
    
    raw_data = deep_analyze_site(url)
    ai_prompt = f"Ты SEO профессионал. Проведи аудит на основе этих данных с сайта (скан главной + структура): {raw_data}. Дай 3 критических ошибки и 3 точки роста."
    advice = get_gemini_response(ai_prompt)
    
    # Сохраняем в Knowledge Base
    cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
    kb = cur.fetchone()[0]; 
    if not kb: kb = []
    kb.append(f"Deep Audit: {advice[:500]}")
    cur.execute("UPDATE projects SET knowledge_base=%s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
    conn.commit(); cur.close(); conn.close()
    
    update_project_progress(pid, "analysis_done")
    bot.delete_message(call.message.chat.id, msg.message_id)
    bot.send_message(call.message.chat.id, f"📊 **Результат аудита:**\n\n{advice}", parse_mode='Markdown')
    open_project_menu(call.message.chat.id, pid, mode="management")

# C. ЗАГРУЗКА
@bot.callback_query_handler(func=lambda call: call.data.startswith("upf_"))
def upload_files(call):
    pid = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "📂 Пришлите текст, фото или PDF. Я оценю полезность.")
    bot.register_next_step_handler(msg, process_upload, pid)

def process_upload(message, pid):
    content = "File Content Placeholder"
    if message.text: content = message.text
    
    check = get_gemini_response(f"Это полезно для SEO сайта? Если нет ответь МУСОР. Если да, кратко суть. Текст: {content[:500]}")
    
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

# D. КЛЮЧЕВЫЕ СЛОВА (Выбор количества)
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
    
    bot.edit_message_text(f"🧠 Подбираю {count} ключевых слов с частотностью...", call.message.chat.id, call.message.message_id)
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT knowledge_base, url FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    kb_text = str(res[0])[:2000]
    
    prompt = f"Составь список из {count} SEO ключевых слов для сайта {res[1]}. База: {kb_text}. Формат: 'Ключевое слово - Частотность (Высокая/Средняя/Низкая)'. Верни список."
    keywords = get_gemini_response(prompt)
    
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    # Отправляем частями, если длинно
    if len(keywords) > 4000:
        bot.send_message(call.message.chat.id, keywords[:4000])
        bot.send_message(call.message.chat.id, keywords[4000:])
    else:
        bot.send_message(call.message.chat.id, keywords)
        
    open_project_menu(call.message.chat.id, pid, mode="management")

# --- 7. СТРАТЕГИЯ И CMS ---

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
        bot.send_message(call.message.chat.id, "⚙️ На какой платформе ваш сайт?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cms_set_"))
def cms_instruction(call):
    _, pid, platform = call.data.split("_") # cms, set, pid, platform
    
    instructions = {
        "wp": "https://wordpress.org/documentation/article/application-passwords/",
        "tilda": "https://help-ru.tilda.cc/api",
        "bitrix": "https://dev.1c-bitrix.ru/learning/course/index.php?COURSE_ID=43&LESSON_ID=3533"
    }
    link = instructions.get(platform, "google.com")
    
    msg = bot.send_message(call.message.chat.id, f"📚 **Инструкция для {platform.upper()}**\n\n1. Перейдите по ссылке: {link}\n2. Создайте ключ доступа.\n3. Пришлите ключ мне в ответном сообщении.", parse_mode='Markdown')
    bot.register_next_step_handler(msg, save_cms_key, pid, platform)

def save_cms_key(message, pid, platform):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_key=%s, platform=%s WHERE id=%s", (message.text, platform, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Доступ сохранен!")
    propose_articles(message.chat.id, pid)

def propose_articles(chat_id, pid):
    bot.send_message(chat_id, "🤖 Генерирую темы для первых статей...")
    titles_raw = get_gemini_response("Придумай 2 SEO заголовка для статей. Раздели символом |")
    titles = titles_raw.split("|")
    if len(titles) < 2: titles = ["Тема 1", "Тема 2"]
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"1. {titles[0].strip()[:20]}...", callback_data=f"write_{pid}_0"),
               types.InlineKeyboardButton(f"2. {titles[1].strip()[:20]}...", callback_data=f"write_{pid}_1"),
               types.InlineKeyboardButton("🔄 Показать еще 2", callback_data=f"more_titles_{pid}"))
    
    bot.send_message(chat_id, f"Выбери тему:\n1. {titles[0]}\n2. {titles[1]}", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article(call):
    # Логика написания -> nana banana -> утверждение
    pid = call.data.split("_")[1]
    wait = bot.send_message(call.message.chat.id, "✍️ Пишу статью и рисую картинку...")
    
    text = get_gemini_response("Напиши SEO статью на выбранную тему. 1500 знаков.")
    
    # Картинка Nana Banana
    img_prompt = get_gemini_response("Prompt for image generation 3 words english")
    img_url = f"https://api.nanobanana.pro/v1/generate?prompt={img_prompt[:50]}"
    
    # Сохраняем черновик
    fake_link = f"http://site.com/draft-{int(time.time())}"
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO articles (project_id, content, published_url, status) VALUES (%s, %s, %s, 'pending') RETURNING id", (pid, text, fake_link))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("✏️ Переписать (1 раз)", callback_data=f"rewrite_{aid}"))
    
    bot.delete_message(call.message.chat.id, wait.message_id)
    try:
        bot.send_photo(call.message.chat.id, img_url, caption=f"Статья готова!\n{text[:100]}...\n\n🔗 {fake_link}", reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, f"Статья готова (картинка не загрузилась)!\n{text[:100]}...", reply_markup=markup)

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
    # Здесь повтор генерации
    bot.send_message(call.message.chat.id, "✅ Новая версия готова. Утверждаем?", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Утвердить", callback_data=f"approve_{aid}")))

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE articles SET status='published' WHERE id=%s", (aid,))
    conn.commit(); cur.close(); conn.close()
    bot.edit_message_caption("✅ **Опубликовано на сайте!**", call.message.chat.id, call.message.message_id)

# --- 8. ТАРИФЫ И АДМИНКА ---

def show_tariffs(user_id):
    # Расчет годовых цен (Цена * 12 * 0.7)
    p_start_y = int(1400 * 12 * 0.7)
    p_prof_y = int(2500 * 12 * 0.7)
    p_agent_y = int(7500 * 12 * 0.7)
    
    txt = (f"💎 **ТАРИФЫ**\n\n"
           f"1️⃣ **Тест-драйв** (500р) - 5 ген.\n\n"
           f"2️⃣ **СЕО Старт**\nМесяц: 1400р | Год: {p_start_y}р (-30%)\n\n"
           f"3️⃣ **СЕО Профи**\nМесяц: 2500р | Год: {p_prof_y}р (-30%)\n\n"
           f"4️⃣ **PBN Агент**\nМесяц: 7500р | Год: {p_agent_y}р (-30%)")
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    # Месячные
    markup.add(types.InlineKeyboardButton("Тест (500р)", callback_data="buy_test"),
               types.InlineKeyboardButton("Старт (1400р)", callback_data="buy_start_1m"))
    markup.add(types.InlineKeyboardButton("Профи (2500р)", callback_data="buy_pro_1m"),
               types.InlineKeyboardButton("Агент (7500р)", callback_data="buy_agent_1m"))
    # Годовые
    markup.add(types.InlineKeyboardButton(f"Старт ГОД ({p_start_y}р)", callback_data="buy_start_1y"))
    markup.add(types.InlineKeyboardButton(f"Профи ГОД ({p_prof_y}р)", callback_data="buy_pro_1y"))
    markup.add(types.InlineKeyboardButton(f"Агент ГОД ({p_agent_y}р)", callback_data="buy_agent_1y"))
    
    bot.send_message(user_id, txt, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def payment_method(call):
    plan = call.data
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Картой (РФ)", callback_data=f"pay_rub_{plan}"),
               types.InlineKeyboardButton("⭐ Telegram Stars", callback_data=f"pay_star_{plan}"))
    bot.edit_message_text("Выберите метод оплаты:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def process_payment(call):
    # pay_rub_buy_start_1y
    parts = call.data.split("_")
    currency = parts[1]
    plan_code = "_".join(parts[3:]) # start_1y
    
    # Симуляция оплаты
    amount = 500 # заглушка
    
    conn = get_db_connection(); cur = conn.cursor()
    # Обновляем статистику оплат
    col = "total_paid_rub" if currency == "rub" else "total_paid_stars"
    cur.execute(f"UPDATE users SET tariff=%s, {col}={col}+%s WHERE user_id=%s", (plan_code, amount, call.from_user.id))
    conn.commit(); cur.close(); conn.close()
    
    bot.send_message(call.message.chat.id, f"✅ Оплата прошла! Тариф {plan_code} активирован.")

def show_profile(uid):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT tariff, gens_left, balance FROM users WHERE user_id=%s", (uid,))
    u = cur.fetchone()
    # Статьи
    cur.execute("SELECT count(*) FROM articles WHERE status='published' AND project_id IN (SELECT id FROM projects WHERE user_id=%s)", (uid,))
    arts = cur.fetchone()[0]
    cur.close(); conn.close()
    
    txt = f"👤 **Профиль**\nТариф: {u[0]}\nГенераций: {u[1]}\nСтатей опубликовано: {arts}"
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Пополнить баланс", callback_data="buy_test"))
    bot.send_message(uid, txt, reply_markup=markup, parse_mode='Markdown')

def show_admin_panel(uid):
    conn = get_db_connection(); cur = conn.cursor()
    # Онлайн (примерно) - тут просто общее кол-во для примера
    cur.execute("SELECT count(*) FROM users")
    users_total = cur.fetchone()[0]
    # Прибыль
    cur.execute("SELECT sum(total_paid_rub), sum(total_paid_stars) FROM users")
    money = cur.fetchone()
    rub = money[0] if money[0] else 0
    stars = money[1] if money[1] else 0
    # Статьи
    cur.execute("SELECT count(*) FROM articles WHERE status='published'")
    arts = cur.fetchone()[0]
    cur.close(); conn.close()
    
    txt = f"⚙️ **АДМИНКА**\n\n👥 Пользователей: {users_total}\n💰 Прибыль: {rub} руб / {stars} stars\n📄 Статей всего: {arts}"
    bot.send_message(uid, txt)

# --- 9. KEEP ALIVE & SCHEDULER ---
def keep_alive():
    # Пингует сам себя каждые 14 минут
    while True:
        time.sleep(14 * 60) # 14 минут
        if APP_URL:
            try:
                requests.get(APP_URL)
                print("Ping sent to keep alive")
            except: pass

def run_scheduler():
    schedule.every().day.at("10:00").do(daily_warmup)
    
    # Запускаем пинговалку в отдельном потоке
    threading.Thread(target=keep_alive, daemon=True).start()
    
    while True:
        schedule.run_pending()
        time.sleep(60)

def daily_warmup():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE tariff='Нет тарифа' OR tariff IS NULL")
    users = cur.fetchall()
    msg = "🚀 **SEO сам себя не сделает!**\nПора заняться сайтом. Выберите тариф."
    for u in users:
        try: bot.send_message(u[0], msg); time.sleep(0.2)
        except: continue
    cur.close(); conn.close()

# --- ЗАПУСК ---
app = Flask(__name__)
@app.route('/')
def h(): return "Bot Active", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    bot.infinity_polling(skip_pending=True)
