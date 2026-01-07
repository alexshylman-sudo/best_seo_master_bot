from google import genai
from google.genai import types

# --- НАСТРОЙКИ ---
API_KEY = "AIzaSyANQATAveM7ef-NvCpS0ftKGapQcQJUwRA"  # <--- Вставьте сюда ваш ключ AIza...
MODEL_NAME = "imagen-3.0-generate-001"

def test_google_image():
    print(f"🔑 Проверяем ключ: {API_KEY[:10]}...")
    print(f"🎨 Модель: {MODEL_NAME}")
    
    try:
        client = genai.Client(api_key=API_KEY)
        
        print("🚀 Отправляю запрос на генерацию (рисуем кота)...")
        response = client.models.generate_images(
            model=MODEL_NAME,
            prompt='A cute fluffy cat sitting on a windowsill, photorealistic, 8k',
            config=types.GenerateImagesConfig(number_of_images=1)
        )
        
        if response.generated_images:
            image_bytes = response.generated_images[0].image.image_bytes
            # Пробуем сохранить, чтобы убедиться, что байты пришли
            with open("test_cat.png", "wb") as f:
                f.write(image_bytes)
            print("\n✅ УРА! КЛЮЧ РАБОТАЕТ!")
            print("Картинка test_cat.png успешно сохранена рядом со скриптом.")
        else:
            print("\n❌ Ответ от Google пришел, но он пустой (без картинки).")
            
    except Exception as e:
        print("\n❌ ОШИБКА ГЕНЕРАЦИИ:")
        print("-" * 30)
        print(e)
        print("-" * 30)
        
        # Расшифровка популярных ошибок
        err_str = str(e)
        if "403" in err_str:
            print("💡 СОВЕТ: Ошибка 403. Скорее всего, вы из региона (РФ/РБ), где Image Generation заблокирован.")
            print("Попробуйте создать новый ключ под VPN (США) в новом аккаунте.")
        elif "404" in err_str:
            print("💡 СОВЕТ: Ошибка 404. Модель не найдена. Возможно, у вашего аккаунта нет доступа к 'imagen-3.0'.")
        elif "400" in err_str:
            print("💡 СОВЕТ: Ошибка 400. Billing не включен или нарушены правила безопасности (safety filters).")

if __name__ == "__main__":
    test_google_image()
