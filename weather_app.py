import requests
from dotenv import load_dotenv
import os
import json
import time
from storage import get_cached_data, save_cached_data

load_dotenv()

API_KEY = os.getenv("OW_API_KEY")

CACHE_MAX_AGE_HOURS = 3  # Максимальный возраст кэша в часах

def make_api_request(url: str, error_msg: str, delay: int = 1) -> dict:
    """
    Универсальная функция для выполнения API запросов с обработкой ошибок и повторными попытками
    
    Args:
        url: URL для запроса
        error_msg: Сообщение об ошибке, если данные не получены
        delay: Задержка перед повторной попыткой (для рекурсии)
    
    Returns:
        dict: JSON ответ от API
    """
    try:
        response = requests.get(url)
    except requests.exceptions.RequestException:
        response = None

    if response is None or response.status_code >= 500 or response.status_code == 429:
        if delay <= 4:
            print(f"Сервер недоступен, повторная попытка через {delay} секунд")
            time.sleep(delay)
            delay *= 2
            return make_api_request(url, error_msg, delay)
        raise ConnectionError("Не удалось подключиться")

    check_status_code(response.status_code)
    
    data = response.json()
    if not data:
        raise Exception(error_msg)
    return data

def get_weather(city: str) -> tuple[dict, str]:
    """
    Получить погоду по названию города
    
    Returns:
        tuple: (weather_data, city_name_ru)
    """
    latitude, longitude, city_name = get_coordinates(city)    
    weather_data = get_weather_by_coordinates(latitude, longitude, city_name)
    return weather_data, city_name

def get_weather_by_coordinates(lat:float, lon:float, city_name:str = None) -> dict:
    # Проверяем кэш
    cached = get_cached_data(lat, lon, "weather")
    if cached:
        # Если передано локальное имя, добавляем его в кэшированные данные
        if city_name:
            cached['_local_name'] = city_name
        return cached
    
    # Запрашиваем данные из API
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={API_KEY}&units=metric&lang=ru"
    data = make_api_request(url, f"Нет данных для координат ({lat}, {lon})")
    
    # Если передано локальное имя города, добавляем его в ответ
    if city_name:
        data['_local_name'] = city_name
    
    # Сохраняем в кэш
    save_cached_data(lat, lon, "weather", data)
    
    return data

def get_coordinates(city:str) -> tuple[float, float, str]:
    """
    Получить координаты города с кэшированием
    
    Args:
        city: Название города
        
    Returns:
        tuple: (lat, lon, city_name_ru)
    """
    # Нормализуем название для кэша
    cache_key = city.lower().strip()
    
    # Проверяем кэш (используем lat=0, lon=0 как placeholder для geocoding)
    # Реальный ключ будет: hash(0.0000_0.0000_geocoding_{city})
    cached = get_cached_data(0, 0, f"geocoding_{cache_key}")
    if cached:
        return cached['lat'], cached['lon'], cached['city_name']
    
    # Запрашиваем из API
    url = f"https://api.openweathermap.org/geo/1.0/direct?q={city}&appid={API_KEY}"
    data = make_api_request(url, f"Город {city} не найден")
    
    # Проверяем, что API вернул результаты
    if not data or len(data) == 0:
        raise Exception(f"Город {city} не найден")
    
    # Возвращаем русское название города если есть, иначе английское    
    city_name = data[0].get("local_names", {}).get("ru", data[0]["name"])
    lat = data[0]["lat"]
    lon = data[0]["lon"]
    
    # Сохраняем в кэш
    cache_data = {"lat": lat, "lon": lon, "city_name": city_name}
    save_cached_data(0, 0, f"geocoding_{cache_key}", cache_data)
    
    return lat, lon, city_name

def get_hourly_weather(latitude: float, longitude: float) -> dict:
    # Проверяем кэш
    cached = get_cached_data(latitude, longitude, "forecast")
    if cached:
        return cached
    
    # Запрашиваем данные из API
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={latitude}&lon={longitude}&appid={API_KEY}&units=metric&lang=ru"
    data = make_api_request(url, f"Нет данных для координат ({latitude}, {longitude})")
    
    # Сохраняем в кэш
    save_cached_data(latitude, longitude, "forecast", data)
    
    return data

def get_air_pollution(latitude: float, longitude: float) -> dict:
    # Проверяем кэш
    cached = get_cached_data(latitude, longitude, "air_pollution")
    if cached:
        return cached['list'][0].get('components', {})
    
    # Запрашиваем данные из API
    url = f"https://api.openweathermap.org/data/2.5/air_pollution?lat={latitude}&lon={longitude}&appid={API_KEY}"
    data = make_api_request(url, f"Нет данных для координат ({latitude}, {longitude})")
    
    # Сохраняем в кэш
    save_cached_data(latitude, longitude, "air_pollution", data)
    
    return data['list'][0].get('components', {})

