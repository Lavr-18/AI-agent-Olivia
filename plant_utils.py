import asyncio
import json
import logging
import os
import pickle
import time
import requests
import re
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple, Union, NamedTuple
import pandas as pd
from io import BytesIO
import numpy as np
import string
from contextlib import contextmanager

import config

logger = logging.getLogger(__name__)

# Типизированная структура для растения
class Plant(NamedTuple):
    """Структура данных для представления растения"""
    name: str
    price: float
    stock: float
    url: Optional[str] = None
    care_info: Optional[str] = None
    plant_type: Optional[str] = None

# Константы
EMBEDDINGS_FILE = "plants_embeddings.pkl"
PROCESSED_SHEET_FILE = "processed_sheet.xlsx"
SHEET_ID = "1iaBpr26eWvLGjWftdHk2eCIPdezooENr"
OUTPUT_FILE = "downloaded_spreadsheet.xlsx"
EMBEDDING_MODEL = "text-embedding-3-small"
FUZZY_MATCH_THRESHOLD = 0.8  # Порог для нечеткого сопоставления (от 0 до 1)

# Новые константы для управления файлами и параметрами:
MAX_OLD_JSON_FILES = 2  # Максимальное количество старых JSON файлов для хранения
MAX_OLD_PLANTS_FILES = 3  # Максимальное количество старых файлов отфильтрованных растений
MOYSKLAD_FILE_PREFIX = "moysklad_stock_"
PLANTS_FILTERED_PREFIX = "plants_filtered_"
JSON_FILE_SUFFIX = ".json"
EMBEDDING_BATCH_SIZE = 20
EMBEDDING_BATCH_PAUSE = 0.5
EMBEDDING_DIM_DEFAULT = 1536
VECTOR_SEARCH_SCORE_THRESHOLD = 0.5
EMBEDDINGS_FILE_MAX_AGE = 3600  # 1 час в секундах

# Глобальные переменные для хранения данных о растениях
plants_data: List[Dict[str, Any]] = []
plants_embeddings: List[List[float]] = []
latest_stock_file: Optional[str] = None

# Папки, содержащие растения в МойСклад
PLANT_FOLDER_KEYWORDS = [
    "КОМНАТНЫЕ РАСТЕНИЯ",
    "ИСКУССТВЕННЫЕ РАСТЕНИЯ",
    "Для флорариума",

]

def extract_plant_base_name(plant_name: str) -> Tuple[str, str, str]:
    """
    Приводит название растения к нижнему регистру и разбивает его на:
    (base_name, size, full_without_units).

    Ищет в названии размер по паттерну вида "число/число".
    Пример: "Аглаонема Лемон Минт 12/40 см" ->
             ("аглаонема лемон минт", "12/40", "аглаонема лемон минт 12/40")
             
    Если размер не найден, возвращает plant_name как есть.
    """
    plant_name = plant_name.lower().strip()
    match = re.search(r'(\d+/\d+)', plant_name)
    if match:
        size = match.group(1)
        base_name = plant_name[:match.start()].strip()
        full_without_units = f"{base_name} {size}".strip()
        return base_name, size, full_without_units
    else:
        return plant_name, "", plant_name

@contextmanager
def safe_file_operation(file_path: str, mode: str = 'r', encoding: Optional[str] = None):
    """
    Контекстный менеджер для безопасных файловых операций.
    Автоматически закрывает файл и обрабатывает исключения.
    """
    file = None
    try:
        file = open(file_path, mode, encoding=encoding) if encoding else open(file_path, mode)
        yield file
    except Exception as e:
        logger.error(f"Ошибка при работе с файлом {file_path}: {e}")
        raise
    finally:
        if file:
            file.close()

def get_plant_name(plant: Dict[str, Any]) -> str:
    """
    Извлекает название растения из различных полей в порядке приоритета.
    
    Args:
        plant: Словарь с данными о растении
        
    Returns:
        str: Название растения или "Неизвестное растение", если не удалось найти
    """
    # Используем универсальный поиск по приоритету ключей
    val = _get_value_by_priority(plant, ["Название", "name", "Растение"])
    return str(val).strip() if val else "Неизвестное растение"

def get_plant_stock(plant: Dict[str, Any]) -> float:
    """
    Извлекает информацию об остатке растения из различных полей.
    
    Args:
        plant: Словарь с данными о растении
        
    Returns:
        float: Значение остатка растения или 0.0, если не найдено
    """
    # Получаем значение остатка через универсальную функцию
    raw = _get_value_by_priority(plant, ["остаток (мойсклад)", "остаток", "stock"])
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.debug(f"Не удалось преобразовать остаток '{raw}' в число")
        return 0.0

def get_plant_price(plant: Dict[str, Any]) -> Optional[str]:
    """
    Извлекает цену растения из различных полей.
    
    Args:
        plant: Словарь с данными о растении
        
    Returns:
        Optional[str]: Цена растения или None, если не найдена
    """
    # Получаем значение цены через универсальную функцию
    raw = _get_value_by_priority(plant, ["Розничная цена", "price"])
    # Прекращаем преобразование цены в число, возвращаем строку
    if raw is None:
        return None
    return str(raw).strip()

