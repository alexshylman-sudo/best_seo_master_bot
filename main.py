import os
import time
import json
import threading
import logging
import re
import requests
import schedule
import telebot
from telebot import types
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types as genai_types
import io

# --- КОНФИГУРАЦИЯ ---
load_dotenv()

TOKEN = os.getenv('TELEGRAM_TOKEN')
DB_URL = os.getenv('DATABASE_URL')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
APP_URL = os.getenv('APP_URL')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

bot = telebot.TeleBot(TOKEN, parse_mode='HTML')
client = genai.Client(api_key=GEMINI_KEY)

# Настройка логгирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Глобальный контекст (для потерянных состояний)
USER_CONTEXT = {} 

# --- 1. КЛАСС РАБОТЫ С БАЗОЙ ДАННЫХ ---
class Database:
    def __init__(self, db_url):
        self.db_url = db_url
        self.init_db()

    def get_connection(self):
        return psycopg2.connect(self.db_url, cursor_factory=RealDictCursor)

    def init_db(self):
        """Создание таблиц и патчинг схемы"""
        queries = [
            """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance INT DEFAULT 0,
                tariff TEXT DEFAULT 'Нет тарифа',
                gens_left INT DEFAULT 2,
                is_admin BOOLEAN DEFAULT FALSE,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_paid_rub INT DEFAULT 0,
                total_paid_stars INT DEFAULT 0
            );""",
            """CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                url TEXT,
                type TEXT DEFAULT 'site',
                info JSONB DEFAULT '{}',
                knowledge_base JSONB DEFAULT '[]',
                keywords TEXT DEFAULT '',
                cms_url TEXT,
                cms_login TEXT,
                cms_password TEXT,
                cms_key TEXT,
                progress JSONB DEFAULT '{"info_done": false, "analysis_done": false, "upload_done": false}'
            );""",
            """CREATE TABLE IF NOT EXISTS articles (
                id SERIAL PRIMARY KEY,
                project_id INT REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT,
                content TEXT,
                status TEXT DEFAULT 'draft',
                rewrite_count INT DEFAULT 0,
                published_url TEXT
            );""",
            """CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                amount INT,
                currency TEXT,
                tariff_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );"""
        ]
        
        conn = self.get_connection()
        with conn.cursor() as cur:
            for q in queries:
                cur.execute(q)
            conn.commit()
            self._patch_schema(cur, conn)
        conn.close()

    def _patch_schema(self, cur, conn):
        """Добавление недостающих колонок без потери данных"""
        # Пример проверки и добавления колонки (паттерн)
        columns_check = {
            'projects': ['cms_key', 'cms_password', 'cms_login', 'cms_url'],
            'users': ['total_paid_stars']
        }
        
        for table, cols in columns_check.items():
            cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}';")
            existing = [row['column_name'] for row in cur.fetchall()]
            for col in cols:
                if col not in existing:
                    logger.info(f"Patching DB: Adding {col} to {table}")
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT;")
        conn.commit()

    def register_user(self, user_id):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
            if not cur.fetchone():
                if user_id == ADMIN_ID:
                    cur.execute("""INSERT INTO users (user_id, tariff, gens_left, is_admin) 
                                   VALUES (%s, 'GOD_MODE', 9999, TRUE)""", (user_id,))
                    # Создание дефолтных проектов админа
                    self.create_project(user_id, "https://designservice.group/", admin_force=True)
                    self.create_project(user_id, "https://ecosteni.ru/", admin_force=True)
                else:
                    cur.execute("INSERT INTO users (user_id) VALUES (%s)", (user_id,))
                conn.commit()
                return True
        conn.close()
        return False

    def get_user(self, user_id):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET last_active = NOW() WHERE user_id = %s RETURNING *", (user_id,))
            user = cur.fetchone()
            conn.commit()
        conn.close()
        return user

    def create_project(self, user_id, url, admin_force=False):
        # Глобальная проверка уникальности URL
        conn = self.get_connection()
        with conn.cursor() as cur:
            if not admin_force:
                cur.execute("SELECT id FROM projects WHERE url = %s", (url,))
                if cur.fetchone():
                    conn.close()
                    return None # Уже существует
            
            cur.execute("INSERT INTO projects (user_id, url) VALUES (%s, %s) RETURNING id", (user_id, url))
            pid = cur.fetchone()['id']
            conn.commit()
        conn.close()
        return pid

    def get_user_projects(self, user_id):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE user_id = %s ORDER BY id DESC", (user_id,))
            res = cur.fetchall()
        conn.close()
        return res
    
    def get_project(self, project_id):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
            res = cur.fetchone()
        conn.close()
        return res

    def update_project(self, project_id, field, value, json_field=False):
        conn = self.get_connection()
        with conn.cursor() as cur:
            if json_field:
                # Для JSONB обновляем или мержим
                cur.execute(f"UPDATE projects SET {field} = {field} || %s::jsonb WHERE id = %s", (json.dumps(value), project_id))
            else:
                cur.execute(f"UPDATE projects SET {field} = %s WHERE id = %s", (value, project_id))
            conn.commit()
        conn.close()

    def update_balance_gens(self, user_id, gens_delta):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET gens_left = gens_left + %s WHERE user_id = %s", (gens_delta, user_id))
            conn.commit()
        conn.close()

    def get_last_project(self, user_id):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
            res = cur.fetchone()
        conn.close()
        return res

    def delete_project(self, project_id):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            conn.commit()
        conn.close()
        
    def add_payment(self, user_id, amount, tariff, gens):
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO payments (user_id, amount, currency, tariff_name) VALUES (%s, %s, 'RUB', %s)", 
                        (user_id, amount, tariff))
            cur.execute("UPDATE users SET balance = balance + %s, tariff = %s, gens_left = gens_left + %s WHERE user_id = %s",
                        (amount, tariff, gens, user_id))
            conn.commit()
        conn.close()

