import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RETAIL_CRM = os.getenv("RETAIL_CRM")
MOY_SKLAD = os.getenv("MOY_SKLAD")
RETAIL_CRM_BOT_TOKEN = os.getenv("RETAIL_CRM_BOT_TOKEN")

API_URL = "https://mg-s1.retailcrm.pro/api/bot/v1"
RETAILCRM_BASE_URL = "https://tropichouse.retailcrm.ru"

# MG API конфигурация
MG_URL = "https://mg-s1.retailcrm.pro/api/bot/v1"
MG_TOKEN = RETAIL_CRM_BOT_TOKEN
MG_HEADERS = MG_TOKEN

# Группы менеджеров
MANAGER_B2B = {"symbol": "manager b2b", "id": 71, "group": "b2b"}
MANAGER_B2C = {"symbol": "manager", "id": 2, "group": "b2c"}