def get_plant_care(plant: Dict[str, Any]) -> str:
    """
    Извлекает информацию по уходу за растением.
    
    Args:
        plant: Словарь с данными о растении
        
    Returns:
        str: Информация по уходу или пустая строка
    """
    # Получаем информацию по уходу через универсальную функцию
    val = _get_value_by_priority(plant, ["Уход (список)", "Уход", "уход"])
    return str(val).strip() if val else ""

def get_plant_url(plant: Dict[str, Any]) -> Optional[str]:
    """
    Извлекает URL растения.
    
    Args:
        plant: Словарь с данными о растении
        
    Returns:
        Optional[str]: URL или None, если не найден
    """
    # Получаем URL растения через универсальную функцию
    val = _get_value_by_priority(plant, ["Ссылка на товар", "Ссылка на товар "])
    return str(val).strip() if val and str(val).strip() else None

def convert_to_plant_model(plant_dict: Dict[str, Any]) -> Plant:
    """
    Преобразует словарь с данными о растении в структуру Plant.
    
    Args:
        plant_dict: Словарь с данными о растении
        
    Returns:
        Plant: Структурированные данные о растении
    """
    name = get_plant_name(plant_dict)
    price = get_plant_price(plant_dict) or 0.0
    stock = get_plant_stock(plant_dict)
    url = get_plant_url(plant_dict)
    care_info = get_plant_care(plant_dict)
    
    # Определяем тип растения по первому слову названия
    base_name, _, _ = extract_plant_base_name(name)
    base_words = base_name.split()
    plant_type = base_words[0] if base_words else "неизвестный"
    
    return Plant(
        name=name,
        price=price,
        stock=stock,
        url=url,
        care_info=care_info,
        plant_type=plant_type
    )

# Универсальная вспомогательная функция для получения значения по приоритету ключей
def _get_value_by_priority(data: Dict[str, Any], keys: List[str], default=None) -> Any:
    """
    Извлекает значение из словаря по приоритету ключей.
    Проверяет каждый ключ из списка по порядку и возвращает первое найденное значение.
    
    Args:
        data: Исходный словарь с данными
        keys: Список ключей для проверки в порядке приоритета
        default: Значение по умолчанию, если ни один ключ не найден
        
    Returns:
        Any: Найденное значение или default, если ничего не найдено
    """
    if not data or not isinstance(data, dict):
        return default
    
    for key in keys:
        if key in data and data[key] not in (None, "", "None", "null"):
            return data[key]
    
    return default

# Универсальная функция для очистки старых файлов любой группы
async def cleanup_old_files(prefix: str, suffix: str, max_files_to_keep: int):
    """
    Удаляет старые файлы, начинающиеся с prefix и заканчивающиеся suffix, оставляя max_files_to_keep самых новых.
    """
    try:
        files = [f for f in os.listdir('.') if f.startswith(prefix) and f.endswith(suffix)]
        if len(files) <= max_files_to_keep:
            logger.info(f"Найдено {len(files)} файлов с префиксом '{prefix}'. Удаление не требуется.")
            return
        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        to_delete = files[max_files_to_keep:]
        for fp in to_delete:
            try:
                os.remove(fp)
                logger.info(f"Удален старый файл: {fp}")
            except Exception as e:
                logger.error(f"Ошибка при удалении файла {fp}: {e}")
        logger.info(f"Удалено {len(to_delete)} старых файлов с префиксом '{prefix}'.")
    except Exception as e:
        logger.error(f"Ошибка при очистке файлов с префиксом '{prefix}': {e}")

async def cleanup_old_json_files(max_files_to_keep: int = MAX_OLD_JSON_FILES):
    await cleanup_old_files(MOYSKLAD_FILE_PREFIX, JSON_FILE_SUFFIX, max_files_to_keep)

async def cleanup_old_plants_files(max_files_to_keep: int = MAX_OLD_PLANTS_FILES):
    await cleanup_old_files(PLANTS_FILTERED_PREFIX, JSON_FILE_SUFFIX, max_files_to_keep)

async def get_stock() -> list:
    """
    Получает данные об остатках из МойСклад API.
    
    Returns:
        list: Список с данными об остатках товаров
    """
    url = 'https://api.moysklad.ru/api/remap/1.2/report/stock/all'
    headers = {
        "Authorization": f"Bearer {config.MOY_SKLAD}",
        "Accept": "application/json;charset=utf-8"
    }
    params = {"limit": 1000}
    all_stocks = []
    offset = 0
    
    while True:
        params["offset"] = offset
        
        try:
            response = await asyncio.to_thread(requests.get, url, headers=headers, params=params)
            
            if response.status_code != 200:
                logger.error(f"Ошибка при получении остатков: {response.status_code}, {response.text}")
                break
            
            data = response.json()
            rows = data.get("rows", [])
            
            if not rows:
                break
                
            all_stocks.extend(rows)
            offset += len(rows)
            
            if len(rows) < params["limit"]:
                break
                
        except Exception as e:
            logger.error(f"Ошибка при запросе к API МойСклад: {e}")
            break
    
    logger.info(f"Получено {len(all_stocks)} записей об остатках из МойСклад")
    return all_stocks

