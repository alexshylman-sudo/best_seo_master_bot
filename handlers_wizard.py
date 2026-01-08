import threading
import json
import io
import traceback
import base64
from telebot import types
from google.genai import types as genai_types
from config import bot, client, USER_CONTEXT, SURVEY_STATE, COMPETITOR_STATE
from database import get_db_connection, update_project_progress
from utils import send_step_animation, parse_sitemap, deep_analyze_site, get_gemini_response, send_safe_message, clean_and_parse_json
from handlers_core import open_proj_mgmt

# --- ИМПОРТ МОДУЛЯ ПОИСКА ---
from seo_search import search_relevant_links, format_search_results

# Кэш
SEARCH_CACHE = {}

# STEP 1: NEW SITE
@bot.callback_query_handler(func=lambda call: call.data == "new_site")
def new_site_start(call):
    try: bot.answer_callback_query(call.id)
    except: pass
    msg = bot.send_message(call.message.chat.id, "🔗 Введите URL сайта (обязательно с http:// или https://):")
    bot.register_next_step_handler(msg, check_url_step)

def check_url_step(message):
    def _process_url():
        try:
            url = message.text.strip()
            if not url.startswith("http"):
                msg = bot.send_message(message.chat.id, "❌ Нужен URL с http://.")
                bot.register_next_step_handler(msg, check_url_step)
                return
            clean_check_url = url.rstrip('/')
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT id FROM projects WHERE url LIKE %s OR url LIKE %s", (clean_check_url, clean_check_url + '/'))
            exists = cur.fetchone()
            cur.close()
            conn.close()
            if exists:
                markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 В меню", callback_data="back_main"))
                bot.send_message(message.chat.id, f"🚫 **Этот сайт уже добавлен!**", parse_mode='Markdown', reply_markup=markup)
                return

            # STEP 2: SCAN
            send_step_animation(message.chat.id, "scan", "⏳ **Шаг 2. Сканирую сайт...**")
            sitemap_links = parse_sitemap(url)

            # SAVE PROJECT
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO projects (user_id, type, url, info, sitemap_links, progress) VALUES (%s, 'site', %s, '{}', %s, '{}') RETURNING id", (message.from_user.id, url, json.dumps(sitemap_links)))
            pid = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            
            update_project_progress(pid, "step2_scan_done") # DONE
            USER_CONTEXT[message.from_user.id] = pid

            if not sitemap_links:
                bot.send_message(message.chat.id, "⚠️ Карта сайта не найдена. Анализирую главную страницу...")
            else:
                bot.send_message(message.chat.id, f"✅ Успешно! Найдено {len(sitemap_links)} страниц.")
            
            scraped_data, _ = deep_analyze_site(url)
            prompt = f"Analyze this site: {url}. Content: {scraped_data[:3000]}. Give a short SEO summary in Russian."
            analysis = get_gemini_response(prompt)
            bot.send_message(message.chat.id, f"📊 **Экспресс-анализ сайта:**\n\n{analysis}")

            # START STEP 3
            send_step_animation(message.chat.id, "survey", "📝 **Шаг 3. Опрос (Брифинг)**")
            start_survey_logic(message.chat.id, message.from_user.id, pid)

        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
            traceback.print_exc()
    threading.Thread(target=_process_url).start()

