import sys
from pathlib import Path
import uuid
import requests
import logging
from datetime import datetime

# Принудительно добавляем папку, где лежит app.py, в список поиска модулей Python
sys.path.append(str(Path(__file__).parent))
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
import db_manager as db

import os
import json

try:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# Инициализируем базу данных при запуске приложения
db.init_db()

@st.cache_data(ttl=3600)
def get_region_coordinates(region_name: str):
    """Возвращает (lat, lon) по названию региона или None."""
    geolocator = Nominatim(user_agent="agro_calculator_mvp")
    try:
        location = geolocator.geocode(region_name, timeout=10)
        if location:
            return (location.latitude, location.longitude)
    except (GeocoderTimedOut, GeocoderUnavailable):
        return None
    return None


@st.cache_data(ttl=1800)
def fetch_weather_forecast(lat: float, lon: float, days: int = 7):
    """Возвращает JSON-ответ Open-Meteo или None при ошибке."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "precipitation_sum,precipitation_probability_max,"
                 "temperature_2m_max,temperature_2m_min,wind_speed_10m_max",
        "timezone": "auto",
        "forecast_days": days,
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None

class PriceItem(BaseModel):
    category: str
    name: str
    price: float
    url: str = ""
    source: str = ""


class PricesResponse(BaseModel):
    items: list[PriceItem]


# Замените старую функцию на эту
# ============================================================
# Gemini: поиск цен. Требует: pip install -U google-genai pydantic
# Ключ берётся из .streamlit/secrets.toml (GEMINI_API_KEY)
# ============================================================
import os
import json

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


LOG_PATH = Path(__file__).parent / "gemini_debug.log"


def _log(msg: str) -> None:
    """Пишет строку в gemini_debug.log рядом с app.py. Не роняет приложение."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().isoformat()} =====\n{msg}\n")
    except Exception:
        pass


def _get_gemini_api_key() -> str | None:
    """Ключ из .streamlit/secrets.toml, иначе из переменной окружения."""
    try:
        if "GEMINI_API_KEY" in st.secrets:
            key = st.secrets["GEMINI_API_KEY"]
            if key:
                return str(key)
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY") or None

from datetime import timedelta

def _gemini_on_cooldown() -> bool:
    until = st.session_state.get("gemini_cooldown_until")
    return bool(until and datetime.now() < until)

