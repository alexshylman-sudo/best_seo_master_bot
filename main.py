import os
import threading
import time
import schedule
import psycopg2
import json
from urllib.parse import urlparse
from telebot import TeleBot, types
from flask import Flask
from google import genai
from dotenv import load_dotenv

# 1. КОНФИГУРАЦИЯ
load_dotenv()

# Проверяем переменные окружения (чтобы не упало с ошибкой NoneType)
ADMIN_ID = int(os.getenv("ADMIN_ID", "203473623"))
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)

# Глобальный словарь для временного хранения контекста (какой проект сейчас активен у юзера)
user_active_project = {}

# 2. БАЗА ДАННЫХ
def get_db_connection():
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        print(f"DB Connection Error: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    
    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, 
            free_generations_left INT DEFAULT 5, 
            tier TEXT DEFAULT 'Тест', 
            is_admin BOOLEAN DEFAULT FALSE,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица проектов (info храним как JSON-строку для гибкости)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY, 
            user_id BIGINT, 
            type TEXT, 
            url TEXT, 
            info TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Добавляем админа
    cur.execute("INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE", (ADMIN_ID,))
    
    conn.commit()
    cur.close()
    conn.close()
    print("✅ База данных инициализирована.")

# 3. УТИЛИТЫ И ПЛАНИРОВЩИК
def is_valid_url(url):
    try:
        res = urlparse(url)
        return all([res.scheme, res.netloc])
    except: return False

def send_weekly_retention():
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    
    try:
        idea = client.models.generate_content(
            model="gemini-2.0-flash", 
            contents=["Напиши очень короткую (1 предложение) мотивационную цитату для маркетолога на русском языке."]
        ).text
    except:
        idea = "Время создавать контент!"

    for u in users:
        try: 
            bot.send_message(u[0], f"🚀 **Буст недели!**\n\n{idea}", parse_mode='Markdown')
            time.sleep(0.5) 
        except: continue
    
    cur.close()
    conn.close()

def run_scheduler():
    schedule.every().monday.at("10:00").do(send_weekly_retention)
    while True: 
        schedule.run_pending()
        time.sleep(60)

# 4. МЕНЮ И ИНТЕРФЕЙС
def get_main_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Новый проект", callback_data="add_project"),
        types.InlineKeyboardButton("📂 Мои проекты", callback_data="list_projects"),
        types.InlineKeyboardButton("💎 Профиль", callback_data="profile")
    )
    if user_id == ADMIN_ID: 
        markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))
    return markup

def show_project_menu(chat_id, project_id, is_new=False, message_id=None):
    text = f"✅ **Проект #{project_id} создан!**" if is_new else f"📂 **Управление проектом #{project_id}**"
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📝 Заполнить бриф (Опрос)", callback_data=f"surv_start_{project_id}"),
        types.InlineKeyboardButton("🤖 Генерировать контент", callback_data=f"ai_mode_{project_id}"),
        types.InlineKeyboardButton("🔙 В главное меню", callback_data="main_menu")
    )
    
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='Markdown')
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')

# --- 5. ОБРАБОТЧИКИ КОМАНД (ВАЖНО: ОНИ ДОЛЖНЫ БЫТЬ ВЫШЕ TEXT HANDLER) ---

@bot.message_handler(commands=['start'])
def welcome(message):
    init_db()
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (message.from_user.id,))
        conn.commit()
        cur.close()
        conn.close()
    
    bot.send_message(message.chat.id, "🚀 **AI Director** приветствует вас!\nЯ помогу создать контент для ваших проектов.", reply_markup=get_main_menu(message.from_user.id), parse_mode='Markdown')

# --- 6. CALLBACK HANDLERS (КНОПКИ) ---

@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def back_to_main(call):
    bot.edit_message_text("Главное меню:", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu(call.from_user.id))

