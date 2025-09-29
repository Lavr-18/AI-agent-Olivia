import json
import config
from openai import AsyncOpenAI
import plant_utils
import telegrambot
from chat_context import ChatContext, DialogState, PERSONA_DESCRIPTION, ADDRESS_INFO, RESPONSE_FORMAT_INSTRUCTIONS
from agents import Agent, Runner, function_tool, ModelSettings, RunContextWrapper
import logging
import re
from datetime import datetime, timedelta # <--- ИЗМЕНЕНИЕ: Добавлен timedelta
from typing import Optional # <--- ДОБАВЛЕНО для type hinting

# Константы для логики 24-часового окна
REVIEW_WINDOW_HOURS = 24
# ВНИМАНИЕ: Укажите точную подстроку, которую отправляет другой бот
# при запросе отзыва, чтобы наш бот мог установить таймер.
# Например: "Оцените нашу работу по пятибалльной шкале."
REVIEW_REQUEST_TRIGGER = "оцените нашу работу"

logger = logging.getLogger(__name__)

# Возможные категории запросов для менеджера
CATEGORY_REPLIES = {
    "live_photo": "Сейчас я спрошу у коллеги, чтобы он сделал для вас свеженькое фото растения 📸 Немного подождите, хорошо?",
    "multiple_plants": "Для вашего большого заказа я уже зову нашего менеджера 🤗 Пожалуйста, подождите немного!",
    "order_question": "Сейчас уточню детали вашего заказа и скоро вернусь с ответом 📦",
    "call_request": "Понял, сейчас попрошу менеджера связаться с вами по телефону 📞",
    "reclamation": "Я передам ваш вопрос нашему менеджеру по рекламациям 🙏 Подождите, пожалуйста.",
    "ask_human": "Конечно, я позову менеджера, который поможет лично 🤝 Подождите чуть-чуть!",
    # "delivery": "Сейчас свяжусь с менеджером по доставке 🚚 Немного подождите, пожалуйста.",
    "office_plant": "Зову нашего эксперта по озеленению офисов 🌿 Он скоро свяжется с вами.",
}

# Префиксы уведомлений для менеджера
DETAILS_PREFIX = {
    "live_photo": "Пользователь запрашивает живое фото растения",
    "multiple_plants": "Пользователь спрашивает про заказ нескольких растений",
    "order_question": "Вопрос по текущему заказу",
    "call_request": "Пользователь просит позвонить",
    "reclamation": "Рекламация",
    "ask_human": "Пользователь просит связать с менеджером",
    # "delivery": "Вопрос по доставке",
    "office_plant": "Пользователь интересуется растениями для офиса",
}

# Таблица сопоставления размеров растения и подходящих горшков
PLANT_POT_SIZE_MAPPING = {
    # Диаметр горшка у растения -> диаметр кашпо, которое подбираем
    9: (10, 12),
    10: (10, 12),
    11: (12, 15),
    12: (12, 15),
    13: (15, 18),
    14: (15, 18),
    15: (15, 18),
    16: (19, 21),
    17: (19, 21),
    18: (19, 21),
    19: (21, 25),
    20: (21, 25),
    21: (21, 25),
    22: (21, 25),
    23: (26, 29),
    24: (28, 30),
    25: (30, 35),
    26: (30, 35),
    27: (30, 35),
    28: (30, 35),
    29: (35, 40),
    30: (35, 40),
    31: (35, 40),
    32: (35, 40),
    33: (40, 45),
    34: (40, 45),
    35: (40, 45),
    36: (43, 50),
    37: (50, 59),
    38: (50, 59),
    39: (50, 59),
    40: (50, 59),
    41: (50, 59),
    42: (50, 59),
    43: (50, 59),
    44: (50, 59),
    45: (50, 59),
    46: (60, 70),
    47: (60, 70),
    48: (60, 70),
    49: (60, 70),
    50: (60, 70),
    51: (60, 70),
    52: (60, 70),
    53: (60, 70)
}


