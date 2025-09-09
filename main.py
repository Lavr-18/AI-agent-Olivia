import asyncio
import json
import logging
import pickle
import re
import os
import time
import base64
from datetime import datetime
from threading import Thread, Lock
from typing import Dict, List, Any, Optional, Tuple
from PIL import Image
from io import BytesIO
import pandas as pd
import requests
import websocket
# Предполагается, что все ключи и настройки хранятся в config.py
import config

# Подтягиваем класс ChatContext и функцию run_unified_agent (вместо старой)
from chat_context import ChatContext, DialogState, PERSONA_DESCRIPTION, ADDRESS_INFO, RESPONSE_FORMAT_INSTRUCTIONS
from bot_agent import run_unified_agent # Импортируем новую функцию из bot_agent.py

# Функции для работы с растениями (RAG, векторный поиск и т.п.)
import plant_utils

# Настройка логирования в файл и консоль
log_dir = 'logs'
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
log_file = os.path.join(log_dir, f'app_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logger.info(f"Запуск приложения. Логи записываются в: {log_file}")

# --------------------------------------- #
#        Параметры и глобальные переменные
# --------------------------------------- #

API_URL = config.API_URL
TOKEN = config.RETAIL_CRM_BOT_TOKEN
HEADERS = {"X-Bot-Token": TOKEN, "Content-Type": "application/json"}

OPENAI_API_KEY = config.OPENAI_API_KEY
MOY_SKLAD_TOKEN = config.MOY_SKLAD
RETAILCRM_API_KEY = config.RETAIL_CRM
RETAILCRM_BASE_URL = config.RETAILCRM_BASE_URL
RETAILCRM_STORE_CODE = "tropichouse"

MODEL_NAME = "gpt-4.1-mini"
VISION_MODEL = "gpt-4o"        # условное название модели, если используете аналоги Vision
EMBEDDING_MODEL = "text-embedding-3-small"

MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60

# Очередь входящих сообщений и главный event-loop
message_queue = asyncio.Queue()
main_event_loop = None

# Инициализируем OpenAI-клиент (асинхронный)
from openai import AsyncOpenAI
openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

# Храним ChatContext для каждого chat_id
chat_contexts: Dict[str, ChatContext] = {}

# Для безопасной работы с веб-сокетом в многопоточном окружении
ws_lock = Lock()
contexts_lock = Lock()

# Для объединения сообщений от пользователей
user_messages: Dict[str, List[Tuple[str, Optional[str]]]] = {}  # chat_id -> [(text, image_url), ...]
user_timers: Dict[str, asyncio.Task] = {}  # chat_id -> task
MESSAGE_DELAY = 1  # задержка в секундах


# --------------------------------------- #
#      Функции управления контекстами    #
# --------------------------------------- #

def cleanup_expired_contexts():
    """Удаляет устаревшие контексты диалогов для экономии памяти."""
    with contexts_lock:
        expired_chats = []
        for chat_id, context in chat_contexts.items():
            if context.is_expired():
                expired_chats.append(chat_id)
        
        for chat_id in expired_chats:
            del chat_contexts[chat_id]
            logger.info(f"[cleanup_expired_contexts] Удален устаревший контекст для chat {chat_id}")
        
        if expired_chats:
            logger.info(f"[cleanup_expired_contexts] Очищено {len(expired_chats)} устаревших контекстов")


# --------------------------------------- #
#      Асинхронные обёртки для HTTP
# --------------------------------------- #

async def async_get(url, **kwargs):
    return await asyncio.to_thread(requests.get, url, **kwargs)

async def async_post(url, **kwargs):
    return await asyncio.to_thread(requests.post, url, **kwargs)


# --------------------------------------- #
#  Основные функции обработки сообщений
# --------------------------------------- #

async def handle_client_message(chat_id: str, message_text: str):
    """
    Вызывается при получении нового текстового сообщения от клиента.
    Создаёт (или берёт существующий) ChatContext и передаёт сообщение в LLM-агента.
    """
    logger.info(f"[handle_client_message] chat {chat_id}, text: {message_text[:50] if message_text else 'None'}")

    # Если это команда /start или /reset, сбрасываем контекст независимо от текущего состояния
    if message_text and message_text.strip().lower() in ["/start"]:
        logger.info(f"[handle_client_message] Получена команда сброса для chat {chat_id}")
        # Создаем новый контекст, полностью сбрасывая старый
        chat_contexts[chat_id] = ChatContext(chat_id)
        # Продолжаем обработку, чтобы агент мог сгенерировать приветствие после сброса

    # Получаем или создаем контекст для чата
    if chat_id not in chat_contexts:
        chat_contexts[chat_id] = ChatContext(chat_id)
        logger.info(f"[handle_client_message] Создан новый контекст для chat {chat_id}")
    else:
        # Проверяем, не истек ли срок действия контекста (7 дней)
        if chat_contexts[chat_id].is_expired():
            logger.info(f"[handle_client_message] Контекст для chat {chat_id} истек, создаем новый")
            chat_contexts[chat_id] = ChatContext(chat_id)

    context = chat_contexts[chat_id]

    # Проверяем, что сообщение не пустое
    if not message_text:
        logger.warning(f"[handle_client_message] Получено пустое сообщение для chat {chat_id}")
        await send_message(chat_id, "Извините, я получил пустое сообщение. Пожалуйста, повторите ваш запрос.")
        return

    # Передаём сообщение в наш новый единый агент
    try:
        # run_unified_agent теперь сам обрабатывает диалог и возвращает готовый текст ответа
        bot_reply = await run_unified_agent(context, message_text, openai_client)

        # Мы отправляем сообщение только если bot_reply не пустой (т.е. '')
        # Это позволяет игнорировать сообщения, которые должен обрабатывать другой бот
        if bot_reply:
            await send_message(chat_id, bot_reply)

    except Exception as e:
        logger.error(f"[handle_client_message] Ошибка при обработке сообщения для chat {chat_id}: {e}", exc_info=True)
        await send_message(chat_id, "Извините, произошла ошибка при обработке вашего запроса. Пожалуйста, повторите или введите /start для перезапуска диалога.")


async def handle_client_image(chat_id: str, image_url: str):
    """
    Обрабатывает фотографию от клиента.
    Анализирует изображение и передает результат агенту.
    """
    # Получаем или создаем контекст для чата
    if chat_id not in chat_contexts:
        chat_contexts[chat_id] = ChatContext(chat_id)
        logger.info(f"[handle_client_image] Создан новый контекст для chat {chat_id}")
    else:
        # Проверяем, не истек ли срок действия контекста (7 дней)
        if chat_contexts[chat_id].is_expired():
            logger.info(f"[handle_client_image] Контекст для chat {chat_id} истек, создаем новый")
            chat_contexts[chat_id] = ChatContext(chat_id)
    
    context = chat_contexts[chat_id]

    try:
        # Проверка, что image_url не None или пустая строка
        if not image_url:
            logger.error(f"[handle_client_image] Получен пустой image_url для chat {chat_id}")
            await send_message(chat_id, "Извините, не удалось получить изображение. Попробуйте отправить его снова.")
            return
            
        logger.info(f"[handle_client_image] Получено изображение для chat {chat_id}: {image_url}")
        resp = await async_get(image_url)
        if resp is None:
            logger.error(f"[handle_client_image] async_get вернул None для {image_url}")
            await send_message(chat_id, "Извините, не удалось загрузить фотографию. Попробуйте отправить её снова.")
            return
            
        if resp.status_code != 200:
            logger.error(f"[handle_client_image] Не удалось получить картинку: {resp.status_code}")
            await send_message(chat_id, "Извините, что-то пошло не так при загрузке фотографии. Не могли бы вы отправить её ещё раз? Если проблема повторится, попробуйте сделать новое фото.")
            return

        # Проверка, что resp.content не None
        if not resp.content:
            logger.error(f"[handle_client_image] Пустой resp.content для {image_url}")
            await send_message(chat_id, "Извините, полученное изображение оказалось пустым. Не могли бы вы отправить его ещё раз?")
            return
            
        image_bytes = resp.content
        # Анализируем фото
        result = await analyze_image(image_bytes)
        
        # Проверка, что result не None
        if result is None:
            logger.error(f"[handle_client_image] analyze_image вернул None для chat {chat_id}")
            await send_message(chat_id, "Извините, не удалось проанализировать изображение. Попробуйте отправить другое фото.")
            return
        
        # Формируем сообщение для агента на основе анализа
        if result.get("is_plant"):
            plant_name = result.get("plant_name", "Неизвестное растение")
            description = result.get("description", "")
            # Формируем "внутреннее" сообщение для агента о том, что пришло фото
            internal_message = f"Пользователь прислал фото растения: {plant_name}. {description}"
            logger.info(f"[handle_client_image] Растение опознано: {plant_name}. Передаем агенту: '{internal_message}'")
        else:
            description = result.get("description", "").strip()
            if description:
                internal_message = f"Пользователь прислал фото, но это похоже не растение. Описание: {description}"
            else:
                internal_message = "Пользователь прислал фото, но распознать его не удалось."
            logger.info(f"[handle_client_image] Растение не опознано. Передаем агенту: '{internal_message}'")

        # Передаём внутреннее сообщение в единый агент
        try:
            bot_reply = await run_unified_agent(context, internal_message, openai_client)
            
            if bot_reply is None:
                logger.error(f"[handle_client_image] bot_reply=None после обработки фото для chat {chat_id}")
                bot_reply = "Извините, произошла техническая ошибка после анализа фото. Пожалуйста, повторите запрос."
                
            await send_message(chat_id, bot_reply)
        except Exception as agent_e:
            logger.error(f"[handle_client_image] Ошибка при вызове run_unified_agent для chat {chat_id} после фото: {agent_e}", exc_info=True)
            await send_message(chat_id, "Извините, произошла ошибка при обработке вашего фото. Пожалуйста, попробуйте еще раз.")

    except Exception as e:
        # Логируем основную ошибку handle_client_image
        logger.error(f"[handle_client_image] Глобальная ошибка для chat {chat_id}: {e}", exc_info=True)
        await send_message(chat_id, "Прошу прощения, возникли технические сложности при обработке фото. Попробуйте отправить его ещё раз.")


async def analyze_image(image_content: bytes) -> dict:
    """
    Анализирует изображение для определения растения.
    Использует Vision модель для распознавания и описания в естественном стиле.
    """
    try:
        # Открываем изображение
        img = Image.open(BytesIO(image_content))

        # При необходимости уменьшаем размер
        width, height = img.size
        max_side = 1024
        if max(width, height) > max_side:
            scale = max_side / float(max(width, height))
            new_w = int(width * scale)
            new_h = int(height * scale)
            img = img.resize((new_w, new_h))
            logger.info(f"[analyze_image] Изображение уменьшено до: {new_w}x{new_h}")

        # Сохраняем в буфер как JPEG
        buf = BytesIO()
        img.save(buf, format='JPEG')
        buf.seek(0)
        encoded_image = base64.b64encode(buf.read()).decode("utf-8")

        # Улучшенный промпт для более естественного описания
        prompt_content = [
            {
                "type": "text",
                "text": (
                    "Представь, что ты опытный флорист-консультант. "
                    "Посмотри на фотографию и опиши, что ты видишь. "
                    "Если это растение, определи его тип и особенности. "
                    "Если это не растение, просто опиши, что видишь на фото. "
                    "Если на фото предполоожительно большое растение, добавь это в description"
                    "Ответ должен быть в формате JSON с полями:\n"
                    "- is_plant (bool): true если на фото растение\n"
                    "- plant_name (string): название растения или null\n"
                    "- description (string): дружелюбное описание того, что видно на фото\n"
                    "- confidence (float): уверенность в определении от 0 до 1\n"
                )
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}
            }
        ]

        # Вызываем Vision-модель
        response = await openai_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты - опытный флорист-консультант магазина TropicHouse. "
                        "Твоя задача - помочь определить растение на фото и дать полезные рекомендации. "
                        "Используй дружелюбный, профессиональный тон. Отвечай как живой консультант, "
                        "но строго в формате JSON."
                    )
                },
                {
                    "role": "user",
                    "content": prompt_content
                }
            ],
            temperature=0.3
        )

        raw_answer = response.choices[0].message.content.strip()
        logger.info(f"[analyze_image] Модель вернула:\n{raw_answer}")

        try:
            # Удаляем маркеры кода, если они присутствуют
            cleaned_answer = raw_answer
            if cleaned_answer.startswith("```json"):
                cleaned_answer = cleaned_answer[7:]
            if cleaned_answer.startswith("```"):
                cleaned_answer = cleaned_answer[3:]
            if cleaned_answer.endswith("```"):
                cleaned_answer = cleaned_answer[:-3]
            
            cleaned_answer = cleaned_answer.strip()
            result = json.loads(cleaned_answer)
            
            # Проверяем и дополняем обязательные поля
            if not isinstance(result.get("is_plant"), bool):
                logger.warning("[analyze_image] Некорректный формат is_plant в ответе модели")
                result["is_plant"] = False
                
            if not result.get("plant_name"):
                result["plant_name"] = "Неизвестное растение"
                
            if not result.get("description"):
                result["description"] = "Нет описания"
                
            if not isinstance(result.get("confidence"), (int, float)):
                result["confidence"] = 0.0
                
            if result.get("is_plant") and not result.get("care_tips"):
                result["care_tips"] = "Общие рекомендации: умеренный полив, хорошее освещение без прямых солнечных лучей"
                
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"[analyze_image] Ошибка парсинга JSON: {e}\nОтвет модели:\n{raw_answer}")
            # Пытаемся извлечь информацию из текстового ответа
            is_plant = "растение" in raw_answer.lower()
            return {
                "is_plant": is_plant,
                "plant_name": "Неизвестное растение" if is_plant else None,
                "description": raw_answer[:200],
                "confidence": 0.3 if is_plant else 0.0,
            }

    except Exception as e:
        logger.error(f"[analyze_image] Ошибка при анализе изображения: {e}")
        return {
            "is_plant": False,
            "plant_name": None,
            "description": "К сожалению, не удалось проанализировать фотографию",
            "confidence": 0.0,
        }


