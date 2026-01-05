import os
import logging
import threading
import time
import schedule
import psycopg2
from telebot import TeleBot, types
from flask import Flask
from google import genai
from dotenv import load_dotenv

# 1. Настройки
load_dotenv()
ADMIN_ID = 203473623
DB_URL = os.getenv("DATABASE_URL")

bot = TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = genai.Client()

TIERS = {
    "test": {"name": "Тест-драйв (10 ген.)", "price": 500, "stars": 270, "no_year": True},
    "start": {"name": "SEO Старт", "price": 1500, "stars": 800},
    "pro": {"name": "SEO Профи", "price": 5000, "stars": 2700},
    "pbn": {"name": "PBN Агент", "price": 15000, "stars": 8000},
}

# 2. База данных
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, free_generations_left INT DEFAULT 2, tier TEXT DEFAULT 'Тест', is_admin BOOLEAN DEFAULT FALSE)")
    cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, user_id BIGINT, type TEXT, url TEXT, business_info TEXT, keywords TEXT)")
    cur.execute("INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    conn.commit()
    cur.close(); conn.close()

# --- НОВОЕ: РАССЫЛКА И ПЛАНИРОВЩИК ---

def send_weekly_retention():
    """Генерирует уникальный контент и фото, затем рассылает всем"""
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users"); users = cur.fetchall()
    
    # Gemini придумывает промпт для картинки и текст
    idea = client.models.generate_content(model="gemini-2.0-flash", contents=["Придумай 1 короткую сюрреалистичную идею для фото SEO-успеха на англ. и 1 мотивирующее предложение на рус."]).text
    
    # Здесь должна быть ссылка на ваш API Nano Banana. Пока используем плейсхолдер.
    image_url = f"https://api.nanobanana.pro/v1/generate?prompt={idea[:100]}" 

    for user in users:
        try:
            bot.send_photo(user[0], photo=image_url, caption=f"🚀 **Ваш еженедельный импульс!**\n\n{idea}", parse_mode='Markdown')
        except: continue
    cur.close(); conn.close()

def run_scheduler():
    schedule.every().monday.at("10:00").do(send_weekly_retention)
    while True:
        schedule.run_pending()
        time.sleep(60)

# 3. Клавиатуры
def get_main_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Новая площадка", callback_data="add_project"),
        types.InlineKeyboardButton("📂 Мои проекты", callback_data="list_projects"),
        types.InlineKeyboardButton("💎 Тарифы", callback_data="show_tiers"),
        types.InlineKeyboardButton("❓ Помощь", callback_data="help_data")
    )
    if user_id == ADMIN_ID:
        markup.add(types.InlineKeyboardButton("⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_main"))
    return markup

# 4. Логика "Новая площадка" (Выбор типа)
@bot.callback_query_handler(func=lambda call: call.data == "add_project")
def choose_platform(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🌐 Сайт", callback_data="type_site"),
        types.InlineKeyboardButton("📸 Instagram", callback_data="type_inst"),
        types.InlineKeyboardButton("📱 Telegram", callback_data="type_tg"),
        types.InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")
    )
    bot.edit_message_text("🎯 **Выберите тип площадки для продвижения:**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("type_"))
def start_survey(call):
    platform_type = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, f"1/5. Введите ссылку на ваш {platform_type}:")
    bot.register_next_step_handler(msg, step_business, {"type": platform_type})

def step_business(message, data):
    data["url"] = message.text
    msg = bot.send_message(message.chat.id, "2/5. Чем занимается ваш бизнес?")
    bot.register_next_step_handler(msg, step_city, data)

def step_city(message, data):
    data["business"] = message.text
    msg = bot.send_message(message.chat.id, "3/5. Ваш город (или 'РФ'):")
    bot.register_next_step_handler(msg, step_audience, data)

def step_audience(message, data):
    data["city"] = message.text
    msg = bot.send_message(message.chat.id, "4/5. Опишите вашу целевую аудиторию:")
    bot.register_next_step_handler(msg, step_count, data)

def step_count(message, data):
    data["audience"] = message.text
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    markup.add("30", "50", "100")
    msg = bot.send_message(message.chat.id, "5/5. Сколько ключевых слов подготовить? (Советую 50)", reply_markup=markup)
    bot.register_next_step_handler(msg, generate_seo_core, data)

def generate_seo_core(message, data):
    count = message.text
    bot.send_message(message.chat.id, "🪄 Gemini 2.0 анализирует нишу и создает ключи...", reply_markup=types.ReplyKeyboardRemove())
    prompt = f"Создай {count} SEO-ключей для {data['type']} {data['url']}. Бизнес: {data['business']}, город: {data['city']}, ЦА: {data['audience']}. Разбей на категории."
    res = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Утвердить и в базу", callback_data="confirm_project"))
    bot.send_message(message.chat.id, f"🔍 **Результат:**\n\n{res.text}", reply_markup=markup, parse_mode='Markdown')

# 5. Остальные Callback и AI-лимиты
@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    if call.data == "main_menu":
        bot.edit_message_text("🚀 **AI Content-Director 2026**", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu(call.from_user.id))
    elif call.data == "show_tiers":
        # Логика тарифов как раньше...
        pass
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['start'])
def welcome(message):
    init_db()
    bot.send_message(message.chat.id, "✅ Система запущена!", reply_markup=get_main_menu(message.from_user.id))

@bot.message_handler(content_types=['text', 'photo'])
def handle_ai(message):
    user_id = message.from_user.id
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT free_generations_left, tier, is_admin FROM users WHERE user_id = %s", (user_id,))
    u = cur.fetchone()
    
    if not u[2] and u[0] <= 0 and u[1] == 'Тест':
        return bot.reply_to(message, "⚠️ Бесплатные попытки (2) закончились. Выберите тариф.")

    res = client.models.generate_content(model="gemini-2.0-flash", contents=[message.text or "SEO-анализ"])
    if not u[2] and u[0] > 0: cur.execute("UPDATE users SET free_generations_left = free_generations_left - 1 WHERE user_id = %s", (user_id,))
    conn.commit(); cur.close(); conn.close()
    bot.reply_to(message, res.text)

# 6. Запуск
app = Flask(__name__)
@app.route('/')
def h(): return "OK", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    bot.infinity_polling(skip_pending=True)