def filter_plants(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Фильтрует товары, оставляя только растения.
    
    Args:
        items: Список товаров
        
    Returns:
        List[Dict[str, Any]]: Отфильтрованный список растений
    """
    filtered = []
    
    for item in items:
        folder = item.get("folder", {})
        
        if isinstance(folder, dict):
            folder_name = folder.get("pathName", "") + " " + folder.get("name", "")
        else:
            folder_name = str(folder)
            
        if any(keyword in folder_name for keyword in PLANT_FOLDER_KEYWORDS):
            filtered.append(item)
    
    logger.info(f"Отфильтровано {len(filtered)} растений из {len(items)} позиций")
    return filtered

async def parse_json_to_plants(filename: str) -> List[Dict[str, Any]]:
    """
    Преобразует JSON с данными из МойСклад в список растений.
    
    Args:
        filename: Путь к JSON файлу
        
    Returns:
        List[Dict[str, Any]]: Список растений
    """
    try:
        # Загружаем данные из файла
        json_data = None
        with safe_file_operation(filename, 'r', encoding='utf-8') as f:
            json_data = json.load(f)
        
        if not json_data:
            logger.error(f"Файл {filename} пуст или не содержит данных JSON")
            return []
            
        # Фильтруем только растения
        plants = filter_plants(json_data)
        
        # Конвертируем числовые значения
        for plant in plants:
            plant["stock"] = float(plant.get("stock", 0))
            plant["price"] = float(plant.get("price", 0))
        
        logger.info(f"Загружено {len(plants)} растений из файла {filename}")
        return plants
        
    except Exception as e:
        logger.error(f"Ошибка при обработке JSON файла {filename}: {e}")
        return []

async def export_to_json(stocks: List[Dict[str, Any]]) -> str:
    """
    Экспортирует данные об остатках в JSON файл.
    
    Args:
        stocks: Список данных об остатках
        
    Returns:
        str: Имя созданного файла
    """
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{MOYSKLAD_FILE_PREFIX}{now}{JSON_FILE_SUFFIX}"
    output_data = []
    
    for item in stocks:
        stock = float(item.get("stock", 0))
        
        # Включаем только товары с положительным остатком
        if stock > 0:
            folder = item.get("folder", {})
            product_folder = folder.get("pathName", "")
            group_name = folder.get("name", "")
            sale_price = item.get("salePrice", 0)
            price = sale_price / 100 if sale_price else 0
            
            output_data.append({
                "folder": product_folder,
                "group": group_name,
                "name": item.get("name", ""),
                "article": item.get("article", ""),
                "stock": stock,
                "price": price
            })
    
    # Фильтруем только растения
    filtered_data = filter_plants(output_data)
    
    # Сохраняем в файл
    try:
        with safe_file_operation(filename, 'w', encoding='utf-8') as f:
            json.dump(filtered_data, f, ensure_ascii=False, indent=4)
        
        logger.info(f"Экспортировано {len(filtered_data)} растений в {filename}")
        return filename
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных в JSON: {e}")
        return ""

async def create_embeddings(plants: List[Dict[str, Any]], openai_client) -> List[List[float]]:
    """
    Создает эмбеддинги для растений.
    
    Args:
        plants: Список растений
        openai_client: OpenAI клиент
        
    Returns:
        List[List[float]]: Список эмбеддингов для растений
    """
    # Создаем текстовые описания растений для эмбеддингов, используя все поля из данных
    plant_descriptions = []
    for plant in plants:
        desc_parts = []
        for key, val in plant.items():
            if val is not None and str(val).strip():
                desc_parts.append(f"{key}: {val}")
        plant_descriptions.append(". ".join(desc_parts))
    
    # Определяем размерность эмбеддингов
    embedding_dim = EMBEDDING_DIM_DEFAULT  # Значение по умолчанию
    try:
        sample_resp = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL, input=["Тест"]
        )
        embedding_dim = len(sample_resp.data[0].embedding)
    except Exception as e:
        logger.error(f"Ошибка определения размерности эмбеддингов: {e}")
    
    # Создаем эмбеддинги батчами
    embeddings = []
    for i in range(0, len(plant_descriptions), EMBEDDING_BATCH_SIZE):
        batch = plant_descriptions[i:i+EMBEDDING_BATCH_SIZE]
        try:
            resp = await openai_client.embeddings.create(
                model=EMBEDDING_MODEL, input=batch
            )
            embeddings.extend([d.embedding for d in resp.data])
            await asyncio.sleep(EMBEDDING_BATCH_PAUSE)  # Пауза для избежания лимитов API
        except Exception as e:
            logger.error(f"Ошибка при создании эмбеддингов для батча {i}-{i+len(batch)}: {e}")
            # Заполняем нулевыми векторами в случае ошибки
            embeddings.extend([[0] * embedding_dim] * len(batch))
    
    logger.info(f"Создано {len(embeddings)} эмбеддингов для растений")
    return embeddings

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Вычисляет косинусное сходство между двумя векторами.
    
    Args:
        vec1: Первый вектор
        vec2: Второй вектор
        
    Returns:
        float: Косинусное сходство (от -1 до 1)
    """
    if len(vec1) != len(vec2):
        return 0.0
    
    try:
        v1 = np.array(vec1, dtype=np.float32)
        v2 = np.array(vec2, dtype=np.float32)
        
        denom = float(np.linalg.norm(v1) * np.linalg.norm(v2))
        
        if denom == 0.0:
            return 0.0
            
        return float(np.dot(v1, v2) / denom)
    except Exception as e:
        logger.error(f"Ошибка при вычислении косинусного сходства: {e}")
        return 0.0

async def vector_search(query: str, top_k: int, openai_client) -> List[Dict[str, Any]]:
    """
    Выполняет векторный поиск по запросу и возвращает top_k наиболее релевантных растений.
    
    Args:
        query: Текст запроса
        top_k: Количество результатов для возврата
        openai_client: OpenAI клиент
        
    Returns:
        List[Dict[str, Any]]: Список наиболее релевантных растений
    """
    # Вызываем более полную функцию и отбрасываем скоры
    results = await vector_search_with_score(query, top_k, openai_client)
    return [plant for plant, _ in results]

async def vector_search_with_score(query: str, top_k: int, openai_client) -> List[Tuple[Dict[str, Any], float]]:
    """
    Выполняет векторный поиск по запросу и возвращает top_k пар (растение, сходство).
    
    Args:
        query: Текст запроса
        top_k: Количество результатов для возврата
        openai_client: OpenAI клиент
        
    Returns:
        List[Tuple[Dict[str, Any], float]]: Список пар (растение, сходство)
    """
    global plants_data, plants_embeddings
    
    # Проверяем наличие данных
    if not plants_data or not plants_embeddings:
        logger.warning("Нет загруженных данных о растениях")
        await initialize_data(openai_client)
        
        if not plants_data or not plants_embeddings:
            logger.error("Не удалось загрузить данные о растениях")
            return []
    
    # Проверяем соответствие размеров
    if len(plants_data) != len(plants_embeddings):
        logger.error(f"Несоответствие размеров данных: {len(plants_data)} растений и {len(plants_embeddings)} эмбеддингов")
        return []
    
    try:
        # Создаем эмбеддинг для запроса
        resp = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[query]
        )
        query_emb = resp.data[0].embedding
        
        # Вычисляем сходство с каждым растением
        similarities = []
        for i, emb in enumerate(plants_embeddings):
            sim = cosine_similarity(query_emb, emb)
            similarities.append((plants_data[i], sim))
        
        # Сортируем по убыванию сходства и берем top_k
        similarities.sort(key=lambda x: x[1], reverse=True)
        
        logger.info(f"Найдено {min(top_k, len(similarities))} растений по запросу '{query}'")
        return similarities[:top_k]
    
    except Exception as e:
        logger.error(f"Ошибка при векторном поиске: {e}")
        return []

