import os
import logging
from google import genai  # Новый импорт для SDK 2026
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

# Настройка Gemini API (автоматически берёт ключ из env)
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
        # Инициализация модели (актуальная версия на 2026)
        model = genai.GenerativeModel('gemini-3-flash')  # Новая, стабильная; альтернатива: 'gemini-2.5-flash'

        # Улучшенный системный промпт для SEO-эксперта (с трендами 2026)
        system_prompt = (
            "Ты профессиональный SEO-эксперт с 10+ лет опыта в 2026 году. Учитывай тренды: AI-powered search (SGE+), voice SEO, zero-click, E-E-A-T 2.0. "
            "Анализируй запрос: ключи, семантика, on-page/off-page, структура, мобильность, скорость. "
            "Предлагай улучшения для Google/Yandex/Bing. Ответь кратко, структурировано, на русском. Если не SEO, перенаправь."
        )

        # Подготовка контента (мультимодал для фото)
        content = [system_prompt]
        if message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content.append({"mime_type": "image/jpeg", "data": downloaded_file})
            content.append(message.caption or "Анализируй это изображение в контексте SEO")
        else:
            content.append(message.text)

        # Генерация с ретраями
        for attempt in range(3):
            try:
                response = model.generate_content(content)
                break
            except RequestException as re:
                logger.warning(f"Сетевая ошибка, попытка {attempt+1}: {str(re)}")
                time.sleep(2 ** attempt)
        else:
            raise Exception("Не удалось сгенерировать после 3 попыток")

        # Обработка ответа
        if response.candidates:
            text = response.candidates[0].content.parts[0].text.strip()
        else:
            text = response.text.strip() if hasattr(response, 'text') else ""
            if not text and response.prompt_feedback.block_reason:
                text = f"⚠️ Заблокировано: {response.prompt_feedback.block_reason}. Уточните."

        if text:
            for i in range(0, len(text), 4000):
                bot.reply_to(message, text[i:i+4000])
        else:
            bot.reply_to(message, "⚠️ Пустой ответ. Перефразируйте.")

    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        bot.reply_to(message, f"❌ Ошибка API: {str(e)}.")

if __name__ == "__main__":
    bot.remove_webhook()
    logger.info("Бот запущен...")
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            logger.error(f"Polling ошибка: {str(e)}. Перезапуск...")
            time.sleep(5)