def _set_gemini_cooldown(seconds: int = 120) -> None:
    st.session_state["gemini_cooldown_until"] = datetime.now() + timedelta(seconds=seconds)

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_prices_for_items(items: list[tuple[str, str]]) -> list[dict]:
    """
    Вход:  [(категория, название), ...]
    Выход: [{"cat": str, "name": str, "price": float, "url": str, "source": str}, ...]

    Бросает RuntimeError при ошибке сети/API — чтобы Streamlit не закэшировал пустоту.
    """
    _log(f"ВЫЗОВ, позиций: {len(items)}: {items}")

    if not items:
        _log("  -> пустой вход, выходим")
        return []

    if not GENAI_AVAILABLE:
        _log("  -> ОШИБКА: google-genai не установлен")
        raise RuntimeError(
            "Не установлены библиотеки. Выполните: pip install -U google-genai pydantic"
        )

    api_key = _get_gemini_api_key()
    _log(f"  -> ключ найден: {bool(api_key)} (первые 8 симв: {(api_key or '')[:8]!r})")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY не найден. Добавьте его в .streamlit/secrets.toml"
        )

    client = genai.Client(api_key=api_key)

    lines = "\n".join(f"- [{cat}] {name}" for cat, name in items)
    prompt = (
        "Найди в интернете актуальные цены и прямые ссылки на следующие товары "
        "для сада/огорода (Россия, рубли):\n"
        + lines
        + "\n\n"
        "Верни ответ СТРОГО в формате JSON-массива, без markdown-обёрток "
        "и без каких-либо пояснений. Каждый элемент массива должен иметь поля: "
        "category (строка), name (строка), price (число, рубли), "
        "url (строка, прямая ссылка на товар), source (строка, название магазина). "
        "Сохраняй category и name ровно как во входном списке (посимвольно). "
        "Если точную цену найти не удалось — поставь price: 0 и url: \"\". "
        "Ответ должен начинаться с [ и заканчиваться ]."
    )
    _log(f"  -> prompt:\n{prompt}")

    cfg = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.2,
        # ВАЖНО: не передаём response_schema / response_mime_type —
        # они несовместимы с google_search на стороне API.
    )

    try:
        resp = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=cfg,
        )
    except Exception as e:
        _log(f"  -> ИСКЛЮЧЕНИЕ от Gemini: {type(e).__name__}: {e}")
        raise RuntimeError(f"Gemini API вернул ошибку: {e}") from e

    raw = (resp.text or "").strip()
    _log(f"  -> сырой ответ ({len(raw)} символов):\n{raw[:3000]}")

    if not raw:
        raise RuntimeError("Gemini вернул пустой ответ (сработал safety-фильтр?)")

    # Снимаем markdown-обёртку, если она есть
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _log(f"  -> JSON не распарсился: {e}")
        raise RuntimeError(
            f"Не удалось распарсить JSON от Gemini: {e}. Ответ: {raw[:500]}"
        ) from e

    _log(f"  -> распарсено, тип: {type(data).__name__}")

    # Модель может вернуть массив или {"items": [...]}
    if isinstance(data, dict):
        items_list = data.get("items") or data.get("results") or []
    elif isinstance(data, list):
        items_list = data
    else:
        items_list = []

    result: list[dict] = []
    for item in items_list:
        if not isinstance(item, dict):
            continue
        if "category" not in item or "name" not in item:
            continue
        try:
            price = float(item.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        result.append({
            "cat":    str(item["category"]),
            "name":   str(item["name"]),
            "price":  price,
            "url":    str(item.get("url") or ""),
            "source": str(item.get("source") or ""),
        })

    _log(f"  -> итог: {len(result)} элементов: {result}")

    # Не кэшируем пустой результат — иначе следующие полчаса будет 0
    if not result:
        fetch_prices_for_items.clear()

    return result

# ГАРАНТИРОВАННАЯ ИНИЦИАЛИЗАЦИЯ (если сессия абсолютно чистая)
if "calc_area" not in st.session_state: st.session_state["calc_area"] = 100
if "calc_density" not in st.session_state: st.session_state["calc_density"] = 4
if "calc_yield" not in st.session_state: st.session_state["calc_yield"] = 2.5
if "calc_price" not in st.session_state: st.session_state["calc_price"] = 150
if "selected_crop" not in st.session_state: st.session_state["selected_crop"] = "Клубника"
if "custom_expenses" not in st.session_state:
    st.session_state.custom_expenses = []

def all_risk_ids() -> list[str]:
    ids = []
    for crop in CROPS.values():
        ids.extend(r["id"] for r in crop["risks"])
    ids.extend(r["id"] for r in COMMON_RISKS)
    return ids

# --- ПРОВЕРКА И ЗАГРУЗКА ДАННЫХ АВТОРИЗАЦИИ ИЗ КУКИ / СЕССИИ ---
if "user_registered" not in st.session_state:
    st.session_state.user_registered = False

# --- Справочник культур и их рисков ---
CROPS = {
    "Клубника": {
        "risks": [
            {
                "id": "strawberry_gray_mold",
                "name": "Серая гниль клубники",
                "crop_loss_percentage": 55,
                "desc": (
                    "Затяжные дожди и умеренная температура (+15...+20°C) при загущенных посадках. "
                    "Угроза загнивания ягод и потери до 80% урожая. "
                    "Чеклист: Удалить старые и нижние листья для проветривания; Замульчировать почву сухой соломой или хвоей; "
                    "Приостановить полив; Провести профилактическую обработку биофунгицидами (Фитоспорин-М, Алирин-Б); "
                    "Своевременно удалять и уничтожать пораженные ягоды."
                ),
                "recs": {
                    "Лекарства": ["Фитоспорин-М", "Алирин-Б"],
                    "Агротехника и обслуживание": ["Мульча соломенная", "Секатор садовый"],
                }
            },
            {
                "id": "strawberry_mite",
                "name": "Земляничный клещ",
                "crop_loss_percentage": 30,
                "desc": (
                    "Сухая и жаркая погода (выше +20°C) при высокой влажности внутри куста. "
                    "Вредитель деформирует молодые листья и угнетает кусты. "
                    "Чеклист: Осмотреть молодые неразвернувшиеся листья в центре куста; Удалить сильно поврежденные растения; "
                    "Провести вечернюю обработку биоакарицидами (Фитоверм, Клещевит); "
                    "Отрегулировать умеренный полив методом дождевания; Избегать избытка азотных удобрений."
                ),
                "recs": {
                    "Лекарства": ["Фитоверм", "Клещевит"],
                    "Агротехника и обслуживание": ["Мульча соломенная", "Секатор садовый"],
                    "Индикаторы": ["Лупа садовая 10x"],
                }
            },
        ]
    },
    "Морковь": {
        "risks": [
            {
                "id": "carrot_alternaria",
                "name": "Альтернариоз моркови (бурая пятнистость)",
                "crop_loss_percentage": 35,
                "desc": (
                    "Резкие перепады влажности, частые росы и жаркая погода (выше +25°C). "
                    "Вызывает отмирание ботвы и задержку роста корнеплодов. "
                    "Чеклист: Исключить вечерний полив дождеванием (поливать строго под корень утром); "
                    "Проредить всходы для лучшей циркуляции воздуха; Провести внекорневую подкормку калием для повышения иммунитета; "
                    "При первых признаках обработать посадки биофунгицидами (Бактофит) или разрешенными медьсодержащими препаратами."
                ),
                "recs": {
                    "Лекарства": ["Бактофит", "Медьсодержащий препарат"],
                    "Подкормки": ["Калийное удобрение (внекорневое)"],
                }
            },
            {
                "id": "carrot_fly",
                "name": "Морковная муха",
                "crop_loss_percentage": 45,
                "desc": (
                    "Стабильная теплая (+15...+22°C) и влажная погода в период вегетации. "
                    "Личинки повреждают корнеплоды, делая их горькими и непригодными для хранения. "
                    "Чеклист: Установить желтые клеевые ловушки для мониторинга лёта; Опудрить междурядья табачной пылью, золой или горчицей; "
                    "Укрыть грядки тонким белым спанбондом; Проводить прореживание только поздно вечером, чтобы не привлекать муху запахом; "
                    "Применить биоинсектициды в фазе активного лёта."
                ),
                "recs": {
                    "Индикаторы": ["Жёлтые клеевые ловушки"],
                    "Отрава": ["Биоинсектицид (например, Фитоверм)"],
                    "Агротехника и обслуживание": ["Спанбонд белый тонкий"],
                }
            },
        ]
    },
    "Картофель": {
        "risks": [
            {
                "id": "potato_late_blight",
                "name": "Фитофтороз картофеля",
                "crop_loss_percentage": 60,
                "desc": (
                    "Высокая влажность воздуха (более 75%), обильные росы, туманы и затяжные дожди при +15...+22°C. "
                    "Стремительно уничтожает ботву и заражает клубни. "
                    "Чеклист: Провести высокое окучивание кустов для защиты клубней; Оценить состояние нижних листьев; "
                    "Провести превентивное опрыскивание фунгицидами до затяжных дождей; "
                    "Приостановить внесение азота; Скосить и уничтожить ботву за 2 недели до выкопки при сильном заражении."
                ),
                "recs": {
                    "Лекарства": ["Системный фунгицид (Ридомил Голд)"],
                }
            },
            {
                "id": "potato_beetle",
                "crop_loss_percentage": 40,
                "name": "Колорадский жук",
                "desc": (
                    "Установившаяся солнечная погода с температурой выше +22°C. "
                    "Активизирует выход имаго, массовую яйцекладку и прожорливость личинок. "
                    "Чеклист: Проверить нижнюю сторону листьев на наличие оранжевых яйцекладок; "
                    "Организовать ручной сбор жуков и личинок на небольших участках; "
                    "Применить биологические препараты (Битоксибациллин, Фитоверм) по личинкам младших возрастов; "
                    "Проводить обработку строго утром или вечером, избегая палящего солнца."
                ),
                "recs": {
                    "Отрава": ["Битоксибациллин", "Фитоверм"],
                }
            },
        ]
    },
}

COMMON_RISKS = [
    {
        "id": "heavy_rain",
        "name": "Риск: сильные и затяжные дожди",
        "crop_loss_percentage": 30,
        "desc": (
            "Обильные осадки вызывают переувлажнение почвы, застой воды, вспышки грибковых болезней (фитофтороз, гнили) и полегание растений. "
            "Чеклист: Проверить и очистить дренажные канавы; Окучить корнеплоды и картофель для отвода воды от стеблей; "
            "Прекратить любые поливы; Провести защитную обработку системными фунгицидами или биопрепаратами за сутки до начала ливня; "
            "Рыхлить почву в междурядьях сразу после окончания дождей для восстановления аэрации."
        ),
        "recs": {
            "Агротехника и обслуживание": ["Дренажные канавы (лопата штыковая)"],
            "Лекарства": ["Системный фунгицид"],
        }
    },
    {
        "id": "common_drought",
        "name": "Общий риск: засуха / аномальная жара",
        "crop_loss_percentage": 35,
        "desc": (
            "Возможен дефицит влаги и взрывное размножение многих опасных вредителей. "
            "Чеклист: Исключить азотные подкормки; Внести калийно-фосфорное удобрение; Уложить толстый слой мульчи; "
            "Подготовить затеняющие сетки; Отрегулировать график полива; Провести мониторинг вредителей; "
            "Выбрать температурное окно для обработок; Заменить жесткую химию на биопрепараты; "
            "Добавить в раствор антистрессовые адаптогены."
        ),
        "recs": {
            "Подкормки": ["Калийно-фосфорное удобрение"],
            "Индикаторы": ["Термометр-гигрометр"],
            "Агротехника и обслуживание": ["Мульча", "Затеняющая сетка"],
        }
    },
    {
        "id": "common_waterlogging",
        "name": "Общий риск: переувлажнение и застой воды",
        "crop_loss_percentage": 50,
        "desc": (
            "Длительный избыток влаги в корневой зоне. Приводит к кислородному голоданию (асфиксии), загниванию корней и увяданию надземной части. "
            "Чеклист: Организовать экстренный отвод воды (прокопать водоотводные каналы); Ограничить механическую обработку мокрой земли; "
            "Проколоть вилами тяжелую почву в междурядьях для доступа воздуха; Провести внекорневую (по листу) подкормку фосфором и янтарной кислотой "
            "после спада воды для стимуляции восстановления корней."
        ),
        "recs": {
            "Агротехника и обслуживание": ["Вилы садовые"],
        }
    },
    {
        "id": "strong_wind",
        "name": "Общий риск: сильный ветер",
        "crop_loss_percentage": 20,
        "desc": (
            "Шквалистые порывы ветра и бури. Угрожают механическими повреждениями стеблей, срывом цветов/завязей, иссушением почвы и полеганием культур. "
            "Чеклист: Проверить надежность крепления опор, шпалер и подвязать высокорослые культуры (томаты, картофель); "
            "Надежно закрепить укрывные материалы, парники и затеняющие сетки; Провести глубокий полив накануне ветра для утяжеления почвы; "
            "Создать временные ветрозащитные кулисы из подручных материалов; Осмотреть посадки после шторма и обработать поврежденные растения антистрессовыми составами."
        ),
        "recs": {
            "Агротехника и обслуживание": ["Шпалера / опоры", "Ветрозащитная сетка"],
        }
    },
]

CATEGORIES = [
    "Лекарства",
    "Подкормки",
    "Отрава",
    "Индикаторы",
    "Агротехника и обслуживание",
]

# Базовые расходы, актуальные для любой культуры независимо от прогноза погоды.
# Ключ — категория, значение — список позиций.
DEFAULT_RECS: dict[str, list[str]] = {
    "Лекарства": [
        "Фитоспорин-М (базовый биофунгицид)",
    ],
    "Подкормки": [
        "Комплексное NPK-удобрение (весеннее)",
        "Органическое удобрение (компост/перегной)",
    ],
    "Отрава": [
        "Средство от сорняков (гербицид сплошного действия)",
    ],
    "Индикаторы": [
        "pH-метр почвенный",
        "Термометр-гигрометр",
    ],
    "Агротехника и обслуживание": [
        "Садовый инвентарь (лопата, тяпка, секатор)",
        "Расходники для опрыскивателя (распылитель, фильтры)",
        "Перчатки и средства индивидуальной защиты",
    ],
}

OTHER_REGION = "Другой регион"
REGION_OPTIONS = ["Приморский край", "Московская область", OTHER_REGION]

def _safe_max(values, default=0):
    """Безопасный max для списков из Open-Meteo, где могут быть None."""
    if not values:
        return default
    clean = [v for v in values if v is not None]
    return max(clean) if clean else default


def get_weather_risks(crop_name: str, region_name: str) -> list[dict]:
    """
    Возвращает список активных рисков.
    Для 'Другой регион' сетевые запросы не выполняются —
    сразу отдаём все риски культуры + все общие.
    """
    # Заглушка: без геокодинга и без прогноза
    if region_name == OTHER_REGION or not region_name:
        st.info(
            "🌍 Для «Другого региона» прогноз не запрашивается. "
            "Показаны все возможные риски для выбранной культуры."
        )
        return CROPS[crop_name]["risks"] + COMMON_RISKS

    coords = get_region_coordinates(region_name)
    if not coords:
        return _fallback_risks(crop_name, "Не удалось определить координаты региона.")

    weather = fetch_weather_forecast(*coords)
    if not weather or "daily" not in weather:
        return _fallback_risks(crop_name, "Сервис погоды временно недоступен.")

    daily = weather["daily"]

    max_rain_sum = _safe_max(daily.get("precipitation_sum"))
    max_rain_prob = _safe_max(daily.get("precipitation_probability_max"))
    max_temp = _safe_max(daily.get("temperature_2m_max"))
    max_wind = _safe_max(daily.get("wind_speed_10m_max"))

    # Все потенциальные риски для культуры + общие
    candidates = CROPS[crop_name]["risks"] + COMMON_RISKS
    active_ids = set()

    # --- Общие риски ---

    # Сильные и затяжные дожди
    if max_rain_sum >= 20 or max_rain_prob >= 70:
        active_ids.add("heavy_rain")

    # Переувлажнение и застой воды
    if max_rain_sum >= 15 or max_rain_prob >= 60:
        active_ids.add("common_waterlogging")

    # Засуха / аномальная жара
    if max_temp >= 30 and max_rain_sum < 1 and max_rain_prob < 30:
        active_ids.add("common_drought")

    # Сильный ветер
    if max_wind >= 15:
        active_ids.add("strong_wind")

    # --- Риски по культурам ---

    if crop_name == "Клубника":
        # Серая гниль: высокая влажность/дожди и не слишком жарко
        if (max_rain_sum >= 10 or max_rain_prob >= 60) and max_temp <= 25:
            active_ids.add("strawberry_gray_mold")

        # Земляничный клещ: сухая и жаркая погода
        if max_temp >= 25 and max_rain_sum < 2 and max_rain_prob < 40:
            active_ids.add("strawberry_mite")

    elif crop_name == "Морковь":
        # Альтернариоз: перепады влажности / дожди
        if max_rain_sum >= 8 or max_rain_prob >= 50:
            active_ids.add("carrot_alternaria")

        # Морковная муха: тепло и влажно
        if max_temp >= 18 and (max_rain_sum >= 5 or max_rain_prob >= 50):
            active_ids.add("carrot_fly")

    elif crop_name == "Картофель":
        # Фитофтороз: затяжные дожди / высокая вероятность осадков
        if max_rain_sum >= 15 or max_rain_prob >= 70:
            active_ids.add("potato_late_blight")

        # Колорадский жук: температура выше 22°C
        if max_temp >= 22:
            active_ids.add("potato_beetle")

    active = [r for r in candidates if r["id"] in active_ids]

    if not active:
        st.info(
            "🌤️ По прогнозу значимых погодных рисков "
            "для выбранной культуры не выявлено."
        )

    return active

def _fallback_risks(crop_name: str, reason: str) -> list[dict]:
    """Резервный вариант: показываем все риски с пометкой."""
    st.warning(f"⚠️ {reason} Показаны все возможные риски.")
    return CROPS[crop_name]["risks"] + COMMON_RISKS

def get_active_recommendations(active_risks: list[dict]) -> dict[str, list[str]]:
    """
    Сливает рекомендации активных рисков с DEFAULT_RECS.
    Возвращает {категория: [уникальные имена]}.
    DEFAULT_RECS гарантируют присутствие всех категорий независимо от погоды.
    """
    result: dict[str, list[str]] = {c: [] for c in CATEGORIES}

    # 1. Сначала — постоянные (базовые) позиции
    for cat, items in DEFAULT_RECS.items():
        bucket = result.setdefault(cat, [])
        for name in items:
            if name not in bucket:
                bucket.append(name)

    # 2. Затем — рекомендации активных погодных рисков (тоже без дублей)
    for r in active_risks:
        for cat, items in r.get("recs", {}).items():
            bucket = result.setdefault(cat, [])
            for name in items:
                if name not in bucket:
                    bucket.append(name)

    return result

# def get_mock_expenses():
#     """Заглушка: имитация спарсенных расходов из БД"""
#     return [
#         {"id": 1, "cat": "1. Лекарства", "name": "Фитоспорин", "price": 450, "url": "https://example.com"},
#         {"id": 2, "cat": "2. Подкормки", "name": "Азотное удобрение", "price": 890, "url": "https://example.com"},
#         {"id": 3, "cat": "3. Отрава", "name": "Актара", "price": 320, "url": "https://example.com"},
#         {"id": 4, "cat": "4. Индикаторы", "name": "Лакмусовые тест-полоски", "price": 150, "url": "https://example.com"},
#         {"id": 5, "cat": "5. Агротехника и обслуживание", "name": "Ремонт опрыскивателя", "price": 2500, "url": "https://example.com"},
#     ]



# --- 2. ИНТЕРФЕЙС ПОЛЬЗОВАТЕЛЯ (STREAMLIT) ---
st.set_page_config(page_title="АгроКалькулятор MVP", layout="wide")

# --- 1. ИНИЦИАЛИЗАЦИЯ И ХРАНЕНИЕ ДАННЫХ ПОЛЬЗОВАТЕЛЯ ---
# Инициализируем аккаунты (имитация базы данных в памяти)
if "registered_users" not in st.session_state:
    st.session_state.registered_users = {
        "admin": {"password": "admin", "region": "Приморский край"} # Демо-пользователь для проверки входа
    }
if "user_registered" not in st.session_state:
    st.session_state.user_registered = False

# ЖЕСТКОЕ СОХРАНЕНИЕ ДАННЫХ КАЛЬКУЛЯТОРА
# Регистрируем дефолтные значения инпутов в сессии, чтобы они НЕ сбрасывались при rerun()
if "calc_area" not in st.session_state: st.session_state.calc_area = 100
if "calc_density" not in st.session_state: st.session_state.calc_density = 4
if "calc_yield" not in st.session_state: st.session_state.calc_yield = 2.5
if "calc_price" not in st.session_state: st.session_state.calc_price = 150


# --- 2. МОДАЛЬНЫЕ ОКНА (ВХОД И РЕГИСТРАЦИЯ) ---

@st.dialog("🔑 Вход в систему")
def login_modal():
    """Окно для авторизации существующего пользователя через SQLite"""
    login_user = st.text_input("Логин:", key="login_username")
    login_pass = st.text_input("Пароль:", type="password", key="login_password")
    
    if st.button("🚪 Войти", use_container_width=True):
        # 1. Проверяем пользователя в реальной базе данных SQLite
        user_region = db.check_login(login_user, login_pass)
        
        if user_region is not None:
            # Превращаем кортеж из БД в строку, если нужно
            region_str = user_region[0] if isinstance(user_region, tuple) else user_region
            
            # 2. Авторизуем пользователя в сессии
            st.session_state.user_registered = True
            st.session_state.username = login_user
            st.session_state.user_region = region_str

            # --- 1. Основные параметры калькулятора ---
            saved_data = db.load_calc_data(login_user)
            if saved_data:
                st.session_state["calc_area"]   = float(saved_data["area"])
                st.session_state["calc_density"] = int(saved_data["density"])
                st.session_state["calc_yield"]   = float(saved_data["base_yield"])
                st.session_state["calc_price"]   = float(saved_data["market_price"])
                # восстановить отметки по каждому риску
                saved_risks = db.load_risk_checks(login_user)   # dict[risk_id, bool]
                # --- 3. Свои категории расходов ---
                saved_custom = db.load_custom_expenses(login_user)
                st.session_state.custom_expenses = [
                    {"uid": uuid.uuid4().hex, "cat": r["cat"], "price": float(r["price"])}
                    for r in saved_custom
                ]
                
                for rid in all_risk_ids():
                    st.session_state[f"risk_{rid}"] = saved_risks.get(rid, False)

                if saved_data.get("crop_name"):
                    st.session_state["selected_crop"] = saved_data["crop_name"]
                # миграция со старой схемы: medicine_price -> exp_1
                if saved_data.get("medicine_price") is not None:
                    st.session_state["exp_1"] = float(saved_data["medicine_price"])

            # --- 2. Переопределённые цены по категориям (перезаписывают exp_1 при наличии) ---
            

            st.success(f"Рады возвращению, {login_user}! Данные успешно восстановлены.")
            st.rerun()
        else:
            st.error("Неверный логин или пароль.")

@st.dialog("📝 Регистрация нового фермера")
def registration_modal():
    """Окно для создания нового аккаунта в SQLite базе данных"""
    reg_user = st.text_input("Придумайте логин:", placeholder="Ivan_Farmer", key="reg_username")
    reg_pass = st.text_input("Придумайте пароль:", type="password", key="reg_password")
    reg_region = st.selectbox("Ваш регион:", REGION_OPTIONS, key="reg_region")
    
    if st.button("🚀 Создать аккаунт", use_container_width=True):
        if reg_user and reg_pass:
            # 1. Пытаемся записать нового пользователя в реальную БД SQLite
            is_success = db.register_user(reg_user, reg_pass, reg_region)
            
            if is_success:
                # 2. Если запись прошла успешно, авторизуем его в текущей сессии
                st.session_state.user_registered = True
                st.session_state.username = reg_user
                st.session_state.user_region = reg_region
                
                # 3. Сохраняем стартовые данные калькулятора, которые он уже успел ввести на экране
                db.save_calc_data(
                    reg_user,
                    st.session_state.get("calc_area", 100),
                    st.session_state.get("calc_density", 4),
                    st.session_state.get("calc_yield", 2.5),
                    st.session_state.get("calc_price", 150),
                    st.session_state.get("exp_1", 450),           # ← было exp_med_val
                    st.session_state.get("risk_pests_check", False),
                    st.session_state.get("selected_crop", "Клубника"),  # ← новое
                    st.session_state.get("risk_checked", False)
                )
                
                st.success("Аккаунт успешно создан и привязан к базе данных!")
                st.rerun()
            else:
                # Если такой логин уже занят, db.register_user вернет False
                st.error("Пользователь с таким логином уже существует в системе.")
        else:
            st.error("Пожалуйста, заполните все поля.")


# --- 3. ИНТЕРФЕЙС И ВЫЗОВ ОКОН С КНОПОК ---

st.title("🌱 Интерактивный АгроКалькулятор Доходов")

# Плашка авторизации
if st.session_state.user_registered:
    st.info(f"🟢 Вы вошли как **{st.session_state.username}**. Регион: {st.session_state.user_region}")
else:
    col_text, col_login, col_reg = st.columns([3, 1, 1])
    with col_text:
        st.warning("⚠️ Зарегистрируйтесь или войдите, чтобы сохранить настройки вашей фермы.")
    with col_login:
        if st.button("🔑 Войти", use_container_width=True):
            login_modal()
    with col_reg:
        if st.button("📝 Регистрация", use_container_width=True):
            registration_modal()

st.write("---")

# Инициализация сессионного хранилища для кастомных цен и категорий (чтобы streamlit не сбрасывал их при перезагрузке)

# Разделение экрана на две колонки: Настройки и Результаты
col_inputs, col_results = st.columns([1, 1])

with col_inputs:
    st.header("📋 Ввод данных и Управление")

    selected_crop = st.selectbox(
        "Выберите агрокультуру:",
        options=list(CROPS.keys()),
        key="selected_crop",
    )
    st.caption(f"Выбрана культура: **{selected_crop}**")
    
    # Требование 1: Базовый расчет урожая
    st.subheader("1. Параметры участка")
    area = st.number_input("Площадь участка (кв. м)", min_value=1, value=100, step=10, key="calc_area")
    density = st.number_input("Плотность посадки (кустов на кв. м)", min_value=1, value=4, step=1, key="calc_density")
    base_yield_per_bush = st.number_input("Базовая урожайность с куста (кг)", min_value=0.1, value=2.5, step=0.5, key="calc_yield")
    market_price = st.number_input("Рыночная цена продажи за 1 кг (руб)", min_value=1, value=150, step=10, key="calc_price")

    total_bushes = area * density
    potential_yield = total_bushes * base_yield_per_bush

    # Требование 2: Чек-лист рисков и ручной ввод инцидентов
    st.subheader("2. Чек-лист погоды и вредителей")
    st.caption("Данные автоматически подгружаются на основе прогноза погоды")
    
    user_region = st.session_state.get("user_region")
    if not user_region:
        st.info("🌍 Укажите регион, чтобы увидеть прогноз и риски для вашей местности.")
        user_region = st.selectbox(
            "Регион для прогноза:",
            REGION_OPTIONS,
            key="guest_region",
        )
    risks = get_weather_risks(selected_crop, user_region)

    # Доля сохранившегося урожая: перемножаем (1 - loss/100) по каждому
    # подтверждённому риску. При отсутствии рисков = 1.0
    survival_factor = 1.0

    for r in risks:
        st.warning(f"**{r['name']}**\n\n_{r['desc']}_")
        rid = r["id"]
        widget_key = f"risk_{rid}"
        if widget_key not in st.session_state:
            st.session_state[widget_key] = False

        # Берём процент потерь из словаря риска (fallback — 15)
        loss_pct = float(r.get("crop_loss_percentage", 15))

        if st.checkbox(
            f"🚨 Подтверждены потери: {r['name']} (−{loss_pct:.0f}% урожая)",
            key=widget_key,
        ):
            survival_factor *= (1.0 - loss_pct / 100.0)

    # Суммарная доля потерь (для отображения в дельте метрики)
    active_losses = (1.0 - survival_factor) * 100.0

    if st.session_state.user_registered:
        if st.button("🔄 Принудительно обновить цены",
                    help="Удалит позиции с ценой 0 и заново запросит их у Gemini"):
            n = db.reset_zero_price_items(st.session_state.username)
            st.cache_data.clear()
            st.success(f"Удалено {n} позиций без цены. Перезапрашиваю…")
            st.rerun()

    # Требование 3: Динамические расходы, парсинг и кастомные цены
    st.subheader("3. Управление расходами")
    
    # Возможность добавить свою категорию расходов
    new_cat_name = st.text_input(
        "Добавить свою категорию расходов:",
        placeholder="Например: Логистика",
        key="new_cat_name_input",
    )
    new_cat_price = st.number_input(
        "Стоимость для вашей категории (руб):",
        min_value=0.0,
        value=0.0,
        step=0.01,
        key="new_cat_price_input",
    )
    if st.button("➕ Добавить категорию"):
        if new_cat_name.strip():
            st.session_state.custom_expenses.append({
                "uid": uuid.uuid4().hex,
                "cat": new_cat_name.strip(),
                "price": float(new_cat_price),
            })
            if st.session_state.user_registered:
                db.replace_custom_expenses(
                    st.session_state.username,
                    [
                        {"cat": c["cat"], "price": c["price"]}
                        for c in st.session_state.custom_expenses
                    ],
                )
            st.rerun()

    # --- 4.1 Собрать актуальные рекомендации из активных рисков ---
    recommendations = get_active_recommendations(risks)

    if st.session_state.user_registered:
        username = st.session_state.username

        # --- 4.2 Определяем, что надо бы подтянуть через Gemini ---
        dismissed = db.load_dismissed_items(username)
        to_fetch: list[tuple[str, str]] = []
        for cat, names in recommendations.items():
            known = db.known_item_names(username, cat)
            for name in names:
                if name in known:
                    continue
                if (cat, name) in dismissed:
                    continue
                to_fetch.append((cat, name))

        # --- 4.2.1 Пробуем Gemini, но не спамим ---
        if to_fetch and not _gemini_on_cooldown():
            with st.spinner("🔎 Ищу актуальные цены…"):
                try:
                    fetched = fetch_prices_for_items(to_fetch)
                except RuntimeError as e:
                    msg = str(e)
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        _set_gemini_cooldown(120)
                        st.warning(
                            "⏳ Лимит Gemini API временно исчерпан. "
                            "Рекомендации показаны без цен — заполните вручную "
                            "(повторный автозапрос через 2 минуты)."
                        )
                    else:
                        st.warning(msg)
                    fetched = []
            if fetched:
                db.add_expense_items(username, fetched)
                st.rerun()
        elif to_fetch and _gemini_on_cooldown():
            st.info(
                "⏳ Gemini на паузе после недавней ошибки. "
                "Рекомендации показаны без цен — заполните вручную."
            )

        # --- 4.3 Сливаем рекомендации + БД в один список ---
        db_items = db.load_expense_items(username)
        db_index = {(i["cat"], i["name"]): i for i in db_items}

        all_items: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for cat, names in recommendations.items():
            for name in names:
                key = (cat, name)
                if key in dismissed:
                    continue
                if key in db_index:
                    all_items.append(db_index[key])
                else:
                    # виртуальная позиция — покажем без цены, сохраним при вводе
                    all_items.append({
                        "id": None, "cat": cat, "name": name,
                        "price": 0.0, "url": "", "source": "",
                    })
                seen.add(key)
        for it in db_items:
            if (it["cat"], it["name"]) not in seen:
                all_items.append(it)
    else:
        all_items = []
        for cat, names in recommendations.items():
            for name in names:
                all_items.append({
                    "id": None, "cat": cat, "name": name,
                    "price": 0.0, "url": "", "source": "",
                })

    # --- 4.4 Группировка по категориям ---
    grouped: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for it in all_items:
        grouped.setdefault(it["cat"], []).append(it)

    total_expenses = 0.0
    items_to_delete: list[dict] = []      # храним сам item — нужны cat и name

    for cat in CATEGORIES:
        items = grouped.get(cat, [])
        if not items:
            continue
        cat_sum = sum(float(i.get("price") or 0) for i in items)
        header = f"**{cat}** — {len(items)} поз., ≈ {cat_sum:,.0f} руб."
        with st.expander(header, expanded=False):
            virtual_to_save: list[dict] = []   # объявите ДО цикла по CATEGORIES

            for it in items:
                is_virtual = it.get("id") is None
                key_id = it["id"] if not is_virtual else f"{it['cat']}::{it['name']}"

                c1, c2, c3, c4 = st.columns([2.8, 1.7, 1.1, 0.5])
                with c1:
                    st.write(it["name"])

                with c2:
                    wkey_url = f"item_url_{key_id}"
                    if wkey_url not in st.session_state:
                        st.session_state[wkey_url] = it.get("url") or ""
                    new_url = st.text_input(
                        "Ссылка",
                        value=st.session_state[wkey_url],
                        key=wkey_url,
                        placeholder="Вставьте https://…",
                        label_visibility="collapsed",
                    )
                    if (not is_virtual and st.session_state.user_registered
                            and new_url.strip() != (it.get("url") or "")):
                        db.update_expense_item_url(username, it["id"], new_url.strip())

                with c3:
                    wkey = f"item_price_{key_id}"
                    if wkey not in st.session_state:
                        st.session_state[wkey] = float(it.get("price") or 0.0)
                    val = st.number_input(
                        "Цена, ₽", min_value=0.0, step=1.0,
                        key=wkey, label_visibility="collapsed",
                    )
                    total_expenses += val
                    if (not is_virtual and st.session_state.user_registered
                            and abs(val - float(it.get("price") or 0.0)) > 0.01):
                        db.update_expense_item_price(username, it["id"], val)

                with c4:
                    if not is_virtual and st.session_state.user_registered:
                        if st.button("🗑️", key=f"del_item_{it['id']}"):
                            items_to_delete.append(it)

                # виртуальная рекомендация: как только заполнена цена или URL —
                # сохранить в БД (станет «реальной» при следующем rerun)
                if (st.session_state.user_registered and is_virtual
                        and (val > 0 or new_url.strip())):
                    virtual_to_save.append({
                        "cat": it["cat"], "name": it["name"],
                        "price": float(val),
                        "url": new_url.strip(),
                        "source": it.get("source") or "",
                    })

            # ручное добавление позиции в эту категорию
            if st.session_state.user_registered:
                new_name = st.text_input(
                    "➕ Добавить позицию в категорию",
                    key=f"add_name_{cat}",
                    placeholder="Название",
                )
                if st.button("Добавить", key=f"add_btn_{cat}"):
                    if new_name.strip():
                        db.add_expense_items(username, [
                            {"cat": cat, "name": new_name.strip(),
                             "price": 0.0, "url": "", "source": ""}
                        ])
                        st.rerun()

    # --- 4.5 Свои категории расходов (ВНЕ цикла по CATEGORIES!) ---
    if st.session_state.custom_expenses:
        st.markdown("**Дополнительные категории (ваши):**")
        for ce in list(st.session_state.custom_expenses):
            c1, c2, c3 = st.columns([3, 1.5, 0.5])
            with c1:
                st.write(ce["cat"])
            with c2:
                new_price = st.number_input(
                    "₽",
                    min_value=0.0,
                    value=float(ce["price"]),
                    step=1.0,
                    key=f"custom_price_{ce['uid']}",
                    label_visibility="collapsed",
                )
                if abs(new_price - float(ce["price"])) > 0.01:
                    ce["price"] = float(new_price)
                    if st.session_state.user_registered:
                        db.replace_custom_expenses(
                            st.session_state.username,
                            [
                                {"cat": c["cat"], "price": c["price"]}
                                for c in st.session_state.custom_expenses
                            ],
                        )
            with c3:
                if st.button("🗑️", key=f"del_custom_{ce['uid']}", help="Удалить категорию"):
                    st.session_state.custom_expenses.remove(ce)
                    if st.session_state.user_registered:
                        db.replace_custom_expenses(
                            st.session_state.username,
                            [
                                {"cat": c["cat"], "price": c["price"]}
                                for c in st.session_state.custom_expenses
                            ],
                        )
                    st.rerun()

        # учитываем в общей сумме ОДИН раз — и теперь снаружи цикла
        total_expenses += sum(float(c["price"]) for c in st.session_state.custom_expenses)

    # --- 4.6 Удаление после цикла ---
    need_rerun = False
    if items_to_delete:
        for it_del in items_to_delete:
            db.dismiss_expense_item(username, it_del["cat"], it_del["name"])
        need_rerun = True
    if virtual_to_save:
        db.add_expense_items(username, virtual_to_save)
        need_rerun = True
    if need_rerun:
        st.rerun()

with col_results:
    st.header("📊 Экономика и Расчеты (Мгновенно)")
    
    # Перерасчет урожая с учетом подтвержденных потерь из чек-листа
    final_yield = potential_yield * survival_factor
    final_yield = max(0.0, final_yield)
    
    expected_revenue = final_yield * market_price
    expected_profit = expected_revenue - total_expenses
    
    # Визуальные карточки с метриками
    st.metric(label="Всего кустов на участке", value=f"{total_bushes} шт.")
    
    if active_losses > 0:
        st.metric(
            label="Прогноз урожая на продажу",
            value=f"{final_yield:,.1f} кг",
            delta=f"-{active_losses:.1f}% из-за рисков",
        )
    else:
        st.metric(label="Прогноз урожая на продажу",
                  value=f"{final_yield:,.1f} кг")
        
    st.metric(label="Общие расходы (Парсинг + Ваши правки)", value=f"{total_expenses:,.0f} руб.")
    
    # Выделяем цветом итоговую прибыль
    if expected_profit >= 0:
        st.success(f"### Ожидаемая чистая прибыль: {expected_profit:,.0f} руб.")
    else:
        st.error(f"### Ожидаемый убыток: {expected_profit:,.0f} руб.")

    # Требование 4: Обратная связь с техподдержкой
    st.write("---")
    st.subheader("📬 Обратная связь по методике расчета")
    with st.form("support_form"):
        user_message = st.text_area("Что не так с расчетами или ценами?", placeholder="Например: Неверно рассчитана базовая цена Фитоспорина для Приморского края...")
        submit_button = st.form_submit_button("📎 Отправить обращение в поддержку")
        if submit_button:
            if user_message:
                # В будущем здесь бэкенд отправит вебхук в Telegram
                st.info("Заглушка бэкенда: Сообщение успешно перехвачено системой! На реальном сервере оно мгновенно улетит в Telegram-чат разработчика.")
            else:
                st.error("Введите текст обращения.")

# --- СОХРАНЕНИЕ ПАРАМЕТРОВ ФЕРМЫ И ОТМЕТОК РИСКОВ ---
if st.session_state.user_registered:
    st.write("---")
    if st.button("💾 Сохранить параметры фермы", use_container_width=True):
        username = st.session_state.username

        risk_states = {
            rid: bool(st.session_state.get(f"risk_{rid}", False))
            for rid in all_risk_ids()
        }

        db.save_calc_data(
            username,
            st.session_state.calc_area,
            st.session_state.calc_density,
            st.session_state.calc_yield,
            st.session_state.calc_price,
            st.session_state.get("exp_1", 0),
            int(any(risk_states.values())),
            st.session_state.get("selected_crop", "Клубника"),
        )
        db.save_risk_checks(username, risk_states)
        st.success("Параметры и отметки рисков сохранены.")