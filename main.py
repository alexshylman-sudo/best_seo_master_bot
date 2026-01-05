import os
import logging
import threading
import time
from flask import Flask
from google import genai
import telebot
from telebot.types import Message
from dotenv import load_dotenv

# 1. Настройка окружения и логирования
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 2. Flask-сервер для "обмана" проверок Render (Health Check)
app = Flask(__name__)

@app.route('/')
def health_check():
    return "SEO Bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# 3. Инициализация Telegram и Gemini
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = genai.Client()

@bot.message_handler(commands=['start'])
def send_welcome(message: Message):
    bot.reply_to(message, "✅ Бот онлайн и готов к SEO-анализу! Пришлите ваш вопрос или скриншот сайта.")

@bot.message_handler(content_types=['text', 'photo'])
def handle_message(message: Message):
    try:
        system_prompt = "Ты профессиональный SEO-эксперт. Отвечай структурировано на русском языке."
        content = [system_prompt]

        if message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content.append({"mime_type": "image/jpeg", "data": downloaded_file})
            content.append(message.caption or "Проанализируй скриншот сайта")
        else:
            content.append(message.text)

        # Генерация с защитой от лимитов (ошибка 429)
        response = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model="gemini-2.0-flash", 
                    contents=content
                )
                break
            except Exception as e:
                if "429" in str(e):
                    logger.warning(f"Лимит исчерпан. Пауза 30 сек (попытка {attempt+1})")
                    time.sleep(30)
                else:
                    raise e
        
        if response and response.text:
            text = response.text
            for i in range(0, len(text), 4000):
                bot.reply_to(message, text[i:i+4000])
        else:
            bot.reply_to(message, "⚠️ Не удалось получить ответ от ИИ.")

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        bot.reply_to(message, f"❌ Произошла ошибка: {str(e)}")

# 4. Запуск
if __name__ == "__main__":
    # Запуск Flask в фоне для Render
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Сброс вебхука и запуск опроса
    bot.remove_webhook()
    logger.info("Бот запущен!")
    
    while True:
        try:
            # skip_pending=True предотвращает лавину старых сообщений при запуске
            bot.polling(none_stop=True, interval=0, timeout=40, skip_pending=True)
        except Exception as e:
            logger.error(f"Ошибка Polling: {e}. Перезапуск через 5 сек...")
            time.sleep(5)