def download_google_sheet_as_excel(sheet_id: str = SHEET_ID, output_file: str = OUTPUT_FILE) -> bool:
    """
    Скачивает Google Sheet как Excel файл.
    
    Args:
        sheet_id: ID таблицы Google Sheets
        output_file: Имя выходного файла
        
    Returns:
        bool: True, если скачивание успешно, иначе False
    """
    try:
        # Формируем URL для экспорта
        export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
        
        # Скачиваем файл
        response = requests.get(export_url)
        
        if response.status_code == 200:
            # Сохраняем файл
            with safe_file_operation(output_file, 'wb') as f:
                f.write(response.content)
            
            logger.info(f"Файл успешно скачан как {output_file}")
            
            # Обрабатываем файл (удаляем фрагменты текста вида [11111])
            process_excel_file(output_file)
            logger.info(f"Файл успешно обработан")
            
            return True
        else:
            logger.error(f"Ошибка при скачивании: {response.status_code}, {response.text}")
            return False
    
    except Exception as e:
        logger.error(f"Ошибка при скачивании Google Sheet: {e}")
        return False

def process_excel_file(file_path: str) -> None:
    """
    Обрабатывает Excel файл, удаляя фрагменты текста вида [11111]
    
    Args:
        file_path: Путь к Excel файлу
    """
    logger.info(f"Обработка Excel файла {file_path}...")
    temp_file = file_path.replace('.xlsx', '_temp.xlsx')
    
    try:
        # Читаем все листы
        dfs = {}
        with pd.ExcelFile(file_path) as excel_file:
            for sheet_name in excel_file.sheet_names:
                dfs[sheet_name] = pd.read_excel(excel_file, sheet_name=sheet_name)
        
        # Обрабатываем каждый лист
        with pd.ExcelWriter(temp_file, engine='openpyxl') as writer:
            for sheet_name, df in dfs.items():
                # Удаляем фрагменты вида [11111] из текстовых колонок
                for column in df.columns:
                    if df[column].dtype == 'object':
                        df[column] = df[column].apply(
                            lambda x: re.sub(r'\[\d+\]', '', str(x)) if pd.notna(x) else x
                        )
                
                # Сохраняем обработанный лист
                df.to_excel(writer, sheet_name=sheet_name, index=False)
        
        # Заменяем исходный файл обработанным
        if os.path.exists(temp_file):
            if os.path.exists(file_path):
                os.remove(file_path)
            os.rename(temp_file, file_path)
            logger.info(f"Файл {file_path} успешно обновлен")
        else:
            logger.error(f"Ошибка: временный файл {temp_file} не был создан")
    
    except Exception as e:
        logger.error(f"Ошибка при обработке Excel файла: {e}")
        
        # Удаляем временный файл, если он существует
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass



async def merge_moysklad_with_sheet(moysklad_data: List[Dict[str, Any]], sheet_file: str = OUTPUT_FILE) -> str:
    """
    Объединяет данные из МойСклад с данными из Google Sheets.
    Добавляет колонку с остатками из МойСклад.
    
    Если растение из МойСклад отсутствует в Excel, добавляет новую строку
    с нужными значениями.
    """
    try:
        # Создаём словарь для быстрого доступа к price/stock.
        stock_dict = {}
        for plant in moysklad_data:
            raw_name = plant.get("name", "").strip()
            raw_name = re.sub(r'\[\d+\]', '', raw_name).strip().lower()
            _, _, full_name = extract_plant_base_name(raw_name)
            
            stock_dict[full_name] = {
                "price": plant.get("price", 0),
                "stock": plant.get("stock", 0),
                "original_name": plant.get("name", "").strip()
            }
        
        output_file = PROCESSED_SHEET_FILE
        dfs = {}
        
        with pd.ExcelFile(sheet_file) as excel_file:
            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(excel_file, sheet_name=sheet_name)
                
                # Ищем колонку "Растение" (или по любой вашей логике)
                name_column = None
                for col in df.columns:
                    if "растение" in col.lower():
                        name_column = col
                        break
                
                # Убеждаемся, что есть колонка "остаток (мойсклад)"
                if "остаток (мойсклад)" not in df.columns:
                    df["остаток (мойсклад)"] = None
                
                found_plants = set()
                if name_column:
                    # Пробегаемся по каждой строке и ставим остатки, если растение есть
                    for idx, row in df.iterrows():
                        plant_name = str(row[name_column]).strip()
                        plant_name = re.sub(r'\[\d+\]', '', plant_name).strip().lower()
                        _, _, full_name = extract_plant_base_name(plant_name)
                        
                        if full_name in stock_dict:
                            df.at[idx, "остаток (мойсклад)"] = stock_dict[full_name]["stock"]
                            found_plants.add(full_name)
                
                # Добавляем новые строки для растений, которых нет в Excel
                missing_plants = [k for k in stock_dict if k not in found_plants]
                
                for mp in missing_plants:
                    data = stock_dict[mp]
                    new_row = {}
                    original_name = data["original_name"]
                    new_row["Название"] = original_name
                    new_row["Грунт"] = "-"
                    new_row["Пересадка"] = "-"
                    new_row["Растение"] = "-"
                    new_row["Кашпо/Горшок"] = "в техническом горшке"
                    new_row["Уход (список)"] = "-"
                    new_row["Освещение"] = "-"
                    new_row["Полив"] = "-"
                    # Преобразуем price в целое
                    try:
                        new_row["Розничная цена"] = int(data["price"])
                    except:
                        new_row["Розничная цена"] = data["price"]
                    
                    # Генерируем символьный код для нового растения
                    generated_code = generate_symbolic_code(original_name)
                    new_row["Символьный код в админке (не удалять)"] = generated_code
                    new_row["Ссылка на товар"] = "ссылка"
                        
                    new_row["остаток (мойсклад)"] = data["stock"]
                    
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                
                # Теперь добавляем все растения из МойСклад в технических горшках
                for stock_key, data in stock_dict.items():
                    original_name = data["original_name"]
                    new_row = {}
                    new_row["Название"] = f"{original_name} (тех)"
                    new_row["Грунт"] = "-"
                    new_row["Пересадка"] = "-"
                    new_row["Растение"] = "-" 
                    new_row["Кашпо/Горшок"] = "в техническом горшке"
                    new_row["Уход (список)"] = "-"
                    new_row["Освещение"] = "-"
                    new_row["Полив"] = "-"
                    # Преобразуем price в целое
                    try:
                        new_row["Розничная цена"] = int(data["price"])
                    except:
                        new_row["Розничная цена"] = data["price"]
                    
                    # Генерируем символьный код для растения в техническом горшке
                    generated_code = generate_symbolic_code(f"{original_name}_tech")
                    new_row["Символьный код в админке (не удалять)"] = generated_code
                    new_row["Ссылка на товар"] = "ссылка"
                        
                    new_row["остаток (мойсклад)"] = data["stock"]
                    
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                
                dfs[sheet_name] = df
        
        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            for sheet_name, df in dfs.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)
        
        logger.info(f"Файл сохранён: {output_file}")
        return output_file
    
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return sheet_file

