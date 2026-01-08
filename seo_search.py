import logging
import re
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

# ЖЕСТКИЙ ЧЕРНЫЙ СПИСОК
BANNED_WORDS = [
    'казино', 'casino', 'ставки', 'bet', 'slots', 'слоты', 'vulkan', '1xbet',
    'порно', 'porn', 'xxx', 'sex', 'dating', 'знакомства', 'webcam',
    'query', 'definition', 'meaning', 'translate', 'перевод', 'словарь', # Убираем мусор словарей
    'login', 'sign up', 'регистрация', 'вход', 'cart', 'корзина'
]

def is_valid_result(result):
    """
    Проверяет результат на адекватность.
    Возвращает True, если ссылка безопасная и русская.
    """
    title = result.get('title', '').lower()
    snippet = result.get('body', '').lower()
    url = result.get('href', '').lower()

    # 1. Проверка на стоп-слова в заголовке и URL
    for word in BANNED_WORDS:
        if word in title or word in url:
            return False

    # 2. Проверка на наличие русских букв (Кириллицы) в заголовке
    # Если заголовок полностью на английском (например "Norsk oversettelse") - это мусор
    if not re.search('[а-яё]', title):
        return False

    return True

def search_relevant_links(query: str, max_results: int = 10) -> list:
    """
    Ищет в DuckDuckGo с жесткой фильтрацией.
    """
    # Добавляем оператор site:.ru для гарантии РФ сегмента
    # Добавляем -купить -цена, чтобы искать статьи, а не магазины конкурентов
    safe_query = f"{query} site:.ru -купить -цена -магазин"
    
    logger.info(f"🚀 Запуск поиска (STRICT). Запрос: {safe_query}")
    results = []
    
    # Запрашиваем больше (25), так как часть отсеем фильтрами
    try:
        with DDGS() as ddgs:
            ddg_gen = ddgs.text(
                keywords=safe_query,
                region='ru-ru',
                safesearch='moderate',
                timelimit='y', # Только свежее (год)
                max_results=25 
            )
            
            for r in ddg_gen:
                if is_valid_result(r):
                    results.append({
                        'title': r.get('title', 'Без заголовка'),
                        'href': r.get('href', '#'),
                        'snippet': r.get('body', '')
                    })
                    
                    # Как только набрали 10 чистых ссылок - хватит
                    if len(results) >= max_results:
                        break

    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
        return []

    return results

def format_search_results(links: list) -> str:
    if not links:
        return "😔 Не удалось найти подходящие статьи. Попробуйте уточнить тему."

    msg = "🌐 **Отобранные статьи (RU сегмент):**\n\n"
    
    for i, link in enumerate(links, 1):
        title = link['title'].replace("<", "").replace(">", "")
        url = link['href']
        msg += f"{i}. <a href='{url}'><b>{title}</b></a>\n\n"

    msg += "Проверьте список и нажмите кнопку ниже."
    return msg