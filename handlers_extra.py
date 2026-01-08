import threading
import json
import base64
from telebot import types
from config import bot, UPLOAD_STATE
from database import get_db_connection, update_project_progress
from utils import send_step_animation, send_safe_message, get_gemini_response, clean_and_parse_json, format_html_for_chat
from handlers_core import list_projects

# --- DELETION ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("ask_del_"))
def ask_del_confirmation(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑 Да, удалить из бота", callback_data=f"confirm_del_{pid}"))
    markup.add(types.InlineKeyboardButton("🔙 Нет, оставить", callback_data=f"proj_settings_{pid}"))
    bot.edit_message_text("⚠️ **Удаление проекта**\n\nПроект удалится из бота. Статьи на сайте останутся.", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_del_"))
def delete_project_finally(call):
    try: bot.answer_callback_query(call.id, "Удаляю..."); 
    except: pass
    pid = call.data.split("_")[-1]
    conn = None
    try:
        conn = get_db_connection()
        if not conn: return
        cur = conn.cursor()
        cur.execute("DELETE FROM articles WHERE project_id=%s", (pid,))
        cur.execute("DELETE FROM projects WHERE id=%s", (pid,))
        conn.commit()
        cur.close()
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, "✅ **Проект удален.**")
        list_projects(call.from_user.id, call.message.chat.id)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")
    finally:
        if conn: conn.close()

# --- PHOTO UPLOAD ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("kb_add_photo_"))
def kb_add_photo(call):
    pid = call.data.split("_")[3]
    UPLOAD_STATE[call.from_user.id] = pid
    bot.send_message(call.message.chat.id, "🖼 Отправьте фото.")

@bot.message_handler(content_types=['photo'])
def handle_photo_upload(message):
    uid = message.from_user.id
    if uid not in UPLOAD_STATE: return 
    pid = UPLOAD_STATE[uid]
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        b64 = base64.b64encode(downloaded).decode('utf-8')
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT style_images FROM projects WHERE id=%s", (pid,))
        imgs = cur.fetchone()[0] or []
        imgs.append(b64)
        cur.execute("UPDATE projects SET style_images=%s WHERE id=%s", (json.dumps(imgs), pid))
        conn.commit()
        cur.close()
        conn.close()
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 7)", callback_data=f"finish_step6_{pid}"))
        bot.reply_to(message, f"✅ Фото загружено ({len(imgs)}/30).", reply_markup=markup)
    except: pass

# --- ARTICLE GENERATION ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("test_article_"))
def test_article_start_wrapper(call):
    pid = call.data.split("_")[2]
    propose_test_topics(call.message.chat.id, pid)

def propose_test_topics(chat_id, pid):
    bot.send_message(chat_id, "⏳ Генерирую темы...")
    def _gen():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info = res[0] or {}
            kw = res[1] or ""
            
            prompt = (
                f"Role: SEO Expert. Task: Generate 5 viral blog article titles for this niche: "
                f"{info.get('survey_step1', 'General')}. "
                f"Keywords context: {str(kw)[:200]}. "
                f"Language: Russian. "
                f"Strict Output Format: JSON Array of Strings ONLY. Example: [\"Title 1\", \"Title 2\"]"
            )
            
            resp = get_gemini_response(prompt)
            topics = clean_and_parse_json(resp)
            
            if not topics or not isinstance(topics, list):
                topics = ["Почему важно SEO?", "Как продвинуть сайт?", "Тренды 2025 года", "Секреты оптимизации", "Ошибки новичков"]
            
            info["temp_topics"] = topics
            cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
            conn.commit()
            cur.close()
            conn.close()
            
            markup = types.InlineKeyboardMarkup(row_width=1)
            for i, t in enumerate(topics[:5]):
                if isinstance(t, dict): t = list(t.values())[0]
                btn_text = str(t).replace('"', '').replace("'", "").strip()
                if not btn_text: btn_text = f"Вариант {i+1}"
                if len(btn_text) > 60: btn_text = btn_text[:57] + "..."
                markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"write_{pid}_topic_{i}"))
                
            bot.send_message(chat_id, "Выберите тему:", reply_markup=markup)
        except Exception as e: bot.send_message(chat_id, f"Error: {e}")
    threading.Thread(target=_gen).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def write_article_handler(call):
    try: bot.answer_callback_query(call.id, "Пишу..."); 
    except: pass
    parts = call.data.split("_")
    pid = parts[1]
    idx = int(parts[3])
    bot.delete_message(call.message.chat.id, call.message.message_id)
    send_step_animation(call.message.chat.id, "article", "⏳ Пишу статью...")
    
    def _write():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT info, approved_internal_links, approved_external_links FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info = res[0]
            
            topics = info.get("temp_topics", [])
            title = topics[idx]
            
            prompt = f"Write SEO article about '{title}'. Yoast SEO optimized. JSON format: {{'html_content':'...', 'seo_data': {{...}}}}"
            resp = get_gemini_response(prompt)
            data = clean_and_parse_json(resp)
            content = data.get('html_content', resp) if data else resp
            seo = data if data else {}
            
            cur.execute("INSERT INTO articles (project_id, title, content, seo_data, status) VALUES (%s, %s, %s, %s, 'draft') RETURNING id", (pid, title, content, json.dumps(seo)))
            aid = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            
            update_project_progress(pid, "step10_article_done")
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Утвердить", callback_data=f"pre_approve_{aid}"))
            send_safe_message(call.message.chat.id, format_html_for_chat(content), reply_markup=markup)
        except Exception as e: bot.send_message(call.message.chat.id, f"Error: {e}")
    threading.Thread(target=_write).start()