def analyze_air_pollution(air_pollution_data: dict) -> dict:
    """
    Анализирует данные о загрязнении воздуха и определяет качество воздуха
    
    Args:
        air_pollution_data: Словарь с концентрациями загрязнителей
        Пример: {'co': 225.83, 'no': 3.72, 'no2': 8.06, 'o3': 44.71, 'so2': 1.24, 'pm2_5': 8.08, 'pm10': 8.39, 'nh3': 0.37}
    
    Returns:
        dict: Результат анализа с общим индексом и детальной информацией
    """
    # Стандарты качества воздуха (индекс: название, диапазоны для каждого загрязнителя)
    standards = {
        1: {
            "name": "Хорошее",
            "ranges": {
                "so2": (0, 20),
                "no2": (0, 40),
                "pm10": (0, 20),
                "pm2_5": (0, 10),
                "o3": (0, 60),
                "co": (0, 4400)
            }
        },
        2: {
            "name": "Удовлетворительное",
            "ranges": {
                "so2": (20, 80),
                "no2": (40, 70),
                "pm10": (20, 50),
                "pm2_5": (10, 25),
                "o3": (60, 100),
                "co": (4400, 9400)
            }
        },
        3: {
            "name": "Умеренное",
            "ranges": {
                "so2": (80, 250),
                "no2": (70, 150),
                "pm10": (50, 100),
                "pm2_5": (25, 50),
                "o3": (100, 140),
                "co": (9400, 12400)
            }
        },
        4: {
            "name": "Плохое",
            "ranges": {
                "so2": (250, 350),
                "no2": (150, 200),
                "pm10": (100, 200),
                "pm2_5": (50, 75),
                "o3": (140, 180),
                "co": (12400, 15400)
            }
        },
        5: {
            "name": "Очень плохое",
            "ranges": {
                "so2": (350, float('inf')),
                "no2": (200, float('inf')),
                "pm10": (200, float('inf')),
                "pm2_5": (75, float('inf')),
                "o3": (180, float('inf')),
                "co": (15400, float('inf'))
            }
        }
    }
    
    # Определяем индекс для каждого загрязнителя
    pollutant_indices = {}
    for pollutant, value in air_pollution_data.items():
        for index in range(1, 6):
            if pollutant in standards[index]["ranges"]:
                min_val, max_val = standards[index]["ranges"][pollutant]
                if min_val <= value < max_val:
                    pollutant_indices[pollutant] = index
                    break
    
    # Общий индекс - максимальный из всех загрязнителей
    overall_index = max(pollutant_indices.values()) if pollutant_indices else 1
    overall_status = standards[overall_index]["name"]
    
    # Детальная информация о каждом загрязнителе
    details = []
    pollutant_names = {
        "so2": "SO₂ (диоксид серы)",
        "no2": "NO₂ (диоксид азота)",
        "pm10": "PM₁₀ (твердые частицы)",
        "pm2_5": "PM₂.₅ (мелкие частицы)",
        "o3": "O₃ (озон)",
        "co": "CO (угарный газ)",
        "no": "NO (оксид азота)",
        "nh3": "NH₃ (аммиак)"
    }
    
    for pollutant, value in air_pollution_data.items():
        if pollutant in pollutant_indices:
            index = pollutant_indices[pollutant]
            status = standards[index]["name"]
            pollutant_name = pollutant_names.get(pollutant, pollutant.upper())
            
            # Определяем, превышает ли норму (индекс > 1 означает не "Хорошее")
            if index == 1:
                assessment = "в норме"
            elif index == 2:
                assessment = "немного повышен"
            elif index == 3:
                assessment = "умеренно повышен"
            elif index == 4:
                assessment = "значительно повышен"
            else:
                assessment = "критически повышен"
            
            details.append({
                "pollutant": pollutant_name,
                "value": f"{value} мкг/м³",
                "index": index,
                "status": status,
                "assessment": assessment
            })
    
    return {
        "overall_index": overall_index,
        "overall_status": overall_status,
        "details": details
    }



def check_status_code(status_code: int):
    if status_code != 200:
        if status_code == 400:
            error_msg = "Ошибка в запросе, введите название города или координаты"
        elif status_code == 401:
            error_msg = "Ошибка авторизации. Проверьте API ключ"
        else:
            error_msg = f"Ошибка API: {status_code}"
        raise Exception(error_msg)