async def update_plant_data(openai_client) -> bool:
    """
    Обновляет данные о растениях из МойСклад и Google Sheets по новому алгоритму:
    1. Скачивание файла с Google Sheets
    2. Добавление колонки "остаток" из МойСклад
    3. Фильтрация данных (только растения с остатком >= 1)
    4. Сохранение отфильтрованных данных в JSON
    5. Создание эмбеддингов (векторизация)
    
    Args:
        openai_client: OpenAI клиент
        
    Returns:
        bool: True, если обновление успешно, иначе False
    """
    global plants_data, plants_embeddings, latest_stock_file
    
    try:
        # Шаг 1: Очистка старых файлов
        await cleanup_old_json_files()
        
        # Шаг 2: Получение данных из МойСклад для сопоставления остатков
        stocks = await get_stock()
        if not stocks:
            logger.error("Не удалось получить остатки из МойСклад")
            return False
            
        # Преобразуем данные МойСклад в удобный формат
        plants_data_ms = await parse_json_to_plants(await export_to_json(stocks))
        
        # Шаг 3: Скачивание данных из Google Sheets
        sheets_success = download_google_sheet_as_excel(SHEET_ID, OUTPUT_FILE)
        if not sheets_success:
            logger.error("Не удалось скачать Google Sheet")
            return False
        
        logger.info(f"Google Sheet успешно скачан как {OUTPUT_FILE}")
        
        # Шаг 4: Добавление колонки "остаток" к данным из Google Sheets
        processed_file = await merge_moysklad_with_sheet(plants_data_ms, OUTPUT_FILE)
        logger.info(f"Файл с добавленными остатками создан: {processed_file}")
        
        # Шаг 5: Загрузка данных из Excel и фильтрация (остаток >= 1)
        sheet_data = []
        filtered_data = []
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        filtered_json_file = f"{PLANTS_FILTERED_PREFIX}{now}{JSON_FILE_SUFFIX}"
        
        with pd.ExcelFile(processed_file) as excel_file:
            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(excel_file, sheet_name=sheet_name)
                
                # Фильтруем только растения с остатком >= 1
                if 'остаток (мойсклад)' in df.columns:
                    df_filtered = df[df['остаток (мойсклад)'] >= 1].copy()
                    logger.info(f"На листе {sheet_name}: отфильтровано {len(df_filtered)} растений из {len(df)}")
                    
                    # Сохраняем отфильтрованные данные
                    for _, row in df_filtered.iterrows():
                        item_data = row.to_dict()
                        filtered_data.append(item_data)
                else:
                    logger.warning(f"На листе {sheet_name} нет колонки 'остаток (мойсклад)'")
                    # Включаем все строки, если нет колонки остатка
                    for _, row in df.iterrows():
                        item_data = row.to_dict()
                        sheet_data.append(item_data)
        
        # Если есть отфильтрованные данные, используем их, иначе используем все данные
        final_data = filtered_data if filtered_data else sheet_data
        
        # Шаг 6: Сохранение отфильтрованных данных в JSON
        try:
            with safe_file_operation(filtered_json_file, 'w', encoding='utf-8') as f:
                json.dump(final_data, f, ensure_ascii=False, indent=4)
            logger.info(f"Отфильтрованные данные сохранены в {filtered_json_file}")
            latest_stock_file = filtered_json_file
            
            # Очистка старых файлов plants_filtered
            await cleanup_old_plants_files()
            
        except Exception as e:
            logger.error(f"Ошибка при сохранении JSON: {e}")
            return False
        
        # Шаг 7: Создание эмбеддингов для отфильтрованных данных
        logger.info(f"Создание эмбеддингов для {len(final_data)} растений...")
        plants_embeddings = await create_embeddings(final_data, openai_client)
        plants_data = final_data
        
        # Шаг 8: Сохранение данных и эмбеддингов
        save_data = {
            'file': filtered_json_file,
            'processed_file': processed_file,
            'file_mtime': os.path.getmtime(filtered_json_file),
            'plants_data': plants_data,
            'embeddings': plants_embeddings,
            'timestamp': datetime.now().isoformat()
        }
        
        with safe_file_operation(EMBEDDINGS_FILE, 'wb') as f:
            pickle.dump(save_data, f)
        
        logger.info(f"Данные и эмбеддинги сохранены в {EMBEDDINGS_FILE}")
        return True
    
    except Exception as e:
        logger.error(f"Ошибка при обновлении данных о растениях: {e}")
        return False

