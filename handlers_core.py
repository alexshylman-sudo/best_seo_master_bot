from telebot import types
from config import bot, ADMIN_ID, SUPPORT_ID, USER_CONTEXT, LINK_UPLOAD_STATE, SURVEY_STATE, COMPETITOR_STATE, UPLOAD_STATE
from database import get_db_connection, update_last_active
from utils import send_step_animation

# --- MENUS ---
def main_menu_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("➕ Новый проект", "📂 Мои проекты")
    markup.add("👤 Профиль", "💎 Тарифы")
    markup.add("🆘 Техподдержка")
    if user_id == ADMIN_ID: markup.add("⚙️ Админка")
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    update_last_active(user_id)
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (user_id, gens_left) VALUES (%s, 2) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit()
        cur.close()
        conn.close()
    bot.send_message(user_id, "👋 Привет! Я AI SEO Master.\nПомогу продвинуть твой сайт в топ.", reply_markup=main_menu_markup(user_id))

@bot.message_handler(func=lambda m: m.text in ["➕ Новый проект", "📂 Мои проекты", "👤 Профиль", "💎 Тарифы", "🆘 Техподдержка", "⚙️ Админка", "🔙 В меню"])
def menu_handler(message):
    uid = message.from_user.id
    txt = message.text
    update_last_active(uid)
    if txt == "➕ Новый проект":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🌐 Сайт", callback_data="new_site"),
                   types.InlineKeyboardButton("📸 Инстаграм (Скоро)", callback_data="soon"),
                   types.InlineKeyboardButton("✈️ Телеграм (Скоро)", callback_data="soon"))
        bot.send_message(uid, "Выберите тип площадки:", reply_markup=markup)
    elif txt == "📂 Мои проекты": list_projects(uid, message.chat.id)
    elif txt == "👤 Профиль": show_profile(uid)
    elif txt == "💎 Тарифы": show_tariff_periods(uid)
    elif txt == "🆘 Техподдержка":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Написать", url=f"tg://user?id={SUPPORT_ID}"))
        bot.send_message(uid, "Напишите в поддержку:", reply_markup=markup)
    elif txt == "⚙️ Админка" and uid == ADMIN_ID: show_admin_panel(uid)
    elif txt == "🔙 В меню":
        # Clear states
        if uid in UPLOAD_STATE: del UPLOAD_STATE[uid]
        if uid in SURVEY_STATE: del SURVEY_STATE[uid]
        if uid in LINK_UPLOAD_STATE: del LINK_UPLOAD_STATE[uid]
        if uid in COMPETITOR_STATE: del COMPETITOR_STATE[uid]
        bot.send_message(uid, "Главное меню", reply_markup=main_menu_markup(uid))

@bot.callback_query_handler(func=lambda call: call.data == "soon")
def soon_alert(call): 
    try: bot.answer_callback_query(call.id, "🚧 В разработке...")
    except: pass