def extract_plant_diameter(plant_name: str) -> int | None:
    """Извлекает диаметр горшка из названия растения"""
    # Ищем паттерны типа "12/45 см", "d17 см", "21/110 см"
    patterns = [
        r'(\d+)/\d+\s*см',  # "12/45 см" - берем первое число
        r'd(\d+)\s*см',  # "d17 см" - берем число после d
        r'(\d+)\s*см',  # "21 см" - просто число с см
    ]

    for pattern in patterns:
        match = re.search(pattern, plant_name)
        if match:
            diameter = int(match.group(1))
            # Проверяем, что диаметр в разумных пределах (5-60 см)
            if 5 <= diameter <= 60:
                return diameter

    return None


def generate_pot_link(plant_diameter: int) -> str:
    """Генерирует ссылку на подходящие горшки по размеру растения"""
    if plant_diameter not in PLANT_POT_SIZE_MAPPING:
        # Если точного соответствия нет, используем ближайший размер
        closest_diameter = min(PLANT_POT_SIZE_MAPPING.keys(),
                               key=lambda x: abs(x - plant_diameter))
        pot_range = PLANT_POT_SIZE_MAPPING[closest_diameter]
    else:
        pot_range = PLANT_POT_SIZE_MAPPING[plant_diameter]

    min_diameter, max_diameter = pot_range

    # Формируем ссылку с нужными параметрами
    base_url = "https://tropichouse.ru/catalog/gorshki_i_kashpo/filter"
    link = f"{base_url}/diameter-from-{min_diameter}-to-{max_diameter}/apply/"

    return link


async def check_and_send_pot_suggestion(chat_id: str, selected_plants: list):
    """Проверяет растения в техническом горшке и отправляет предложение купить кашпо"""
    from main import send_message

    tech_pot_plants = []

    # Проверяем каждое выбранное растение
    for plant in selected_plants:
        pot_info = plant.get("Кашпо/Горшок", "")
        if "в техническом горшке" in pot_info:
            tech_pot_plants.append(plant)

    if not tech_pot_plants:
        return  # Нет растений в технических горшках

    # Формируем сообщение для каждого растения в техническом горшке
    for plant in tech_pot_plants:
        plant_name = plant.get("Название", "растение")
        diameter = extract_plant_diameter(plant_name)

        if diameter:
            pot_link = generate_pot_link(diameter)
            message = f"""🪴 Кстати! Ваше растение "{plant_name}" поставляется в техническом горшке. 

Рекомендуем дополнить заказ красивым кашпо подходящего размера:
{pot_link}

Это не только украсит интерьер, но и обеспечит растению лучшие условия! 🌿✨"""
        else:
            # Если не удалось определить размер, отправляем общую ссылку
            message = f"""🪴 Кстати! Ваше растение "{plant_name}" поставляется в техническом горшке. 

Рекомендуем дополнить заказ красивым кашпо:
https://tropichouse.ru/catalog/gorshki_i_kashpo/

Это не только украсит интерьер, но и обеспечит растению лучшие условия! 🌿✨"""

        await send_message(chat_id, message)


