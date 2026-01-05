import os
import logging
import threading
import time
import psycopg2
from telebot import TeleBot, types
from flask import Flask
from google import genai
from dotenv import load_dotenv

# 1. Настройки и инициализация
load_dotenv()
ADMIN_ID = 203473623
WHITE_LIST_DOMAINS = ["designservice.group", "ecosteni.ru"]
DB_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = genai.Client()

# Конфигурация тарифов
TIERS = {
    "test": {"name": "Тест-драйв (10 ген.)", "price": 500, "stars": 270, "no_year": True},
    "start": {"name": "SEO Старт", "price": 1500, "stars": 800},
    "pro": {"name": "SEO Профи", "price": 5000, "stars": 2700},
    "pbn": {"name": "PBN Агент (10 площадок)", "price": 15000, "stars": 8000},
}

# 2. Работа с БД и проверка прав
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            free_generations_left INT DEFAULT 2,
            tier TEXT DEFAULT 'Тест',
            is_admin BOOLEAN DEFAULT FALSE,
            balance_rub INT DEFAULT 0,
            balance_stars INT DEFAULT 0
        )
    """)
    cur.execute("INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    conn.commit()
    cur.close()
    conn.close()

def is_partner_site(url):
    return any(domain in str(url).lower() for domain in WHITE_LIST_DOMAINS)

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

# 4. Логика кнопок (Callback Query)
@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    user_id = call.from_user.id
    
    if call.data == "main_menu":
        bot.edit_message_text("🚀 **AI Content-Director 2026**\nВаша система управления SEO готова.", 
                              call.message.chat.id, call.message.message_id, reply_markup=get_main_menu(user_id), parse_mode='Markdown')

    elif call.data == "show_tiers":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, data in TIERS.items():
            markup.add(types.InlineKeyboardButton(data['name'], callback_data=f"tier_{key}"))
        markup.add(types.InlineKeyboardButton("🏠 Назад", callback_data="main_menu"))
        bot.edit_message_text("💎 **Выберите тариф:**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

    elif call.data.startswith("tier_"):
        tier = call.data.split("_")[1]
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("📅 На 1 месяц", callback_data=f"period_{tier}_month"))
        if not TIERS[tier].get("no_year"):
            markup.add(types.InlineKeyboardButton("📅 На 1 год (-30%)", callback_data=f"period_{tier}_year"))
        markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="show_tiers"))
        bot.edit_message_text(f"⏳ Выбрано: **{TIERS[tier]['name']}**\nВыберите период:", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

    elif call.data.startswith("period_"):
        _, tier, period = call.data.split("_")
        price = TIERS[tier]['price'] if period == "month" else TIERS[tier]['price'] * 12 * 0.7
        stars = TIERS[tier]['stars'] if period == "month" else TIERS[tier]['stars'] * 12 * 0.7
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(f"💳 Карта ({int(price)}₽)", callback_data=f"pay_card_{tier}_{period}"),
            types.InlineKeyboardButton(f"⭐ Звезды ({int(stars)}⭐)", callback_data=f"pay_stars_{tier}_{period}"),
            types.InlineKeyboardButton("⬅️ Назад", callback_data=f"tier_{tier}")
        )
        bot.edit_message_text(f"💳 **Оплата: {TIERS[tier]['name']}**\nСумма: {int(price)}₽ / {int(stars)}⭐", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

    elif call.data in ["add_project", "list_projects", "help_data"]:
        bot.answer_callback_query(call.id, "Раздел в разработке (Блок 5)")

    elif call.data == "admin_main":
        if user_id != ADMIN_ID: return
        # Логика статистики из БД...
        bot.answer_callback_query(call.id, "Загрузка статистики...")

    bot.answer_callback_query(call.id)

# 5. Обработка SEO-запросов (Gemini 2.0)
@bot.message_handler(commands=['start'])
def start_cmd(message):
    init_db()
    bot.send_message(message.chat.id, "✅ Бот активирован!", reply_markup=get_main_menu(message.from_user.id))

@bot.message_handler(content_types=['text', 'photo'])
def handle_message(message):
    user_id = message.from_user.id
    # Здесь должна быть проверка лимитов и вызов Gemini как в Блоке 4...
    bot.reply_to(message, "⚙️ Анализирую через Gemini 2.0 Flash...")

# 6. Flask для Render
app = Flask(__name__)
@app.route('/')
def health(): return "OK", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.infinity_polling(skip_pending=True)