# --------------------------------------- #
#   Функции отправки сообщений клиенту
# --------------------------------------- #

async def send_message(chat_id: str, message: str):
    """
    Отправка текстового сообщения пользователю через RetailCRM Bot API.
    """
    # Проверка входных параметров
    if message is None:
        logger.error(f"[send_message] Ошибка: message=None для chat_id={chat_id}")
        message = "Извините, произошла техническая ошибка. Пожалуйста, повторите запрос или напишите /start для перезапуска диалога."
    
    # Проверяем, что сообщение не пустое
    if not message or not message.strip():
        logger.info(f"[send_message] Пустое сообщение для chat_id={chat_id}, пропускаем отправку")
        return
    
    url = f"{API_URL}/messages"
    data = {
        "chat_id": int(chat_id),
        "type": "text",
        "content": message,
        "scope": "public"
    }
    try:
        # Используем безопасное логирование
        log_msg = message[:50] if message else "None"
        logger.info(f"[send_message] -> chat {chat_id}: {log_msg}")
        
        # Отправляем JSON без экранирования кириллицы
        resp = await async_post(url, headers=HEADERS, data=json.dumps(data, ensure_ascii=False).encode('utf-8'))
        if resp is None:
            logger.error("[send_message] Ошибка: async_post вернул None")
            return
            
        if resp.status_code not in (200, 201):
            logger.error(f"[send_message] Ошибка: {resp.status_code}, {resp.text}")
        else:
            # Добавляем проверку, что resp.content не None перед вызовом json()
            try:
                if resp.content:
                    json_response = resp.json()
                    if json_response:
                        logger.info(f"[send_message] Отправлено, message_id={json_response.get('message_id')}")
                    else:
                        logger.info("[send_message] Отправлено, json_response=None")
                else:
                    logger.info("[send_message] Отправлено, нет content в ответе")
            except Exception as json_err:
                logger.error(f"[send_message] Ошибка при обработке JSON ответа: {json_err}")
    except Exception as e:
        logger.error(f"[send_message] Ошибка: {e}")


