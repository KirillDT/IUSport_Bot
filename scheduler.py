import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import config
from database import get_db, UserSession, SpotTracker, BookingPlan, NotificationSettings, ReminderLog
from security import load_credentials

logger = logging.getLogger("scheduler")
TZ = ZoneInfo(config.TIMEZONE)
scheduler = AsyncIOScheduler(timezone=TZ)


def _cookies(user):
    sessionid, csrftoken = load_credentials(user)
    return {"sessionid": sessionid, "csrftoken": csrftoken}


def _headers(user):
    _, csrftoken = load_credentials(user)
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrftoken or "",
        "Origin": config.SPORT_API_URL.rstrip("/"),
        "Referer": config.SPORT_API_URL.rstrip("/") + "/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }


def _response_payload(response):
    try:
        return response.json()
    except Exception:
        return None


def _response_detail(response):
    payload = _response_payload(response)
    if isinstance(payload, dict):
        return payload.get("detail") or payload.get("message") or payload
    return response.text[:500]


def _bool_value(item, key):
    props = item.get("extendedProps") or {}
    value = item.get(key)
    if value is None:
        value = props.get(key)
    return value


def training_id_of(item):
    props = item.get("extendedProps") or {}
    return props.get("id") or item.get("id")


def training_status(item):
    checked = _bool_value(item, "checked_in")
    can = _bool_value(item, "can_check_in")
    spots = None
    props = item.get("extendedProps") or {}
    for key in ("free_spots", "available_spots", "spots_left", "remaining_places", "freePlaces"):
        if item.get(key) is not None:
            spots = item.get(key); break
        if props.get(key) is not None:
            spots = props.get(key); break
    if checked is True:
        return "booked"
    if spots == 0:
        return "full"
    if can is True:
        return "open"
    if can is False:
        return "closed"
    return "unknown"


