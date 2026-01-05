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

# 1. НАСТРОЙКИ
load_dotenv()
ADMIN_ID = 203473623
DB_URL = os.getenv("DATABASE_URL")

bot = TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = genai.Client()

TIERS = {
    "test": {"name": "Тест-драйв (10 ген.)", "price": 500, "stars": 270},
    "start": {"name": "SEO Старт", "price": 1500, "stars": 800},
    "pro": {"name": "SEO Профи", "price": 5000, "stars": 2700},
    "pbn": {"name": "PBN Агент", "price": 15000, "stars": 8000},
}

# 2. БАЗА ДАННЫХ
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, free_generations_left INT DEFAULT 2, tier TEXT DEFAULT 'Тест', is_admin BOOLEAN DEFAULT FALSE)")
    cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, user_id BIGINT, type TEXT, url TEXT, info TEXT, keywords TEXT)")
    cur.execute("INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    conn.commit()
    cur.close(); conn.close()

# 3. РАССЫЛКА И ПРОГРЕВ
def send_weekly_retention():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users"); users = cur.fetchall()
    
    # Генерация идеи и картинки через ИИ
    idea = client.models.generate_content(model="gemini-2.0-flash", contents=["Придумай 1 короткую идею для фото SEO-успеха на англ. и мотивирующий текст на рус."]).text
    image_url = f"https://api.nanobanana.pro/v1/generate?prompt={idea[:100]}" 

    for user in users:
        try:
            bot.send_photo(user[0], photo=image_url, caption=f"🚀 **Еженедельный импульс!**\n\n{idea}", parse_mode='Markdown')
        except: continue
    cur.close(); conn.close()

def run_scheduler():
    schedule.every().monday.at("10:00").do(send_weekly_retention)
    while True:
        schedule.run_pending()
        time.sleep(60)

# 4. ГЛАВНОЕ МЕНЮ И КНОПКИ
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

@bot.callback_query_handler(func=lambda call: call.data == "add_project")
def choose_platform(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🌐 Сайт", callback_data="type_site"),
        types.InlineKeyboardButton("📸 Instagram", callback_data="type_inst"),
        types.InlineKeyboardButton("📱 Telegram", callback_data="type_tg")
    )
    bot.edit_message_text("🎯 **Выберите тип площадки:**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

# 5. РАСШИРЕННЫЙ ОПРОС (8 ШАГОВ)
@bot.callback_query_handler(func=lambda call: call.data.startswith("type_"))
def start_survey(call):
    data = {"type": call.data.split("_")[1]}
    msg = bot.send_message(call.message.chat.id, "1/8. Введите ссылку на ваш ресурс:")
    bot.register_next_step_handler(msg, step_2, data)

def step_2(m, d): d["url"] = m.text; msg = bot.send_message(m.chat.id, "2/8. Подробно опишите нишу (через запятую):"); bot.register_next_step_handler(msg, step_3, d)
def step_3(m, d): d["biz"] = m.text; msg = bot.send_message(m.chat.id, "3/8. Приоритетные товары/услуги (через запятую):"); bot.register_next_step_handler(msg, step_4, d)
def step_4(m, d): d["prod"] = m.text; msg = bot.send_message(m.chat.id, "4/8. География (города или РФ):"); bot.register_next_step_handler(msg, step_5, d)
def step_5(m, d): d["geo"] = m.text; msg = bot.send_message(m.chat.id, "5/8. Целевая аудитория (боли, возраст):"); bot.register_next_step_handler(msg, step_6, d)
def step_6(m, d): d["ca"] = m.text; msg = bot.send_message(m.chat.id, "6/8. Конкуренты (сайты или названия):"); bot.register_next_step_handler(msg, step_7, d)
def step_7(m, d): d["comp"] = m.text; msg = bot.send_message(m.chat.id, "7/8. Ваши УТП (почему вы?):"); bot.register_next_step_handler(msg, step_8, d)
def step_8(m, d): 
    d["usp"] = m.text
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    markup.add("50", "150", "300", "500")
    msg = bot.send_message(m.chat.id, "8/8. Сколько ключей подготовить? (до 500):", reply_markup=markup)
    bot.register_next_step_handler(msg, finish_survey, d)

def finish_survey(message, data):
    count = message.text
    bot.send_message(message.chat.id, "🪄 Gemini 2.0 генерирует семантическое ядро...", reply_markup=types.ReplyKeyboardRemove())
    prompt = f"Создай {count} SEO-ключей для {data['url']}. Ниша: {data['biz']}. Продукты: {data['prod']}. Гео: {data['geo']}. ЦА: {data['ca']}. Конкуренты: {data['comp']}. УТП: {data['usp']}. Разбей на кластеры."
    res = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
    bot.send_message(message.chat.id, f"🔍 **Ваш результат:**\n\n{res.text}", parse_mode='Markdown', reply_markup=get_main_menu(message.from_user.id))

# 6. ЛИМИТЫ И ЗАПУСК
@bot.message_handler(commands=['start'])
def welcome(message):
    init_db()
    bot.send_message(message.chat.id, "🚀 AI SEO Director готов!", reply_markup=get_main_menu(message.from_user.id))

@bot.message_handler(content_types=['text', 'photo'])
def handle_ai(message):
    user_id = message.from_user.id
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT free_generations_left, tier, is_admin FROM users WHERE user_id = %s", (user_id,))
    u = cur.fetchone()
    if not u[2] and u[0] <= 0 and u[1] == 'Тест':
        return bot.reply_to(message, "⚠️ Лимит исчерпан. Перейдите на тариф.")
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
    threading.Thread(target=run_scheduler, daemon=True).start()
    bot.infinity_polling(skip_pending=True)
