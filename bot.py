import asyncio
import logging
from datetime import datetime, timedelta
import time
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import os
from dotenv import load_dotenv

# Импортируем функции из weather_app
from weather_app import (
    get_weather, 
    get_weather_by_coordinates, 
    get_hourly_weather,
    get_air_pollution,
    analyze_air_pollution,
    get_coordinates
)

# Импортируем функции хранения данных
from storage import load_user, save_user, load_all_users, cleanup_old_cache, normalize_coordinates, clear_user_cache

load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# Загружаем данные пользователей из файла
user_data = {}
try:
    all_users = load_all_users()
    # Преобразуем строковые ID в int
    for user_id_str, data in all_users.items():
        user_data[int(user_id_str)] = data
    logger.info(f"Загружено данных {len(user_data)} пользователей")
except Exception as e:
    logger.error(f"Ошибка загрузки данных: {e}")

# Очищаем устаревший кэш при запуске
try:
    deleted = cleanup_old_cache()
    if deleted > 0:
        logger.info(f"Удалено {deleted} устаревших файлов кэша")
except Exception as e:
    logger.error(f"Ошибка очистки кэша: {e}")

# FSM состояния
class WeatherStates(StatesGroup):
    waiting_for_city = State()
    waiting_for_two_cities = State()
    waiting_for_extended_input = State()
    waiting_for_manual_coordinates = State()
    waiting_for_interval = State()
    waiting_for_notification_city = State()

# ============= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ДАННЫХ =============

def update_user_location(user_id: int, lat: float, lon: float, city: str) -> None:
    """
    Обновить местоположение пользователя с очисткой старого кэша
    
    Args:
        user_id: ID пользователя
        lat: Новая широта
        lon: Новая долгота
        city: Название города
    """
    # Нормализуем координаты для согласованности с кэшем
    norm_lat, norm_lon = normalize_coordinates(lat, lon)
    
    # Проверяем, изменились ли координаты
    if user_id in user_data and user_data[user_id].get('location'):
        old_location = user_data[user_id]['location']
        old_lat = old_location.get('lat')
        old_lon = old_location.get('lon')
        
        # Если координаты изменились, очищаем старый кэш
        if old_lat is not None and old_lon is not None:
            if abs(old_lat - norm_lat) > 0.01 or abs(old_lon - norm_lon) > 0.01:  # Изменение > 1км
                clear_user_cache(old_lat, old_lon)
                logger.info(f"Очищен кэш для старых координат пользователя {user_id}")
    
    # Обновляем данные с нормализованными координатами
    if user_id not in user_data:
        user_data[user_id] = {}
    
    user_data[user_id]['location'] = {'lat': norm_lat, 'lon': norm_lon, 'city': city}
    
    # Сохраняем в файл
    save_user(user_id, user_data[user_id])

# ============= КЛАВИАТУРЫ =============

def get_main_menu(user_id=None):
    """Главное меню бота"""
    buttons = [
        [InlineKeyboardButton(text="🌤 Поиск по названию", callback_data="current_weather")],
        [InlineKeyboardButton(text="🧭 Поиск по геолокации", callback_data="geo_search")],
        [InlineKeyboardButton(text="🏛 Сравнение городов", callback_data="compare_cities")],
        [InlineKeyboardButton(text="🔔 Погодные уведомления", callback_data="notifications")]
    ]
    
    # Добавляем кнопку с сохраненным городом, если есть
    if user_id and user_id in user_data and user_data[user_id].get('location'):
        city = user_data[user_id]['location'].get('city', 'Ваше местоположение')
        buttons.insert(0, [InlineKeyboardButton(text=f"📍 Погода {city}", callback_data="weather_saved_location")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_weather_actions_menu(lat=None, lon=None):
    """
    Меню действий после получения погоды
    
    Args:
        lat, lon: Координаты (для inline режима и stateless кнопок)
    """
    coords = f"{lat}|{lon}" if lat is not None and lon is not None else None
    
    ext_cb = f"extended_data|{coords}" if coords else "extended_data"
    fc_cb = f"forecast_5days|{coords}" if coords else "forecast_5days"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Расширенные данные", callback_data=ext_cb)],
        [InlineKeyboardButton(text="📅 Прогноз на 5 дней", callback_data=fc_cb)],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_to_menu")]
    ])

def get_location_keyboard():
    """Клавиатура для отправки геолокации"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить местоположение", request_location=True)],
            [KeyboardButton(text="✏️ Ввести координаты вручную")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    return keyboard

def get_cancel_keyboard():
    """Клавиатура с кнопкой отмены"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")]
    ])

def get_forecast_keyboard(days_data, lat=None, lon=None):
    """Клавиатура для навигации по прогнозу"""
    buttons = []
    coords = f"{lat}|{lon}" if lat is not None and lon is not None else None
    back_cb = f"back_to_weather|{coords}" if coords else "back_to_weather"
    
    for i, day_info in enumerate(days_data):
        date_str = day_info['date']
        day_cb = f"day_{i}|{coords}" if coords else f"day_{i}"
        buttons.append([InlineKeyboardButton(text=f"📅 {date_str}", callback_data=day_cb)])
        
    # Добавляем кнопки навигации
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_button(lat=None, lon=None):
    """Кнопка возврата"""
    coords = f"{lat}|{lon}" if lat is not None and lon is not None else None
    fc_cb = f"forecast_5days|{coords}" if coords else "forecast_5days"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к прогнозу", callback_data=fc_cb)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")]
    ])

def get_extended_data_keyboard(lat=None, lon=None):
    """Клавиатура для расширенных данных"""
    coords = f"{lat}|{lon}" if lat is not None and lon is not None else None
    back_cb = f"back_to_weather|{coords}" if coords else "back_to_weather"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")]
    ])