async def _fetch_training(tg_id: int, training_id: int):
    db = get_db()
    try:
        user = db.query(UserSession).filter(UserSession.telegram_id == tg_id).first()
        if not user or not user.sessionid or not user.csrftoken:
            return None, "Сессия не настроена."
        now = datetime.now(TZ)
        url = f"{config.SPORT_API_URL.rstrip('/')}/api/calendar/trainings"
        params = {
            "start": now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S"),
            # Критический фикс: занятие может быть через 7 дней, поэтому +1 день было недостаточно.
            "end": (now + timedelta(days=21)).replace(hour=23, minute=59, second=59, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        async with httpx.AsyncClient(cookies=_cookies(user), timeout=7.0) as client:
            response = await client.get(url, params=params, headers=_headers(user))
        if response.status_code in (401, 403):
            return None, "Сессия истекла или сервер отклонил авторизацию."
        if response.status_code != 200:
            return None, f"Не удалось обновить занятие: HTTP {response.status_code}"
        items = response.json()
        for item in items:
            if str(training_id_of(item)) == str(training_id):
                return item, None
        return None, "Занятие больше не найдено в актуальном расписании."
    except Exception as exc:
        logger.exception("Ошибка обновления занятия %s для %s", training_id, tg_id)
        return None, f"Ошибка обновления расписания: {exc}"
    finally:
        db.close()


def _classify_error(status_code, payload):
    text = str(payload).lower()
    code = payload.get("code") if isinstance(payload, dict) else None
    if status_code in (401, 403):
        return "auth", "Сессия истекла или недействительна. Обнови данные через /start."
    if code == 2 or "cannot check in" in text:
        return "closed", "Запись ещё закрыта."
    if "full" in text or "no spots" in text or "no places" in text or "мест" in text:
        return "full", "Свободных мест нет."
    if "already" in text and ("check" in text or "enroll" in text or "book" in text):
        return "booked", "Ты уже записан."
    return "error", f"Сервер отклонил запись: {payload}"


async def send_checkin_request(tg_id: int, training_id: int):
    db = get_db()
    try:
        user = db.query(UserSession).filter(UserSession.telegram_id == tg_id).first()
        if not user or not user.sessionid or not user.csrftoken:
            return False, "auth", "Сессия не настроена. Обнови данные через /start."
        cookies = _cookies(user)
        headers = _headers(user)
    finally:
        db.close()

    url = f"{config.SPORT_API_URL.rstrip('/')}/api/training/{training_id}/check_in"
    deadline = asyncio.get_running_loop().time() + config.BOOKING_WAIT_SECONDS
    last_kind, last_message = "error", "Сервер не подтвердил запись."

    async with httpx.AsyncClient(timeout=7.0, cookies=cookies, follow_redirects=False) as client:
        while asyncio.get_running_loop().time() < deadline:
            if _is_booking_cancelled(tg_id, training_id):
                return False, "cancelled", "Автозапись отменена."

            item, state_error = await _fetch_training(tg_id, training_id)
            if item:
                state = training_status(item)
                if state == "booked":
                    return True, "booked", "Запись подтверждена."
                if state == "full":
                    return False, "full", "Свободных мест нет."
                if state == "closed":
                    last_kind, last_message = "closed", "Запись ещё закрыта."
                    await asyncio.sleep(config.BOOKING_POLL_INTERVAL_MS / 1000)
                    continue
            elif state_error:
                last_kind, last_message = "error", state_error
                if "сессия" in state_error.lower():
                    return False, "auth", state_error
                await asyncio.sleep(config.BOOKING_POLL_INTERVAL_MS / 1000)
                continue

            try:
                # Последняя проверка непосредственно перед POST.
                if _is_booking_cancelled(tg_id, training_id):
                    return False, "cancelled", "Автозапись отменена."
                response = await client.post(url, json={}, headers=headers)
                payload = _response_payload(response)
                logger.info("Запись user=%s training=%s HTTP=%s code=%s", tg_id, training_id, response.status_code, payload.get("code") if isinstance(payload, dict) else "-")
                if response.status_code in (200, 201, 302, 303):
                    await asyncio.sleep(0.2)
                    confirmed, confirm_error = await _fetch_training(tg_id, training_id)
                    if confirmed and training_status(confirmed) == "booked":
                        return True, "booked", "Запись подтверждена."
                    if payload is None or not isinstance(payload, dict) or not payload.get("detail"):
                        last_kind, last_message = "error", "Запрос принят, но сайт пока не подтвердил запись."
                    else:
                        last_kind, last_message = _classify_error(response.status_code, payload)[0:2]
                else:
                    last_kind, last_message = _classify_error(response.status_code, payload if payload is not None else response.text[:300])
                    if last_kind == "auth":
                        return False, last_kind, last_message
                    if last_kind == "booked":
                        return True, last_kind, last_message
            except httpx.HTTPError as exc:
                last_kind, last_message = "error", f"Ошибка сети: {exc}"
            await asyncio.sleep(config.BOOKING_RETRY_INTERVAL_MS / 1000)

    return False, last_kind, last_message


def _update_plan(tg_id, training_id, status, error=None):
    db = get_db()
    try:
        plan = db.query(BookingPlan).filter(BookingPlan.telegram_id == tg_id, BookingPlan.training_id == training_id).first()
        if plan:
            plan.status = status
            plan.last_error = error
            db.commit()
    finally:
        db.close()


def _is_booking_cancelled(chat_id: int, training_id: int) -> bool:
    """Отсутствующий или отменённый BookingPlan означает, что автозапись отменена."""
    db = get_db()
    try:
        plan = db.query(BookingPlan).filter(
            BookingPlan.telegram_id == chat_id,
            BookingPlan.training_id == training_id,
        ).first()
        return plan is None or plan.status == "cancelled"
    finally:
        db.close()


async def booking_job(chat_id: int, group_id: int, training_id: int, training_name: str, notify: bool = True):
    from bot import bot

    # Пользователь мог отменить задачу в момент, когда APScheduler уже начал job.
    if _is_booking_cancelled(chat_id, training_id):
        logger.info("Автозапись отменена до запуска: user=%s training=%s", chat_id, training_id)
        return

    _update_plan(chat_id, training_id, "trying", None)

    if notify:
        await bot.send_message(chat_id, f"⏳ Проверяю запись на «{training_name}»…")

    # Повторная проверка после отправки уведомления.
    if _is_booking_cancelled(chat_id, training_id):
        logger.info("Автозапись отменена во время подготовки: user=%s training=%s", chat_id, training_id)
        return

    success, kind, message = await send_checkin_request(chat_id, training_id)

    # Не позволяем отменённой пользователем задаче вернуть статус booked.
    if _is_booking_cancelled(chat_id, training_id):
        logger.info("Автозапись отменена во время запроса: user=%s training=%s", chat_id, training_id)
        return

    if success:
        _update_plan(chat_id, training_id, "booked", None)
        await bot.send_message(chat_id, f"✅ Ты записан на «{training_name}».\n{message}")
    else:
        if kind == "cancelled":
            return
        status = {"closed": "waiting", "full": "full", "auth": "auth_error"}.get(kind, "error")
        _update_plan(chat_id, training_id, status, message)
        if kind == "closed":
            # API может открыть запись с небольшой задержкой. Повторяем тихо, без спама,
            # но не продолжаем попытки после начала самой тренировки.
            db = get_db()
            try:
                plan = db.query(BookingPlan).filter(
                    BookingPlan.telegram_id == chat_id,
                    BookingPlan.training_id == training_id
                ).first()
                training_start = plan.training_start.replace(tzinfo=TZ) if plan and plan.training_start else None
            finally:
                db.close()

            if _is_booking_cancelled(chat_id, training_id):
                return

            if training_start and datetime.now(TZ) >= training_start:
                await bot.send_message(chat_id, f"❌ Запись на «{training_name}» не открылась до начала тренировки.")
                _update_plan(chat_id, training_id, "error", message)
                return

            retry_at = datetime.now(TZ) + timedelta(seconds=10)
            scheduler.add_job(
                booking_job,
                trigger="date",
                run_date=retry_at,
                args=[chat_id, group_id, training_id, training_name, False],
                id=f"booking_{chat_id}_{training_id}",
                replace_existing=True,
            )
            return

        extra = "\\n\\nОбнови данные через /start." if kind == "auth" else ""
        await bot.send_message(chat_id, f"❌ Не удалось записаться на «{training_name}».\\n\\n{message}{extra}")




def plan_booking(bot, chat_id, group_id, training_id, training_start_time, training_name):
    """Создаёт ровно одну автозапись на пользователя и занятие."""
    if training_start_time.tzinfo is None:
        training_start_time = training_start_time.replace(tzinfo=TZ)
    else:
        training_start_time = training_start_time.astimezone(TZ)
    open_time = training_start_time - timedelta(days=7)
    now = datetime.now(TZ)
    trigger_time = max(open_time, now + timedelta(seconds=1))

    db = get_db()
    try:
        plan = db.query(BookingPlan).filter(BookingPlan.telegram_id == chat_id, BookingPlan.training_id == training_id).first()
        if not plan:
            plan = BookingPlan(telegram_id=chat_id, training_id=training_id, training_name=training_name, training_start=training_start_time.replace(tzinfo=None), booking_open=open_time.replace(tzinfo=None), status="waiting")
            db.add(plan)
        else:
            # Повторное нажатие не создаёт вторую конкурирующую задачу.
            plan.training_name = training_name
            plan.training_start = training_start_time.replace(tzinfo=None)
            plan.booking_open = open_time.replace(tzinfo=None)
            if plan.status not in ("booked", "trying"):
                plan.status = "waiting"
                plan.last_error = None
        db.commit()
        # SQLAlchemy по умолчанию протухает атрибуты объекта после commit().
        # После закрытия Session обращение к plan.status вызывает
        # DetachedInstanceError. Сохраняем нужное значение до close().
        plan_status = plan.status if plan is not None else None
    finally:
        db.close()

    # Уже подтверждённую запись не перезаписываем новой задачей.
    if plan_status == "booked":
        return None, open_time
    scheduler.add_job(booking_job, trigger="date", run_date=trigger_time, args=[chat_id, group_id, training_id, training_name], id=f"booking_{chat_id}_{training_id}", replace_existing=True)
    return trigger_time, open_time


def restore_pending_bookings():
    """Восстанавливает запланированные записи после перезапуска процесса."""
    db = get_db()
    restored = 0
    try:
        plans = db.query(BookingPlan).filter(BookingPlan.status == "waiting").all()
        now = datetime.now(TZ)
        for plan in plans:
            run_at = plan.booking_open.replace(tzinfo=TZ) if plan.booking_open.tzinfo is None else plan.booking_open.astimezone(TZ)
            run_at = max(run_at, now + timedelta(seconds=1))
            scheduler.add_job(booking_job, trigger="date", run_date=run_at, args=[plan.telegram_id, None, plan.training_id, plan.training_name], id=f"booking_{plan.telegram_id}_{plan.training_id}", replace_existing=True)
            restored += 1
    finally:
        db.close()
    logger.info("Восстановлено запланированных записей: %s", restored)
    return restored


def cancel_booking(chat_id, training_id):
    """Отменяет запланированную автозапись и удаляет её из БД.

    Отсутствие BookingPlan трактуется booking_job как отмена, поэтому
    удаление записи безопасно даже если APScheduler уже начал выполнение job.
    """
    job_id = f"booking_{chat_id}_{training_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        # Job мог уже выполниться или быть удалён ранее.
        pass

    db = get_db()
    try:
        plan = db.query(BookingPlan).filter(
            BookingPlan.telegram_id == chat_id,
            BookingPlan.training_id == training_id,
        ).first()
        if plan:
            db.delete(plan)
            db.commit()
            logger.info("Автозапись удалена: user=%s training=%s", chat_id, training_id)
    finally:
        db.close()


def get_active_jobs():
    result = []
    for job in scheduler.get_jobs():
        if not job.id.startswith("booking_"):
            continue
        result.append({"job_id": job.id, "run_date": job.next_run_time, "training_name": job.args[3] if len(job.args) > 3 else "Тренировка", "chat_id": job.args[0], "training_id": job.args[2]})
    return result


def _delete_booking_plan(tg_id: int, training_id: int):
    """Удаляет локальный план после подтверждённой отмены записи на сайте."""
    db = get_db()
    try:
        plan = db.query(BookingPlan).filter(
            BookingPlan.telegram_id == tg_id,
            BookingPlan.training_id == training_id,
        ).first()
        if plan:
            db.delete(plan)
            db.commit()
            logger.info("Локальный BookingPlan удалён: user=%s training=%s", tg_id, training_id)
    finally:
        db.close()


async def cancel_live_enrollment(tg_id: int, training_id: int):
    """Отменяет запись и подтверждает результат по актуальному состоянию занятия."""
    db = get_db()
    try:
        user = db.query(UserSession).filter(UserSession.telegram_id == tg_id).first()
        if not user or not user.sessionid or not user.csrftoken:
            return False, "Сессия не настроена."
        cookies = _cookies(user)
        headers = _headers(user)
    finally:
        db.close()

    url = f"{config.SPORT_API_URL.rstrip('/')}/api/training/{training_id}/cancel_check_in"

    try:
        async with httpx.AsyncClient(
            timeout=7.0,
            cookies=cookies,
            follow_redirects=False,
        ) as client:
            response = await client.post(url, json={}, headers=headers)

        # 3xx может означать редирект на страницу авторизации, поэтому
        # одного HTTP-кода недостаточно. После отмены всегда перепроверяем
        # реальное состояние занятия через API календаря.
        if response.status_code not in (200, 201, 302, 303):
            return False, f"Сайт отклонил отмену: HTTP {response.status_code}."

        if response.status_code in (302, 303):
            location = response.headers.get("location", "").lower()
            if "login" in location or "auth" in location:
                return False, "Сессия истекла или сервер перенаправил запрос на авторизацию."

        # Состояние на сайте может обновиться не мгновенно.
        # Делаем несколько коротких проверок, прежде чем сообщить об успехе.
        for delay in (0.25, 0.75, 1.5):
            await asyncio.sleep(delay)
            item, error = await _fetch_training(tg_id, training_id)

            if item is not None:
                state = training_status(item)
                if state != "booked":
                    _delete_booking_plan(tg_id, training_id)
                    return True, "Запись отменена ."

            if error and "сессия" in error.lower():
                return False, error

        return False, "Запрос на отмену отправлен, но сайт не подтвердил отмену записи."

    except httpx.HTTPError as exc:
        logger.exception("Ошибка отмены записи user=%s training=%s", tg_id, training_id)
        return False, f"Ошибка сети: {exc}"




async def fetch_my_current_enrollments(tg_id: int):
    db = get_db()
    try:
        user = db.query(UserSession).filter(UserSession.telegram_id == tg_id).first()
        if not user or not user.sessionid or not user.csrftoken:
            return []
        now = datetime.now(TZ)
        url = f"{config.SPORT_API_URL.rstrip('/')}/api/calendar/trainings"
        params = {"start": now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S"), "end": (now + timedelta(days=21)).replace(hour=23, minute=59, second=59, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")}
        async with httpx.AsyncClient(cookies=_cookies(user), timeout=7.0) as client:
            response = await client.get(url, params=params, headers=_headers(user))
        if response.status_code != 200:
            return []
        result = []
        for item in response.json():
            if _bool_value(item, "checked_in") is True:
                tid = training_id_of(item)
                if tid is not None:
                    result.append({"id": int(tid), "title": item.get("title") or (_bool_value(item, "group_name") or "Тренировка"), "start": item.get("start", "")})
        return result
    except Exception:
        logger.exception("Ошибка получения записей %s", tg_id)
        return []
    finally:
        db.close()


async def keep_alive_session_job():
    from bot import bot
    db = get_db()
    try:
        users = db.query(UserSession).all()
        for user in users:
            if not user.sessionid or not user.csrftoken:
                continue
            now = datetime.now(TZ)
            url = f"{config.SPORT_API_URL.rstrip('/')}/api/calendar/trainings"
            params = {"start": now.strftime("%Y-%m-%dT%H:%M:%S"), "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")}
            try:
                async with httpx.AsyncClient(cookies=_cookies(user), timeout=7.0) as client:
                    response = await client.get(url, params=params, headers=_headers(user))
                if response.status_code == 200:
                    user.last_seen_at = datetime.now()
                    db.commit()
                elif response.status_code in (401, 403):
                    await bot.send_message(user.telegram_id, "⚠️ Сессия на сайте истекла. Обнови данные через /start.")
            except Exception as exc:
                logger.warning("Ошибка проверки сессии %s: %s", user.telegram_id, exc)
    finally:
        db.close()