# --------------------------------------- #
#   Callbacks WebSocket для RetailCRM
# --------------------------------------- #

def dialog_assigned(dialog_id: int) -> bool:
    """Проверяет, назначен ли диалог менеджеру"""
    try:
        # Пробуем получить информацию о чате
        url = f"{API_URL}/dialogs"
        params = {"id": dialog_id}
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        
        if resp.status_code == 404:
            logger.info(f"[dialog_assigned] Чат {dialog_id} не найден, считаем неназначенным")
            return False
            
        resp.raise_for_status()
        data = resp.json()
        
        # API может возвращать список или словарь
        if isinstance(data, list):
            # Если список, ищем диалог с нужным ID
            for dialog in data:
                if isinstance(dialog, dict) and dialog.get('id') == dialog_id:
                    assigned = dialog.get('is_assigned')
                    if assigned:
                        logger.info(f"Диалог {dialog_id} уже назначен менеджеру")
                        return True
            logger.info(f"Диалог {dialog_id} не найден в списке или не назначен")
            return False
        else:
            logger.warning(f"[dialog_assigned] Неожиданный тип данных от API: {type(data)}")
            return False
        
    except Exception as e:
        logger.error(f"[dialog_assigned] Ошибка при проверке диалога {dialog_id}: {e}")
        # В случае ошибки считаем диалог неназначенным, чтобы бот мог отвечать
        return False


