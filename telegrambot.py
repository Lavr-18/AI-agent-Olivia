from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
import config
import logging
import requests
from datetime import datetime
import json
import time

logger = logging.getLogger(__name__)

# Инициализация бота использует config.BOT_TOKEN, который будет обновлен из config.py/env
try:
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    logger.info("Телеграм-бот успешно инициализирован")
except Exception as e:
    logger.error(f"Не удалось инициализировать Телеграм-бот: {e}")

# УДАЛЕНО: SELLER_CHAT_ID = -1002540034535 (Теперь ID берутся из config)


def api_request(method, endpoint, params=None, json_data=None, headers=None):
    """Универсальная функция для запросов к API"""
    try:
        base_url = config.MG_URL if endpoint.startswith("/users") else config.API_URL
        url = f"{base_url}{endpoint}"

        if not headers:
            headers = {"X-Bot-Token": config.MG_TOKEN if endpoint.startswith("/users") else config.RETAIL_CRM_BOT_TOKEN}

        if method == "GET":
            resp = requests.get(url, params=params, headers=headers, timeout=5)
        elif method == "POST":
            resp = requests.post(url, json=json_data, headers=headers, timeout=5)
        elif method == "PATCH":
            resp = requests.patch(url, json=json_data, headers=headers, timeout=5)
        else:
            return None, f"Неподдерживаемый метод: {method}"

        if resp.status_code in [200, 201, 204]:
            return resp.json() if resp.content else {}, None

        error = f"HTTP {resp.status_code}: {resp.text}"
        logger.error(f"[api_request] {error}")
        return None, error

    except Exception as e:
        logger.error(f"[api_request] Ошибка: {e}")
        return None, str(e)


def get_online_managers(group):
    """Возвращает список онлайн-менеджеров указанной группы"""
    params = {"group": group, "online": 1, "active": 1, "limit": 50}
    data, error = api_request("GET", "/users", params=params)

    if error:
        return []

    if isinstance(data, dict):
        return data.get('users', []) or []
    return data if isinstance(data, list) else []


def choose_manager(managers):
    """Выбирает менеджера с наименьшим количеством активных диалогов"""
    return min(managers, key=lambda u: u.get("activeDialogs", 0)) if managers else None