# RETRY HANDLER FOR STEP 2 (Resume)
@bot.callback_query_handler(func=lambda call: call.data.startswith("step2_retry_"))
def step2_retry(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step2_scan_done")
    send_step_animation(call.message.chat.id, "survey", "📝 **Шаг 3. Опрос**")
    start_survey_logic(call.message.chat.id, call.from_user.id, pid)

# STEP 3: SURVEY
def start_survey_logic(chat_id, user_id, pid):
    SURVEY_STATE[user_id] = {'pid': pid, 'step': 1}
    msg = bot.send_message(chat_id, "📝 **Вопрос 1/5**\nКратко опишите суть вашего сайта/бизнеса. О чем он?", parse_mode='Markdown')
    bot.register_next_step_handler(msg, survey_step_router)

def survey_step_router(message):
    uid = message.from_user.id
    if uid not in SURVEY_STATE: return
    state = SURVEY_STATE[uid]
    step = state['step']
    pid = state['pid']
    text = message.text
    if text.startswith('/'): return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0] or {}
    info[f'survey_step{step}'] = text
    cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
    conn.commit()
    cur.close()
    conn.close()

    if step == 1:
        SURVEY_STATE[uid]['step'] = 2
        msg = bot.send_message(message.chat.id, "📝 **Вопрос 2/5: Ваша целевая аудитория?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 2:
        SURVEY_STATE[uid]['step'] = 3
        msg = bot.send_message(message.chat.id, "📝 **Вопрос 3/5: Регион продвижения?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 3:
        SURVEY_STATE[uid]['step'] = 4
        msg = bot.send_message(message.chat.id, "📝 **Вопрос 4/5: Ваши преимущества (УТП)?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 4:
        SURVEY_STATE[uid]['step'] = 5
        msg = bot.send_message(message.chat.id, "📝 **Вопрос 5/5: Тон коммуникации (Tone of Voice)?**")
        bot.register_next_step_handler(msg, survey_step_router)
    elif step == 5:
        del SURVEY_STATE[uid]
        update_project_progress(pid, "step3_survey_done") 
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🚀 Идем дальше", callback_data=f"step4_comp_start_{pid}"))
        bot.send_message(message.chat.id, "✅ **Опрос завершен!**", reply_markup=markup)
    
@bot.callback_query_handler(func=lambda call: call.data.startswith("srv_"))
def retry_survey(call):
    pid = call.data.split("_")[-1]
    start_survey_logic(call.message.chat.id, call.from_user.id, pid)

# STEP 4: COMPETITORS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step4_comp_start_"))
def step4_comp_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "competitors", "🕵️‍♂️ **Шаг 4. Анализ конкурентов**")
    COMPETITOR_STATE[call.from_user.id] = pid
    msg = bot.send_message(call.message.chat.id, "🔗 Пришлите **URL сайта конкурента**:")
    bot.register_next_step_handler(msg, step4_analyze_comp_logic)

def step4_analyze_comp_logic(message):
    uid = message.from_user.id
    if uid not in COMPETITOR_STATE: return
    pid = COMPETITOR_STATE[uid]
    
    url = message.text.strip()
    if not url.startswith("http"):
        msg = bot.send_message(message.chat.id, "❌ Нужна ссылка с http.")
        bot.register_next_step_handler(msg, step4_analyze_comp_logic)
        return

    bot.send_message(message.chat.id, "⏳ Анализирую...")
    
    def _analyze():
        try:
            scraped_data, _ = deep_analyze_site(url)
            prompt = f"Role: SEO Expert. Analyze: {url}.\nSnippet: {scraped_data[:2000]}\nTask: Write a VERY BRIEF (2 sentences) opinion on their SEO quality. OUTPUT LANGUAGE: RUSSIAN."
            opinion = get_gemini_response(prompt)
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
            info = cur.fetchone()[0] or {}
            clist = info.get("competitors_list", [])
            clist.append({"url": url, "opinion": opinion})
            info["competitors_list"] = clist
            cur.execute("UPDATE projects SET info=%s WHERE id=%s", (json.dumps(info), pid))
            conn.commit()
            cur.close()
            conn.close()
            
            send_safe_message(message.chat.id, f"🧐 **Мнение ИИ:**\n{opinion}")
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("➕ Добавить еще", callback_data=f"step4_add_more_{pid}"))
            markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 5)", callback_data=f"finish_step4_{pid}"))
            bot.send_message(message.chat.id, "Что делаем дальше?", reply_markup=markup)
            
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
            
    threading.Thread(target=_analyze).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("step4_add_more_"))
