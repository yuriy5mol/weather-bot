import json
import os
import time
import hashlib
from pathlib import Path

# Константы
USER_DATA_FILE = "user_data.json"
CACHE_DIR = ".cache"
CACHE_MAX_AGE_SECONDS = 600  # 10 минут

# Создаем директорию для кэша, если её нет
Path(CACHE_DIR).mkdir(exist_ok=True)

# ============= РАБОТА С ДАННЫМИ ПОЛЬЗОВАТЕЛЕЙ =============

def load_user(user_id: int) -> dict:
    """
    Загрузить данные пользователя из файла
    
    Args:
        user_id: ID пользователя Telegram
        
    Returns:
        dict: Данные пользователя или пустой словарь, если пользователь не найден
    """
    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            all_users = json.load(f)
            return all_users.get(str(user_id), {})
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}

def save_user(user_id: int, data: dict) -> None:
    """
    Сохранить данные пользователя в файл
    
    Args:
        user_id: ID пользователя Telegram
        data: Данные для сохранения
    """
    # Загружаем все данные
    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            all_users = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        all_users = {}
    
    # Обновляем данные пользователя
    all_users[str(user_id)] = data
    
    # Сохраняем обратно
    with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(all_users, f, ensure_ascii=False, indent=2)

def load_all_users() -> dict:
    """
    Загрузить данные всех пользователей
    
    Returns:
        dict: Словарь {user_id: user_data}
    """
    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

# ============= РАБОТА С КЭШЕМ API =============

def normalize_coordinates(lat: float, lon: float) -> tuple[float, float]:
    """
    Нормализовать координаты для группировки близких запросов
    
    Округляет координаты до 2 знаков после запятой (~1.1 км точность).
    Это позволяет использовать один кэш для близких точек.
    
    Args:
        lat: Широта
        lon: Долгота
        
    Returns:
        tuple: (нормализованная широта, нормализованная долгота)
    
    Example:
        >>> normalize_coordinates(55.7504461, 37.6174943)
        (55.75, 37.62)
        >>> normalize_coordinates(55.754, 37.6204)
        (55.75, 37.62)  # Те же координаты!
    """
    return round(lat, 2), round(lon, 2)

def _get_cache_key(lat: float, lon: float, endpoint: str) -> str:
    """
    Создать ключ кэша на основе координат и endpoint
    
    Args:
        lat: Широта
        lon: Долгота
        endpoint: Название API endpoint (weather, forecast, air_pollution)
        
    Returns:
        str: Хэш-ключ для кэша
    """
    # Нормализуем координаты для группировки близких запросов
    norm_lat, norm_lon = normalize_coordinates(lat, lon)
    key_string = f"{norm_lat:.2f}_{norm_lon:.2f}_{endpoint}"
    # Создаем MD5 хэш для имени файла
    return hashlib.md5(key_string.encode()).hexdigest()

def _get_cache_path(cache_key: str) -> str:
    """Получить путь к файлу кэша"""
    return os.path.join(CACHE_DIR, f"{cache_key}.json")

def get_cached_data(lat: float, lon: float, endpoint: str) -> dict | None:
    """
    Получить данные из кэша, если они актуальны
    
    Args:
        lat: Широта
        lon: Долгота
        endpoint: Название API endpoint
        
    Returns:
        dict | None: Кэшированные данные или None, если кэш отсутствует/устарел
    """
    cache_key = _get_cache_key(lat, lon, endpoint)
    cache_path = _get_cache_path(cache_key)
    
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cache_data = json.load(f)
        
        # Проверяем возраст кэша
        age = time.time() - cache_data.get("cached_at", 0)
        
        if age < CACHE_MAX_AGE_SECONDS:
            return cache_data.get("data")
        else:
            # Кэш устарел, удаляем
            os.remove(cache_path)
            return None
            
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None

def save_cached_data(lat: float, lon: float, endpoint: str, data: dict) -> None:
    """
    Сохранить данные в кэш
    
    Args:
        lat: Широта (оригинальная)
        lon: Долгота (оригинальная)
        endpoint: Название API endpoint
        data: Данные для кэширования
    """
    cache_key = _get_cache_key(lat, lon, endpoint)
    cache_path = _get_cache_path(cache_key)
    
    # Нормализуем координаты для хранения
    norm_lat, norm_lon = normalize_coordinates(lat, lon)
    
    cache_data = {
        "lat": norm_lat,  # Сохраняем нормализованные координаты
        "lon": norm_lon,
        "endpoint": endpoint,
        "cached_at": time.time(),
        "data": data
    }
    
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

def clear_user_cache(old_lat: float, old_lon: float) -> None:
    """
    Удалить кэш для старых координат пользователя
    
    Args:
        old_lat: Старая широта
        old_lon: Старая долгота
    """
    endpoints = ["weather", "forecast", "air_pollution"]
    
    for endpoint in endpoints:
        cache_key = _get_cache_key(old_lat, old_lon, endpoint)
        cache_path = _get_cache_path(cache_key)
        
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
        except OSError:
            pass  # Игнорируем ошибки удаления

def cleanup_old_cache() -> int:
    """
    Удалить все устаревшие файлы кэша
    
    Returns:
        int: Количество удаленных файлов
    """
    deleted_count = 0
    
    try:
        for filename in os.listdir(CACHE_DIR):
            if filename.endswith(".json"):
                filepath = os.path.join(CACHE_DIR, filename)
                
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        cache_data = json.load(f)
                    
                    age = time.time() - cache_data.get("cached_at", 0)
                    
                    if age >= CACHE_MAX_AGE_SECONDS:
                        os.remove(filepath)
                        deleted_count += 1
                        
                except (json.JSONDecodeError, KeyError, OSError):
                    # Если файл поврежден, удаляем его
                    try:
                        os.remove(filepath)
                        deleted_count += 1
                    except OSError:
                        pass
    except OSError:
        pass
    
    return deleted_count