def on_message(ws, message):
    """
    Вызывается при входящем сообщении по WebSocket.
    Парсим JSON, ищем текст/фото от клиента и кладём в очередь message_queue.
    """
    global main_event_loop
    try:
        data = json.loads(message)
        if data.get("type") == "message_new":
            message_data = data.get("data", {}).get("message", {})
            chat_id = message_data.get("chat_id")
            dialog_id = message_data.get("dialog",{}).get("id")
            if not chat_id:
                logger.warning("[on_message] Нет chat_id, пропускаем")
                return

            channel_info = message_data.get("chat", {}).get("channel", {})
            channel_id = channel_info.get("id")
            channel_name = (channel_info.get("name") or "").lower()

            # Проверка, что сообщение пришло из нужного канала
            # (опционально, если нужно фильтровать)
            try:
                is_target_channel = (int(channel_id) == 18 or int(channel_id) == 13) if channel_id else False
            except (ValueError, TypeError):
                is_target_channel = False
                
            if not is_target_channel:
                logger.info(f"[on_message] Сообщение из другого канала ({channel_name}), игнорируем")
                return

            from_data = message_data.get("from", {})
            sender_type = from_data.get("type")
            incoming_type = message_data.get("type")
            content = message_data.get("content", {})

            # Обрабатываем только сообщения от клиента
            if sender_type == "customer":
                # Проверяем, не назначен ли диалог уже менеджеру
                if dialog_assigned(dialog_id):
                    logger.info(f"[on_message] Диалог {chat_id} уже назначен менеджеру, переводим в MANAGER_CALLED")
                    # Создаем или обновляем контекст чата
                    if chat_id not in chat_contexts:
                        chat_contexts[chat_id] = ChatContext(chat_id)
                    
                    context = chat_contexts[chat_id]
                    context.dialog_id = dialog_id
                    context.change_state(DialogState.MANAGER_CALLED)
                    return
                
                # Создаем или обновляем контекст чата
                if chat_id not in chat_contexts:
                    chat_contexts[chat_id] = ChatContext(chat_id)
                
                context = chat_contexts[chat_id]
                context.dialog_id = dialog_id # Устанавливаем dialog_id в контексте
                
                # Сохраняем информацию о канале
                context.channel_info = {
                    "id": channel_id,
                    "name": channel_name
                }
                
                # Сохраняем информацию о пользователе
                context.user_info = {
                    "id": from_data.get("id"),
                    "name": from_data.get("name", "Неизвестный пользователь")
                }
                
                if incoming_type == "text":
                    message_text = content.get("text") if isinstance(content, dict) else str(content) if content else None
                    if message_text and message_text.strip():
                        logger.info(f"[on_message] Текст от клиента: {message_text}")
                        if main_event_loop:
                            asyncio.run_coroutine_threadsafe(message_queue.put((chat_id, message_text)), main_event_loop)
                    else:
                        logger.warning(f"[on_message] Получено пустое текстовое сообщение для chat {chat_id}")
                elif incoming_type == "image":
                    items = message_data.get("items", [])
                    if items and isinstance(items, list):
                        first_item = items[0]
                        if isinstance(first_item, dict) and first_item.get("kind") == "image":
                            img_url = first_item.get("preview_url")
                            if img_url and main_event_loop:
                                logger.info(f"[on_message] Изображение от клиента: {img_url}")
                                asyncio.run_coroutine_threadsafe(message_queue.put((chat_id, None, img_url)), main_event_loop)
            elif sender_type in ["manager", "user"]:
                # Сообщения от менеджеров - переводим диалог в режим MANAGER_CALLED
                logger.info(f"[on_message] Сообщение от менеджера ({sender_type}), переводим в режим MANAGER_CALLED")
                
                # Создаем или обновляем контекст чата
                if chat_id not in chat_contexts:
                    chat_contexts[chat_id] = ChatContext(chat_id)
                
                context = chat_contexts[chat_id]
                context.dialog_id = dialog_id
                
                # Переводим в состояние MANAGER_CALLED
                context.change_state(DialogState.MANAGER_CALLED)
            else:
                # Неизвестный тип отправителя - логируем и игнорируем
                logger.warning(f"[on_message] Неизвестный тип отправителя: {sender_type}, игнорируем сообщение для chat {chat_id}")

    except json.JSONDecodeError:
        logger.error(f"[on_message] JSONDecodeError: {message}")
    except Exception as e:
        logger.error(f"[on_message] Ошибка: {e}")