def step4_add_more(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    msg = bot.send_message(call.message.chat.id, "🔗 Следующая ссылка:")
    COMPETITOR_STATE[call.from_user.id] = pid
    bot.register_next_step_handler(msg, step4_analyze_comp_logic)

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step4_"))
def finish_step4_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step4_competitors_done") 
    step5_links_start(call)

# --- STEP 5: LINKS (ВНУТРЕННИЕ + ВНЕШНИЕ) ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("step5_links_"))
def step5_links_start(call):
    """Начало шага 5: Сначала Внутренние ссылки"""
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    if call.from_user.id in COMPETITOR_STATE: del COMPETITOR_STATE[call.from_user.id]
    
    send_step_animation(call.message.chat.id, "links", "🔗 **Шаг 5. Генератор ссылок**")
    kb_gen_internal_logic(call.message.chat.id, pid)

def kb_gen_internal_logic(chat_id, pid):
    bot.send_message(chat_id, "⚙️ **Часть 1.** Анализирую структуру сайта для перелинковки...")
    def _scan():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT url FROM projects WHERE id=%s", (pid,))
        url = cur.fetchone()[0]
        cur.close()
        conn.close()
        
        links = parse_sitemap(url)
        clean_links = [l for l in links if not any(x in l for x in ['.jpg', '.png', 'wp-admin', 'feed', '.xml'])]
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE projects SET approved_internal_links=%s WHERE id=%s", (json.dumps(clean_links[:100]), pid))
        conn.commit()
        cur.close()
        conn.close()
        
        msg = f"✅ Найдено внутренних страниц: {len(clean_links)}."
        if len(clean_links) > 0:
            if len(clean_links) <= 10:
                msg += "\n\n" + "\n".join(clean_links)
                bot.send_message(chat_id, msg)
            else:
                msg += f"\n(Показаны первые 10 из {len(clean_links)}):\n" + "\n".join(clean_links[:10])
                bot.send_message(chat_id, msg)
        
        markup = types.InlineKeyboardMarkup()
        # КНОПКА ПЕРЕХОДА К ПОИСКУ ВНЕШНИХ ССЫЛОК
        markup.add(types.InlineKeyboardButton("🌐 Часть 2: Найти внешние ссылки", callback_data=f"step5_ext_start_{pid}"))
        bot.send_message(chat_id, "Внутренняя структура сохранена. Теперь найдем авторитетные источники?", reply_markup=markup)

    threading.Thread(target=_scan).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("step5_ext_start_") or call.data.startswith("retry_ext_"))
def step5_start_external_search(call):
    """Часть 2: Поиск в DuckDuckGo (или повторный поиск)"""
    try: bot.answer_callback_query(call.id, "Ищу..."); 
    except: pass
    
    # Определяем pid, учитывая разные callback'и
    if "retry_ext_" in call.data:
        pid = call.data.split("_")[-1]
    else:
        pid = call.data.split("_")[-1]
        
    chat_id = call.message.chat.id
    
    # Удаляем старое сообщение с кнопками, если это повтор
    if "retry_ext_" in call.data:
        try: bot.delete_message(chat_id, call.message.message_id)
        except: pass

    bot.send_message(chat_id, "🔎 **Часть 2.** Ищу качественные статьи (только RU, без спама)...")
    
    def _search_thread():
        # Получаем тему и ключи
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
        res = cur.fetchone()
        info = res[0] or {}
        # Пробуем взять ключи, если нет - то тему опроса
        topic = res[1] if res[1] else info.get('survey_step1', '')
        
        # Если совсем пусто
        if not topic or len(topic) < 3:
            topic = "SEO продвижение"

        # Берем только первые 30 символов темы, чтобы запрос не был слишком длинным и мусорным
        short_topic = topic.split(',')[0].split('.')[0][:50]
        
        # Формируем запрос: "Полезная статья {ТЕМА}"
        query = f"Полезная статья {short_topic}"
        
        # Ищем через наш обновленный модуль
        results = search_relevant_links(query, max_results=10)
        
        # Сохраняем во временный кэш
        SEARCH_CACHE[call.from_user.id] = {'pid': pid, 'links': results}
        
        # Отправляем пользователю список
        msg_text = format_search_results(results)
        
        markup = types.InlineKeyboardMarkup()
        
        if results:
            # ДВЕ КНОПКИ: ОДОБРИТЬ ВСЕ или ИСКАТЬ ЕЩЕ
            markup.add(types.InlineKeyboardButton("✅ Одобрить и продолжить", callback_data=f"save_ext_{pid}"))
            markup.add(types.InlineKeyboardButton("🔄 Найти другие варианты", callback_data=f"retry_ext_{pid}"))
        else:
            markup.add(types.InlineKeyboardButton("🔄 Попробовать еще раз", callback_data=f"retry_ext_{pid}"))
            markup.add(types.InlineKeyboardButton("➡️ Пропустить шаг", callback_data=f"finish_step5_{pid}"))
            
        bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)

    threading.Thread(target=_search_thread).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("save_ext_"))
