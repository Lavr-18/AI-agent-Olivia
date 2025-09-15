import logging
import json
from enum import Enum
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime, timedelta

# Константы для промптов LLM
PERSONA_DESCRIPTION = """
## Личность и роль
Ты – Оливия, 27-летняя доброжелательная девушка-менеджер магазина растений TropicHouse. Ты эксперт в биологии и флористике.

## Стиль общения
- Общайся легко, дружелюбно и с заботой
- Используй официально-деловой тон, но не слишком строгий
- Добавляй эмоциональность и теплые обороты речи
- Уместно используй милые эмоджи 🌿🌱🌴

## Основные задачи
1. Помочь подобрать растение под потребности клиента
2. Проконсультировать по наличию растений
3. Довести до продажи

## Определение неприхотливости растений
При рекомендации растений помни:
- **Неприхотливые растения** - это растения с уходом "Легкий (подходит новичкам)" и "Средний (для опытных)"
- **Прихотливые растения** - это растения с уходом "Сложный (для продвинутых)"

## Алгоритм диалога
1. В первом сообщении: кратко представься и спроси, что нужно пользователю
2. В последующих: не представляйся повторно, продолжай диалог
3. Последовательно уточняй важные параметры:
   - Размер растения (напольное >90см или настольное <90см)
   - Назначение (домой, в офис или в подарок)
   - Особые требования к уходу/освещению
4. Задавай вопросы по очереди, не все сразу
5. После оформления заказа ОБЯЗАТЕЛЬНО предложи полезные аксессуары для ухода за растением

## Особые ситуации
- Если растения нет в наличии: предложи предзаказ (3-10 дней) или альтернативы
- При вопросе о доставке: дай ссылку https://tropichouse.ru/help/delivery/
- При подборе приоритизируй: сначала растения в кашпо, затем в техническом горшке
- Приоритизируй живые растения, но предлагай искусственные если клиент интересуется
"""
ADDRESS_INFO = """🏢 НАШ АДРЕС
г. Москва, БЦ "Платформа", Спартаковский переулок, д.2, стр.1 6 подъезд, 4 этаж, офис 33

🚗 На территории БЦ для клиентов есть бесплатная парковка, чтобы воспользоваться парковкой, необходимо заранее заказать пропуск на въезд.

⏰ РЕЖИМ РАБОТЫ
Пн – Пт: с 10:00 до 19:00
Сб – Вс: с 11:00 до 19:00

📞 ТЕЛЕФОН
+7 (495) 221-88-38"""

RESPONSE_FORMAT_INSTRUCTIONS = """
# Формат вывода информации о растениях

## Структура блока информации о растении:
```
N. {название растения}
Уход: {краткое описание ухода}
Цена: от {цена} руб.
Ссылка: {полный URL}
```

## Правила:
1. Нумеруй растения последовательно (1., 2., 3.)
2. Предлагай не больше ТРЁХ растений за один раз
3. Выбирай наиболее подходящие под запрос пользователя
4. Перед списком добавь краткий приветливый вводный абзац с эмоджи
   Пример: «Вот несколько растений, которые отлично подойдут для вашей гостиной 🌿»
5. После списка добавь 1-2 предложения с предложением помощи в выборе
"""

logger = logging.getLogger(__name__)


# --------------------------- #
#        Состояния диалога   #
# --------------------------- #

class DialogState(Enum):
    START = "start"  # Начало диалога
    ASK_SIZE = "ask_size"  # Уточнение размера растения
    ASK_LOCATION = "ask_location"  # Уточнение места размещения
    PLANT_SEARCH = "search"  # Поиск и выбор растений
    OUT_OF_STOCK = "outofstock"  # Растение не в наличии, но можно заказать
    ORDERING = "order"  # Процесс оформления заказа
    CART_MANAGEMENT = "cart"  # Управление корзиной
    CART_CHECKOUT = "checkout"  # Оформление всего заказа из корзины
    UPSELL = "upsell"  # Предложение дополнительных товаров
    COMPLETED = "completed"  # Диалог завершён
    MANAGER_CALLED = "manager_called"  # Менеджер был вызван, бот не отвечает на сообщения


# --------------------------- #
#    Класс для контекста     #
# --------------------------- #