async def classify_intent(ctx: RunContextWrapper[ChatContext] | ChatContext, text: str) -> str:
    """Классифицирует запрос пользователя для выделения случаев, требующих уведомления менеджера."""
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    system_prompt = """
# Инструкция по классификации запросов клиентов
Ты анализируешь запросы клиентов магазина растений и классифицируешь их по следующим категориям:

## Категории (верни ТОЛЬКО одну из этих категорий):
- live_photo: запрос живого фото растения
- multiple_plants: запрос на заказ нескольких растений
- order_question: вопросы по ранее сделанному заказу (статус заказа, отмена заказа, проблемы с доставкой уже оформленного заказа)
- call_request: просьба позвонить, связаться по телефону
- pot_request: вопросы только про кашпо и горшки (без растений)
- reclamation: рекламации (только серьёзные случаи)
- ask_human: просьба позвать менеджера/человека
- delivery: вопросы по доставке
- office_plant: вопросы о растениях для офиса, озеленение офиса, b2b заказы
- none: все прочие запросы

## Приоритизация:
ОСОБЕННО ВНИМАТЕЛЬНО отслеживай запросы связанные с:
- растения для офиса
- озеленение офиса
- b2b услуги
- корпоративные заказы
- вопросы по уже сделанным заказам (когда и что привезут, изменить заказ, отменить заказ, где мой заказ)
- просьбы позвонить, связаться по телефону (перезвоните, позвоните мне, нужен звонок)
- вопросы только про кашпо и горшки (покажите кашпо, нужен горшок, какие кашпо есть)

## Формат ответа:
Верни ТОЛЬКО ОДНО СЛОВО из списка категорий выше, без пояснений.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]
    response = await client.chat.completions.create(model="gpt-4.1-mini", messages=messages)
    category = response.choices[0].message.content.strip().lower()
    if category not in CATEGORY_REPLIES:
        return "none"
    return category


# Динамические инструкции с учётом состояния диалога

def _make_instructions(ctx: RunContextWrapper[ChatContext] | ChatContext, agent: Agent[ChatContext]) -> str:
    # Получаем базовые инструкции
    instructions = f"{PERSONA_DESCRIPTION}\n{ADDRESS_INFO}\n{RESPONSE_FORMAT_INSTRUCTIONS}"

    # Проверяем тип объекта и получаем state
    if isinstance(ctx, RunContextWrapper):
        state = ctx.context.state
        cart = ctx.context.cart
    else:
        state = ctx.state
        cart = ctx.cart

    # Добавляем информацию о корзине, если в ней есть товары
    if cart:
        cart_info = f"\n## Текущая корзина:\n{ctx.context.get_cart_summary() if isinstance(ctx, RunContextWrapper) else ctx.get_cart_summary()}"
        instructions += cart_info

    # Добавляем контекстно-зависимые инструкции
    if state == DialogState.START:
        instructions += """
## Текущее состояние: Начало диалога
Используй ТОЧНО ЭТОТ шаблон для первого сообщения:

Здравствуйте! 
Меня зовут Оливия 🍀 Я менеджер Tropic House, созданная на основе искусственного интеллекта. 
Меня обучили подбирать растения под Ваши задачи и я могу проконсультировать Вас по наличию 🌳🌴🌵
Если мне не хватит компетентности, сразу переведу наш диалог на специалиста🌷

НЕ ИЗМЕНЯЙ ЭТОТ ТЕКСТ. Используй его как есть.
"""
    elif state == DialogState.ASK_SIZE:
        instructions += "\n## Текущее состояние: Уточнение размера\nСфокусируйся на выяснении предпочтительного размера растения (напольное >90см или настольное <90см)."
    elif state == DialogState.ASK_LOCATION:
        instructions += "\n## Текущее состояние: Уточнение места размещения\nУточни, куда планируется поставить растение (дом, офис, подарок)."
    elif state == DialogState.PLANT_SEARCH:
        instructions += """
## Текущее состояние: Подбор растений
Ищи растения, максимально соответствующие критериям пользователя.
ВАЖНО: После показа растений ВСЕГДА предлагай добавить растение в корзину с помощью функции add_to_cart.
Спрашивай: "Хотите добавить это растение в корзину?" или "Добавить в корзину?"
"""
    elif state == DialogState.OUT_OF_STOCK:
        instructions += "\n## Текущее состояние: Растение не в наличии\nМягко предложи оформить предзаказ с доставкой через 3-10 дней или рассмотреть альтернативы. Можно добавить в корзину как предзаказ."
    elif state == DialogState.ORDERING:
        instructions += "\n## Текущее состояние: Оформление заказа\nПозови менеджера и подготовь всю информацию для оформления заказа."
    elif state == DialogState.CART_MANAGEMENT:
        instructions += """