@bot.callback_query_handler(func=lambda call: call.data == "add_project")
def platform_choice(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🌐 Веб-сайт", callback_data="type_site"))
    bot.edit_message_text("🎯 **Выберите тип площадки:**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data == "type_site")
def ask_url(call):
    msg = bot.send_message(call.message.chat.id, "🔗 **Введите URL сайта:**\n(Например: https://example.com)")
    bot.register_next_step_handler(msg, validate_url_step)

def validate_url_step(message):
    url = message.text.strip()
    if not is_valid_url(url):
        msg = bot.send_message(message.chat.id, "❌ Некорректный URL. Обязательно с http:// или https://. Попробуйте снова:")
        bot.register_next_step_handler(msg, validate_url_step)
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO projects (user_id, type, url, info) VALUES (%s, 'site', %s, '{}') RETURNING id", (message.from_user.id, url))
    project_id = cur.fetchone()[0]
    conn.commit()
    cur.close(); conn.close()
    
    show_project_menu(message.chat.id, project_id, is_new=True)

# ОПРОСНИК
@bot.callback_query_handler(func=lambda call: call.data.startswith("surv_start_"))
def start_survey(call):
    p_id = call.data.split("_")[2]
    msg = bot.send_message(call.message.chat.id, "1/6. Опишите нишу бизнеса (кратко):")
    bot.register_next_step_handler(msg, s2, {"p_id": p_id, "data": {}})

def s2(m, d): d["data"]["niche"]=m.text; msg=bot.send_message(m.chat.id, "2/6. Какой основной продукт/услуга?"); bot.register_next_step_handler(msg, s3, d)
def s3(m, d): d["data"]["product"]=m.text; msg=bot.send_message(m.chat.id, "3/6. Кто целевая аудитория (ЦА)?"); bot.register_next_step_handler(msg, s4, d)
def s4(m, d): d["data"]["geo"]=m.text; msg=bot.send_message(m.chat.id, "4/6. Основные конкуренты:"); bot.register_next_step_handler(msg, s5, d)
def s5(m, d): d["data"]["competitors"]=m.text; msg=bot.send_message(m.chat.id, "5/6. Ваши преимущества (УТП):"); bot.register_next_step_handler(msg, s6, d)
def s6(m, d): 
    d["data"]["usp"] = m.text
    p_id = d["p_id"]
    json_info = json.dumps(d["data"], ensure_ascii=False)
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET info = %s WHERE id = %s", (json_info, p_id))
    conn.commit(); cur.close(); conn.close()
    
    bot.send_message(m.chat.id, "✅ **Бриф сохранен!**")
    show_project_menu(m.chat.id, p_id)

# СПИСОК ПРОЕКТОВ
@bot.callback_query_handler(func=lambda call: call.data == "list_projects")
def list_projects(call):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, url FROM projects WHERE user_id = %s ORDER BY id DESC LIMIT 5", (call.from_user.id,))
    projects = cur.fetchall()
    cur.close(); conn.close()
    
    if not projects:
        bot.answer_callback_query(call.id, "У вас пока нет проектов.")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in projects:
        btn_text = f"{p[1][:30]}..." if len(p[1]) > 30 else p[1]
        markup.add(types.InlineKeyboardButton(f"🌐 {btn_text}", callback_data=f"open_proj_{p[0]}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="main_menu"))
    bot.edit_message_text("📂 **Ваши проекты:**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_"))
def open_project(call):
    p_id = call.data.split("_")[2]
    show_project_menu(call.message.chat.id, p_id, message_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "profile")
def show_profile(call):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT free_generations_left, tier FROM users WHERE user_id = %s", (call.from_user.id,))
    res = cur.fetchone()
    cur.close(); conn.close()
    txt = f"👤 **Профиль**\n\n🆔 `{call.from_user.id}`\n⚡ Лимиты: **{res[0]}**\n💎 Тариф: **{res[1]}**"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="main_menu"))
    bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

# ВЫБОР ПРОЕКТА ДЛЯ AI
@bot.callback_query_handler(func=lambda call: call.data.startswith("ai_mode_"))
def activate_ai_mode(call):
    p_id = call.data.split("_")[2]
    user_active_project[call.from_user.id] = p_id 
    msg = f"⚡ **Режим AI (Проект #{p_id})**\n\nНапишите задачу (пост, заголовок, текст)..."
    bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, parse_mode='Markdown')

# --- 7. AI HANDLER (САМЫЙ ПОСЛЕДНИЙ - ЭТО ВАЖНО!) ---

@bot.message_handler(content_types=['text'])
def ai_handler(message):
    user_id = message.from_user.id
    
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    cur.execute("SELECT free_generations_left, is_admin FROM users WHERE user_id = %s", (user_id,))
    u = cur.fetchone()
    
    if not u: # Юзера нет в базе
        cur.close(); conn.close()
        return bot.send_message(message.chat.id, "Нажмите /start для начала.")

    if not u[1] and u[0] <= 0:
        cur.close(); conn.close()
        return bot.reply_to(message, "⚠️ **Лимит исчерпан!**")

    # Контекст
    p_id = user_active_project.get(user_id)
    context_promt = ""
    if p_id:
        cur.execute("SELECT info, url FROM projects WHERE id = %s", (p_id,))
        proj = cur.fetchone()
        if proj and proj[0] != '{}':
            try:
                data = json.loads(proj[0])
                context_promt = f"Context: Site {proj[1]}, Niche {data.get('niche')}, Product {data.get('product')}, Target {data.get('geo')}, USP {data.get('usp')}."
            except: pass
            
    wait_msg = bot.reply_to(message, "⏳ Думаю...")
    try:
        full_prompt = f"{context_promt}\nUser Request: {message.text}"
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[full_prompt])
        
        if not u[1]:
            cur.execute("UPDATE users SET free_generations_left = free_generations_left - 1 WHERE user_id = %s", (user_id,))
            conn.commit()
            
        bot.edit_message_text(response.text, message.chat.id, wait_msg.message_id, parse_mode='Markdown')
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка AI: {e}", message.chat.id, wait_msg.message_id)
    
    cur.close(); conn.close()

# 8. ЗАПУСК
app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot Running", 200

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    bot.infinity_polling(skip_pending=True)