# --- PROJECTS & DISPATCHER ---
def list_projects(user_id, chat_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, url FROM projects WHERE user_id = %s ORDER BY id ASC", (user_id,))
    projs = cur.fetchall()
    cur.close()
    conn.close()
    if not projs:
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
        bot.send_message(chat_id, "📂 У вас пока нет проектов.", reply_markup=markup)
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in projs:
        btn_text = p[1].replace("https://", "").replace("http://", "").replace("www.", "")[:30]
        markup.add(types.InlineKeyboardButton(f"🌐 {btn_text}", callback_data=f"open_proj_mgmt_{p[0]}"))
    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
    bot.send_message(chat_id, "Ваши проекты:", reply_markup=markup)

def send_resume_wizard(chat_id, pid, step_num, step_name, callback):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"▶️ Продолжить: Шаг {step_num}", callback_data=callback))
    markup.add(types.InlineKeyboardButton("❌ Удалить проект", callback_data=f"ask_del_{pid}"))
    bot.send_message(
        chat_id, 
        f"🚧 **Настройка не завершена!**\n\nВы остановились на:\n👉 **Шаг {step_num}. {step_name}**\n\nНужно завершить настройку.", 
        reply_markup=markup,
        parse_mode='Markdown'
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("open_proj_mgmt_"))
def open_proj_mgmt(call, mode="management", msg_id=None, new_site_url=None):
    """
    DISPATCHER: Checks project progress. If incomplete, forces Wizard flow.
    """
    try: bot.answer_callback_query(call.id)
    except: pass
    
    # Extract ID
    if isinstance(call, types.CallbackQuery):
        pid = call.data.split("_")[3]
        uid = call.from_user.id
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
    else:
        pid = call.data.split("_")[3] if hasattr(call, 'data') else None
        uid = call.from_user.id
        chat_id = call.chat.id
        
    USER_CONTEXT[uid] = pid
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT url, progress FROM projects WHERE id = %s", (pid,))
    res = cur.fetchone()
    cur.close()
    conn.close()
    
    if not res: 
        bot.send_message(chat_id, "❌ Проект не найден.")
        return

    url = res[0]
    progress = res[1] or {}

    # === WIZARD CHECKPOINTS ===
    if not progress.get("step2_scan_done"):
        send_resume_wizard(chat_id, pid, 2, "Сканирование сайта", f"step2_retry_{pid}")
        return
    if not progress.get("step3_survey_done"):
        send_resume_wizard(chat_id, pid, 3, "Опрос (Брифинг)", f"srv_{pid}")
        return
    if not progress.get("step4_competitors_done"):
        send_resume_wizard(chat_id, pid, 4, "Анализ конкурентов", f"step4_comp_start_{pid}")
        return
    if not progress.get("step5_links_done"):
        send_resume_wizard(chat_id, pid, 5, "Генерация ссылок", f"step5_links_{pid}")
        return
    if not progress.get("step6_gallery_done"):
        send_resume_wizard(chat_id, pid, 6, "Галерея (Референсы)", f"step6_gallery_{pid}")
        return
    if not progress.get("step7_imgprompts_done"):
        send_resume_wizard(chat_id, pid, 7, "Генератор стиля фото", f"step7_imgprompts_{pid}")
        return
    if not progress.get("step8_textprompts_done"):
        send_resume_wizard(chat_id, pid, 8, "Текстовые настройки", f"step8_textprompts_{pid}")
        return
    if not progress.get("step9_cms_done"):
        send_resume_wizard(chat_id, pid, 9, "Подключение к сайту", f"step9_cms_{pid}")
        return
    if not progress.get("step10_article_done"):
        send_resume_wizard(chat_id, pid, 10, "Тестовая статья", f"step10_testart_{pid}")
        return
    if not progress.get("step11_strategy_done"):
        send_resume_wizard(chat_id, pid, 11, "Стратегия и План", f"step11_strategy_{pid}")
        return

    # === DASHBOARD (ALL DONE) ===
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🚀 СТРАТЕГИЯ И СТАТЬИ", callback_data=f"strat_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ Настройки проекта", callback_data=f"proj_settings_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
    
    safe_url = url.replace("https://", "").replace("http://", "").rstrip('/')
    text = f"📂 **Проект:** {safe_url}\n✅ Настройка завершена.\n\nУправляйте проектом:"
    try:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("proj_settings_"))
def project_settings_menu(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    pid = call.data.split("_")[2]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("⚡ Написать тестовую статью", callback_data=f"test_article_{pid}"))
    markup.add(types.InlineKeyboardButton("🧠 База Знаний (Стиль)", callback_data=f"kb_menu_{pid}"))
    markup.add(types.InlineKeyboardButton("🔗 Конкуренты", callback_data=f"step4_comp_start_{pid}"))
    markup.add(types.InlineKeyboardButton("⚙️ CMS (Сайт)", callback_data=f"step9_cms_{pid}"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить проект", callback_data=f"ask_del_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"open_proj_mgmt_{pid}"))
    bot.edit_message_text("⚙️ **Настройки проекта**", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

# Placeholder stubs for profile functions not in original main.py
def show_profile(uid): pass
def show_tariff_periods(uid): pass
def show_admin_panel(uid): pass