def get_forecast_navigation_keyboard(lat=None, lon=None):
    """Клавиатура навигации для прогноза"""
    coords = f"{lat}|{lon}" if lat is not None and lon is not None else None
    back_cb = f"back_to_weather|{coords}" if coords else "back_to_weather"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")]
    ])

def get_main_menu_button():
    """Простая кнопка главного меню"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")]
    ])

def get_notifications_keyboard(user_id, is_enabled):
    """Клавиатура управления уведомлениями"""
    notif_data = user_data[user_id].get('notification_data', {})
    location = notif_data.get('location', {}).get('city', 'Не задан')
    interval = notif_data.get('interval', 2)
    
    status = "✅ Включены" if is_enabled else "❌ Выключены"
    toggle_action = "Выключить" if is_enabled else "Включить"
    
    keyboard = [
        [InlineKeyboardButton(text=f"Статус: {status}", callback_data="noop")],
        [InlineKeyboardButton(text=f"🏙 Город: {location}", callback_data="set_notification_city")],
        [InlineKeyboardButton(text=f"⏱ Интервал: {interval}ч", callback_data="set_notification_interval")],
        [InlineKeyboardButton(text=f"🔔 {toggle_action}", callback_data="toggle_notifications")],
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ============= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =============

def format_weather_message(data: dict) -> str:
    """Форматирование сообщения о текущей погоде"""
    temp = data['main']['temp']
    feels_like = data['main']['feels_like']
    humidity = data['main']['humidity']
    pressure = data['main']['pressure']
    wind_speed = data['wind']['speed']
    description = data['weather'][0]['description'].capitalize()
    # Используем локальное имя, если есть, иначе из API
    city = data.get('_local_name', data['name'])
    country = data['sys']['country']
    
    message = f"🌍 <b>{city}, {country}</b>\n\n"
    message += f"🌡 Температура: <b>{temp}°C</b>\n"
    message += f"🤔 Ощущается как: {feels_like}°C\n"
    message += f"💧 Влажность: {humidity}%\n"
    message += f"🌪 Ветер: {wind_speed} м/с\n"
    message += f"📊 Давление: {pressure} гПа\n"
    message += f"☁️ {description}"
    
    return message


POLLUTANT_ICONS = {
    "CO": "🚗",
    "NO₂": "🏭",
    "NO": "🏭",
    "O₃": "🛡️",
    "SO₂": "🌋",
    "PM₂.₅": "😷",
    "PM₁₀": "🌫",
    "NH₃": "🤢"
}

ASSESSMENT_ICONS = {
    "в норме": "🟢",
    "немного повышен": "🟡",
    "повышен": "🟠",
    "высокий": "🔴",
    "очень высокий": "🟣",
    "критический": "☠️"
}

def get_pollutant_emoji(name):
    # Проверяем точное совпадение ключа в начале строки (так как name = "NO₂ (диоксид азота)")
    for key, icon in POLLUTANT_ICONS.items():
        if name.startswith(key):
            return icon
    return "🧪"

def get_assessment_emoji(assessment):
    return ASSESSMENT_ICONS.get(assessment, "⚪")

def format_extended_weather(weather_data: dict, air_data: dict, pollution_analysis: dict) -> str:
    """Форматирование расширенных данных о погоде"""
    # Используем локальное имя, если есть
    city = weather_data.get('_local_name', weather_data['name'])
    country = weather_data['sys']['country']
    temp = weather_data['main']['temp']
    feels_like = weather_data['main']['feels_like']
    humidity = weather_data['main']['humidity']
    pressure = weather_data['main']['pressure']
    wind_speed = weather_data['wind']['speed']
    description = weather_data['weather'][0]['description'].capitalize()
    cloudiness = weather_data['clouds']['all']
    
    # Восход и закат
    sunrise = datetime.fromtimestamp(weather_data['sys']['sunrise']).strftime('%H:%M')
    sunset = datetime.fromtimestamp(weather_data['sys']['sunset']).strftime('%H:%M')
    
    # UV индекс (если есть)
    uvi = weather_data.get('uvi', 'Н/Д')
    
    message = f"🌍 <b>{city}, {country}</b>\n\n"
    message += f"<b>📊 ОСНОВНЫЕ ДАННЫЕ</b>\n"
    message += f"🌡 Температура: <b>{temp}°C</b> (ощущается как {feels_like}°C)\n"
    message += f"💧 Влажность: {humidity}%\n"
    message += f"🌪 Ветер: {wind_speed} м/с\n"
    message += f"📊 Давление: {pressure} гПа\n"
    message += f"☁️ Облачность: {cloudiness}%\n"
    message += f"🌤 {description}\n\n"
    
    message += f"<b>🌅 СОЛНЦЕ</b>\n"
    message += f"🌄 Восход: {sunrise}\n"
    message += f"🌇 Закат: {sunset}\n\n"
    
    if uvi != 'Н/Д':
        message += f"<b>☀️ UV ИНДЕКС</b>\n"
        message += f"UV: {uvi}\n\n"
    
    # Загрязнение воздуха
    message += f"<b>🏭 КАЧЕСТВО ВОЗДУХА</b>\n"
    message += f"Общий статус: <b>{pollution_analysis['overall_status']}</b>\n\n"
    
    if pollution_analysis['details']:
        message += "<b>Детали загрязнения:</b>\n"
        for detail in pollution_analysis['details'][:6]:  # Показываем первые 6
            pollutant_name = detail['pollutant']
            icon = get_pollutant_emoji(pollutant_name)
            assessment = detail['assessment']
            status_icon = get_assessment_emoji(assessment)
            
            message += f"{icon} {pollutant_name}: {detail['value']} - {status_icon} {assessment}\n"
    
    return message

def parse_forecast_data(forecast_data: dict) -> list:
    """Парсинг данных прогноза на 5 дней"""
    daily_forecasts = {}
    
    for item in forecast_data['list']:
        dt = datetime.fromtimestamp(item['dt'])
        date_key = dt.strftime('%Y-%m-%d')
        
        if date_key not in daily_forecasts:
            daily_forecasts[date_key] = {
                'date': dt.strftime('%d.%m (%a)'),
                'temps': [],
                'descriptions': [],
                'humidity': [],
                'wind': [],
                'items': []
            }
        
        daily_forecasts[date_key]['temps'].append(item['main']['temp'])
        daily_forecasts[date_key]['descriptions'].append(item['weather'][0]['description'])
        daily_forecasts[date_key]['humidity'].append(item['main']['humidity'])
        daily_forecasts[date_key]['wind'].append(item['wind']['speed'])
        daily_forecasts[date_key]['items'].append(item)
    
    # Формируем итоговый список
    result = []
    for date_key in sorted(daily_forecasts.keys())[:5]:
        day_data = daily_forecasts[date_key]
        result.append({
            'date': day_data['date'],
            'temp_min': min(day_data['temps']),
            'temp_max': max(day_data['temps']),
            'description': max(set(day_data['descriptions']), key=day_data['descriptions'].count),
            'humidity_avg': sum(day_data['humidity']) // len(day_data['humidity']),
            'wind_avg': sum(day_data['wind']) / len(day_data['wind']),
            'items': day_data['items']
        })
    
    return result


WEATHER_ICONS = {
    "01d": "☀️", "01n": "🌙",
    "02d": "⛅", "02n": "☁️",
    "03d": "☁️", "03n": "☁️",
    "04d": "☁️", "04n": "☁️",
    "09d": "🌧", "09n": "🌧",
    "10d": "🌦", "10n": "🌧",
    "11d": "⛈", "11n": "⛈",
    "13d": "❄️", "13n": "❄️",
    "50d": "🌫", "50n": "🌫"
}

def get_weather_emoji(icon_code):
    return WEATHER_ICONS.get(icon_code, "•")

def format_day_details(day_data: dict) -> str:
    """Форматирование детальной информации о дне"""
    message = f"📅 <b>{day_data['date']}</b>\n\n"
    message += f"🌡 Температура: {day_data['temp_min']:.1f}°C ... {day_data['temp_max']:.1f}°C\n"
    message += f"☁️ {day_data['description'].capitalize()}\n"
    message += f"💧 Влажность: ~{day_data['humidity_avg']}%\n"
    message += f"🌪 Ветер: ~{day_data['wind_avg']:.1f} м/с\n\n"
    
    message += "<b>Почасовой прогноз:</b>\n"
    for item in day_data['items'][:8]:  # Показываем до 8 записей
        dt = datetime.fromtimestamp(item['dt'])
        time_str = dt.strftime('%H:%M')
        temp = item['main']['temp']
        desc = item['weather'][0]['description']
        icon_code = item['weather'][0]['icon']
        emoji = get_weather_emoji(icon_code)
        
        message += f"{emoji} {time_str}: {temp}°C, {desc}\n"
    
    return message

def format_comparison(city1_data: dict, city2_data: dict) -> str:
    """Форматирование сравнения двух городов"""
    # Используем локальные имена, если есть
    city1 = city1_data.get('_local_name', city1_data['name'])
    country1 = city1_data['sys']['country']
    city2 = city2_data.get('_local_name', city2_data['name'])
    country2 = city2_data['sys']['country']
    
    temp1 = city1_data['main']['temp']
    temp2 = city2_data['main']['temp']
    
    feels1 = city1_data['main']['feels_like']
    feels2 = city2_data['main']['feels_like']
    
    humidity1 = city1_data['main']['humidity']
    humidity2 = city2_data['main']['humidity']
    
    wind1 = city1_data['wind']['speed']
    wind2 = city2_data['wind']['speed']
    
    desc1 = city1_data['weather'][0]['description']
    desc2 = city2_data['weather'][0]['description']
    
    message = f"🏙 <b>Сравнение городов</b>\n\n"
    message += f"<b>{city1}, {country1}</b> vs <b>{city2}, {country2}</b>\n\n"
    message += f"🌡 Температура:\n"
    message += f"  • {city1}: <b>{temp1}°C</b>\n"
    message += f"  • {city2}: <b>{temp2}°C</b>\n"
    message += f"  Разница: {abs(temp1 - temp2):.1f}°C\n\n"
    
    message += f"🤔 Ощущается:\n"
    message += f"  • {city1}: {feels1}°C\n"
    message += f"  • {city2}: {feels2}°C\n\n"
    
    message += f"💧 Влажность:\n"
    message += f"  • {city1}: {humidity1}%\n"
    message += f"  • {city2}: {humidity2}%\n\n"
    
    message += f"🌪 Ветер:\n"
    message += f"  • {city1}: {wind1} м/с\n"
    message += f"  • {city2}: {wind2} м/с\n\n"
    
    message += f"☁️ Условия:\n"
    message += f"  • {city1}: {desc1}\n"
    message += f"  • {city2}: {desc2}"
    
    return message

# ============= ОБРАБОТЧИКИ КОМАНД =============

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    if user_id not in user_data:
        user_data[user_id] = {'location': None}
    
    welcome_text = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я бот для прогноза погоды. Выберите интересующее вас действие из меню ниже: 👇"
    )
    
    await message.answer(welcome_text, reply_markup=get_main_menu(user_id))

# ============= ОБРАБОТЧИКИ CALLBACK =============

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    """Возврат в главное меню"""
    user_id = callback.from_user.id
    await callback.message.edit_text(
        "Выберите интересующее вас действие из меню ниже: 👇",
        reply_markup=get_main_menu(user_id)
    )
    await callback.answer()


@router.callback_query(F.data == "weather_saved_location")
async def weather_saved_location(callback: CallbackQuery):
    """Погода для сохраненного местоположения"""
    user_id = callback.from_user.id
    
    if user_id not in user_data or not user_data[user_id].get('location'):
        await callback.answer("Местоположение не сохранено", show_alert=True)
        return
    
    location = user_data[user_id]['location']
    
    try:
        weather_data = get_weather_by_coordinates(location['lat'], location['lon'], location.get('city'))
        formatted_message = format_weather_message(weather_data)
        await callback.message.edit_text(
            formatted_message, 
            parse_mode="HTML", 
            reply_markup=get_weather_actions_menu(location['lat'], location['lon'])
        )
        await callback.answer()
    except Exception as e:
        await callback.answer("Не удалось получить погоду", show_alert=True)

@router.callback_query(F.data.startswith("back_to_weather"))
async def back_to_weather(callback: CallbackQuery):
    """Возврат к погоде"""
    user_id = callback.from_user.id
    
    lat_param, lon_param = None, None
    if "|" in callback.data:
        parts = callback.data.split("|")
        if len(parts) >= 3:
            lat_param = parts[1]
            lon_param = parts[2]
    
    try:
        if lat_param and lon_param:
            lat, lon = float(lat_param), float(lon_param)
            city_name = None 
        elif user_id in user_data and user_data[user_id].get('location'):
            location = user_data[user_id]['location']
            lat, lon, city_name = location['lat'], location['lon'], location.get('city')
        else:
            await callback.answer("Местоположение не сохранено", show_alert=True)
            return

        weather_data = get_weather_by_coordinates(lat, lon, city_name)
        formatted_message = format_weather_message(weather_data)
        
        reply_markup = get_weather_actions_menu(lat, lon)
        
        if callback.inline_message_id:
             await bot.edit_message_text(
                text=formatted_message,
                inline_message_id=callback.inline_message_id,
                parse_mode="HTML",
                reply_markup=reply_markup
             )
        else:
            try:
                await callback.message.edit_text(formatted_message, parse_mode="HTML", reply_markup=reply_markup)
            except Exception:
                pass  # Игнорируем ошибку, если сообщение не изменилось
            
        await callback.answer()
    except Exception as e:
        await callback.answer("Не удалось получить погоду", show_alert=True)

@router.callback_query(F.data == "current_weather")
async def current_weather_callback(callback: CallbackQuery, state: FSMContext):
    """Запрос текущей погоды"""
    try:
        await callback.message.edit_text("🌤 Введите название города:")
    except Exception:
        pass
    await state.set_state(WeatherStates.waiting_for_city)
    await callback.answer()

@router.message(WeatherStates.waiting_for_city)
async def process_city_weather(message: Message, state: FSMContext):
    """Обработка ввода города для текущей погоды"""
    user_id = message.from_user.id
    city = message.text.strip()
    
    try:
        # Получаем погоду и русское название города
        weather_data, city_name_ru = get_weather(city)        
        # Сохраняем местоположение пользователя с русским названием
        lat = weather_data['coord']['lat']
        lon = weather_data['coord']['lon']
        update_user_location(user_id, lat, lon, city_name_ru)
        formatted_message = format_weather_message(weather_data)
        await message.answer(
            formatted_message, 
            parse_mode="HTML", 
            reply_markup=get_weather_actions_menu(lat, lon)
        )
        await state.clear()
    except Exception as e:
        await message.answer("❌ Город не найден.\n\nПопробуйте другое название.", 
                           reply_markup=get_main_menu(user_id))
        await state.clear()

@router.callback_query(F.data.startswith("forecast_5days"))
async def forecast_5days_callback(callback: CallbackQuery):
    """Прогноз на 5 дней"""
    user_id = callback.from_user.id
    
    lat_param, lon_param = None, None
    if "|" in callback.data:
        parts = callback.data.split("|")
        if len(parts) >= 3:
            lat_param = parts[1]
            lon_param = parts[2]
            
    try:
        lat, lon, city_name = None, None, None
        
        if lat_param and lon_param:
            lat, lon = float(lat_param), float(lon_param)
        elif user_id in user_data and user_data[user_id].get('location'):
            location = user_data[user_id]['location']
            lat, lon = location['lat'], location['lon']
            city_name = location.get('city', 'Ваше местоположение')
        else:
            if not callback.inline_message_id:
                try:
                    await callback.message.edit_text(
                        "📍 Сначала отправьте свое местоположение, чтобы получить прогноз.",
                        reply_markup=get_main_menu()
                    )
                except:
                    pass
            await callback.answer("Местоположение не сохранено")
            return
        
        forecast_data = get_hourly_weather(lat, lon)
        days_data = parse_forecast_data(forecast_data)
        
        message_text = f"📅 <b>Прогноз на 5 дней</b>\n🌍 {city_name}\n\nВыберите день:"
        reply_markup = get_forecast_keyboard(days_data, city_name)
        
        if callback.inline_message_id:
             await bot.edit_message_text(
                text=message_text,
                inline_message_id=callback.inline_message_id,
                parse_mode="HTML",
                reply_markup=reply_markup
             )
        else:
            try:
                await callback.message.edit_text(
                    message_text,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            except:
                pass
        await callback.answer()
    except Exception as e:
        error_text = f"❌ Ошибка получения прогноза: {str(e)}"
        if callback.inline_message_id:
             # В inline режиме просто показываем алерт, чтобы не ломать сообщение
             await callback.answer(error_text, show_alert=True)
        else:
            try:
                await callback.message.edit_text(
                    error_text,
                    reply_markup=get_main_menu()
                )
            except:
                pass
            await callback.answer()

@router.callback_query(F.data.startswith("day_"))
async def show_day_details(callback: CallbackQuery):
    """Показать детали конкретного дня"""
    user_id = callback.from_user.id
    
    # Парсим данные: day_0|lat|lon
    data_parts = callback.data.split("|")
    day_part = data_parts[0]
    day_index = int(day_part.split("_")[1])
    
    lat_param, lon_param = None, None
    if len(data_parts) >= 3:
        lat_param =  data_parts[1]
        lon_param = data_parts[2]
        
    try:
        lat, lon = None, None
        
        if lat_param and lon_param:
             lat, lon = float(lat_param), float(lon_param)
        elif user_id in user_data and user_data[user_id].get('location'):
             location = user_data[user_id]['location']
             lat, lon = location['lat'], location['lon']
             city_name = location.get('city', 'Ваше местоположение')
        else:
             if not callback.inline_message_id:
                 await callback.answer("Местоположение не найдено", show_alert=True)
             else:
                 await callback.answer("Местоположение не найдено. Попробуйте обновить поиск.", show_alert=True)
             return

        # Получаем данные прогноза из кэша
        forecast_data = get_hourly_weather(lat, lon)
        days_data = parse_forecast_data(forecast_data)
        
        if day_index >= len(days_data):
            await callback.answer("День не найден", show_alert=True)
            return
        
        day_data = days_data[day_index]
        message_text = format_day_details(day_data)
        
        reply_markup = get_back_button(lat, lon)
        
        if callback.inline_message_id:
             await bot.edit_message_text(
                text=message_text,
                inline_message_id=callback.inline_message_id,
                parse_mode="HTML",
                reply_markup=reply_markup
             )
        else:
            await callback.message.edit_text(
                message_text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {str(e)}", show_alert=True)

@router.callback_query(F.data == "geo_search")
async def geo_search_callback(callback: CallbackQuery, state: FSMContext):
    """Поиск по геолокации"""
    await callback.message.answer(
        "📍 Отправьте свое местоположение или введите координаты вручную:\n\n"
        "ℹ️ Если Telegram не может отправить геолокацию, нажмите кнопку ниже.",
        reply_markup=get_location_keyboard()
    )
    await state.set_state(WeatherStates.waiting_for_manual_coordinates)
    await callback.answer()

@router.message(F.location)
async def process_location(message: Message):
    """Обработка полученной геолокации"""
    user_id = message.from_user.id
    lat = message.location.latitude
    lon = message.location.longitude
    
    try:
        weather_data = get_weather_by_coordinates(lat, lon)
        city_name = weather_data['name']
        
        # Сохраняем местоположение пользователя
        # Сохраняем местоположение пользователя
        update_user_location(user_id, lat, lon, city_name)
        
        # Удаляем клавиатуру геолокации
        try:
            msg = await message.answer("🔎", reply_markup=ReplyKeyboardRemove())
            await msg.delete()
        except:
            pass
        
        formatted_message = format_weather_message(weather_data)
        await message.answer(
            f"✅ Местоположение сохранено!\n\n{formatted_message}",
            parse_mode="HTML",
            reply_markup=get_weather_actions_menu(lat, lon)
        )
    except Exception as e:
        await message.answer(
            "❌ Не удалось получить погоду для этого местоположения.",
            reply_markup=get_main_menu(user_id)
        )

@router.message(WeatherStates.waiting_for_manual_coordinates)
async def process_manual_coordinates(message: Message, state: FSMContext):
    """Обработка ручного ввода координат"""
    user_id = message.from_user.id
    
    # Проверяем, нажал ли пользователь кнопку ручного ввода
    if message.text == "✏️ Ввести координаты вручную":
        await message.answer(
            "📝 Введите координаты в формате:\n"
            "<code>широта, долгота</code>\n\n"
            "Например: <code>55.7558, 37.6173</code> (Москва)\n\n"
            "ℹ️ Координаты можно найти в Google Maps или Яндекс.Картах",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    # Парсим координаты
    try:
        parts = message.text.replace(',', ' ').split()
        if len(parts) != 2:
            await message.answer(
                "❌ Неверный формат! Введите координаты в формате:\n"
                "<code>широта, долгота</code>\n\n"
                "Например: <code>55.7558, 37.6173</code>",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard()
            )
            return
        
        lat = float(parts[0])
        lon = float(parts[1])
        
        # Проверяем диапазон координат
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            await message.answer(
                "❌ Координаты вне допустимого диапазона!\n"
                "Широта: от -90 до 90\n"
                "Долгота: от -180 до 180",
                reply_markup=get_cancel_keyboard()
            )
            return
        
        # Получаем погоду по координатам
        weather_data = get_weather_by_coordinates(lat, lon)
        city_name = weather_data['name']
        
        # Сохраняем местоположение пользователя
        # Сохраняем местоположение пользователя
        update_user_location(user_id, lat, lon, city_name)
        
        # Удаляем клавиатуру
        try:
            msg = await message.answer("🔎", reply_markup=ReplyKeyboardRemove())
            await msg.delete()
        except:
            pass
        
        formatted_message = format_weather_message(weather_data)
        await message.answer(
            f"✅ Местоположение сохранено!\n\n{formatted_message}",
            parse_mode="HTML",
            reply_markup=get_weather_actions_menu(lat, lon)
        )
        await state.clear()
        
    except ValueError:
        await message.answer(
            "❌ Ошибка! Координаты должны быть числами.\n\n"
            "Введите в формате: <code>55.7558, 37.6173</code>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )
    except Exception as e:
        await message.answer(
            f"❌ Ошибка получения погоды: {str(e)}",
            reply_markup=get_main_menu()
        )
        await state.clear()

@router.callback_query(F.data == "notifications")
async def notifications_menu(callback: CallbackQuery):
    """Меню уведомлений"""
    user_id = callback.from_user.id
    
    # Инициализируем данные если их нет
    if user_id not in user_data:
        user_data[user_id] = {'location': None}
        
    notif_data = user_data[user_id].get('notification_data')
    is_enabled = notif_data.get('enabled', False) if notif_data else False
    
    await callback.message.edit_text(
        "🔔 <b>Погодные уведомления</b>\n\n"
        "Настройте уведомления, чтобы получать погоду по расписанию.\n"
        "Уведомления приходят независимо от изменений погоды.",
        reply_markup=get_notifications_keyboard(user_id, is_enabled),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "toggle_notifications")
async def toggle_notifications(callback: CallbackQuery):
    """Включение/выключение уведомлений"""
    user_id = callback.from_user.id
    
    notif_data = user_data[user_id].get('notification_data', {})
    is_enabled = not notif_data.get('enabled', False)
    
    if is_enabled:
        # Включаем
        # Если локация для уведомлений не задана, пробуем взять из основной
        if not notif_data.get('location'):
            current_loc = user_data[user_id].get('location')
            if current_loc:
                notif_data['location'] = current_loc
            else:
                await callback.answer("Сначала задайте город для уведомлений!", show_alert=True)
                return

        notif_data['enabled'] = True
        notif_data['interval'] = notif_data.get('interval', 2)
        # Запускаем через интервал (не сразу)
        notif_data['next_run'] = time.time() + (notif_data['interval'] * 3600)
        
        user_data[user_id]['notification_data'] = notif_data
        save_user(user_id, user_data[user_id])
        
        schedule_user_notification(user_id)
        status_text = "включены"
    else:
        # Выключаем
        notif_data['enabled'] = False
        user_data[user_id]['notification_data'] = notif_data
        save_user(user_id, user_data[user_id])
        
        schedule_user_notification(user_id)
        status_text = "выключены"
        
    await callback.message.edit_reply_markup(
        reply_markup=get_notifications_keyboard(user_id, is_enabled)
    )
    await callback.answer(f"Уведомления {status_text}")

@router.callback_query(F.data == "set_notification_city")
async def set_notification_city_start(callback: CallbackQuery, state: FSMContext):
    """Начало настройки города для уведомлений"""
    await callback.message.edit_text(
        "🏙 Введите название города для уведомлений:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(WeatherStates.waiting_for_notification_city)
    await callback.answer()

@router.message(WeatherStates.waiting_for_notification_city)
async def set_notification_city_finish(message: Message, state: FSMContext):
    """Сохранение города для уведомлений"""
    user_id = message.from_user.id
    city = message.text.strip()
    
    try:
        lat, lon, city_name = get_coordinates(city)
        
        if 'notification_data' not in user_data[user_id]:
            user_data[user_id]['notification_data'] = {}
            
        user_data[user_id]['notification_data']['location'] = {
            'lat': lat, 'lon': lon, 'city': city_name
        }
        
        # Если уведомления включены, обновляем задачу
        if user_data[user_id]['notification_data'].get('enabled'):
             schedule_user_notification(user_id)

        save_user(user_id, user_data[user_id])
        
        # Перепланируем если включено (чтобы обновить данные, но время останется прежним)
        if user_data[user_id]['notification_data'].get('enabled'):
            schedule_user_notification(user_id)
        
        await message.answer(
            f"✅ Город для уведомлений установлен: {city_name}",
            reply_markup=get_notifications_keyboard(user_id, user_data[user_id]['notification_data'].get('enabled'))
        )
        await state.clear()
        
    except Exception as e:
        await message.answer(
            "❌ Город не найден. Попробуйте другое название.",
            reply_markup=get_cancel_keyboard()
        )

@router.callback_query(F.data == "set_notification_interval")
async def set_notification_interval_start(callback: CallbackQuery, state: FSMContext):
    """Начало настройки интервала"""
    await callback.message.edit_text(
        "⏱ Введите интервал в часах (например: 2, 24, или 0.1 для теста):",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(WeatherStates.waiting_for_interval)
    await callback.answer()

@router.message(WeatherStates.waiting_for_interval)
async def set_notification_interval_finish(message: Message, state: FSMContext):
    """Сохранение интервала"""
    user_id = message.from_user.id
    try:
        # Заменяем запятую на точку для поддержки ввода с телефона
        interval_text = message.text.strip().replace(',', '.')
        interval = float(interval_text)
        
        if interval <= 0:
            raise ValueError
        
        if 'notification_data' not in user_data[user_id]:
             user_data[user_id]['notification_data'] = {}
             
        user_data[user_id]['notification_data']['interval'] = interval
        # Сбрасываем таймер на новый интервал (чтобы не ждать старого огромного времени или не получать старое короткое)
        user_data[user_id]['notification_data']['next_run'] = time.time() + (interval * 3600)
        
        save_user(user_id, user_data[user_id])
        
        # Перепланируем если включено
        if user_data[user_id]['notification_data'].get('enabled'):
            schedule_user_notification(user_id)
            
        await message.answer(
            f"✅ Интервал установлен: {interval} ч.\nСледующее уведомление через {interval} ч.",
            reply_markup=get_notifications_keyboard(user_id, user_data[user_id]['notification_data'].get('enabled'))
        )
        await state.clear()
    except ValueError:
        await message.answer(
            "❌ Пожалуйста, введите положительное число (например: 0.5).",
            reply_markup=get_cancel_keyboard()
        )

@router.callback_query(F.data == "compare_cities")
async def compare_cities_callback(callback: CallbackQuery, state: FSMContext):
    """Сравнение городов"""
    await callback.message.edit_text(
        "🏙 Введите два города через запятую:\n\nНапример: Москва, Санкт-Петербург"
    )
    await state.set_state(WeatherStates.waiting_for_two_cities)
    await callback.answer()

@router.message(WeatherStates.waiting_for_two_cities)
async def process_city_comparison(message: Message, state: FSMContext):
    """Обработка сравнения городов"""
    user_id = message.from_user.id
    cities = [city.strip() for city in message.text.split(',')]
    
    if len(cities) != 2:
        await message.answer(
            "❌ Пожалуйста, введите ровно два города через запятую.",
            reply_markup=get_main_menu(user_id)
        )
        await state.clear()
        return
    
    try:
        city1_data, _ = get_weather(cities[0])
        city2_data, _ = get_weather(cities[1])
        
        comparison_message = format_comparison(city1_data, city2_data)
        await message.answer(comparison_message, parse_mode="HTML", reply_markup=get_main_menu_button())
        await state.clear()
    except Exception as e:
        await message.answer(
            "❌ Не удалось найти один или оба города.\n\nПроверьте правильность названий.",
            reply_markup=get_main_menu(user_id)
        )
        await state.clear()

@router.callback_query(F.data.startswith("extended_data"))
async def extended_data_callback(callback: CallbackQuery, state: FSMContext):
    """Расширенные данные о погоде"""
    user_id = callback.from_user.id
    
    lat_param, lon_param = None, None
    if "|" in callback.data:
        parts = callback.data.split("|")
        if len(parts) >= 3:
            lat_param = parts[1]
            lon_param = parts[2]
    
    # Пытаемся получить координаты
    try:
        if lat_param and lon_param:
            lat, lon = float(lat_param), float(lon_param)
            city_name = None
        elif user_id in user_data and user_data[user_id].get('location'):
             location = user_data[user_id]['location']
             lat, lon = location['lat'], location['lon']
             city_name = location.get('city', 'Ваше местоположение')
        else:
            # Если местоположения нет и это не inline, просим ввести
            if not callback.inline_message_id:
                await callback.message.edit_text(
                    "📊 Введите название города или отправьте геолокацию для получения расширенных данных:"
                )
                await state.set_state(WeatherStates.waiting_for_extended_input)
            else:
                await callback.answer("Местоположение не задано", show_alert=True)
            await callback.answer()
            return

        weather_data = get_weather_by_coordinates(lat, lon)
        air_data = get_air_pollution(lat, lon)
        pollution_analysis = analyze_air_pollution(air_data)
        
        extended_message = format_extended_weather(weather_data, air_data, pollution_analysis)
        
        reply_markup = get_extended_data_keyboard(lat, lon)
        
        if callback.inline_message_id:
             await bot.edit_message_text(
                text=extended_message,
                inline_message_id=callback.inline_message_id,
                parse_mode="HTML",
                reply_markup=reply_markup
             )
        else:
            # Если не удалось отредактировать, отправляем новое сообщение
            try:
                await callback.message.edit_text(
                    extended_message,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            except:
                await callback.message.answer(
                    extended_message,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
        await callback.answer()
        
    except Exception as e:
        error_text = f"❌ Ошибка получения данных: {str(e)}"
        if callback.inline_message_id:
            await callback.answer(error_text, show_alert=True)
        else:
            try:
                await callback.message.edit_text(
                    error_text,
                    reply_markup=get_main_menu()
                )
            except:
                pass
        await callback.answer()

@router.message(WeatherStates.waiting_for_extended_input)
async def process_extended_data(message: Message, state: FSMContext):
    """Обработка запроса расширенных данных"""
    
    if message.location:
        # Обработка геолокации
        lat = message.location.latitude
        lon = message.location.longitude
    elif message.text:
        # Обработка названия города
        try:
            lat, lon, city_name = get_coordinates(message.text.strip())
        except Exception as e:
            await message.answer(
                f"❌ Ошибка: {str(e)}",
                reply_markup=get_main_menu()
            )
            await state.clear()
            return
    else:
        await message.answer(
            "❌ Пожалуйста, введите название города или отправьте геолокацию.",
            reply_markup=get_main_menu()
        )
        await state.clear()
        return
    
    try:
        weather_data = get_weather_by_coordinates(lat, lon)
        air_data = get_air_pollution(lat, lon)
        pollution_analysis = analyze_air_pollution(air_data)
        
        extended_message = format_extended_weather(weather_data, air_data, pollution_analysis)
        await message.answer(extended_message, parse_mode="HTML", reply_markup=get_main_menu())
        await state.clear()
    except Exception as e:
        await message.answer(
            f"❌ Ошибка получения данных: {str(e)}",
            reply_markup=get_main_menu()
        )
        await state.clear()

@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    """Пустой callback для информационных кнопок"""
    await callback.answer()

@router.inline_query()
async def inline_weather_query(query: InlineQuery):
    """Обработка inline-запросов"""
    text = query.query.strip()
    
    if not text:
        return
        
    try:
        # Пытаемся получить погоду
        weather_data, city_name_ru = get_weather(text)
        
        # Получаем координаты из ответа API
        lat = weather_data['coord']['lat']
        lon = weather_data['coord']['lon']
        
        # Формируем сообщение
        message_text = format_weather_message(weather_data)
        
        # Получаем иконку
        icon_code = weather_data['weather'][0]['icon']
        icon_url = f"https://openweathermap.org/img/wn/{icon_code}@2x.png"
        
        # Получаем данные о боте для ссылки
        bot_info = await bot.get_me()
        bot_link = f"https://t.me/{bot_info.username}"
        
        # Обновляем текст сообщения, добавляя ссылку
        message_text += f"\n\n🤖 <a href='{bot_link}'>Посмотреть в боте</a>"
        
        # Получаем клавиатуру с действиями, используя координаты
        reply_markup = get_weather_actions_menu(lat, lon)
        
        # Создаем результат
        result = InlineQueryResultArticle(
            id=str(time.time()),
            title=f"{city_name_ru}: {weather_data['main']['temp']}°C",
            description=f"{weather_data['weather'][0]['description'].capitalize()}",
            input_message_content=InputTextMessageContent(
                message_text=message_text,
                parse_mode="HTML"
            ),
            thumbnail_url=icon_url,
            thumbnail_width=100,
            thumbnail_height=100
        )
        
        await query.answer([result], cache_time=1, is_personal=False)
        
    except Exception as e:
        # Если ошибка, просто не отвечаем (Telegram покажет пустой список)
        # Логируем для отладки
        # logger.error(f"Inline error: {e}")
        pass

# ============= ФОНОВЫЕ ЗАДАЧИ =============

# Глобальный планировщик
scheduler = AsyncIOScheduler()


async def send_weather_notification(user_id: int):
    """Отправка уведомления о погоде пользователю"""
    try:
        if user_id not in user_data:
            return
            
        notif_data = user_data[user_id].get('notification_data')
        if not notif_data or not notif_data.get('enabled') or not notif_data.get('location'):
            return
            
        location = notif_data['location']
        weather_data = get_weather_by_coordinates(location['lat'], location['lon'])
        
        # Обновляем время следующего запуска
        interval = notif_data.get('interval', 2)
        notif_data['next_run'] = time.time() + (interval * 3600)
        save_user(user_id, user_data[user_id])
        
        # Формируем сообщение
        temp = weather_data['main']['temp']
        description = weather_data['weather'][0]['description']
        city = location['city']
        
        message = (
            f"🔔 <b>Погодное уведомление</b>\n"
            f"🌍 {city}: {description.capitalize()}\n"
            f"🌡 Температура: {temp}°C\n"
            f"💨 Ветер: {weather_data['wind']['speed']} м/с"
        )
        
        
        # Генерация клавиатуры с действиями
        reply_markup = get_weather_actions_menu(location['lat'], location['lon'])
        
        await bot.send_message(user_id, message, parse_mode="HTML", reply_markup=reply_markup)
        logger.info(f"Отправлено уведомление пользователю {user_id}")
        
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления для {user_id}: {e}")

def schedule_user_notification(user_id: int):
    """Планирование задачи уведомления для пользователя"""
    job_id = f"weather_notif_{user_id}"
    
    # Удаляем старую задачу если есть
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        
    if user_id not in user_data:
        return

    notif_data = user_data[user_id].get('notification_data')
    if not notif_data or not notif_data.get('enabled'):
        return
        
    interval = notif_data.get('interval', 2)
    next_run = notif_data.get('next_run', 0)
    
    # Если время следующего запуска в прошлом, запускаем сейчас
    run_date = None
    if next_run > time.time():
        run_date = datetime.fromtimestamp(next_run)
    else:
        run_date = datetime.now() + timedelta(seconds=10) # Запуск через 10 сек
        
    scheduler.add_job(
        send_weather_notification,
        'interval',
        hours=interval,
        start_date=run_date,
        args=[user_id],
        id=job_id
    )
    logger.info(f"Запланировано уведомление для {user_id} (интервал {interval}ч)")

async def periodic_cache_cleanup():
    """Периодическая очистка устаревшего кэша"""
    try:
        deleted = cleanup_old_cache()
        if deleted > 0:
            logger.info(f"Периодическая очистка: удалено {deleted} устаревших файлов кэша")
    except Exception as e:
        logger.error(f"Ошибка периодической очистки кэша: {e}")

# ============= ЗАПУСК БОТА =============

async def main():
    """Главная функция запуска бота"""
    # Регистрируем роутер
    dp.include_router(router)
    
    # Настраиваем планировщик
    scheduler.add_job(periodic_cache_cleanup, 'interval', hours=1)
    
    # Восстанавливаем задачи уведомлений
    count = 0
@router.message(F.text)
async def handle_text_input(message: Message, state: FSMContext):
    """
    Умная обработка текстового ввода (координаты или город).
    Работает, когда нет активного FSM состояния.
    """
    text = message.text.strip()
    user_id = message.from_user.id
    
    # 1. Пробуем парсить как координаты "lat, lon"
    try:
        parts = text.replace(',', ' ').split()
        if len(parts) == 2:
            lat = float(parts[0])
            lon = float(parts[1])
            
            # Проверка диапазона
            if (-90 <= lat <= 90) and (-180 <= lon <= 180):
                weather_data = get_weather_by_coordinates(lat, lon)
                city_name = weather_data['name'] # Обычно API возвращает ближайший населенный пункт
                
                # Сохраняем и показываем
                update_user_location(user_id, lat, lon, city_name)
                
                # Удаляем возможную клавиатуру
                try:
                    del_msg = await message.answer("...", reply_markup=ReplyKeyboardRemove())
                    await del_msg.delete()
                except:
                    pass

                formatted_message = format_weather_message(weather_data)
                
                await message.answer(
                    formatted_message,
                    parse_mode="HTML",
                    reply_markup=get_weather_actions_menu(lat, lon)
                )
                return
    except ValueError:
        pass # Не числовые координаты
    except Exception as e:
        logger.error(f"Ошибка при обработке координат в smart input: {e}")

    # 2. Если не координаты, пробуем как название города
    try:
        weather_data, city_name_ru = get_weather(text)
        lat = weather_data['coord']['lat']
        lon = weather_data['coord']['lon']
        
        update_user_location(user_id, lat, lon, city_name_ru)
        formatted_message = format_weather_message(weather_data)
        
        await message.answer(
            formatted_message,
            parse_mode="HTML",
            reply_markup=get_weather_actions_menu(lat, lon)
        )
    except Exception:
        # Если и как город не нашли, тогда уже говорим "не понимаю"
        # Но чтобы не спамить в чатах, можно отвечать, только если это личка
        if message.chat.type == "private":
             await message.answer("❌ Не удалось определить город или координаты.\nПопробуйте ввести название точнее.")

# ============= ЗАПУСК БОТА =============

async def main():
    """Главная функция запуска бота"""
    # Регистрируем роутер
    dp.include_router(router)
    
    # Настраиваем планировщик
    scheduler.add_job(periodic_cache_cleanup, 'interval', hours=1)
    
    # Восстанавливаем задачи уведомлений
    count = 0
    for user_id in user_data:
        if user_data[user_id].get('notification_data', {}).get('enabled'):
            schedule_user_notification(user_id)
            count += 1
            
    logger.info(f"Восстановлено {count} задач уведомлений")
    
    scheduler.start()
    logger.info("Бот запущен!")
    
    # Запускаем polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
