import os
import logging
import threading
import psycopg2
from telebot import TeleBot, types
from flask import Flask
from google import genai
from dotenv import load_dotenv

# 1. Настройки
load_dotenv()
ADMIN_ID = 203473623
WHITE_LIST_DOMAINS = ["designservice.group", "ecosteni.ru"]
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
    cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, user_id BIGINT, url TEXT, business_info TEXT, keywords TEXT)")
    cur.execute("INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    conn.commit()
    cur.close()
    conn.close()

# 3. Клавиатуры
def get_main_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🚀 Продвинуть сайт", callback_data="add_project"),
        types.InlineKeyboardButton("💎 Тарифы", callback_data="show_tiers"),
        types.InlineKeyboardButton("📂 Мои проекты", callback_data="list_projects")
    )
    if user_id == ADMIN_ID:
        markup.add(types.InlineKeyboardButton("⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_main"))
    return markup

# 4. Опрос пользователя и генерация ключей
@bot.callback_query_handler(func=lambda call: call.data == "add_project")
def start_survey(call):
    msg = bot.send_message(call.message.chat.id, "1/5. Введите URL вашего сайта:")
    bot.register_next_step_handler(msg, step_business)

def step_business(message):
    data = {"url": message.text}
    msg = bot.send_message(message.chat.id, "2/5. Чем занимается ваш бизнес?")
    bot.register_next_step_handler(msg, step_city, data)

def step_city(message, data):
    data["business"] = message.text
    msg = bot.send_message(message.chat.id, "3/5. В каком городе вы работаете?")
    bot.register_next_step_handler(msg, step_audience, data)

def step_audience(message, data):
    data["city"] = message.text
    msg = bot.send_message(message.chat.id, "4/5. Кто ваша целевая аудитория?")
    bot.register_next_step_handler(msg, step_count, data)

def step_count(message, data):
    data["audience"] = message.text
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    markup.add("30", "50 (Рекомендую)", "100")
    msg = bot.send_message(message.chat.id, "5/5. Сколько ключевых слов подготовить?", reply_markup=markup)
    bot.register_next_step_handler(msg, generate_seo_core, data)

def generate_seo_core(message, data):
    count = message.text.split()[0]
    bot.send_message(message.chat.id, "🪄 Gemini 2.0 создает семантическое ядро...", reply_markup=types.ReplyKeyboardRemove())
    
    prompt = f"Создай {count} SEO-ключей для {data['url']} ({data['business']}) в г. {data['city']} для ЦА: {data['audience']}. Разбей на категории."
    response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Утвердить и в базу", callback_data="confirm_project"))
    bot.send_message(message.chat.id, f"🔍 **Ваши ключи:**\n\n{response.text}", reply_markup=markup, parse_mode='Markdown')

# 5. Обработка всех Callback (Тарифы, Админка)
@bot.callback_query_handler(func=lambda call: True)
def global_callbacks(call):
    if call.data == "show_tiers":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for k, v in TIERS.items(): markup.add(types.InlineKeyboardButton(v['name'], callback_data=f"tier_{k}"))
        bot.edit_message_text("💎 Выберите тариф:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    elif call.data.startswith("tier_"):
        tier = call.data.split("_")[1]
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Месяц", callback_data=f"pay_{tier}_m"), types.InlineKeyboardButton("Год", callback_data=f"pay_{tier}_y"))
        bot.edit_message_text(f"⏳ Период для {TIERS[tier]['name']}:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

# 6. Основная логика сообщений и Лимиты
@bot.message_handler(commands=['start'])
def welcome(message):
    init_db()
    bot.send_message(message.chat.id, "🚀 AI Content-Director ожил!", reply_markup=get_main_menu(message.from_user.id))

@bot.message_handler(content_types=['text', 'photo'])
def handle_ai(message):
    user_id = message.from_user.id
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT free_generations_left, tier, is_admin FROM users WHERE user_id = %s", (user_id,))
    u = cur.fetchone()
    
    if not u[2] and u[0] <= 0 and u[1] == 'Тест':
        return bot.reply_to(message, "⚠️ Лимит (2 ген.) исчерпан. Перейдите на тариф.")

    res = client.models.generate_content(model="gemini-2.0-flash", contents=[message.text or "SEO"])
    if not u[2] and u[0] > 0: cur.execute("UPDATE users SET free_generations_left = free_generations_left - 1 WHERE user_id = %s", (user_id,))
    conn.commit(); cur.close(); conn.close()
    bot.reply_to(message, res.text)

app = Flask(__name__)
@app.route('/')
def h(): return "OK", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.infinity_polling(skip_pending=True)
