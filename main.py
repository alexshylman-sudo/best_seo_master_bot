import os
import telebot
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Настройка Telegram
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))

# Настройка Gemini (используем v1beta для гарантированного доступа к flash)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Прямое указание модели gemini-1.5-flash
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        response = model.generate_content(
            f"Ты SEO-эксперт. Ответь пользователю: {message.text}"
        )
        
        if response.text:
            bot.reply_to(message, response.text)
        else:
            bot.reply_to(message, "⚠️ ИИ вернул пустой ответ. Попробуйте еще раз.")
            
    except Exception as e:
        # Если снова 404, вы увидите это в чате
        bot.reply_to(message, f"❌ Ошибка API: {str(e)}")
# Инициализация модели
model = genai.GenerativeModel(model_name="gemini-1.5-flash")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "✅ Бот обновлен и готов к работе! Пришлите вашу SEO-задачу.")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Прямой вызов генерации
        response = model.generate_content(f"Ты SEO-эксперт. Ответь пользователю: {message.text}")
        bot.reply_to(message, response.text)
    except Exception as e:
        # Вывод ошибки в чат для диагностики
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")

if __name__ == "__main__":
    bot.remove_webhook()
    bot.polling(none_stop=True, skip_pending=True)