async def check_training_reminders():
    from bot import bot
    db = get_db()
    try:
        settings = {x.telegram_id: x for x in db.query(NotificationSettings).all()}
        users = db.query(UserSession).all()
        for user in users:
            if not user.sessionid or not settings.get(user.telegram_id, NotificationSettings(telegram_id=user.telegram_id)).training_reminders:
                continue
            for item in await fetch_my_current_enrollments(user.telegram_id):
                dt = None
                try:
                    dt = datetime.fromisoformat(item["start"].replace("Z", "+00:00")).astimezone(TZ)
                except Exception:
                    continue
                diff = dt - datetime.now(TZ)
                if timedelta(minutes=50) <= diff <= timedelta(minutes=60):
                    tid = item.get("id")
                    if tid is None:
                        continue
                    existing = db.query(ReminderLog).filter(ReminderLog.telegram_id == user.telegram_id, ReminderLog.training_id == int(tid), ReminderLog.training_start == dt.replace(tzinfo=None)).first()
                    if existing:
                        continue
                    await bot.send_message(user.telegram_id, f"⏰ Напоминание\n\n🏃 {item['title']}\n🕐 Начало в {dt.strftime('%d.%m.%Y %H:%M')} — примерно через час.")
                    db.add(ReminderLog(telegram_id=user.telegram_id, training_id=int(tid), training_start=dt.replace(tzinfo=None)))
                    db.commit()
    finally:
        db.close()