def save_external_links(call):
    """Сохраняет найденные ссылки (все 10) и идет дальше"""
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    uid = call.from_user.id
    
    if uid in SEARCH_CACHE:
        links = SEARCH_CACHE[uid]['links']
        
        # Сохраняем в БД
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE projects SET approved_external_links=%s WHERE id=%s", (json.dumps(links), pid))
        conn.commit()
        cur.close()
        conn.close()
        
        del SEARCH_CACHE[uid]
        msg = f"✅ Сохранено {len(links)} внешних источников."
    else:
        msg = "⚠️ Срок действия данных истек. Но мы идем дальше."

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 6)", callback_data=f"finish_step5_{pid}"))
    bot.send_message(call.message.chat.id, msg, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step5_"))
def finish_step5_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step5_links_done")
    step6_gallery_start(call)

# STEP 6: GALLERY
@bot.callback_query_handler(func=lambda call: call.data.startswith("step6_gallery_"))
def step6_gallery_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "gallery", "🖼 **Шаг 6. Галерея (Референсы)**")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Добавить фото", callback_data=f"kb_add_photo_{pid}"))
    markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 7)", callback_data=f"finish_step6_{pid}"))
    bot.send_message(call.message.chat.id, "Загрузите фото-примеры стиля или пропустите.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step6_"))
def finish_step6_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step6_gallery_done")
    step6_gallery_start(call) # Исправлено: если закончили 6 шаг, то должны идти на 7, но у нас тут почему-то рекурсия была.
    # ВАЖНО: Ниже правильная логика перехода на 7 шаг
    step7_imgprompts_start(call)

# STEP 7: IMG PROMPTS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step7_imgprompts_"))
def step7_imgprompts_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "img_prompts", "🎨 **Шаг 7. Генератор визуального стиля**")
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT style_images FROM projects WHERE id=%s", (pid,))
    images = cur.fetchone()[0] or []
    cur.close()
    conn.close()
    
    if len(images) > 0:
        bot.send_message(call.message.chat.id, "⏳ Анализирую фото из галереи...")
        step7_auto_gen(call.message.chat.id, pid)
    else:
        bot.send_message(call.message.chat.id, "⚠️ Фото не загружены. Используем стандартный стиль.")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 8)", callback_data=f"finish_step7_{pid}"))
        bot.send_message(call.message.chat.id, "Нажмите далее.", reply_markup=markup)