## Текущее состояние: Управление корзиной
Пользователь управляет содержимым корзины. Доступны функции:
- add_to_cart: добавить еще растения
- show_cart: показать содержимое корзины
- remove_from_cart: удалить растение из корзины
- checkout_cart: оформить заказ всех растений

После добавления растения в корзину ВСЕГДА спрашивай: "Хотите добавить еще растения или оформим заказ?"
"""
    elif state == DialogState.UPSELL:
        instructions += "\n## Текущее состояние: Предложение дополнительных товаров\nПредложи полезные аксессуары для ухода за растением. Используй функцию suggest_accessories."

    return instructions


@function_tool
async def search(ctx: RunContextWrapper[ChatContext], query: str) -> str:
    """Ищет растения в базе данных по запросу пользователя."""
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

    # Результаты векторного поиска
    results_with_score = await plant_utils.vector_search_with_score(query, top_k=50, openai_client=client)

    processed_results = []
    for plant_data, score in results_with_score:
        item = plant_data.copy()  # Копируем все поля
        item["relevance_score"] = score
        processed_results.append(item)

    # Результаты поиска по имени
    name_search_results = plant_utils.search_plants_by_name(query)
    for plant_data_from_name_search in name_search_results:
        name = plant_data_from_name_search.get("Название")

        # Проверяем, есть ли растение с таким же "Название" уже в результатах
        is_duplicate = any(
            existing_item.get("Название") == name
            for existing_item in processed_results
        )

        if not is_duplicate:
            item = plant_data_from_name_search.copy()  # Копируем все поля
            item["relevance_score"] = 1.0  # Прямые совпадения по имени получают высокий балл
            processed_results.append(item)

    return json.dumps({"results": processed_results, "total_count": len(processed_results), "query": query},
                      ensure_ascii=False)


@function_tool
async def order(ctx: RunContextWrapper[ChatContext], plant: str, quantity: int,
                customer_info: str | None = None) -> str:
    """Оповещает менеджера о заказе."""
    details = f"Заказ: {plant}\nКоличество: {quantity}"
    if customer_info:
        details += f"\nКонтактная информация: {customer_info}"
    result = await telegrambot.notify_seller(details, is_preorder=False, context=ctx.context)

    # Ищем растение в базе данных по названию для проверки технического горшка
    plant_data = plant_utils.search_plants_by_name(plant)
    if plant_data:
        await check_and_send_pot_suggestion(ctx.context.chat_id, plant_data)

    # После успешного заказа переходим к предложению аксессуаров
    ctx.context.change_state(DialogState.UPSELL)

    return json.dumps(result, ensure_ascii=False)


@function_tool
async def preorder(ctx: RunContextWrapper[ChatContext], plant: str, quantity: int,
                   customer_info: str | None = None) -> str:
    """Оповещает менеджера о предзаказе."""
    details = f"ПРЕДЗАКАЗ: {plant}\nКоличество: {quantity}"
    if customer_info:
        details += f"\nКонтактная информация: {customer_info}"
    result = await telegrambot.notify_seller(details, is_preorder=True, context=ctx.context)

    # Ищем растение в базе данных по названию для проверки технического горшка
    plant_data = plant_utils.search_plants_by_name(plant)
    if plant_data:
        await check_and_send_pot_suggestion(ctx.context.chat_id, plant_data)

    # После успешного предзаказа переходим к предложению аксессуаров
    ctx.context.change_state(DialogState.UPSELL)

    return json.dumps(result, ensure_ascii=False)


@function_tool
async def suggest_accessories(ctx: RunContextWrapper[ChatContext]) -> str:
    """Предлагает дополнительные товары после покупки растения."""
    accessories = {
        "Лейки и опрыскиватели": "https://tropichouse.ru/catalog/aksessuary/leyki_i_opryskivateli/",
        "Удобрения": "https://tropichouse.ru/catalog/udobreniya/udobreniya_1/",
        "Фитолампы": "https://tropichouse.ru/catalog/aksessuary/fitolampy/",
        "Приборы для ухода": "https://tropichouse.ru/catalog/aksessuary/pribory_dlya_rasteniy/"
    }

    # Переводим в состояние UPSELL
    ctx.context.change_state(DialogState.UPSELL)

    return json.dumps({"accessories": accessories}, ensure_ascii=False)


@function_tool
async def add_to_cart(ctx: RunContextWrapper[ChatContext], plant: str, quantity: int, order_type: str = "order") -> str:
    """Добавляет растение в корзину для последующего оформления заказа."""
    # Ищем растение в базе данных по названию
    plant_data = plant_utils.search_plants_by_name(plant)

    if not plant_data:
        return "Растение не найдено в каталоге"

    # Берем первое растение из результатов поиска
    selected_plant = plant_data[0] if isinstance(plant_data, list) else plant_data

    # Добавляем в корзину
    ctx.context.add_to_cart(selected_plant, quantity, order_type)

    # Переходим в состояние управления корзиной
    ctx.context.change_state(DialogState.CART_MANAGEMENT)

    plant_name = selected_plant.get("Название", plant)
    return f"✅ Растение '{plant_name}' добавлено в корзину (количество: {quantity})"


@function_tool
async def show_cart(ctx: RunContextWrapper[ChatContext]) -> str:
    """Показывает содержимое корзины."""
    cart_summary = ctx.context.get_cart_summary()

    if ctx.context.cart:
        # Переводим в состояние управления корзиной
        ctx.context.change_state(DialogState.CART_MANAGEMENT)
        return cart_summary + "\n\nВы можете добавить еще растения, удалить что-то из корзины или оформить заказ."
    else:
        return cart_summary


@function_tool
async def checkout_cart(ctx: RunContextWrapper[ChatContext], customer_info: str | None = None) -> str:
    """Оформляет заказ всех растений из корзины."""
    if not ctx.context.cart:
        return "Корзина пуста, нечего заказывать"

    # Формируем детали заказа
    orders = []
    preorders = []

    for item in ctx.context.cart:
        plant_name = item["plant"].get("Название", "Неизвестное растение")
        quantity = item["quantity"]

        if item["type"] == "preorder":
            preorders.append(f"{plant_name} - {quantity} шт.")
        else:
            orders.append(f"{plant_name} - {quantity} шт.")

    # Формируем сообщения для менеджера
    results = []

    if orders:
        order_details = "ЗАКАЗ:\n" + "\n".join(orders)
        if customer_info:
            order_details += f"\n\nКонтактная информация: {customer_info}"

        result = await telegrambot.notify_seller(order_details, is_preorder=False, context=ctx.context)
        results.append(result)

        # Проверяем растения в технических горшках
        cart_plants = [item["plant"] for item in ctx.context.cart if item["type"] == "order"]
        if cart_plants:
            await check_and_send_pot_suggestion(ctx.context.chat_id, cart_plants)

    if preorders:
        preorder_details = "ПРЕДЗАКАЗ:\n" + "\n".join(preorders)
        if customer_info:
            preorder_details += f"\n\nКонтактная информация: {customer_info}"

        result = await telegrambot.notify_seller(preorder_details, is_preorder=True, context=ctx.context)
        results.append(result)

        # Проверяем растения в технических горшках
        cart_plants = [item["plant"] for item in ctx.context.cart if item["type"] == "preorder"]
        if cart_plants:
            await check_and_send_pot_suggestion(ctx.context.chat_id, cart_plants)

    # Очищаем корзину и переходим к предложению аксессуаров
    ctx.context.clear_cart()
    ctx.context.change_state(DialogState.UPSELL)

    return json.dumps({"success": True, "message": "Заказ оформлен"}, ensure_ascii=False)


@function_tool
async def remove_from_cart(ctx: RunContextWrapper[ChatContext], plant_name: str) -> str:
    """Удаляет растение из корзины по названию."""
    # Проверяем, есть ли растение в корзине
    plant_found = False
    for item in ctx.context.cart:
        if plant_name.lower() in item["plant"].get("Название", "").lower():
            plant_found = True
            break

    if not plant_found:
        return f"Растение '{plant_name}' не найдено в корзине"

    # Удаляем растение
    ctx.context.remove_from_cart(plant_name)

    # Остаемся в состоянии управления корзиной
    ctx.context.change_state(DialogState.CART_MANAGEMENT)

    return f"✅ Растение '{plant_name}' удалено из корзины"


agent = Agent[ChatContext](
    name="TropicHouseAgent",
    instructions=_make_instructions,
    model="gpt-4.1-mini",
    tools=[search, order, preorder, suggest_accessories, add_to_cart, show_cart, checkout_cart, remove_from_cart],
    model_settings=ModelSettings(tool_choice="auto"),
)


async def extract_pot_size(text: str) -> int | None:
    """Извлекает размер кашпо из запроса пользователя."""
    # Сначала пытаемся найти размер прямо в тексте
    patterns = [
        r'(\d+)\s*см',  # "20 см", "15см"
        r'd(\d+)',  # "d15", "d20"
        r'диаметр\s*(\d+)',  # "диаметр 20"
        r'размер\s*(\d+)',  # "размер 15"
    ]

    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            size = int(match.group(1))
            # Проверяем, что размер в разумных пределах для кашпо (10-70 см)
            if 10 <= size <= 70:
                return size

    return None


async def extract_plant_quantity(text: str) -> int:
    """Определяет количество растений в запросе пользователя."""
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    system_prompt = """
Определи количество растений, которое хочет заказать пользователь в офис.