def on_error(ws, error):
    logger.error(f"WebSocket ошибка: {error}")
    if "403 Forbidden" in str(error):
        logger.error("Ошибка авторизации токена бота (403).")


def on_close(ws, close_status_code, close_msg):
    logger.warning(f"WebSocket закрыт: {close_status_code} - {close_msg}")


def on_open(ws):
    logger.info("[on_open] WebSocket соединение установлено")
    ws.reconnect_attempts = 0
    ws.reconnect_delay = RECONNECT_DELAY


# --------------------------------------- #
#   Функции для запуска и переподключения
# --------------------------------------- #

def create_websocket():
    """
    Создаёт WebSocketApp для RetailCRM и возвращает его. 
    """
    ws_url = f"{API_URL.replace('https://', 'wss://')}/ws?events=message_new"
    ws = websocket.WebSocketApp(
        ws_url,
        header=["X-Bot-Token: " + TOKEN],
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    ws.reconnect_attempts = 0
    ws.reconnect_delay = RECONNECT_DELAY
    return ws


def run_with_reconnect(ws):
    """
    Запускает ws.run_forever в цикле, чтобы при обрыве связи
    повторно подключаться (до MAX_RECONNECT_ATTEMPTS).
    """
    while True:
        if ws.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            logger.error(f"Достигнуто макс. число попыток переподключения: {MAX_RECONNECT_ATTEMPTS}")
            break

        if ws.reconnect_attempts > 0:
            logger.info(f"Переподключение {ws.reconnect_attempts}/{MAX_RECONNECT_ATTEMPTS}...")
        else:
            logger.info("Старт WebSocket...")

        try:
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logger.error(f"Ошибка в ws.run_forever: {e}")

        current_attempts = ws.reconnect_attempts + 1
        current_delay = min(ws.reconnect_delay * 2, MAX_RECONNECT_DELAY)
        logger.info(f"Следующая попытка переподключения через {current_delay} с...")

        time.sleep(current_delay)

        ws_new = create_websocket()
        ws_new.reconnect_attempts = current_attempts
        ws_new.reconnect_delay = current_delay
        ws = ws_new


def check_connection_status(ws):
    """
    Периодически проверяет, работает ли Bot API (GET /bots).
    """
    while True:
        try:
            test_req = requests.get(f"{API_URL}/bots", headers=HEADERS)
            if test_req.status_code == 200:
                logger.debug("[check_connection_status] API доступен.")
            else:
                logger.warning(f"[check_connection_status] API вернул {test_req.status_code}")
        except Exception as e:
            logger.warning(f"Ошибка check_connection_status: {e}")
        time.sleep(300)  # раз в 5 минут


# --------------------------------------- #
#    Фоновая корутина обработки очереди
# --------------------------------------- #

async def process_user_messages(chat_id: str):
    """
    Обрабатывает сообщения пользователя после задержки.
    Объединяет все сообщения, полученные в течение MESSAGE_DELAY секунд.
    """
    try:
        # Ждем указанное время
        logger.info(f"[process_user_messages] Ожидание {MESSAGE_DELAY} секунд для chat_id {chat_id}")
        await asyncio.sleep(MESSAGE_DELAY)
        
        # Получаем все сообщения пользователя
        messages = user_messages.get(chat_id, [])
        if not messages:
            logger.info(f"[process_user_messages] Нет сообщений для chat_id {chat_id}")
            return
            
        logger.info(f"[process_user_messages] Обработка {len(messages)} сообщений для chat_id {chat_id}")
        
        # Очищаем сообщения пользователя
        user_messages[chat_id] = []
        
        # Разделяем сообщения на текстовые и изображения
        text_messages = []
        image_messages = []
        
        for text, image_url in messages:
            if image_url:
                image_messages.append(image_url)
            elif text:
                text_messages.append(text)
        
        # Обрабатываем изображения
        for image_url in image_messages:
            logger.info(f"[process_user_messages] Обработка изображения для chat_id {chat_id}: {image_url}")
            await handle_client_image(chat_id, image_url)
        
        # Объединяем текстовые сообщения и обрабатываем их
        if text_messages:
            combined_text = "\n".join(text_messages)
            logger.info(f"[process_user_messages] Обработка {len(text_messages)} текстовых сообщений для chat_id {chat_id}")
            await handle_client_message(chat_id, combined_text)
                
    except Exception as e:
        logger.error(f"[process_user_messages] Ошибка: {e}")
    finally:
        # Удаляем таймер пользователя
        if chat_id in user_timers:
            del user_timers[chat_id]
            logger.info(f"[process_user_messages] Таймер удален для chat_id {chat_id}")


async def process_messages():
    """
    Берём задания из очереди message_queue и добавляем их в список сообщений пользователя.
    Запускает таймер для обработки сообщений через MESSAGE_DELAY секунд.
    """
    while True:
        item = await message_queue.get()
        try:
            chat_id = item[0]
            
            # Инициализируем список сообщений для пользователя, если его нет
            if chat_id not in user_messages:
                user_messages[chat_id] = []
            
            # Добавляем сообщение в список
            if len(item) == 2:
                text = item[1]
                user_messages[chat_id].append((text, None))
                logger.info(f"[process_messages] Добавлено текстовое сообщение для chat_id {chat_id}: {text[:50]}")
            elif len(item) == 3:
                image_url = item[2]
                user_messages[chat_id].append((None, image_url))
                logger.info(f"[process_messages] Добавлено изображение для chat_id {chat_id}: {image_url}")
            
            # Если у пользователя уже есть активный таймер, не создаем новый
            if chat_id not in user_timers:
                # Создаем новую задачу для обработки сообщений
                user_timers[chat_id] = asyncio.create_task(process_user_messages(chat_id))
                logger.info(f"[process_messages] Запущен таймер для chat_id {chat_id}, сообщений: {len(user_messages[chat_id])}")
            else:
                logger.info(f"[process_messages] Обновлен таймер для chat_id {chat_id}, сообщений: {len(user_messages[chat_id])}")
            
            message_queue.task_done()
        except Exception as e:
            logger.error(f"[process_messages] Ошибка: {e}")
            await asyncio.sleep(1)


# --------------------------------------- #
#         Инициализация и запуск
# --------------------------------------- #

async def start_bot():
    """
    Запускает всё окружение: инициализацию данных, подключение к WebSocket, фоновые задачи.
    """
    # Проверяем доступность бота
    test_request = await async_get(f"{API_URL}/bots", headers=HEADERS)
    if test_request.status_code == 403:
        logger.error("[start_bot] Ошибка авторизации: неверный токен бота.")
        return

    # Пробуем инициализировать данные (загрузить эмбеддинги из pickle, либо обновить их)
    retry_attempts = 3
    for attempt in range(retry_attempts):
        try:
            logger.info(f"[start_bot] Инициализация данных #{attempt+1}...")
            ok = await plant_utils.initialize_data(openai_client)
            if not ok:
                logger.warning("Данные не проинициализировались, пробуем update_plant_data...")
                await plant_utils.update_plant_data(openai_client)
            break
        except Exception as e:
            logger.error(f"[start_bot] Ошибка init данных: {e}")
            if attempt < (retry_attempts - 1):
                logger.info("Повторим через 5 секунд...")
                await asyncio.sleep(5)
            else:
                logger.critical("Не удалось инициализировать данные после всех попыток.")
                return

    # Запускаем поток веб-сокета
    ws = create_websocket()
    thr_checker = Thread(target=check_connection_status, args=(ws,), daemon=True)
    thr_checker.start()

    ws_thread = Thread(target=run_with_reconnect, args=(ws,), daemon=True)
    ws_thread.start()

    # Запускаем корутину обработки очереди
    asyncio.create_task(process_messages())

    # Запускаем периодическую очистку устаревших контекстов (каждые 6 часов)
    async def periodic_cleanup():
        while True:
            await asyncio.sleep(21600)  # 6 часов
            cleanup_expired_contexts()
    
    asyncio.create_task(periodic_cleanup())

    # Периодически обновляем данные о растениях (например, раз в час)
    while True:
        await asyncio.sleep(3600)
        await plant_utils.update_plant_data(openai_client)


async def main():
    """
    Точка входа при запуске файла main.py напрямую.
    """
    global main_event_loop
    main_event_loop = asyncio.get_event_loop()
    await start_bot()


if __name__ == "__main__":
    asyncio.run(main())