async def initialize_data(openai_client) -> bool:
    """
    Инициализирует данные о растениях, загружая их из файла или обновляя при необходимости.
    
    Args:
        openai_client: OpenAI клиент
        
    Returns:
        bool: True, если инициализация успешна, иначе False
    """
    global plants_data, plants_embeddings, latest_stock_file
    
    try:
        if os.path.exists(EMBEDDINGS_FILE):
            logger.info(f"Найден {EMBEDDINGS_FILE}, проверяем возраст...")
            
            file_age_seconds = time.time() - os.path.getmtime(EMBEDDINGS_FILE)
            
            # Если файл старше 1 часа, обновляем данные
            if file_age_seconds > EMBEDDINGS_FILE_MAX_AGE:
                logger.info("Файл эмбеддингов старше 1 часа. Обновляем...")
                return await update_plant_data(openai_client)
            
            # Загружаем данные из файла
            try:
                with safe_file_operation(EMBEDDINGS_FILE, 'rb') as f:
                    saved_data = pickle.load(f)
                
                if not saved_data:
                    logger.warning("Не удалось загрузить данные из pickle файла")
                    return await update_plant_data(openai_client)
                
                saved_file = saved_data.get('file')
                
                # Проверяем, существует ли файл, на который ссылается pickle
                if saved_file and os.path.exists(saved_file):
                    plants_data = saved_data.get('plants_data', [])
                    plants_embeddings = saved_data.get('embeddings', [])
                    latest_stock_file = saved_data.get('moysklad_file', saved_file)
                    
                    logger.info(f"Загружено {len(plants_data)} растений из {EMBEDDINGS_FILE}")
                    return True
                else:
                    logger.warning("В pickle нет актуального файла. Обновляем данные...")
                    return await update_plant_data(openai_client)
                    
            except Exception as e:
                logger.error(f"Ошибка при загрузке файла: {e}")
                return await update_plant_data(openai_client)
        else:
            logger.info(f"{EMBEDDINGS_FILE} не найден, запускаем обновление данных...")
            return await update_plant_data(openai_client)
    
    except Exception as e:
        logger.error(f"Ошибка при инициализации данных: {e}")
        return await update_plant_data(openai_client)

def search_plants_by_name(query_name: str) -> List[Dict[str, Any]]:
    """
    Выполняет прямой поиск растений по названию (тексту).
    Включает нечеткое сопоставление для обработки опечаток.
    
    Args:
        query_name: Поисковый запрос
        
    Returns:
        List[Dict[str, Any]]: Список найденных растений
    """
    # Если нет данных – возвращаем пусто
    if not plants_data:
        logger.warning("Нет загруженных данных о растениях")
        return []
    
    # Страхуемся от некорректных запросов
    if not query_name or not isinstance(query_name, str) or not query_name.strip():
        logger.warning(f"Некорректный запрос: {query_name}")
        return []
    
    query_lower = query_name.lower().strip()
    
    # 1) Ищем точное совпадение (без учёта регистра)
    exact_matches = []
    for plant in plants_data:
        name_lower = get_plant_name(plant).lower()
        # Убираем лишние скобки, точки и т.п.
        cleaned_plant_name = re.sub(r'[^\w\s\-/]+', '', name_lower).strip()
        cleaned_query = re.sub(r'[^\w\s\-/]+', '', query_lower).strip()
        
        if cleaned_plant_name == cleaned_query:
            exact_matches.append(plant)
    
    if exact_matches:
        logger.info(f"Найдено {len(exact_matches)} точных совпадений по запросу '{query_name}'")
        return exact_matches
    
    # 2) Для нечеткого сопоставления подготавливаем ключевые слова
    query_words = prepare_query_words(query_lower)
    
    # 3) Пробуем нечеткое сопоставление
    matching_plants = []
    for plant in plants_data:
        if is_plant_matching_query(plant, query_lower, query_words):
            # Убираем фильтрацию по остатку > 0, добавляем все совпадения
            matching_plants.append(plant)
    
    logger.info(f"Найдено {len(matching_plants)} растений по запросу '{query_name}' (нечеткое сопоставление)")
    return matching_plants

def prepare_query_words(query: str) -> List[str]:
    """
    Подготавливает список ключевых слов для поиска из запроса.
    Обрабатывает множественное число и другие формы слов.
    
    Args:
        query: Поисковый запрос
        
    Returns:
        List[str]: Список ключевых слов
    """
    # Удаляем знаки препинания и разбиваем на слова
    query_words = query.translate(str.maketrans('', '', string.punctuation)).split()
    
    # Обрабатываем варианты слов
    word_variants = []
    
    for word in query_words:
        # Добавляем оригинальное слово
        word_variants.append(word)
        
        # Проверяем на множественное число (для русского языка)
        if len(word) >= 4:
            if word.endswith('ы'):
                word_variants.append(word[:-1])  # фикусы -> фикус
            elif word.endswith('и'):
                word_variants.append(word[:-1])  # кактуси -> кактус
            elif word.endswith('я'):
                word_variants.append(word[:-1])  # деревья -> дерев + я
                word_variants.append(word[:-1] + 'о')  # деревья -> дерево
            elif word.endswith('ии'):
                word_variants.append(word[:-2] + 'ия')  # лилии -> лилия
            elif word.endswith('сы'):
                word_variants.append(word[:-2] + 'с')  # фикусы -> фикус
    
    # Оставляем только значимые слова (длиной >= 3) и убираем дубликаты
    return list(set([w for w in word_variants if len(w) >= 3]))

