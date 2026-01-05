import os
import logging
import threading
import telebot
import psycopg2
from telebot import types
from google import genai
from flask import Flask
from dotenv import load_dotenv

# 1. Настройки
load_dotenv()
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = genai.Client()
DB_URL = os.getenv("DATABASE_URL") # Не забудьте добавить в Environment Variables на Render

# Состояния для квеста (добавление площадки)
user_states = {} 

# 2. База данных
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            tier TEXT DEFAULT 'Тест',
            balance INT DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            name TEXT,
            url TEXT,
            platform_type TEXT, -- 'Сайт' или 'Соцсеть'
            keywords TEXT,
            target_region TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

# 3. Навигация (ТЗ: Отсутствие тупиков)
def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📂 Мои площадки", callback_data="list_projects"),
        types.InlineKeyboardButton("➕ Новая площадка", callback_data="add_step_1"),
        types.InlineKeyboardButton("💎 Тарифы", callback_data="show_tiers"),
        types.InlineKeyboardButton("📖 Инструкция", callback_data="help_data")
    )
    return markup

def back_to_menu_button():
    return types.InlineKeyboardButton("🏠 В главное меню", callback_data="main_menu")

# 4. Обработка команд
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    
    bot.send_message(
        message.chat.id, 
        "🚀 **AI Content-Director 2026**\nДобро пожаловать в систему линейного управления SEO.",
        reply_markup=get_main_menu(),
        parse_mode='Markdown'
    )

# 5. Линейный квест: Добавление площадки (ТЗ п.2)
@bot.callback_query_handler(func=lambda call: call.data.startswith('add_step'))
def start_add_project(call):
    if call.data == "add_step_1":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🌐 Сайт", callback_data="add_type_web"))
        markup.add(types.InlineKeyboardButton("📱 Соцсеть", callback_data="add_type_social"))
        markup.add(back_to_menu_button())
        bot.edit_message_text("Шаг 1: Выберите тип площадки:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('add_type'))
def process_type(call):
    p_type = "Сайт" if "web" in call.data else "Соцсеть"
    user_states[call.from_user.id] = {'type': p_type}
    bot.edit_message_text(f"Шаг 2: Введите URL вашей площадки (например, https://mysite.com):", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda m: m.from_user.id in user_states and 'url' not in user_states[m.from_user.id])
def process_url(message):
    url = message.text
    # Простая валидация (ТЗ п.1)
    if not url.startswith("http"):
        bot.reply_to(message, "❌ Неверный формат ссылки. Ссылка должна начинаться с http... Попробуйте еще раз:")
        return
    
    user_states[message.from_user.id]['url'] = url
    bot.send_message(message.chat.id, "Шаг 3: Введите название проекта (для вашего удобства):")

@bot.message_handler(func=lambda m: m.from_user.id in user_states and 'name' not in user_states[m.from_user.id])
def process_name(message):
    u_id = message.from_user.id
    data = user_states[u_id]
    
    # Сохранение в БД
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (user_id, name, url, platform_type) VALUES (%s, %s, %s, %s)",
        (u_id, message.text, data['url'], data['type'])
    )
    conn.commit()
    cur.close()
    conn.close()
    
    del user_states[u_id]
    bot.send_message(message.chat.id, f"✅ Проект '{message.text}' успешно добавлен!", reply_markup=get_main_menu())

# 6. Flask (Health Check)
app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.infinity_polling()
