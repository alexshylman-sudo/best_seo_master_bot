import os
import telebot
import google.generativeai as genai
from dotenv import load_dotenv

# 1. Загрузка настроек
load_dotenv()
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')

# Здесь мы будем добавлять вашу логику
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Бот запущен! Опишите задачу, и я приступлю.")

# Запуск
if __name__ == "__main__":
    bot.polling(none_stop=True)