Если пользователь:
- Спрашивает про одно конкретное растение → верни 1
- Спрашивает про несколько растений, озеленение офиса, много растений → верни 2 или больше
- Просто интересуется растениями для офиса без указания количества → верни 1

Верни ТОЛЬКО ЧИСЛО (1, 2, 3 и т.д.) без дополнительного текста.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]
    try:
        response = await client.chat.completions.create(model="gpt-4.1-mini", messages=messages)
        quantity_str = response.choices[0].message.content.strip()
        return int(quantity_str)
    except:
        return 1  # По умолчанию возвращаем 1


async def is_working_hours_and_managers_available(category: str) -> tuple[bool, bool]:
    """Проверяет рабочее время и доступность менеджеров

    Returns:
        tuple: (is_working_hours, has_online_managers)
    """
    now = datetime.now()
    current_hour = now.hour

    # Рабочие часы: до 19:00
    is_working_hours = current_hour < 19

    # Проверяем доступность менеджеров только если требуется их вызов
    has_online_managers = False
    if category in ["office_plant", "multiple_plants", "live_photo", "ask_human", "reclamation", "order_question",
                    "call_request"]:
        # Определяем тип заказа для выбора группы менеджеров
        is_b2b = category == "office_plant"
        manager_group = config.MANAGER_B2B if is_b2b else config.MANAGER_B2C

        try:
            online_managers = telegrambot.get_online_managers(manager_group["id"])
            has_online_managers = len(online_managers) > 0
        except Exception as e:
            logger.error(f"[is_working_hours_and_managers_available] Ошибка проверки менеджеров: {e}")
            has_online_managers = False

    return is_working_hours, has_online_managers


