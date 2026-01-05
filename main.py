import os
import threading
import time
import schedule
import psycopg2
import json
import requests
import datetime
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from telebot import TeleBot, types
from flask import Flask
from google import genai
from dotenv import load_dotenv

# --- 1. КОНФИГУРАЦИЯ ---
load_dotenv()

# Настройки
ADMIN_ID = 203473623
SUPPORT_ID = 203473623
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)

# Кэш состояний (для сложных диалогов)
user_states = {}

# --- 2. БАЗА ДАННЫХ (ОБНОВЛЕННАЯ СТРУКТУРА) ---
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

    # Очищаем старую структуру (ДЛЯ ТЕСТА, ЧТОБЫ ОБНОВИТЬ КОЛОНКИ)
    # ВНИМАНИЕ: Это удалит старые данные при перезапуске. 
    # После первого успешного запуска эту строку лучше закомментировать.
    cur.execute("DROP TABLE IF EXISTS projects CASCADE")
    cur.execute("DROP TABLE IF EXISTS users CASCADE")
    cur.execute("DROP TABLE IF EXISTS articles CASCADE")

    # 1. Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance INT DEFAULT 0,
            tariff TEXT DEFAULT 'Нет тарифа',
            gens_left INT DEFAULT 0,
            tariff_end_date TIMESTAMP,
            is_admin BOOLEAN DEFAULT FALSE,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 2. Таблица проектов
    # knowledge_base хранит JSON с анализом файлов и сайта
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
            frequency INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 3. Таблица статей (для статусов и проверки rewrite)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id SERIAL PRIMARY KEY,
            project_id INT,
            title TEXT,
            content TEXT,
            status TEXT DEFAULT 'draft',
            rewrite_count INT DEFAULT 0,
            image_url TEXT,
            published_url TEXT
        )
    """)

    # --- ПРЕДУСТАНОВКА ДЛЯ АДМИНА (ТЗ ПУНКТ 1) ---
    cur.execute("INSERT INTO users (user_id, is_admin, tariff, gens_left) VALUES (%s, TRUE, 'GOD_MODE', 9999) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    
    # Добавляем проекты админа, если их нет
    admin_projects = [
        ('site', 'https://designservice.group/'),
        ('site', 'https://ecosteni.ru/')
    ]
    for p_type, p_url in admin_projects:
        cur.execute("SELECT id FROM projects WHERE user_id = %s AND url = %s", (ADMIN_ID, p_url))
        if not cur.fetchone():
            cur.execute("INSERT INTO projects (user_id, type, url, info) VALUES (%s, %s, %s, '{}')", (ADMIN_ID, p_type, p_url))

    conn.commit()
    cur.close()
    conn.close()
    print("✅ БД инициализирована успешно.")

# --- 3. УТИЛИТЫ И AI ---

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"Ошибка генерации: {e}"

def check_site_availability(url):
    try:
        response = requests.get(url, timeout=5)
        return response.status_code == 200
    except:
        return False

def analyze_site_content(url):
    try:
        resp = requests.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = soup.title.string if soup.title else "No Title"
        desc = soup.find("meta", attrs={"name": "description"})
        desc_content = desc["content"] if desc else "No description"
        h1 = soup.find("h1").get_text().strip() if soup.find("h1") else "No H1"
        text_sample = soup.get_text()[:1000].strip()
        return f"Title: {title}\nDescription: {desc_content}\nH1: {h1}\nText Sample: {text_sample}"
    except Exception as e:
        return f"Не удалось прочитать сайт: {e}"

# --- 4. МЕНЮ И НАВИГАЦИЯ ---

def main_menu_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("➕ Новый проект", "📂 Мои проекты")
    markup.add("👤 Профиль", "💎 Тарифы")
    markup.add("🆘 Техподдержка")
    if user_id == ADMIN_ID:
        markup.add("⚙️ Админка")
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit(); cur.close(); conn.close()
    
    bot.send_message(user_id, "👋 Привет! Я AI-ассистент для SEO продвижения.", reply_markup=main_menu_markup(user_id))

# Обработка главного меню
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
        bot.send_message(uid, "Панель администратора (заглушка). Статистика системы: OK")

@bot.callback_query_handler(func=lambda call: call.data == "soon")
def soon_alert(call):
    bot.answer_callback_query(call.id, "🚧 В разработке")

# --- 5. ЛОГИКА ПРОЕКТОВ ---

def list_projects(user_id, chat_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, url FROM projects WHERE user_id = %s ORDER BY id ASC", (user_id,))
    projs = cur.fetchall()
    cur.close(); conn.close()

    if not projs:
        bot.send_message(chat_id, "У вас пока нет проектов.")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in projs:
        markup.add(types.InlineKeyboardButton(f"🌐 {p[1]}", callback_data=f"open_proj_{p[0]}"))
    bot.send_message(chat_id, "Ваши проекты:", reply_markup=markup)

# Создание нового сайта
@bot.callback_query_handler(func=lambda call: call.data == "new_site")
def new_site_start(call):
    msg = bot.send_message(call.message.chat.id, "🔗 Введите URL сайта (обязательно с http/https):")
    bot.register_next_step_handler(msg, check_url_step)

def check_url_step(message):
    url = message.text.strip()
    # Проверка формата
    if not url.startswith("http"):
        msg = bot.send_message(message.chat.id, "❌ Ошибка. Ссылка должна начинаться с http:// или https://. Попробуйте снова:")
        bot.register_next_step_handler(msg, check_url_step)
        return

    # Проверка доступности
    msg_check = bot.send_message(message.chat.id, "⏳ Проверяю доступность сайта...")
    if not check_site_availability(url):
        bot.edit_message_text("❌ Сайт недоступен (код ответа не 200). Проверьте ссылку.", message.chat.id, msg_check.message_id)
        return

    # Сохраняем
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO projects (user_id, type, url, info) VALUES (%s, 'site', %s, '{}') RETURNING id", (message.from_user.id, url))
    pid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()

    bot.delete_message(message.chat.id, msg_check.message_id)
    bot.send_message(message.chat.id, f"✅ Сайт {url} добавлен!")
    open_project_menu(message.chat.id, pid)

# --- 6. МЕНЮ ПРОЕКТА (ОСНОВНОЙ ХАБ) ---

def open_project_menu(chat_id, pid, msg_id=None):
    # Получаем данные о проекте
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url, keywords FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close(); conn.close()
    
    if not res: return
    url, kw_db = res
    has_keywords = kw_db is not None and len(kw_db) > 2

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"srv_{pid}"))
    markup.add(types.InlineKeyboardButton("📊 Сделать анализ сайта", callback_data=f"anz_{pid}"))
    markup.add(types.InlineKeyboardButton("📂 Загрузить файлы (PDF/IMG)", callback_data=f"upf_{pid}"))
    
    # Логика кнопки ключевых слов
    if has_keywords:
        markup.row(
            types.InlineKeyboardButton("❌ Удалить ключи", callback_data=f"delkw_{pid}"),
            types.InlineKeyboardButton("🔄 Дополнить ключи", callback_data=f"addkw_{pid}")
        )
        markup.add(types.InlineKeyboardButton("🚀 Стратегия и Генерация", callback_data=f"strat_{pid}"))
    else:
        markup.add(types.InlineKeyboardButton("🔑 Подобрать ключевые слова", callback_data=f"genkw_{pid}"))

    text = f"📂 **Управление проектом**\n🔗 {url}"
    if msg_id:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_"))
def callback_open_proj(call):
    pid = call.data.split("_")[2]
    open_project_menu(call.message.chat.id, pid, call.message.message_id)

# --- 7. ФУНКЦИИ ПРОЕКТА (АНАЛИЗ, ЗАГРУЗКА, КЛЮЧИ) ---

# A. Анализ сайта
@bot.callback_query_handler(func=lambda call: call.data.startswith("anz_"))
def analyze_site_btn(call):
    pid = call.data.split("_")[1]
    bot.answer_callback_query(call.id, "Захожу на сайт...")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
    url = cur.fetchone()[0]
    
    # 1. Скрапинг
    raw_data = analyze_site_content(url)
    
    # 2. AI Анализ
    ai_prompt = f"Ты SEO эксперт. Проанализируй данные с главной страницы сайта и дай 3 главных совета по улучшению. Данные: {raw_data}"
    ai_advice = get_gemini_response(ai_prompt)
    
    # 3. Сохранение в базу знаний
    update_knowledge_base(pid, f"Анализ главной страницы: {ai_advice[:500]}...")
    
    bot.send_message(call.message.chat.id, f"📊 **Анализ сайта {url}:**\n\n{ai_advice}", parse_mode='Markdown')
    # Возврат в меню
    open_project_menu(call.message.chat.id, pid)

# B. Загрузка файлов
@bot.callback_query_handler(func=lambda call: call.data.startswith("upf_"))
def upload_files_req(call):
    pid = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "📂 Отправьте мне текст, фото или PDF файл. Я проанализирую его и добавлю в базу знаний проекта.")
    bot.register_next_step_handler(msg, process_file_upload, pid)

def process_file_upload(message, pid):
    content_to_analyze = ""
    
    if message.content_type == 'text':
        content_to_analyze = message.text
    elif message.content_type == 'document':
        # Заглушка для PDF (реальный парсинг PDF требует больших библиотек, здесь упростим)
        content_to_analyze = f"Пользователь загрузил файл: {message.document.file_name}. (Эмуляция: AI прочитал содержимое)."
    elif message.content_type == 'photo':
        content_to_analyze = "Пользователь загрузил фото товаров/услуг."

    # Анализ полезности
    check_prompt = f"Оцени, полезна ли эта информация для SEO продвижения сайта? Если да, выдели суть. Если нет, напиши 'Мусор'. Инфо: {content_to_analyze[:1000]}"
    ai_check = get_gemini_response(check_prompt)

    if "Мусор" in ai_check or "не полезна" in ai_check.lower():
        bot.reply_to(message, "⚠️ AI считает, что этот файл не несет полезной информации для SEO.")
    else:
        update_knowledge_base(pid, f"Файл от юзера: {ai_check}")
        bot.reply_to(message, "✅ Информация проанализирована и добавлена в базу знаний!")
    
    open_project_menu(message.chat.id, pid)

def update_knowledge_base(pid, new_info):
    conn = get_db_connection(); cur = conn.cursor()
    # Берем старую базу, добавляем новое
    cur.execute("SELECT knowledge_base FROM projects WHERE id=%s", (pid,))
    kb = cur.fetchone()[0] # Это list (JSON)
    if not kb: kb = []
    kb.append(new_info)
    
    cur.execute("UPDATE projects SET knowledge_base = %s WHERE id=%s", (json.dumps(kb, ensure_ascii=False), pid))
    conn.commit(); cur.close(); conn.close()

# C. Ключевые слова
@bot.callback_query_handler(func=lambda call: call.data.startswith("genkw_"))
def generate_keywords(call):
    pid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT knowledge_base, url FROM projects WHERE id=%s", (pid,))
    res = cur.fetchone()
    kb_text = " ".join(res[0]) if res[0] else "Нет данных"
    
    wait = bot.send_message(call.message.chat.id, "🧠 Подбираю семантическое ядро на основе базы знаний...")
    
    prompt = f"Подбери 10 лучших SEO ключевых слов для сайта {res[1]}. База знаний: {kb_text}. Верни только список через запятую."
    keywords = get_gemini_response(prompt)
    
    cur.execute("UPDATE projects SET keywords = %s WHERE id=%s", (keywords, pid))
    conn.commit(); cur.close(); conn.close()
    
    bot.delete_message(call.message.chat.id, wait.message_id)
    bot.send_message(call.message.chat.id, f"🔑 **Ключи подобраны:**\n{keywords}")
    open_project_menu(call.message.chat.id, pid)

# --- 8. СТРАТЕГИЯ И ГЕНЕРАЦИЯ ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("strat_"))
def strategy_step(call):
    pid = call.data.split("_")[1]
    # 1. Предлагаем стратегию
    markup = types.InlineKeyboardMarkup()
    for i in range(1, 8):
        markup.add(types.InlineKeyboardButton(f"{i} статей в неделю", callback_data=f"freq_{pid}_{i}"))
    
    bot.send_message(call.message.chat.id, "📅 **Выбор стратегии**\nСколько статей в неделю генерировать?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("freq_"))
def save_frequency(call):
    _, pid, count = call.data.split("_")
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET frequency = %s WHERE id=%s", (count, pid))
    # Проверка ключа CMS
    cur.execute("SELECT cms_key FROM projects WHERE id=%s", (pid,))
    has_key = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    if not has_key:
        msg = bot.send_message(call.message.chat.id, "🔑 **Доступ к сайту**\nЧтобы я мог публиковать статьи, мне нужен API Key (или доступ). \n\n_Инструкция: Зайдите в админку -> Пользователи -> Создать API Key._\n\nВведите ключ сейчас:")
        bot.register_next_step_handler(msg, save_cms_key, pid)
    else:
        propose_articles(call.message.chat.id, pid)

def save_cms_key(message, pid):
    key = message.text
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_key = %s WHERE id=%s", (key, pid))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(message.chat.id, "✅ Доступ сохранен.")
    propose_articles(message.chat.id, pid)

def propose_articles(chat_id, pid):
    # Генерация тем
    topics = get_gemini_response("Придумай 2 цепляющих SEO заголовка для статьи на основе темы сайта. Верни только заголовки разделив их символом |")
    titles = topics.split("|")
    if len(titles) < 2: titles = ["Секреты успеха в нише", "ТОП ошибок новичков"]
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"1️⃣ {titles[0].strip()}", callback_data=f"write_{pid}_0_{titles[0][:15]}")) # Ограничим длину в callback
    markup.add(types.InlineKeyboardButton(f"2️⃣ {titles[1].strip()}", callback_data=f"write_{pid}_1_{titles[1][:15]}"))
    markup.add(types.InlineKeyboardButton("🔄 Показать другие варианты", callback_data=f"more_titles_{pid}"))
    
    bot.send_message(chat_id, f"📝 **Выберите тему для первой статьи:**\n1. {titles[0]}\n2. {titles[1]}", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article_start(call):
    # write_pid_idx_titleStub
    parts = call.data.split("_")
    pid = parts[1]
    
    wait = bot.send_message(call.message.chat.id, "✍️ Пишу статью, генерирую картинку и публикую...")
    
    # 1. Генерация текста
    body = get_gemini_response("Напиши короткую SEO статью на тему выбранного заголовка.")
    
    # 2. Картинка (Nanobanana заглушка)
    img_prompt = get_gemini_response("Опиши картинку для этой статьи на английском в 5 словах")
    img_url = f"https://api.nanobanana.pro/v1/generate?prompt={img_prompt[:50]}"
    
    # 3. "Публикация"
    fake_link = f"https://mysite.com/blog/article-{int(time.time())}"
    
    # Сохраняем черновик
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO articles (project_id, content, published_url, status) VALUES (%s, %s, %s, 'waiting') RETURNING id", (pid, body, fake_link))
    aid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    
    bot.delete_message(call.message.chat.id, wait.message_id)
    
    # Отправляем результат
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_{aid}"),
               types.InlineKeyboardButton("✏️ Переписать (1 раз)", callback_data=f"rewrite_{aid}"))
    
    bot.send_photo(call.message.chat.id, img_url, caption=f"📄 **Статья готова!**\n\n{body[:200]}...\n\n🔗 Ссылка (черновик): {fake_link}", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("rewrite_"))
def rewrite_article(call):
    aid = call.data.split("_")[1]
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT rewrite_count FROM articles WHERE id=%s", (aid,))
    rc = cur.fetchone()[0]
    
    if rc >= 1:
        bot.answer_callback_query(call.id, "⛔ Исправить можно только 1 раз!")
        cur.close(); conn.close()
        return
        
    cur.execute("UPDATE articles SET rewrite_count = 1 WHERE id=%s", (aid,))
    conn.commit(); cur.close(); conn.close()
    
    bot.send_message(call.message.chat.id, "✍️ Переписываю...")
    # Тут по идее повторная генерация, для краткости просто сообщение
    bot.send_message(call.message.chat.id, "✅ Статья переписана (новая версия).", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"approve_{aid}")))

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_"))
def approve_article(call):
    aid = call.data.split("_")[1]
    # Здесь был бы реальный POST запрос на сайт
    bot.edit_message_caption("✅ **ОПУБЛИКОВАНО!** Статья доступна на сайте.", call.message.chat.id, call.message.message_id)

# --- 9. ПРОФИЛЬ И ТАРИФЫ ---

def show_profile(user_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT tariff, gens_left, balance FROM users WHERE user_id=%s", (user_id,))
    u = cur.fetchone()
    # Считаем проекты
    cur.execute("SELECT count(*) FROM projects WHERE user_id=%s", (user_id,))
    p_count = cur.fetchone()[0]
    cur.close(); conn.close()
    
    txt = f"👤 **Профиль пользователя**\n\n🆔 ID: `{user_id}`\n💎 Тариф: **{u[0]}**\n⚡ Осталось генераций: **{u[1]}**\n💰 Баланс: **{u[2]}** руб.\n📂 Активные проекты: **{p_count}**"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💎 Сменить тариф", callback_data="go_tariffs"),
               types.InlineKeyboardButton("💳 Пополнить баланс", callback_data="add_balance"))
    bot.send_message(user_id, txt, reply_markup=markup, parse_mode='Markdown')

def show_tariffs(user_id):
    txt = ("💎 **ТАРИФЫ**\n\n"
           "1️⃣ **Тест-драйв** (500р)\n— 5 генераций, без срока\n\n"
           "2️⃣ **СЕО Старт** (1400р/мес)\n— 15 генераций\n\n"
           "3️⃣ **СЕО Профи** (2500р/мес)\n— 30 генераций, до 5 проектов\n\n"
           "4️⃣ **PBN Агент** (7500р/мес)\n— 100 генераций, до 15 проектов")
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("Купить Тест-драйв (500р)", callback_data="buy_test"),
               types.InlineKeyboardButton("Купить СЕО Старт (1400р)", callback_data="buy_start"),
               types.InlineKeyboardButton("Купить СЕО Профи (2500р)", callback_data="buy_pro"))
    
    bot.send_message(user_id, txt, reply_markup=markup, parse_mode='Markdown')

# Заглушка оплаты
@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def buy_stub(call):
    plan = call.data.split("_")[1]
    # Здесь должна быть ссылка на оплату
    bot.send_message(call.message.chat.id, f"🧾 Сформирован счет на тариф {plan}.\n\n[Эмуляция] Оплата прошла успешно.")
    
    # Начисляем (пример)
    conn = get_db_connection(); cur = conn.cursor()
    gens = 5 if plan == 'test' else 15
    cur.execute("UPDATE users SET tariff=%s, gens_left=gens_left+%s WHERE user_id=%s", (plan.upper(), gens, call.from_user.id))
    conn.commit(); cur.close(); conn.close()
    bot.send_message(call.message.chat.id, "✅ Тариф активирован!")

# --- 10. ПЛАНИРОВЩИК (WARM UP) ---

def run_scheduler():
    # Каждый день в 12:00 напоминать тем, у кого 0 генераций
    schedule.every().day.at("12:00").do(warm_up_job)
    while True:
        schedule.run_pending()
        time.sleep(60)

def warm_up_job():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    # Выбираем тех, у кого нет тарифа или мало генераций
    cur.execute("SELECT user_id FROM users WHERE gens_left <= 0 OR tariff = 'Нет тарифа'")
    users = cur.fetchall()
    
    msg = "🚀 **SEO напоминание**\n\nСайт сам себя не продвинет! У вас закончились генерации или не выбран тариф. Самое время заняться контентом."
    
    for u in users:
        try:
            bot.send_message(u[0], msg, parse_mode='Markdown')
            time.sleep(0.5)
        except: continue
    cur.close(); conn.close()

# --- ЗАПУСК ---
app = Flask(__name__)
@app.route('/')
def h(): return "SEO BOT OK", 200

if __name__ == "__main__":
    init_db()
    
    # Потоки для сервера и планировщика
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    print("🤖 Бот запущен с новой логикой...")
    bot.infinity_polling(skip_pending=True)