def step7_auto_gen(chat_id, pid):
    def _gen():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT style_images, info FROM projects WHERE id=%s", (pid,))
        res = cur.fetchone()
        images_b64, info = res[0] or [], res[1] or {}
        
        try:
            content_parts = []
            instruction = f"Role: AI Art Director. Context: {info.get('survey_step1', 'General')}. Analyze images. Create English Prompt describing the STYLE."
            content_parts.append(genai_types.Part.from_text(text=instruction))
            for b64_str in images_b64[:3]:
                try:
                    img_bytes = base64.b64decode(b64_str)
                    mime = "image/png" if img_bytes.startswith(b'\x89PNG') else "image/jpeg"
                    content_parts.append(genai_types.Part.from_bytes(data=img_bytes, mime_type=mime))
                except: pass
            
            response = client.models.generate_content(model="gemini-2.0-flash", contents=[genai_types.Content(parts=content_parts)])
            prompt_text = response.text.strip()
            
            cur.execute("UPDATE projects SET approved_prompts=%s WHERE id=%s", (json.dumps([prompt_text]), pid))
            conn.commit()
            bot.send_message(chat_id, f"✅ **Стиль создан:**\n`{prompt_text}`", parse_mode='Markdown')
        except Exception as e:
            bot.send_message(chat_id, f"Ошибка: {e}")
        finally:
            cur.close()
            conn.close()

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 8)", callback_data=f"finish_step7_{pid}"))
        bot.send_message(chat_id, "Стиль сохранен.", reply_markup=markup)
    threading.Thread(target=_gen).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step7_"))
def finish_step7_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step7_imgprompts_done")
    step8_textprompts_start(call)

# STEP 8: TEXT PROMPTS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step8_textprompts_"))
def step8_textprompts_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "text_prompts", "📝 **Шаг 8. Текстовые настройки**")
    bot.send_message(call.message.chat.id, "⏳ Подбираю Negative Prompt...")
    kb_auto_style_gen_logic(call.message.chat.id, pid)

def kb_auto_style_gen_logic(chat_id, pid):
    def _gen_style():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
        info = cur.fetchone()[0] or {}
        niche = info.get("survey_step1", "General Website")
        
        prompt = f"""
        Act as an AI Prompter. Topic: {niche}.
        Task: Create GENERAL style modifiers.
        Rules: Output ONLY two strings separated by '|||'.
        1. Positive prompt. 2. Negative prompt. English only.
        """
        resp = get_gemini_response(prompt)
        try:
            parts = resp.split('|||')
            if len(parts) == 2:
                pos = parts[0].strip()
                neg = parts[1].strip()
                cur.execute("UPDATE projects SET style_prompt=%s, style_negative_prompt=%s WHERE id=%s", (pos, neg, pid))
                conn.commit()
                msg = f"✅ **Настройки сохранены!**\nPos: {pos}\nNeg: {neg}"
            else:
                msg = "⚠️ Использованы стандартные настройки."
        except:
            msg = "⚠️ Ошибка генерации."
        finally:
            cur.close()
            conn.close()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 9)", callback_data=f"finish_step8_{pid}"))
        bot.send_message(chat_id, msg, reply_markup=markup)
    threading.Thread(target=_gen_style).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step8_"))
def finish_step8_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step8_textprompts_done")
    step9_cms_start(call)

# STEP 9: CMS
@bot.callback_query_handler(func=lambda call: call.data.startswith("step9_cms_"))
def step9_cms_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "cms", "🔐 **Шаг 9. Подключение к сайту**")
    
    markup = types.InlineKeyboardMarkup(); 
    markup.add(types.InlineKeyboardButton("🚫 Пропустить (Я настрою позже)", callback_data=f"skip_cms_{pid}"))
    
    msg = bot.send_message(call.message.chat.id, "1. Введите **Логин** администратора WordPress:", reply_markup=markup, parse_mode='Markdown')
    bot.register_next_step_handler(msg, step9_cms_login, pid)

def step9_cms_login(message, pid):
    if message.text.startswith("/"): return
    login = message.text.strip()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_login=%s WHERE id=%s", (login, pid))
    conn.commit()
    cur.close()
    conn.close()
    msg = bot.send_message(message.chat.id, "🔑 2. Теперь введите **Пароль приложения** (Application Password):")
    bot.register_next_step_handler(msg, step9_cms_pass, pid)

