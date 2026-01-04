import os
import logging
import google.generativeai as genai  # Правильный импорт без подчёркивания
import telebot
from telebot.types import Message
from dotenv import load_dotenv
from requests.exceptions import RequestException
import time

# Загрузка переменных окружения (для локального тестирования)
load_dotenv()

# Настройка логирования (в файл и консоль для отладки на Render)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Настройка Telegram бота
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))

# Настройка Gemini API
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Отладка: Вывод списка доступных моделей (проверьте в логах Render)
try:
    for model in genai.list_models():
        if 'generateContent' in model.supported_generation_methods:
            logger.info(f"Доступная модель: {model.name}")
except Exception as e:
    logger.error(f"Ошибка при получении списка моделей: {str(e)}")

@bot.message_handler(commands=['start'])
def send_welcome(message: Message):
    bot.reply_to(message, "✅ Бот обновлен и готов к SEO-задачам! Напишите запрос, например: 'Оптимизируй текст для ключевого слова SEO в 2026'.")

@bot.message_handler(commands=['help'])
def send_help(message: Message):
    help_text = (
        "🛠️ Команды:\n"
        "/start - Запуск бота\n"
        "/help - Эта справка\n\n"
        "Примеры запросов:\n"
        "- Анализ ключевых слов: 'Проанализируй ключи для сайта о маркетинге'\n"
        "- Генерация контента: 'Напиши статью 500 слов о SEO в 2026 году'\n"
        "- Оптимизация: 'Оптимизируй этот текст: [вставьте текст]'\n"
        "- С фото: Отправьте изображение с подписью для анализа (например, скрин сайта)"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(content_types=['text', 'photo'])
def handle_message(message: Message):
    try:
        # Инициализация модели (актуальная стабильная версия)
        model = genai.GenerativeModel('gemini-2.5-flash')  # Или 'gemini-3-flash-preview' для новейшей

        # Улучшенный системный промпт для SEO-эксперта (с учётом трендов 2026: AI-search, SGE, voice)
        system_prompt = (
            "Ты профессиональный SEO-эксперт с 10+ лет опыта в 2026 году. Учитывай тренды: AI-powered search (SGE), voice SEO, zero-click searches, E-E-A-T. "
            "Анализируй запрос: ключевые слова, семантика, on-page/off-page оптимизация, структура контента, мобильность, скорость. "
            "Предлагай улучшения для Google/Yandex. Ответь кратко, структурировано, на русском. Если запрос не о SEO, перенаправь."
        )

        # Подготовка контента (поддержка фото для анализа, напр. скрина сайта)
        content = [system_prompt]
        if message.photo:
            # Скачиваем фото
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content.append({"mime_type": "image/jpeg", "data": downloaded_file})  # Мультимодальный ввод
            content.append(message.caption or "Анализируй это изображение в контексте SEO")
        else:
            content.append(message.text)

        # Генерация контента с ретраями (на случай сетевых ошибок)
        for attempt in range(3):  # 3 попытки
            try:
                response = model.generate_content(content)
                break
            except RequestException as re:
                logger.warning(f"Сетевая ошибка, попытка {attempt+1}: {str(re)}")
                time.sleep(2 ** attempt)  # Экспоненциальный бэкофф
        else:
            raise Exception("Не удалось сгенерировать ответ после 3 попыток")

        # Обработка ответа (учитываем candidates и safety blocks)
        if response.candidates:
            text = response.candidates[0].content.parts[0].text.strip()
        else:
            text = response.text.strip() if hasattr(response, 'text') else ""
            if not text and response.prompt_feedback.block_reason:
                text = f"⚠️ Ответ заблокирован по причине: {response.prompt_feedback.block_reason}. Уточните запрос."

        if text:
            # Разбиение длинного текста на части (лимит Telegram ~4096 символов)
            for i in range(0, len(text), 4000):
                bot.reply_to(message, text[i:i+4000])
        else:
            bot.reply_to(message, "⚠️ ИИ вернул пустой ответ. Попробуйте перефразировать запрос.")

    except Exception as e:
        logger.error(f"Ошибка при обработке сообщения: {str(e)}")
        bot.reply_to(message, f"❌ Ошибка API: {str(e)}. Проверьте ключ или модель.")

if __name__ == "__main__":
    # Сброс вебхука (на случай конфликтов)
    bot.remove_webhook()
    logger.info("Бот запущен...")
    # Polling с none_stop=True и таймаутом для стабильности на Render
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            logger.error(f"Ошибка polling: {str(e)}. Перезапуск через 5 сек...")
            time.sleep(5)