class ChatContext:
    """
    Хранит всю необходимую информацию о ходе диалога: последние сообщения,
    текущее состояние, данные выбранных растений, детали заказа и т.д.
    """

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.dialog_id: Optional[str] = None
        self.created_at = datetime.now()  # Время создания контекста
        self.messages = []  # история (список словарей {"role": ..., "content": ...})
        self.state = DialogState.START
        self.desired_size: Optional[str] = None  # 'floor', 'tabletop', 'any'
        self.desired_location: Optional[str] = None  # 'home', 'office', 'gift', 'any'
        self.selected_plants = None  # Сюда можно складывать выбранные позиции (при необходимости)
        self.cart = []  # Корзина: [{"plant": plant_data, "quantity": int, "type": "order"/"preorder"}, ...]
        self.order_details = None  # Сюда можно складывать детали заказа
        self.potential_groups = None  # Сюда можно складывать сгруппированные результаты (если их >5 и т.п.)
        self.channel_info = None  # Информация о канале (name, id)
        self.user_info = None  # Информация о пользователе (name, id)
        self.out_of_stock_plant = None  # Растение, которого нет в наличии, но пользователь интересуется
        self.out_of_stock_plants = None  # Список растений без остатка, если нашлось несколько
        self.preorder_info = None  # Информация о предзаказе (сроки доставки, комментарии и т.д.)
        self.last_search_query = None  # Последний поисковый запрос для сохранения контекста

    def is_expired(self, days: int = 7) -> bool:
        """Проверяет, истек ли срок действия контекста (по умолчанию 7 дней)."""
        expiry_date = self.created_at + timedelta(days=days)
        return datetime.now() > expiry_date

    def add_message(self, role: str, text: Optional[str], tool_calls: Optional[List[Dict]] = None,
                    tool_call_id: Optional[str] = None, name: Optional[str] = None):
        """Добавляет сообщение в историю диалога, поддерживая формат OpenAI.

        Args:
            role: Роль (system, user, assistant, tool).
            text: Текстовое содержимое сообщения (может быть None для assistant при tool_calls).
            tool_calls: Список вызовов инструментов (для сообщений assistant).
            tool_call_id: ID вызова инструмента (для сообщений tool).
            name: Имя инструмента (для сообщений tool).
        """
        message = {"role": role, "content": text}
        if tool_calls:
            # OpenAI ожидает tool_calls как список объектов, а не JSON-строку
            message["tool_calls"] = tool_calls
            # У OpenAI content должен быть null, если есть tool_calls
            message["content"] = None
        if tool_call_id:
            message["tool_call_id"] = tool_call_id
            # У tool-сообщений в content обычно результат вызова (уже должен быть в text)
        if name:
            message["name"] = name  # Для tool-сообщений

        # Проверка для role="tool": если text=None, ставим content=""
        if role == "tool" and text is None:
            message["content"] = ""
        # Удаляем ключи с None значениями, кроме content у assistant при tool_calls
        # и content у tool (если мы его только что установили в "")
        elif message["content"] is None and role != "assistant":
            del message["content"]

        self.messages.append(message)

    def get_last_n_messages(self, n: int = 5) -> List[Dict[str, Any]]:  # Возвращаемый тип Any из-за tool_calls
        """
        Возвращает последние n сообщений в истории.
        """
        return self.messages[-n:] if len(self.messages) >= n else self.messages

    def reset_out_of_stock_state(self):
        """
        Сбрасывает информацию о состоянии OUT_OF_STOCK,
        используется при переходе в другое состояние.
        """
        self.out_of_stock_plant = None
        self.out_of_stock_plants = None
        self.preorder_info = None

    def reset_preferences(self):
        """Сбрасывает предпочтения пользователя."""
        self.desired_size = None
        self.desired_location = None

    def change_state(self, new_state: DialogState):
        """
        Изменяет состояние диалога с соответствующими сбросами данных.
        """
        logger.info(f"Chat {self.chat_id}: State change {self.state} -> {new_state}")

        # Если переходим из OUT_OF_STOCK в другое состояние, сбрасываем связанные данные
        if self.state == DialogState.OUT_OF_STOCK and new_state != DialogState.OUT_OF_STOCK:
            self.reset_out_of_stock_state()

        # Если переходим в COMPLETED, можно выполнить дополнительные действия при завершении
        if new_state == DialogState.COMPLETED:
            logger.info(f"Диалог в чате {self.chat_id} завершён")
            # При завершении диалога очищаем корзину
            self.clear_cart()

        # Если переходим к новому поиску или начинаем сначала, сбрасываем предпочтения
        if new_state in [DialogState.START, DialogState.PLANT_SEARCH] and self.state not in [DialogState.START,
                                                                                             DialogState.PLANT_SEARCH,
                                                                                             DialogState.CART_MANAGEMENT]:
            self.reset_preferences()
            # НЕ очищаем корзину при переходе к поиску, позволяем накапливать растения

        # Устанавливаем новое состояние
        self.state = new_state

    def reset_dialog(self):
        """Полностью сбрасывает состояние диалога."""
        self.state = DialogState.START
        self.dialog_id = None
        self.messages = []
        self.selected_plants = None
        self.order_details = None
        self.potential_groups = None
        self.reset_out_of_stock_state()
        self.reset_preferences()  # Сбрасываем предпочтения
        self.last_search_query = None  # Сбрасываем последний поисковый запрос
        logger.info(f"Chat {self.chat_id}: Dialog reset")

    def set_out_of_stock_info(self, plant_data: Dict[str, Any], plants_list: Optional[List[Dict[str, Any]]] = None):
        """Устанавливает информацию о растении(ях) отсутствующих в наличии."""
        self.out_of_stock_plant = plant_data
        self.out_of_stock_plants = plants_list

    # Методы для работы с корзиной
    def add_to_cart(self, plant_data: Dict[str, Any], quantity: int = 1, order_type: str = "order"):
        """Добавляет растение в корзину."""
        # Проверяем, есть ли уже такое растение в корзине
        for item in self.cart:
            if item["plant"].get("Название") == plant_data.get("Название"):
                item["quantity"] += quantity
                return

        # Если растения нет в корзине, добавляем новый элемент
        self.cart.append({
            "plant": plant_data,
            "quantity": quantity,
            "type": order_type  # "order" или "preorder"
        })

    def remove_from_cart(self, plant_name: str):
        """Удаляет растение из корзины по названию."""
        self.cart = [item for item in self.cart if item["plant"].get("Название") != plant_name]

    def get_cart_summary(self) -> str:
        """Возвращает краткое описание содержимого корзины."""
        if not self.cart:
            return "Корзина пуста"

        total_items = sum(item["quantity"] for item in self.cart)
        items_text = []

        for item in self.cart:
            plant_name = item["plant"].get("Название", "Неизвестное растение")
            quantity = item["quantity"]
            order_type = " (предзаказ)" if item["type"] == "preorder" else ""
            items_text.append(f"• {plant_name} - {quantity} шт.{order_type}")

        return f"🛒 В корзине ({total_items} растений):\n" + "\n".join(items_text)

    def clear_cart(self):
        """Очищает корзину."""
        self.cart = []
