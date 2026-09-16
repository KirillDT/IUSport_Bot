import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx
from config import config
from database import get_db, UserSession
from security import load_credentials

logger = logging.getLogger("parser")
TZ = ZoneInfo(config.TIMEZONE)


def _cookies(user: UserSession):
    sessionid, csrftoken = load_credentials(user)
    return {"sessionid": sessionid, "csrftoken": csrftoken}


def _headers(user: UserSession):
    _, csrftoken = load_credentials(user)
    return {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrftoken or "",
        "Origin": config.SPORT_API_URL.rstrip("/"),
        "Referer": config.SPORT_API_URL.rstrip("/") + "/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }


def parse_api_datetime(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


async def fetch_live_trainings(tg_id: int):
    db = get_db()
    try:
        user = db.query(UserSession).filter(UserSession.telegram_id == tg_id).first()
        if not user or not user.sessionid or not user.csrftoken:
            logger.warning("Пользователь %s не авторизован", tg_id)
            return []
        now = datetime.now(TZ)
        url = f"{config.SPORT_API_URL.rstrip('/')}/api/calendar/trainings"
        params = {
            "start": now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S"),
            "end": (now + timedelta(days=21)).replace(hour=23, minute=59, second=59, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        async with httpx.AsyncClient(cookies=_cookies(user), timeout=7.0) as client:
            response = await client.get(url, params=params, headers=_headers(user))
        if response.status_code != 200 or "application/json" not in response.headers.get("content-type", ""):
            logger.warning("Расписание: status=%s body=%s", response.status_code, response.text[:200])
            return []
        return response.json()
    except Exception as exc:
        logger.exception("Ошибка получения расписания для %s: %s", tg_id, exc)
        return []
    finally:
        db.close()

async def fetch_training_details(tg_id: int, training_id: int):
    """Получает точную карточку занятия через /api/training/{id}."""
    db = get_db()
    try:
        user = db.query(UserSession).filter(UserSession.telegram_id == tg_id).first()
        if not user or not user.sessionid or not user.csrftoken:
            return None, "Сессия не настроена."
        url = f"{config.SPORT_API_URL.rstrip('/')}/api/training/{training_id}"
        async with httpx.AsyncClient(cookies=_cookies(user), timeout=7.0) as client:
            response = await client.get(url, headers=_headers(user))
        if response.status_code in (401, 403):
            return None, "Сессия истекла или недействительна."
        if response.status_code != 200:
            return None, f"Не удалось получить карточку занятия: HTTP {response.status_code}."
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("training"), dict):
            return None, "Сервер вернул неожиданный формат карточки."
        return payload, None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Ошибка карточки занятия %s для %s: %s", training_id, tg_id, exc)
        return None, "Не удалось получить актуальные данные занятия."
    finally:
        db.close()