db = Database(DB_URL)

# --- 2. AI МОДУЛЬ (GEMINI 2.0) ---
class AIManager:
    def __init__(self):
        self.model = "gemini-2.0-flash-exp" # Или gemini-1.5-flash если 2.0 недоступен

    def generate(self, prompt):
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=prompt
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini Error: {e}")
            return None

    def validate_survey_answer(self, question, answer):
        prompt = f"Вопрос: {question}\nОтвет: {answer}\nЗадача: Проверь ответ на наличие мата, спама или полной бессмыслицы. Если ответ адекватный, верни 'OK'. Если нет - верни 'BAD'."
        res = self.generate(prompt)
        return "OK" in (res or "")

    def analyze_page_content(self, text_content):
        prompt = f"""
        Проведи SEO-аудит и анализ контента сайта на основе этого текста (спаршен с главной страницы):
        {text_content[:10000]}... (обрезано)
        
        Дай ответ в формате JSON:
        {{
            "summary": "Краткое описание бизнеса",
            "usability_tips": ["Совет 1", "Совет 2"],
            "seo_errors": ["Ошибка 1", "Ошибка 2"],
            "tone_of_voice": "Описание тональности"
        }}
        """
        res = self.generate(prompt)
        try:
            # Очистка markdown блоков кода json
            clean_res = res.replace('```json', '').replace('```', '')
            return json.loads(clean_res)
        except:
            return {"raw_analysis": res}

    def classify_file(self, file_content):
        prompt = f"Проанализируй этот текст файла: '{file_content[:500]}...'. Это похоже на список ключевых слов (SEO keys)? Ответь только ДА или НЕТ."
        res = self.generate(prompt)
        return "ДА" in (res or "").upper()

    def generate_keywords(self, info, kb, count):
        prompt = f"""
        Контекст: {json.dumps(info, ensure_ascii=False)}
        База знаний: {json.dumps(kb, ensure_ascii=False)}
        Задача: Составь список из {count} ключевых слов для SEO продвижения этого сайта.
        Формат вывода: Только список слов/фраз, разделенный переносом строки. Без нумерации, без заголовков.
        Сначала высокочастотные, потом средне, потом низко.
        """
        return self.generate(prompt)

    def generate_topics(self, context, count=5):
        prompt = f"""
        Контекст проекта: {context}
        Придумай {count} тем для статей в блог.
        Формат вывода:
        1. **Тема**
        Описание темы...
        (Разделитель)
        """
        return self.generate(prompt)

    def write_article(self, topic, keywords):
        prompt = f"""
        Напиши SEO-статью на тему: "{topic}".
        Используй ключи: {keywords}.
        Объем: ~1500-2500 слов.
        Форматирование: Используй HTML теги <b>, <i>, <h2>, <h3>, <p>, <ul>, <li>. НЕ используй Markdown (** или #).
        Структура: Введение, Основная часть (с подзаголовками), Заключение.
        В самом конце добавь блок:
        <b>Фокусное слово:</b> ...
        <b>SEO Title:</b> ...
        <b>Meta Description:</b> ...
        """
        return self.generate(prompt)

    def rewrite_article(self, text):
        prompt = f"Перепиши этот текст в другом стиле, сохранив HTML теги и смысл:\n{text[:5000]}..." # Gemini имеет большое окно, но для безопасности
        return self.generate(prompt)

