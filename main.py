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

PRICES = {
    "test": {"label": "Тест-драйв (10 ген.)", "price": 500, "stars": 270},
    "start_month": {"label": "SEO Старт (Месяц)", "price": 1500, "stars": 800},
    "pro_month": {"label": "SEO Профи (Месяц)", "price": 5000, "stars": 2700},
    "pbn_month": {"label": "PBN Агент (10 площадок)", "price": 15000, "stars": 8000},
}

# 2. База данных
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            name TEXT,
            url TEXT,
            is_white_list BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    conn.commit()
    cur.close()
    conn.close()

# 3. Вспомогательная логика
def is_partner_site(url):
    return any(domain in url.lower() for domain in WHITE_LIST_DOMAINS)

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

# 4. Основные обработчики
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    bot.send_message(message.chat.id, "🚀 **AI Content-Director 2026**\nВаша система управления SEO готова.", 
                     reply_markup=get_main_menu(user_id), parse_mode='Markdown')

# --- Логика тарифов и оплаты ---
@bot.callback_query_handler(func=lambda call: call.data == "show_tiers")
def show_tiers(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, data in PRICES.items():
        markup.add(types.InlineKeyboardButton(f"{data['label']} — {data['price']}₽", callback_data=f"buy_card_{key}"),
                   types.InlineKeyboardButton(f"💳 Оплатить {data['stars']} ⭐", callback_data=f"buy_stars_{key}"))
    markup.add(types.InlineKeyboardButton("🏠 Назад", callback_data="main_menu"))
    bot.edit_message_text("💎 **Выберите тарифный план:**", call.message.chat.id, call.message.message_id, 
                          reply_markup=markup, parse_mode='Markdown')

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    tier = message.successful_payment.invoice_payload.replace("payload_", "")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET tier = %s, balance_rub = balance_rub + %s WHERE user_id = %s", 
                (tier, message.successful_payment.total_amount / 100, message.from_user.id))
    conn.commit()
    cur.close()
    conn.close()
    bot.send_message(message.chat.id, f"🎉 Тариф {tier} активирован!")

# --- Админ-панель ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_main")
def admin_panel(call):
    if call.from_user.id != ADMIN_ID: return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(balance_rub), SUM(balance_stars) FROM users")
    stats = cur.fetchone()
    cur.close()
    conn.close()
    text = f"⚙️ **Админка**\n\nЮзеров: {stats[0]}\nДоход: {stats[1] or 0}₽ | {stats[2] or 0}⭐"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

# --- Интеллект и SEO ---
@bot.message_handler(content_types=['text', 'photo'])
def handle_seo_request(message):
    user_id = message.from_user.id
    url = message.text or ""
    
    # Проверка лимитов
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT free_generations_left, tier FROM users WHERE user_id = %s", (user_id,))
    u_data = cur.fetchone()
    
    if user_id != ADMIN_ID and not is_partner_site(url) and u_data[0] <= 0 and u_data[1] == 'Тест':
        return bot.reply_to(message, "⚠️ Лимит исчерпан. Выберите тариф.", reply_markup=get_main_menu(user_id))

    try:
        # Генерация контента
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=["Ты SEO-директор 2026. Проанализируй и дай ТЗ:", message.text or "Анализ"]
        )
        if not is_partner_site(url) and user_id != ADMIN_ID and u_data[0] > 0:
            cur.execute("UPDATE users SET free_generations_left = free_generations_left - 1 WHERE user_id = %s", (user_id,))
            conn.commit()
        
        bot.reply_to(message, response.text)
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        cur.close()
        conn.close()

# 5. Flask и Запуск
app = Flask(__name__)
@app.route('/')
def health(): return "OK", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.infinity_polling()