def assign_dialog_to_manager(dialog_id, user_id):
    """Назначает диалог MessageGateway‑бота конкретному менеджеру"""
    headers = {
        "X-Bot-Token": config.RETAIL_CRM_BOT_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {"user_id": int(user_id)}

    data, error = api_request("PATCH", f"/dialogs/{dialog_id}/assign", json_data=payload, headers=headers)

    if not error:
        logger.info(f"[assign_dialog_to_manager] Диалог {dialog_id} → менеджер {user_id}")
        return True
    return False


def get_dialog_by_id(dialog_id, debug=False):
    """Получает диалог по его ID - оптимизированная версия"""
    dialog_id_str = str(dialog_id)

    # Сначала пробуем прямой запрос по ID (если API поддерживает)
    logger.info(f"[get_dialog_by_id] Поиск диалога {dialog_id}")
    data, error = api_request("GET", f"/dialogs/{dialog_id}")

    if not error and data:
        logger.info(f"[get_dialog_by_id] Диалог {dialog_id} найден прямым запросом")
        return data

    # Если прямой запрос не сработал, пробуем с параметром id
    data, error = api_request("GET", "/dialogs", params={"id": dialog_id})

    if not error and data:
        # API может вернуть список или один объект
        if isinstance(data, list) and data:
            logger.info(f"[get_dialog_by_id] Диалог {dialog_id} найден через параметр id (список)")
            return data[0]
        elif isinstance(data, dict):
            logger.info(f"[get_dialog_by_id] Диалог {dialog_id} найден через параметр id (объект)")
            return data

    # Если ничего не помогло, используем старый метод с ограничением
    logger.info(f"[get_dialog_by_id] Переход к поиску по страницам для диалога {dialog_id}")

    page = 1
    limit = 100
    max_pages = 5  # Уменьшили лимит страниц для быстрого поиска

    while page <= max_pages:
        data, error = api_request("GET", "/dialogs", params={"limit": limit, "page": page})

        if error:
            logger.error(f"[get_dialog_by_id] Ошибка получения диалогов (стр. {page}): {error}")
            return None

        # Определяем диалоги в зависимости от формата ответа
        dialogs = []
        if isinstance(data, dict):
            dialogs = data.get('dialogs', []) or []
            # Проверяем пагинацию
            pagination = data.get('pagination', {})
            if pagination.get('currentPage') == pagination.get('totalPageCount'):
                page = max_pages  # Последняя страница
        elif isinstance(data, list):
            dialogs = data
            if len(dialogs) < limit:
                page = max_pages  # Последняя страница

        # Ищем диалог по chat_id
        for dialog in dialogs:
            if str(dialog.get('chat_id')) == dialog_id_str:
                logger.info(f"[get_dialog_by_id] Диалог {dialog_id} найден на странице {page}")
                return dialog

        if not dialogs:
            break

        page += 1
        time.sleep(0.1)  # Уменьшили задержку

    logger.warning(f"[get_dialog_by_id] Диалог {dialog_id} не найден")
    return None


def get_context_info(context):
    """Извлекает информацию из контекста для формирования сообщения"""
    if context is None:
        logger.warning("[get_context_info] Получен пустой контекст")
        return {
            "chat_id": None,
            "subject": "",
            "channel_info": {},
            "user_info": {},
            "selected_plants": [],
            "out_of_stock_plant": {},
            "preorder_info": {},
            "order_details": {},
            "dialog_id": None
        }

    chat_id = getattr(context, "chat_id", None)
    dialog_id = getattr(context, "dialog_id", None)

    # Логируем полученные ID
    if chat_id:
        logger.info(f"[get_context_info] Получен chat_id: {chat_id}")
    else:
        logger.warning("[get_context_info] chat_id отсутствует в контексте")

    if dialog_id:
        logger.info(f"[get_context_info] Получен dialog_id: {dialog_id}")
    else:
        logger.debug("[get_context_info] dialog_id отсутствует в контексте")

    result = {
        "chat_id": chat_id,
        "dialog_id": dialog_id,
        "subject": getattr(context, "subject", ""),
        "channel_info": getattr(context, "channel_info", {}),
        "user_info": getattr(context, "user_info", {}),
        "selected_plants": getattr(context, "selected_plants", []),
        "out_of_stock_plant": getattr(context, "out_of_stock_plant", {}),
        "preorder_info": getattr(context, "preorder_info", {}),
        "order_details": getattr(context, "order_details", {})
    }
    return result


def is_b2b_order(context_info, order_details):
    """Определяет, является ли заказ корпоративным (B2B)"""
    if "офис" in context_info["subject"].lower() or "b2b" in context_info["subject"].lower():
        return True
    if "цветы в офис" in order_details.lower() or "растения для офиса" in order_details.lower():
        return True
    return False


def format_seller_message(context_info, order_details, is_preorder, is_b2b, assignment_result=None):
    """Формирует сообщение для продавца"""
    if is_preorder:
        message = "🔔 <b>Новый ПРЕДЗАКАЗ!</b>\n\n"
    elif context_info["subject"]:
        message = "🔔 <b>Обращение клиента!</b>\n\n"
    else:
        message = "🔔 <b>Новый заказ!</b>\n\n"

    if context_info["subject"]:
        message += f"<b>Тема:</b> {context_info['subject']}\n\n"

    message += f"<b>Тип клиента:</b> {'B2B' if is_b2b else 'B2C'}\n\n"

    # Информация о клиенте
    if context_info["chat_id"]:
        message += f"<b>Информация о клиенте:</b>\n"
        message += f"• ID чата: <code>{context_info['chat_id']}</code>\n"

        if context_info["channel_info"]:
            channel_name = context_info["channel_info"].get('name', 'Неизвестный канал')
            channel_id = context_info["channel_info"].get('id', 'Неизвестный ID')
            message += f"• Канал: {channel_name} (ID: {channel_id})\n"

        if context_info["user_info"]:
            user_name = context_info["user_info"].get('name', 'Неизвестный пользователь')
            user_id = context_info["user_info"].get('id', 'Неизвестный ID')
            message += f"• Клиент: {user_name} (ID: {user_id})\n"

        # Информация о растениях для обычного заказа
        if not is_preorder and context_info["selected_plants"]:
            message += f"• Выбранные растения: {len(context_info['selected_plants'])} шт.\n"
            for i, plant in enumerate(context_info["selected_plants"], 1):
                plant_name = plant.get("Название", "Неизвестное растение")
                message += f"  {i}. {plant_name}\n"

    # Для предзаказа добавляем специальную информацию
    if is_preorder:
        message += f"\n<b>ПРЕДЗАКАЗ (поставка через 7-10 дней):</b>\n"

        if context_info["out_of_stock_plant"]:
            plant = context_info["out_of_stock_plant"]
            message += f"• Растение: {plant.get('Название', 'Неизвестное растение')}\n"
            if 'Цена' in plant:
                message += f"• Цена: {plant['Цена']}\n"
            if 'Ссылка' in plant and plant['Ссылка']:
                message += f"• Ссылка: {plant['Ссылка']}\n"

        if context_info["preorder_info"]:
            for key, value in context_info["preorder_info"].items():
                if key not in ('is_preorder', 'plant_name'):
                    message += f"• {key}: {value}\n"

    # Детали заказа
    if context_info["order_details"]:
        message += f"\n<b>Детали заказа:</b>\n"
        for key, value in context_info["order_details"].items():
            message += f"• {key}: {value}\n"

    # Результат назначения диалога
    if assignment_result:
        message += f"\n<b>Назначение диалога:</b>\n• {assignment_result['message']}\n"

    # Дополнительная информация
    message += f"\n<b>Дополнительная информация:</b>\n{order_details}"

    # Временная метка
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message += f"\n\n<i>Заказ получен: {current_time}</i>"

    return message


def handle_manager_assignment(is_b2b=False, target_dialog_id=None):
    """Обрабатывает назначение диалога соответствующему менеджеру

    Args:
        is_b2b: True для B2B запроса, False для B2C
        target_dialog_id: ID конкретного диалога, в котором пользователь вызвал бота
    """
    try:
        logger.info(
            f"[handle_manager_assignment] Вызов с параметрами: is_b2b={is_b2b}, target_dialog_id={target_dialog_id}")

        # Если не указан конкретный диалог, возвращаем ошибку
        if not target_dialog_id:
            logger.error("[handle_manager_assignment] Не указан ID диалога")
            return {"status": "error", "message": "Не указан ID диалога для назначения менеджера"}

        # Проверяем существование диалога
        dialog = get_dialog_by_id(target_dialog_id)
        if not dialog:
            logger.warning(f"[handle_manager_assignment] Диалог {target_dialog_id} не найден")
            return {"status": "error", "message": f"Диалог {target_dialog_id} не найден"}

        # Проверяем, не назначен ли диалог уже менеджеру
        if isinstance(dialog, dict) and dialog.get("responsible", {}).get("type") == "user":
            logger.warning(f"[handle_manager_assignment] Диалог {target_dialog_id} уже назначен менеджеру")
            return {"status": "warning", "message": "Диалог уже назначен менеджеру"}

        # Выбираем группу менеджеров в зависимости от типа запроса
        manager_group = config.MANAGER_B2B if is_b2b else config.MANAGER_B2C
        managers = get_online_managers(manager_group["id"])

        if not managers:
            return {
                "status": "warning",
                "message": f"Нет онлайн-менеджеров группы {manager_group['group']}. Диалог остается в очереди."
            }

        target_manager = choose_manager(managers)

        if assign_dialog_to_manager(dialog['id'], target_manager["id"]):
            # Формируем имя менеджера из first_name и last_name
            manager_name = f"{target_manager.get('first_name', '')} {target_manager.get('last_name', '')}"
            if not manager_name.strip():  # Если имя пустое, используем запасной вариант
                manager_name = target_manager.get('fullName', 'Unknown')

            return {
                "status": "success",
                "message": f"Диалог {target_dialog_id} назначен менеджеру {manager_name} группы {manager_group['group']}"
            }
        else:
            return {
                "status": "error",
                "message": f"Не удалось назначить диалог {target_dialog_id} менеджеру"
            }

    except Exception as e:
        logger.error(f"[handle_manager_assignment] Ошибка: {e}")
        return {"status": "error", "message": str(e)}


async def notify_seller(order_details: str, is_preorder: bool, context=None) -> dict:
    """Отправляет уведомление продавцу через Telegram"""
    try:
        logger.info(f"[notify_seller] Отправка данных заказа продавцу (is_preorder={is_preorder})")

        # Получаем информацию из контекста
        context_info = get_context_info(context)

        # Определяем тип заказа (B2B/B2C)
        is_b2b = is_b2b_order(context_info, order_details)

        # Назначаем диалог только если есть dialog_id
        assignment_result = None
        target_dialog_id = context_info.get("dialog_id")

        if target_dialog_id:
            logger.info(f"[notify_seller] Назначаем менеджера на диалог {target_dialog_id}")
            assignment_result = handle_manager_assignment(is_b2b, target_dialog_id)
            logger.info(f"[notify_seller] Результат назначения диалога: {assignment_result}")
        else:
            logger.warning("[notify_seller] Отсутствует dialog_id, назначение диалога невозможно")
            assignment_result = {"status": "warning", "message": "Не указан диалог для назначения"}

        # Формируем сообщение для продавца
        message = format_seller_message(context_info, order_details, is_preorder, is_b2b, assignment_result)

        # Добавляем информацию о диалоге в сообщение, если она есть
        if target_dialog_id:
            message = message.replace("• ID чата:", f"• ID диалога: <code>{target_dialog_id}</code>\n• ID чата:")

        # Отправляем сообщение в супергруппу с указанием топика
        await bot.send_message(
            chat_id=int(config.TELEGRAM_CHAT_ID),
            text=message,
            parse_mode="HTML",
            message_thread_id=int(config.TELEGRAM_TOPIC_ID)
        )

        logger.info(f"[notify_seller] Уведомление успешно отправлено продавцу (chat_id: {config.TELEGRAM_CHAT_ID}, topic_id: {config.TELEGRAM_TOPIC_ID})")

        # Возвращаем подтверждение в зависимости от типа заказа
        if is_preorder:
            confirmation_message = "Ура! Ваш предзаказ успешно оформлен! 🎉 Растение будет доступно в течение 7-10 дней. Я лично прослежу, чтобы с вами связались для подтверждения заказа и уточнения деталей доставки. Спасибо, что выбрали наш магазин! 💚"
        else:
            confirmation_message = "Отлично! Ваш заказ успешно оформлен! 🎉 Я уже передала информацию нашему менеджеру, и с вами скоро свяжутся. Если возникнут вопросы, обращайтесь в любое время! Спасибо за выбор нашего магазина! 💚"

        return {"status": "success", "confirmation_message": confirmation_message}

    except Exception as e:
        logger.error(f"[notify_seller] Ошибка при отправке уведомления продавцу: {e}")
        error_message = "К сожалению, при оформлении заказа произошла ошибка. 😥 Пожалуйста, попробуйте еще раз или свяжитесь с нами напрямую."
        return {"status": "error", "error_message": error_message}
