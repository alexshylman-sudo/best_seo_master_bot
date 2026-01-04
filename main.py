import os
import telebot
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# 1. Настройка Telegram
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))

# 2. Настройка Gemini (используем REST для стабильности на Render)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"), transport='rest')

# 3. Инициализация модели через ПОЛНЫЙ путь
model = genai.GenerativeModel(model_name="models/gemini-1.5-flash")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "✅ Бот обновлен! Пришлите вашу SEO-задачу.")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Системная роль SEO-эксперта
        prompt = f"Ты профессиональный SEO-эксперт. Ответь пользователю: {message.text}"
        
        # Генерация контента
        response = model.generate_content(prompt)
        
        if response.text:
            bot.reply_to(message, response.text)
        else:
            bot.reply_to(message, "⚠️ ИИ вернул пустой ответ.")
            
    except Exception as e:
        # Детальный вывод ошибки в чат для проверки
        bot.reply_to(message, f"❌ Ошибка API: {str(e)}")

if __name__ == "__main__":
    # Сброс старых соединений (решает ошибку 409)
    bot.remove_webhook()
    print("Бот запущен...")
    bot.polling(none_stop=True, skip_pending=True)
