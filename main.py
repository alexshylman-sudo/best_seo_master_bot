import os
import threading
import time
import schedule
import psycopg2
import re
from urllib.parse import urlparse
from telebot import TeleBot, types
from flask import Flask
from google import genai
from dotenv import load_dotenv

# 1. КОНФИГУРАЦИЯ
load_dotenv()
ADMIN_ID = 203473623
DB_URL = os.getenv("DATABASE_URL")
bot = TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = genai.Client()

# 2. БАЗА ДАННЫХ
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, free_generations_left INT DEFAULT 2, tier TEXT DEFAULT 'Тест', is_admin BOOLEAN DEFAULT FALSE)")
    cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, user_id BIGINT, type TEXT, url TEXT, info TEXT, keywords TEXT)")
    cur.execute("INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    conn.commit(); cur.close(); conn.close()

# 3. ВАЛИДАЦИЯ И ПЛАНИРОВЩИК
def is_valid_url(url):
    try:
        res = urlparse(url)
        return all([res.scheme, res.netloc])
    except: return False

def send_weekly_retention():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM users"); users = cur.fetchall()
    idea = client.models.generate_content(model="gemini-2.0-flash", contents=["Short surreal SEO success image prompt (EN) and motive quote (RU)"]).text
    img = f"https://api.nanobanana.pro/v1/generate?prompt={idea[:100]}"
    for u in users:
        try: bot.send_photo(u[0], photo=img, caption=f"🚀 **Weekly Boost!**\n\n{idea}", parse_mode='Markdown')
        except: continue
    cur.close(); conn.close()

def run_scheduler():
    schedule.every().monday.at("10:00").do(send_weekly_retention)
    while True: schedule.run_pending(); time.sleep(60)

# 4. МЕНЮ И ОБРАБОТКА "НОВАЯ ПЛОЩАДКА"
def get_main_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("➕ Новая площадка", callback_data="add_project"),
               types.InlineKeyboardButton("📂 Мои проекты", callback_data="list_projects"),
               types.InlineKeyboardButton("💎 Тарифы", callback_data="show_tiers"))
    if user_id == ADMIN_ID: markup.add(types.InlineKeyboardButton("⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_main"))
    return markup

@bot.callback_query_handler(func=lambda call: call.data == "add_project")
def platform_choice(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🌐 Сайт", callback_data="start_site_flow"),
               types.InlineKeyboardButton("📸 Инстаграм", callback_data="type_inst"),
               types.InlineKeyboardButton("📱 Телеграм", callback_data="type_tg"))
    bot.edit_message_text("🎯 **Выберите вариант:**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data == "start_site_flow")
def ask_url(call):
    msg = bot.send_message(call.message.chat.id, "🔗 **Введите URL вашего сайта:**\n(Напр: https://google.com)")
    bot.register_next_step_handler(msg, validate_url_step)

def validate_url_step(message):
    url = message.text.strip()
    if not is_valid_url(url):
        msg = bot.send_message(message.chat.id, "❌ **Ошибка!** Введите корректный URL (с http:// или https://):")
        bot.register_next_step_handler(msg, validate_url_step)
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📝 Заполнить информацию", callback_data=f"surv_{url}"),
               types.InlineKeyboardButton("📊 СЕО анализ", callback_data=f"seo_{url}"),
               types.InlineKeyboardButton("📂 Загрузить данные (PDF/JPG)", callback_data=f"upld_{url}"),
               types.InlineKeyboardButton("🔑 Создать ключевые слова", callback_data=f"keyg_{url}"))
    bot.send_message(message.chat.id, f"✅ **Сайт {url} добавлен!** Выберите действие:", reply_markup=markup, parse_mode='Markdown')

# 5. ОПРОСНИК (7 ШАГОВ)
@bot.callback_query_handler(func=lambda call: call.data.startswith("surv_"))
def start_survey(call):
    url = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, "1/7. Подробная ниша бизнеса (через запятую):")
    bot.register_next_step_handler(msg, s2, {"url": url})

def s2(m, d): d["n"]=m.text; msg=bot.send_message(m.chat.id, "2/7. Приоритетные товары:"); bot.register_next_step_handler(msg, s3, d)
def s3(m, d): d["p"]=m.text; msg=bot.send_message(m.chat.id, "3/7. География (РФ/Города):"); bot.register_next_step_handler(msg, s4, d)
def s4(m, d): d["g"]=m.text; msg=bot.send_message(m.chat.id, "4/7. Целевая аудитория:"); bot.register_next_step_handler(msg, s5, d)
def s5(m, d): d["c"]=m.text; msg=bot.send_message(m.chat.id, "5/7. Ваши конкуренты:"); bot.register_next_step_handler(msg, s6, d)
def s6(m, d): d["k"]=m.text; msg=bot.send_message(m.chat.id, "6/7. Ваши преимущества (УТП):"); bot.register_next_step_handler(msg, s7, d)
def s7(m, d): 
    d["u"]=m.text
    bot.send_message(m.chat.id, "✨ Анализирую данные и сохраняю профиль сайта...")
    # Здесь логика сохранения d в БД
    bot.send_message(m.chat.id, "✅ Профиль заполнен!", reply_markup=get_main_menu(m.from_user.id))

# 6. ЛИМИТЫ И AI
@bot.message_handler(commands=['start'])
def welcome(message):
    init_db()
    bot.send_message(message.chat.id, "🚀 AI Content-Director готов к работе!", reply_markup=get_main_menu(message.from_user.id))

@bot.message_handler(content_types=['text', 'photo', 'document'])
def ai_handler(message):
    user_id = message.from_user.id
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT free_generations_left, tier, is_admin FROM users WHERE user_id = %s", (user_id,))
    u = cur.fetchone()
    if not u[2] and u[0] <= 0 and u[1] == 'Тест':
        cur.close(); conn.close()
        return bot.reply_to(message, "⚠️ Лимит (2 ген.) исчерпан. Выберите тариф.")
    
    # Логика AI генерации
    res = client.models.generate_content(model="gemini-2.0-flash", contents=[message.text or "SEO"])
    if not u[2] and u[0] > 0:
        cur.execute("UPDATE users SET free_generations_left = free_generations_left - 1 WHERE user_id = %s", (user_id,))
    conn.commit(); cur.close(); conn.close()
    bot.reply_to(message, res.text)

# 7. ЗАПУСК
app = Flask(__name__)
@app.route('/')
def h(): return "OK", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True)