def levenshtein_distance(s1: str, s2: str) -> int:
    """
    Вычисляет расстояние Левенштейна между двумя строками.
    
    Args:
        s1: Первая строка
        s2: Вторая строка
        
    Returns:
        int: Расстояние Левенштейна
    """
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    
    if len(s2) == 0:
        return len(s1)
    
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    
    return previous_row[-1]

def fuzzy_string_match(s1: str, s2: str) -> float:
    """
    Вычисляет степень схожести двух строк (от 0 до 1).
    
    Args:
        s1: Первая строка
        s2: Вторая строка
        
    Returns:
        float: Степень схожести (1.0 - идеальное совпадение)
    """
    if not s1 or not s2:
        return 0.0
    
    distance = levenshtein_distance(s1, s2)
    max_len = max(len(s1), len(s2))
    
    if max_len == 0:
        return 1.0
    
    return 1.0 - (distance / max_len)

def is_plant_matching_query(plant: Dict[str, Any], query: str, query_words: List[str]) -> bool:
    """
    Проверяет, соответствует ли растение поисковому запросу.
    Для соответствия все слова из запроса должны присутствовать в названии растения.
    Включает нечеткое сопоставление для обработки опечаток.
    
    Args:
        plant: Данные о растении
        query: Полный текст запроса
        query_words: Подготовленные ключевые слова запроса
        
    Returns:
        bool: True, если растение соответствует запросу
    """
    # Получаем текстовые поля для поиска
    plant_name = plant.get("name", "").lower() if isinstance(plant.get("name"), str) else ""
    plant_type = plant.get("Растение", "").lower() if isinstance(plant.get("Растение"), str) else ""
    plant_title = plant.get("Название", "").lower() if isinstance(plant.get("Название"), str) else ""
    plant_care = plant.get("Уход (список)", "").lower() if isinstance(plant.get("Уход (список)"), str) else ""
    plant_article = plant.get("article", "").lower() if isinstance(plant.get("article"), str) else ""
    plant_folder = plant.get("folder", "").lower() if isinstance(plant.get("folder"), str) else ""
    plant_group = plant.get("group", "").lower() if isinstance(plant.get("group"), str) else ""
    plant_pot = plant.get("Кашпо/Горшок", "").lower() if isinstance(plant.get("Кашпо/Горшок"), str) else ""
    plant_light = plant.get("Освещение", "").lower() if isinstance(plant.get("Освещение"), str) else ""
    plant_water = plant.get("Полив", "").lower() if isinstance(plant.get("Полив"), str) else ""
    plant_tag = plant.get("Тег (Народное название)", "").lower() if isinstance(plant.get("Тег (Народное название)"), str) else ""
    
    # Объединяем все текстовые поля для поиска
    search_text = f"{plant_name} {plant_type} {plant_title} {plant_care} {plant_article} {plant_folder} {plant_group} {plant_pot} {plant_light} {plant_water} {plant_tag}".strip()
    
    # Базовое имя растения (без размеров)
    effective_name = plant_name if plant_name else plant_title
    base_name, _, _ = extract_plant_base_name(effective_name)
    
    # Проверяем точное вхождение всего запроса
    cleaned_query = query.translate(str.maketrans('', '', string.punctuation)).strip()
    
    if cleaned_query and (cleaned_query in search_text or cleaned_query in base_name):
        return True
    
    # Проверяем вхождение ВСЕХ отдельных слов
    if query_words:
        # Функция для проверки нечеткого соответствия слова в тексте
        def word_matches_fuzzy(word: str, text: str) -> bool:
            # Сначала проверяем точное вхождение
            if word in text:
                return True
            
            # Если точного вхождения нет, проверяем нечеткое соответствие
            for text_word in text.split():
                match_score = fuzzy_string_match(word, text_word)
                if match_score >= FUZZY_MATCH_THRESHOLD:
                    logger.debug(f"Нечеткое совпадение: '{word}' ~ '{text_word}' (score: {match_score:.2f})")
                    return True
            
            return False
        
        # Счетчик найденных слов
        found_words = 0
        for word in query_words:
            if word_matches_fuzzy(word, search_text) or word_matches_fuzzy(word, base_name):
                found_words += 1
        
        # Все слова из запроса должны быть найдены
        return found_words == len(query_words)
    
    return False

def generate_symbolic_code(name: str) -> str:
    """
    Генерирует символьный код для растения на основе его названия.
    Используется для новых растений, добавляемых из МойСклад.
    
    Args:
        name: Название растения
        
    Returns:
        str: Символьный код для растения
    """
    # Переводим в нижний регистр и удаляем квадратные скобки с числами
    name = re.sub(r'\[\d+\]', '', name.lower())
    
    # Удаляем знаки препинания и заменяем пробелы на подчеркивания
    name = re.sub(r'[^\w\s]', '', name)
    code = re.sub(r'\s+', '_', name.strip())
    
    # Транслитерация кириллицы в латиницу (пример реализации)
    rus_chars = {'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
                 'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
                 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
                 'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
                 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'}
    
    transliterated = ''
    for char in code:
        transliterated += rus_chars.get(char, char)
    
    return transliterated