ai = AIManager()

# --- 3. УТИЛИТЫ ---
def escape_md(text):
    """Экранирование для MarkdownV2 (если вдруг понадобится), но мы используем HTML"""
    return text # Для HTML это не критично, если не юзать < > в тексте.

def send_safe_message(chat_id, text, markup=None):
    """Разбивка длинных сообщений"""
    if not text: return
    max_len = 4000
    parts = []
    while len(text) > 0:
        if len(text) > max_len:
            part = text[:max_len]
            # Ищем последний перенос строки или пробел
            last_break = part.rfind('\n')
            if last_break == -1: last_break = part.rfind(' ')
            if last_break == -1: last_break = max_len
            
            parts.append(text[:last_break])
            text = text[last_break:]
        else:
            parts.append(text)
            text = ""
            
    for i, part in enumerate(parts):
        try:
            m = markup if i == len(parts) - 1 else None
            bot.send_message(chat_id, part, reply_markup=m, parse_mode='HTML')
        except Exception as e:
            # Fallback to plain text if HTML fails
            bot.send_message(chat_id, part, reply_markup=m)

def check_url_status(url):
    try:
        r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        return r.status_code == 200
    except:
        return False

# --- 4. FLASK SERVER (Keep-alive) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "AI SEO Master is Alive", 200

def run_flask():
    app.run(host='0.0.0.0', port=5000)

def keep_alive_ping():
    try:
        requests.get(APP_URL)
        logger.info("Ping sent")
    except:
        pass

schedule.every(14).minutes.do(keep_alive_ping)

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(1)

# --- 5. TELEGRAM BOT HANDLERS ---

# --- ГЛАВНОЕ МЕНЮ ---
def main_menu_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Новый проект", "📂 Мои проекты")
    markup.add("👤 Профиль", "💎 Тарифы")
    markup.add("🆘 Техподдержка")
    if user_id == ADMIN_ID:
        markup.add("⚙️ Админка")
    return markup

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    db.register_user(user_id)
    bot.send_message(user_id, f"Привет, {message.from_user.first_name}! Я AI SEO Master.\nПомогу продвинуть твой сайт в топ.", reply_markup=main_menu_markup(user_id))

@bot.message_handler(func=lambda m: m.text == "➕ Новый проект")
def new_project(message):
    msg = bot.send_message(message.chat.id, "Введите URL вашего сайта (включая https://):")
    bot.register_next_step_handler(msg, process_url)

def process_url(message):
    url = message.text.strip()
    if not url.startswith('http'):
        url = 'https://' + url
    
    status_msg = bot.send_message(message.chat.id, "⏳ Проверяю доступность сайта...")
    
    if not check_url_status(url):
        bot.edit_message_text("⛔ Сайт недоступен (не вернул код 200). Проверьте ссылку.", message.chat.id, status_msg.message_id)
        return

    pid = db.create_project(message.from_user.id, url)
    if pid is None:
        bot.edit_message_text("⛔ Этот сайт уже есть в системе.", message.chat.id, status_msg.message_id)
        return

    bot.delete_message(message.chat.id, status_msg.message_id)
    bot.send_message(message.chat.id, f"✅ Проект {url} создан!", reply_markup=project_menu_inline(pid))

