import os
import logging
import threading
import psycopg2
from telebot import TeleBot, types
from flask import Flask
from dotenv import load_dotenv

# 1. Настройки и инициализация
load_dotenv()
ADMIN_ID = 203473623
WHITE_LIST_DOMAINS = ["designservice.group", "ecosteni.ru"]
DB_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = TeleBot(os.getenv("TELEGRAM_TOKEN"))

# 2. База данных
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Таблица пользователей
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
    # Таблица проектов
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            name TEXT,
            url TEXT,
            is_white_list BOOLEAN DEFAULT FALSE
        )
    """)
    # Назначение владельца админом
    cur.execute("INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    conn.commit()
    cur.close()
    conn.close()

# 3. Вспомогательная логика
def get_main_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Новая площадка", callback_data="add_project"),
        types.InlineKeyboardButton("📂 Мои проекты", callback_data="list_projects"),
        types.InlineKeyboardButton("💎 Тарифы", callback_data="show_tiers")
    )
    if user_id == ADMIN_ID:
        markup.add(types.InlineKeyboardButton("⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_main"))
    return markup

# 4. Обработчики команд
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    
    bot.send_message(
        message.chat.id, 
        "🚀 **AI Content-Director 2026**\nВаша система управления SEO готова к работе.",
        reply_markup=get_main_menu(user_id),
        parse_mode='Markdown'
    )

# 5. БЛОК 2: Админ-панель
@bot.callback_query_handler(func=lambda call: call.data == "admin_main")
def admin_panel(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "Доступ запрещен!")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT tier, COUNT(*) FROM users GROUP BY tier")
    tiers = cur.fetchall()
    cur.execute("SELECT SUM(balance_rub), SUM(balance_stars) FROM users")
    revenue = cur.fetchone()
    cur.close()
    conn.close()

    res_text = f"⚙️ **Панель управления**\n\n👥 Юзеров: {total_users}\n"
    res_text += f"💰 Доход: {revenue[0] or 0}₽ | {revenue[1] or 0}⭐\n\n"
    res_text += "📊 Статистика тарифов:\n"
    for t, count in tiers:
        res_text += f"— {t}: {count}\n"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 Рассылка (Retention)", callback_data="admin_broadcast"))
    markup.add(types.InlineKeyboardButton("🏠 В меню", callback_data="main_menu"))
    
    bot.edit_message_text(res_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast(call):
    msg = bot.send_message(call.message.chat.id, "Введите текст сообщения для рассылки всем пользователям:")
    bot.register_next_step_handler(msg, send_broadcast_step)

def send_broadcast_step(message):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    cur.close()
    conn.close()

    success = 0
    for u in users:
        try:
            bot.send_message(u[0], f"📢 **Сообщение от AI-Директора:**\n\n{message.text}", parse_mode='Markdown')
            success += 1
        except: continue
    bot.send_message(ADMIN_ID, f"✅ Рассылка завершена. Успешно: {success}")

# 6. Flask и Запуск
app = Flask(__name__)
@app.route('/')
def health(): return "OK", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.remove_webhook()
    logger.info("Бот запущен с Блоком 1 и 2!")
    bot.infinity_polling()