async def run_unified_agent(context: ChatContext, user_message: str, message_data: dict, openai_client=None) -> Optional[str]:
    """Обёртка над Agents SDK Runner с сохранением истории сообщений.
    Теперь включает логику фильтрации сообщений от ботов и 24-часовой таймер."""

    # ----------------------------------------------------------------
    # 1. ЛОГИКА ФИЛЬТРАЦИИ И УСТАНОВКИ ТАЙМЕРА (НОВЫЙ БЛОК)
    # ----------------------------------------------------------------
    # REVIEW_WINDOW_HOURS и REVIEW_REQUEST_TRIGGER должны быть определены выше в файле

    sender_type = message_data.get("from", {}).get("type")

    # 1.1. Логика для ботов/системы (УСТАНОВКА ТАЙМЕРА)
    if sender_type in ["bot", "system"]:
        # Проверяем, является ли это сообщение триггером запроса отзыва
        # Проверяем нижний регистр, чтобы быть менее чувствительными к регистру
        if REVIEW_REQUEST_TRIGGER in user_message.lower():
            # Устанавливаем таймер
            context.review_request_timestamp = datetime.now()
            logger.info(f"[run_unified_agent] Установлен таймер запроса отзыва для chat {context.chat_id}.")

        # Игнорируем все сообщения от ботов и системы
        logger.info(f"[run_unified_agent] Игнорируем сообщение от отправителя: {sender_type}")
        return None  # Бот молчит

    # 1.2. Логика для клиента (ПРОВЕРКА ТАЙМЕРА)
    elif sender_type == "customer":
        # Если таймер установлен И прошло меньше 24 часов
        if (context.review_request_timestamp and
                datetime.now() - context.review_request_timestamp < timedelta(hours=REVIEW_WINDOW_HOURS)):

            # Игнорируем сообщение клиента в течение 24 часов
            logger.info(
                f"[run_unified_agent] Игнорируем сообщение клиента (chat {context.chat_id}) в пределах 24-часового окна отзыва.")
            return None  # Бот молчит

        # Если таймер установлен И прошло БОЛЕЕ 24 часов
        elif context.review_request_timestamp and datetime.now() - context.review_request_timestamp >= timedelta(
                hours=REVIEW_WINDOW_HOURS):
            # Сбрасываем таймер
            context.review_request_timestamp = None
            logger.info(f"[run_unified_agent] Сброшен таймер 24h для chat {context.chat_id}.")

    # ----------------------------------------------------------------
    # 2. ОСНОВНАЯ ЛОГИКА АГЕНТА (СКОРРЕКТИРОВАННАЯ)
    # ----------------------------------------------------------------

    # Сохраняем сообщение пользователя в контекст (если оно не было отфильтровано)
    context.add_message(role="user", text=user_message)

    # Проверяем, не был ли уже вызван менеджер ранее
    # Пропускаем проверку для команды /start
    if context.state == DialogState.MANAGER_CALLED and not user_message.strip().lower() in ["/start"]:
        # Если менеджер уже был вызван, не отвечаем на сообщения клиента
        logger.info(f"[run_unified_agent] Менеджер уже вызван для chat {context.chat_id}, бот не отвечает на сообщения")
        return None  # <-- ИЗМЕНЕНО: return "" заменено на return None

    # Проверяем категории, требующие уведомления менеджера
    category = await classify_intent(context, user_message)

    # Специальная логика для office_plant: вызываем менеджера только при количестве > 1
    if category == "office_plant":
        quantity = await extract_plant_quantity(user_message)
        if quantity <= 1:
            # Для одного растения не вызываем менеджера, обрабатываем обычным агентом
            category = "none"

    # Специальная обработка для запросов про кашпо - отвечаем ссылкой
    if category == "pot_request":
        pot_size = await extract_pot_size(user_message)
        if pot_size:
            # Если размер указан, генерируем ссылку на подходящие кашпо
            pot_link = generate_pot_link(pot_size)
            response = f"🪴 Вот подходящие кашпо диаметром {pot_size} см:\n{pot_link}"
        else:
            # Если размер не указан, показываем общий каталог кашпо
            response = "🪴 Вот наш каталог кашпо и горшков:\nhttps://tropichouse.ru/catalog/gorshki_i_kashpo/"

        context.add_message(role="assistant", text=response)
        return response

    if category != "none":
        # Специальная обработка для вопросов по заказу и просьб позвонить - переводим в MANAGER_CALLED без отправки ответа
        if category in ["order_question", "call_request"]:
            details = f"{DETAILS_PREFIX[category]}: {user_message}"
            context.subject = DETAILS_PREFIX[category]
            await telegrambot.notify_seller(details, is_preorder=False, context=context)
            # Переводим в состояние MANAGER_CALLED без отправки ответа пользователю
            context.change_state(DialogState.MANAGER_CALLED)
            return None  # <-- ИЗМЕНЕНО: return "" заменено на return None

        # Проверяем рабочее время и доступность менеджеров
        is_working_hours, has_online_managers = await is_working_hours_and_managers_available(category)

        # Если нерабочее время и нет менеджеров онлайн
        if not is_working_hours and not has_online_managers:
            after_hours_message = "Запрос принят, свяжемся с Вами в рабочее время ⏰"
            context.add_message(role="assistant", text=after_hours_message)

            # Сохраняем запрос для обработки в рабочее время
            details = f"{DETAILS_PREFIX[category]}: {user_message}"
            context.subject = DETAILS_PREFIX[category]
            await telegrambot.notify_seller(details, is_preorder=False, context=context)

            return after_hours_message

        # Обычная логика для рабочего времени или при наличии онлайн менеджеров
        details = f"{DETAILS_PREFIX[category]}: {user_message}"
        # Добавляем тему в контекст для передачи в notify_seller
        context.subject = DETAILS_PREFIX[category]
        # Отправляем уведомление менеджеру
        await telegrambot.notify_seller(details, is_preorder=False, context=context)
        # Отправляем ответ клиенту
        reply = CATEGORY_REPLIES[category]
        # Сохраняем ответ в контекст
        context.add_message(role="assistant", text=reply)
        # Устанавливаем новое состояние: менеджер вызван
        context.change_state(DialogState.MANAGER_CALLED)
        return reply

    # Запускаем агента
    history = context.get_last_n_messages(20)
    messages = [{"role": "system", "content": _make_instructions(context, agent)}] + history
    result = await Runner.run(agent, messages, context=context)

    # Проверяем, перешли ли мы в состояние UPSELL после выполнения агента
    if context.state == DialogState.UPSELL:
        # Импортируем функцию отправки аксессуаров
        await send_accessories_message(context.chat_id)
        # Переводим в завершенное состояние
        context.change_state(DialogState.COMPLETED)
        return None  # <-- ИЗМЕНЕНО: return "" заменено на return None

    # Сохраняем ответ ассистента в контекст
    if result.final_output is not None:
        context.add_message(role="assistant", text=result.final_output)
    return result.final_output

async def send_accessories_message(chat_id: str):
    """Отправляет сообщение с дополнительными товарами после покупки"""
    from main import send_message

    message = """🌿 Отлично! Ваш заказ оформлен! 

А чтобы ваше растение чувствовало себя максимально комфортно, предлагаю полезные аксессуары:

🚿 **Лейки и опрыскиватели**
https://tropichouse.ru/catalog/aksessuary/leyki_i_opryskivateli/

🌱 **Удобрения** 
https://tropichouse.ru/catalog/udobreniya/udobreniya_1/

💡 **Фитолампы**
https://tropichouse.ru/catalog/aksessuary/fitolampy/

🔧 **Приборы для ухода**
https://tropichouse.ru/catalog/aksessuary/pribory_dlya_rasteniy/

Нужна консультация по аксессуарам? Обращайтесь! 😊"""

    await send_message(chat_id, message)