def step9_cms_pass(message, pid):
    pwd = message.text.strip()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET cms_password=%s WHERE id=%s", (pwd, pid))
    cur.execute("UPDATE projects SET cms_url=url WHERE id=%s AND cms_url IS NULL", (pid,))
    conn.commit()
    cur.close()
    conn.close()
    
    update_project_progress(pid, "step9_cms_done") 
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➡️ Идем дальше (Шаг 10)", callback_data=f"step10_testart_{pid}"))
    bot.send_message(message.chat.id, "✅ CMS данные сохранены!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("skip_cms_"))
def skip_cms_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step9_cms_done")
    step10_testart_start(call)

# STEP 10: ARTICLE
@bot.callback_query_handler(func=lambda call: call.data.startswith("step10_testart_"))
def step10_testart_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "article", "⚡ **Шаг 10. Тестовая статья**")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✍️ Написать статью", callback_data=f"test_article_{pid}")) 
    markup.add(types.InlineKeyboardButton("➡️ Пропустить (Шаг 11)", callback_data=f"finish_step10_{pid}"))
    bot.send_message(call.message.chat.id, "Напишем первую статью?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_step10_"))
def finish_step10_handler(call):
    pid = call.data.split("_")[-1]
    update_project_progress(pid, "step10_article_done")
    step11_strategy_start(call)

# STEP 11: STRATEGY
@bot.callback_query_handler(func=lambda call: call.data.startswith("step11_strategy_"))
def step11_strategy_start(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[-1]
    send_step_animation(call.message.chat.id, "strategy", "🚀 **Шаг 11. Стратегия и Контент-план**")
    strategy_start_helper(call, pid)

def strategy_start_helper(call, pid):
    markup = types.InlineKeyboardMarkup(row_width=4)
    btns = [types.InlineKeyboardButton(str(i), callback_data=f"freq_{pid}_{i}") for i in range(1, 8)]
    markup.add(*btns)
    bot.send_message(call.message.chat.id, "📅 **Контент-план**\nСколько статей в неделю вы хотите публиковать?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("freq_"))
def save_freq_and_plan(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    _, pid, freq = call.data.split("_")
    freq = int(freq)
    
    bot.edit_message_text(f"📅 Генерирую план...", call.message.chat.id, call.message.message_id)
    def _gen_plan():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT info, keywords FROM projects WHERE id=%s", (pid,))
            res = cur.fetchone()
            info_json = res[0] or {}
            kw = res[1] or ""
            prompt = f"Role: SEO Expert. Create Content Plan for {freq} articles. Topic: {info_json.get('survey_step1','')}. Output JSON: [{{'day':'Mon','time':'10:00','topic':'...'}}]"
            ai_resp = get_gemini_response(prompt)
            calendar_plan = clean_and_parse_json(ai_resp)
            if not calendar_plan: calendar_plan = [{"day": "Monday", "time": "10:00", "topic": "Intro Article"}]
            info_json["temp_plan"] = calendar_plan
            cur.execute("UPDATE projects SET frequency=%s, info=%s WHERE id=%s", (freq, json.dumps(info_json), pid))
            conn.commit()
            cur.close()
            conn.close()
            
            msg_text = "🗓 **План:**\n\n"
            for item in calendar_plan: msg_text += f"**{item['day']}**: {item['topic']}\n"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Утвердить план", callback_data=f"approve_plan_{pid}"))
            bot.send_message(call.message.chat.id, msg_text, reply_markup=markup, parse_mode='Markdown')
        except Exception as e: bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")
    threading.Thread(target=_gen_plan).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_plan_"))
def approve_plan(call):
    try: bot.answer_callback_query(call.id); 
    except: pass
    pid = call.data.split("_")[2]
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT info FROM projects WHERE id=%s", (pid,))
    info = cur.fetchone()[0]
    plan = info.get("temp_plan", [])
    cur.execute("UPDATE projects SET content_plan=%s WHERE id=%s", (json.dumps(plan), pid))
    conn.commit()
    cur.close()
    conn.close()
    
    update_project_progress(pid, "step11_strategy_done") 
    
    send_step_animation(call.message.chat.id, "done", "🎉 **Поздравляю! Настройка завершена!**")
    open_proj_mgmt(call, mode="management")