async def monitor_free_spots_job():
    from bot import bot
    db = get_db()
    try:
        trackers = db.query(SpotTracker).all()
        for tracker in trackers:
            item, error = await _fetch_training(tracker.telegram_id, tracker.training_id)
            if not item:
                continue
            state = training_status(item)
            if state != "open":
                continue
            settings = db.query(NotificationSettings).filter(NotificationSettings.telegram_id == tracker.telegram_id).first()
            notify = settings is None or settings.free_spot_notifications
            auto_book = tracker.auto_book or (settings.auto_book_free_spot if settings else False)
            if auto_book:
                success, kind, message = await send_checkin_request(tracker.telegram_id, tracker.training_id)
                if success:
                    await bot.send_message(tracker.telegram_id, f"✅ Освободилось место — я автоматически записал тебя на «{tracker.training_name}».")
                    db.delete(tracker)
                    continue
                if kind == "auth":
                    if notify:
                        await bot.send_message(tracker.telegram_id, "⚠️ Не удалось автоматически записаться: сессия истекла. Обнови данные через /start.")
                    continue
                # При гонке за место сохраняем подписку и попробуем снова позже.
                if kind == "full":
                    continue
            if notify:
                await bot.send_message(tracker.telegram_id, f"🔔 Появилось место!\n\n🏃 {tracker.training_name}\n🕐 {tracker.training_time}\n\nМожно записаться прямо сейчас.")
                db.delete(tracker)
        db.commit()
    finally:
        db.close()