# --- МЕНЮ ПРОЕКТА ---
def project_menu_inline(project_id):
    proj = db.get_project(project_id)
    if not proj: return None
    
    markup = types.InlineKeyboardMarkup()
    progress = proj['progress']
    keywords = proj['keywords']
    
    # Кнопка Стратегия
    if keywords and len(keywords) > 20:
        markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strategy_{project_id}"))

    # Onboarding logic
    if not progress.get('info_done'):
        markup.add(types.InlineKeyboardButton("📝 Добавить информацию (Опрос)", callback_data=f"survey_{project_id}"))
    if not progress.get('analysis_done'):
        markup.add(types.InlineKeyboardButton("📊 Анализ сайта (AI)", callback_data=f"analysis_{project_id}"))
    
    markup.add(types.InlineKeyboardButton("📂 Загрузить файлы", callback_data=f"upload_{project_id}"))
    
    kw_text = "🔑 Подобрать ключевые слова" if not keywords else "🔑 Пересобрать ключи / Удалить"
    markup.add(types.InlineKeyboardButton(kw_text, callback_data=f"keywords_{project_id}"))
    
    markup.add(types.InlineKeyboardButton("⚙️ Настройки сайта (CMS)", callback_data=f"cms_{project_id}"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить проект", callback_data=f"delete_{project_id}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_projects"))
    
    return markup

@bot.message_handler(func=lambda m: m.text == "📂 Мои проекты")
def my_projects(message):
    projs = db.get_user_projects(message.from_user.id)
    if not projs:
        bot.send_message(message.chat.id, "У вас пока нет проектов.")
        return
    
    markup = types.InlineKeyboardMarkup()
    for p in projs:
        markup.add(types.InlineKeyboardButton(f"{p['url']}", callback_data=f"open_project_{p['id']}"))
    bot.send_message(message.chat.id, "Ваши проекты:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    uid = call.from_user.id
    data = call.data
    
    if data.startswith("open_project_"):
        pid = int(data.split("_")[2])
        bot.edit_message_text(f"Управление проектом #{pid}", call.message.chat.id, call.message.message_id, reply_markup=project_menu_inline(pid))

    elif data == "back_projects":
        bot.delete_message(call.message.chat.id, call.message.message_id)
        my_projects(call.message)

    elif data.startswith("delete_"):
        pid = int(data.split("_")[1])
        db.delete_project(pid)
        bot.answer_callback_query(call.id, "Проект удален")
        my_projects(call.message)

    elif data.startswith("survey_"):
        pid = int(data.split("_")[1])
        start_survey(call.message, pid)

    elif data.startswith("analysis_"):
        pid = int(data.split("_")[1])
        run_analysis(call.message, pid)

    elif data.startswith("upload_"):
        pid = int(data.split("_")[1])
        msg = bot.send_message(call.message.chat.id, "Отправьте файл (.txt, .docx, .pdf) или просто текст сообщения.")
        bot.register_next_step_handler(msg, process_file_upload, pid)

    elif data.startswith("keywords_"):
        pid = int(data.split("_")[1])
        choose_keywords_count(call.message, pid)

    elif data.startswith("gen_keys_"):
        # gen_keys_{pid}_{count}
        _, _, pid, count = data.split("_")
        generate_keys_process(call.message, int(pid), int(count))

    elif data.startswith("cms_"):
        pid = int(data.split("_")[1])
        start_cms_setup(call.message, pid)

    elif data.startswith("strategy_"):
        pid = int(data.split("_")[1])
        show_strategy_menu(call.message, pid)
        
    elif data.startswith("topic_gen_"):
        pid = int(data.split("_")[2])
        generate_topics_handler(call.message, pid)

    elif data.startswith("write_article_"):
        # write_article_{pid} (нужно сохранять выбранную тему, здесь упростим для примера)
        bot.answer_callback_query(call.id, "Функция выбора темы в разработке (нужен стейт темы)")

# --- 6. МОДУЛИ ЛОГИКИ ---

# 6.1 ОПРОС
SURVEY_QUESTIONS = [
    "Какова основная цель вашего сайта?",
    "Кто ваша целевая аудитория?",
    "Кто ваши главные конкуренты?",
    "В чем ваше УТП (Уникальное Торговое Предложение)?",
    "Какое ГЕО продвижения (Город, Страна)?",
    "Дополнительные пожелания (свободная форма)."
]

def start_survey(message, pid, step=0, answers=None):
    if answers is None: answers = []
    
    if step < len(SURVEY_QUESTIONS):
        msg = bot.send_message(message.chat.id, f"Вопрос {step+1}/{len(SURVEY_QUESTIONS)}:\n{SURVEY_QUESTIONS[step]}")
        bot.register_next_step_handler(msg, process_survey_answer, pid, step, answers)
    else:
        # Финиш
        info = {
            "goal": answers[0], "audience": answers[1], "competitors": answers[2],
            "utp": answers[3], "geo": answers[4], "extra": answers[5]
        }
        db.update_project(pid, "info", info, json_field=True)
        # Обновляем прогресс
        proj = db.get_project(pid)
        progress = proj['progress']
        progress['info_done'] = True
        db.update_project(pid, "progress", progress, json_field=True) # Замена JSONB полностью (нужно аккуратнее, но для примера сойдет)
        
        bot.send_message(message.chat.id, "✅ Опрос завершен! Данные сохранены.", reply_markup=project_menu_inline(pid))

def process_survey_answer(message, pid, step, answers):
    text = message.text
    if not text:
        msg = bot.send_message(message.chat.id, "Пожалуйста, введите текст.")
        bot.register_next_step_handler(msg, process_survey_answer, pid, step, answers)
        return

    # Валидация
    valid_status = ai.validate_survey_answer(SURVEY_QUESTIONS[step], text)
    if valid_status != "OK":
        msg = bot.send_message(message.chat.id, "⛔ Ответ не принят (мат или бессмыслица). Попробуйте еще раз.")
        bot.register_next_step_handler(msg, process_survey_answer, pid, step, answers)
        return
    
    answers.append(text)
    start_survey(message, pid, step+1, answers)

# 6.2 АНАЛИЗ САЙТА
def run_analysis(message, pid):
    bot.send_message(message.chat.id, "🕵️‍♂️ Начинаю глубокий анализ сайта. Это займет около 30-60 секунд...")
    
    proj = db.get_project(pid)
    url = proj['url']
    
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Сбор данных
        text_content = soup.get_text(separator=' ', strip=True)
        meta_title = soup.title.string if soup.title else "No Title"
        
        # AI Анализ
        analysis_result = ai.analyze_page_content(text_content)
        analysis_result['meta_title'] = meta_title
        
        # Сохранение
        db.update_project(pid, "knowledge_base", [analysis_result], json_field=True)
        
        # Апдейт прогресса
        progress = proj['progress']
        progress['analysis_done'] = True
        db.update_project(pid, "progress", json.dumps(progress), json_field=False)
        
        # Отчет
        report = f"✅ **Анализ завершен!**\n\n**Резюме:** {analysis_result.get('summary', '-')}\n\n**Советы:**\n" + "\n".join(analysis_result.get('usability_tips', []))
        send_safe_message(message.chat.id, report)
        bot.send_message(message.chat.id, "Меню проекта:", reply_markup=project_menu_inline(pid))
        
    except Exception as e:
        logger.error(e)
        bot.send_message(message.chat.id, "Ошибка при анализе сайта. Возможно, стоит защита от ботов.")

# 6.3 ЗАГРУЗКА ФАЙЛОВ
def process_file_upload(message, pid):
    content = ""
    if message.document:
        # Скачивание файла (упрощенно для .txt)
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        try:
            content = downloaded_file.decode('utf-8')
        except:
            bot.send_message(message.chat.id, "Ошибка кодировки. Поддерживается только UTF-8 txt.")
            return
    elif message.text:
        content = message.text
    else:
        bot.send_message(message.chat.id, "Непонятный формат.")
        return

    # Классификация AI
    is_keywords = ai.classify_file(content)
    
    if is_keywords:
        db.update_project(pid, "keywords", content)
        bot.send_message(message.chat.id, "✅ Файл распознан как Ключевые слова и сохранен!", reply_markup=project_menu_inline(pid))
    else:
        db.update_project(pid, "knowledge_base", [{"source": "file", "content": content[:2000]}], json_field=True)
        bot.send_message(message.chat.id, "✅ Информация добавлена в Базу Знаний.", reply_markup=project_menu_inline(pid))

# 6.4 КЛЮЧЕВЫЕ СЛОВА
def choose_keywords_count(message, pid):
    markup = types.InlineKeyboardMarkup(row_width=3)
    btns = [types.InlineKeyboardButton(str(n), callback_data=f"gen_keys_{pid}_{n}") for n in [10, 50, 100, 200]]
    markup.add(*btns)
    bot.send_message(message.chat.id, "Сколько ключей собрать?", reply_markup=markup)

def generate_keys_process(message, pid, count):
    bot.send_message(message.chat.id, "🧠 AI генерирует семантическое ядро...")
    proj = db.get_project(pid)
    
    keys = ai.generate_keywords(proj['info'], proj['knowledge_base'], count)
    
    if keys:
        db.update_project(pid, "keywords", keys)
        send_safe_message(message.chat.id, f"Готово! Вот список:\n\n{keys}")
        
        # Предлагаем скачать
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📥 Скачать .txt", callback_data=f"dl_keys_{pid}"))
        bot.send_message(message.chat.id, "Действия:", reply_markup=project_menu_inline(pid))
    else:
        bot.send_message(message.chat.id, "Ошибка генерации.")

# 6.5 CMS НАСТРОЙКИ
def start_cms_setup(message, pid):
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True)
    markup.add("WordPress", "Tilda (API)", "Bitrix")
    msg = bot.send_message(message.chat.id, "Выберите CMS:", reply_markup=markup)
    bot.register_next_step_handler(msg, cms_step_1, pid)

def cms_step_1(message, pid):
    cms_type = message.text
    # Инструкция для WP
    if "WordPress" in cms_type:
        text = "Для WordPress нужно:\n1. Установить плагин 'Application Passwords' (или встроен в WP 5.6+)\n2. Зайти в Users -> Profile -> Application Passwords.\n3. Создать новый пароль.\n\nВведите URL сайта для API (обычно https://site.com):"
        msg = bot.send_message(message.chat.id, text, reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, cms_step_2, pid)
    else:
        bot.send_message(message.chat.id, "Пока поддерживается автопостинг только для WordPress.")

def cms_step_2(message, pid):
    url = message.text
    db.update_project(pid, "cms_url", url)
    msg = bot.send_message(message.chat.id, "Введите Логин администратора:")
    bot.register_next_step_handler(msg, cms_step_3, pid)

def cms_step_3(message, pid):
    login = message.text
    db.update_project(pid, "cms_login", login)
    msg = bot.send_message(message.chat.id, "Введите Пароль приложения (App Password):")
    bot.register_next_step_handler(msg, cms_step_4, pid)

def cms_step_4(message, pid):
    pwd = message.text
    db.update_project(pid, "cms_password", pwd)
    bot.send_message(message.chat.id, "✅ Настройки CMS сохранены!", reply_markup=project_menu_inline(pid))

# 6.6 ГЕНЕРАЦИЯ СТАТЕЙ И ПУБЛИКАЦИЯ
def show_strategy_menu(message, pid):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💡 Генерировать темы", callback_data=f"topic_gen_{pid}"))
    bot.send_message(message.chat.id, "Стратегия контента.", reply_markup=markup)

def generate_topics_handler(message, pid):
    bot.send_message(message.chat.id, "Генерирую темы...")
    proj = db.get_project(pid)
    context = f"Info: {proj['info']}\nKeys: {proj['keywords'][:500]}"
    topics = ai.generate_topics(context)
    send_safe_message(message.chat.id, topics)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✍️ Написать статью (Тест)", callback_data=f"write_article_{pid}"))
    bot.send_message(message.chat.id, "Чтобы написать статью, скопируйте тему и отправьте боту (в след. версии будет выбор кнопками).", reply_markup=markup)

# --- ПУБЛИКАЦИЯ В WP ---
def publish_to_wp(pid, title, content):
    proj = db.get_project(pid)
    if not proj['cms_url'] or not proj['cms_password']:
        return "Нет настроек CMS"

    import base64
    credentials = f"{proj['cms_login']}:{proj['cms_password']}"
    token = base64.b64encode(credentials.encode())
    headers = {'Authorization': 'Basic ' + token.decode('utf-8')}
    
    post = {
        'title': title,
        'content': content,
        'status': 'publish'
    }
    
    endpoint = f"{proj['cms_url']}/wp-json/wp/v2/posts"
    try:
        r = requests.post(endpoint, headers=headers, json=post)
        if r.status_code == 201:
            return r.json().get('link')
        else:
            return f"Ошибка: {r.text}"
    except Exception as e:
        return str(e)

# --- 7. ПРОФИЛЬ И ТАРИФЫ (Упрощенно) ---
@bot.message_handler(func=lambda m: m.text == "👤 Профиль")
def profile(message):
    user = db.get_user(message.from_user.id)
    text = f"""
    👤 **ID:** {user['user_id']}
    📅 **Регистрация:** {user['joined_at']}
    💎 **Тариф:** {user['tariff']}
    ⚡ **Генераций:** {user['gens_left']}
    """
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "💎 Тарифы")
def tariffs(message):
    text = """
    **Тарифы:**
    1. Тест-драйв: 500р (5 ген)
    2. СЕО Старт: 1400р/мес (15 ген)
    3. PBN Агент: 7500р/мес (100 ген)
    
    _Оплата в разработке_
    """
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

# --- 8. ЗАПУСК ---
def start_bot():
    # Запуск планировщика и сервера в отдельных потоках
    threading.Thread(target=run_flask).start()
    threading.Thread(target=run_scheduler).start()
    
    logger.info("Bot started...")
    bot.infinity_polling()

if __name__ == "__main__":
    start_bot()
