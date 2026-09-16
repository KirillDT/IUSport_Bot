import asyncio
import logging
import sys
from html import escape
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

from config import config
from database import AllowedUser, BookingPlan, NotificationSettings, ReminderLog, SpotTracker, UserSession, get_db
from parser import fetch_live_trainings, fetch_training_details, parse_api_datetime
from scheduler import cancel_booking, cancel_live_enrollment, check_training_reminders, fetch_my_current_enrollments, get_active_jobs, keep_alive_session_job, monitor_free_spots_job, plan_booking, restore_pending_bookings, scheduler, training_id_of, training_status
from security import save_credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S", handlers=[RotatingFileHandler("bot.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
TZ = ZoneInfo(config.TIMEZONE)


def normalize_username(value: str) -> str:
    value = (value or "").strip().lower()
    for prefix in ("https://t.me/", "http://t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.lstrip("@").split("/", 1)[0]


def is_owner(user_id: int) -> bool:
    return user_id == config.OWNER_TELEGRAM_ID


def get_role(user_id: int):
    if is_owner(user_id):
        return "owner"
    db = get_db()
    try:
        row = db.query(AllowedUser).filter(AllowedUser.telegram_id == user_id).first()
        return row.role if row else None
    finally:
        db.close()


def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or get_role(user_id) == "admin"


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)
        if is_owner(user.id) or is_admin(user.id):
            return await handler(event, data)
        db = get_db()
        try:
            username = normalize_username(user.username or "")
            row = db.query(AllowedUser).filter((AllowedUser.telegram_id == user.id) | ((AllowedUser.username == username) if username else False)).first()
            if row:
                row.telegram_id = user.id
                row.username = username or row.username
                row.last_seen_at = datetime.now()
                db.commit()
                return await handler(event, data)
        finally:
            db.close()
        if isinstance(event, Message):
            await event.answer("🔒 Доступ ограничен.\n\nОбратись к владельцу или администратору бота, чтобы получить доступ.")
        elif isinstance(event, CallbackQuery):
            await event.answer("🔒 Доступ не предоставлен.", show_alert=True)


dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())


def main_menu(user_id: int):
    rows = [
        [KeyboardButton(text="📅 Расписание"), KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="⚙️ Настройки")],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="🛠 Управление")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def simple_nav(*rows):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=x) for x in row] for row in rows], resize_keyboard=True)


def pair_inline_buttons(items):
    """Размещает inline-кнопки по две в строке."""
    rows = []
    for i in range(0, len(items), 2):
        rows.append(items[i:i + 2])
    return rows


def settings_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Уведомления", callback_data="settings:notifications")],
        [InlineKeyboardButton(text="🔐 Авторизация", callback_data="settings:auth")],
        [InlineKeyboardButton(text="🕐 Часовой пояс", callback_data="settings:timezone")],
    ])


def management_menu(user_id: int):
    rows = []
    if is_owner(user_id):
        rows.append([KeyboardButton(text="👤 Администраторы")])
    rows += [[KeyboardButton(text="➕ Выдать доступ"), KeyboardButton(text="➖ Забрать доступ")], [KeyboardButton(text="🔎 Найти пользователя"), KeyboardButton(text="📊 Пользователи")], [KeyboardButton(text="📢 Рассылка")], [KeyboardButton(text="🏠 Главное меню")]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_menu():
    return simple_nav(("❌ Отмена",))


class CookieStates(StatesGroup):
    waiting_for_sessionid = State()
    waiting_for_csrftoken = State()


class AdminStates(StatesGroup):
    waiting_for_grant = State()
    waiting_for_revoke = State()
    waiting_for_add_admin = State()
    waiting_for_remove_admin = State()
    waiting_for_search = State()
    waiting_for_broadcast = State()


class UIStates(StatesGroup):
    current_sport = State()


def fmt_dt(value):
    dt = value if isinstance(value, datetime) else parse_api_datetime(value)
    if not dt:
        return "время неизвестно"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def capacity_text(item):
    props = item.get("extendedProps") or {}
    free = None
    total = None
    for k in ("free_spots", "available_spots", "spots_left", "remaining_places", "freePlaces"):
        if item.get(k) is not None:
            free = item[k]; break
        if props.get(k) is not None:
            free = props[k]; break
    for k in ("capacity", "max_places", "total_places", "places", "limit"):
        if item.get(k) is not None:
            total = item[k]; break
        if props.get(k) is not None:
            total = props[k]; break
    if free is not None and total is not None:
        return f"👥 Места: {free}/{total} свободно"
    if free is not None:
        return f"👥 Свободных мест: {free}"
    status = training_status(item)
    return {"open": "🟢 Запись открыта", "full": "🔴 Мест нет", "booked": "✅ Ты уже записан", "closed": "🕐 Запись ещё закрыта"}.get(status, "ℹ️ Статус уточняется")


def training_card(item, include_actions=True):
    props = item.get("extendedProps") or {}
    name = item.get("title") or props.get("group_name") or "Тренировка"
    safe_name = escape(str(name))
    dt = parse_api_datetime(item.get("start", ""))
    text = f"🏃 <b>{safe_name}</b>\n📅 {fmt_dt(dt)}"
    place = item.get("place") or props.get("place")
    if place:
        text += f"\n📍 {escape(str(place))}"
    text += f"\n{capacity_text(item)}"
    teachers = item.get("teachers") or props.get("teachers")
    if teachers:
        names = []
        for teacher in teachers:
            if isinstance(teacher, dict):
                full = " ".join(str(x) for x in (teacher.get("first_name"), teacher.get("last_name")) if x)
                if full: names.append(full)
        if names:
            text += f"\n👨‍🏫 {escape(', '.join(names))}"
    status = training_status(item)
    if status == "closed" and dt:
        open_time = dt - timedelta(days=7)
        text += f"\n🔓 Открытие записи: {fmt_dt(open_time)}"
    return text


def merge_training_detail(calendar_item, payload):
    """Добавляет в календарную запись поля из точного training endpoint."""
    training = (payload or {}).get("training") or {}
    group = training.get("group") or {}
    sport = group.get("sport") or {}
    merged = dict(calendar_item or {})
    props = dict(merged.get("extendedProps") or {})
    if training.get("id") is not None: merged["id"] = training["id"]
    for key in ("start", "end", "place"):
        if training.get(key) is not None: merged[key] = training[key]
    if group.get("name") and not merged.get("title"): merged["title"] = group["name"]
    if group.get("capacity") is not None: merged["capacity"] = group["capacity"]
    if group.get("teachers"): merged["teachers"] = group["teachers"]
    if group.get("id") is not None: props["group_id"] = group["id"]
    if group.get("name"): props["group_name"] = group["name"]
    if payload.get("can_check_in") is not None: merged["can_check_in"] = payload["can_check_in"]
    if payload.get("checked_in") is not None: merged["checked_in"] = payload["checked_in"]
    merged["extendedProps"] = props
    return merged


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    db = get_db()
    try:
        row = db.query(UserSession).filter(UserSession.telegram_id == message.from_user.id).first()
        if not row or not row.sessionid or not row.csrftoken:
            await message.answer("Привет! 👋\n\nДля работы нужна авторизация на сайте спорта. Отправь значение cookie <code>sessionid</code>.", parse_mode="HTML", reply_markup=cancel_menu())
            await state.set_state(CookieStates.waiting_for_sessionid)
            return
        row.last_seen_at = datetime.now()
        db.commit()
    finally:
        db.close()
    await message.answer("👋 Добро пожаловать!\n\nВыбери действие в меню.", reply_markup=main_menu(message.from_user.id))


@dp.message(CookieStates.waiting_for_sessionid)
async def process_sessionid(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))
        return
    if not message.text: return
    await state.update_data(sessionid=message.text.strip())
    await message.answer("✅ Получено. Теперь отправь cookie <code>csrftoken</code>.", parse_mode="HTML")
    await state.set_state(CookieStates.waiting_for_csrftoken)


@dp.message(CookieStates.waiting_for_csrftoken)
async def process_csrftoken(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))
        return
    if not message.text: return
    data = await state.get_data()
    sessionid = data.get("sessionid")
    csrftoken = message.text.strip()
    if not sessionid:
        await state.clear(); await message.answer("Не удалось сохранить данные. Запусти /start ещё раз."); return
    db = get_db()
    try:
        row = db.query(UserSession).filter(UserSession.telegram_id == message.from_user.id).first()
        if not row: row = UserSession(telegram_id=message.from_user.id)
        row.username = normalize_username(message.from_user.username or "") or "student"
        save_credentials(row, sessionid, csrftoken)
        row.last_seen_at = datetime.now()
        db.add(row)
        settings = db.query(NotificationSettings).filter(NotificationSettings.telegram_id == message.from_user.id).first()
        if not settings: db.add(NotificationSettings(telegram_id=message.from_user.id, timezone=config.TIMEZONE))
        db.commit()
    finally:
        db.close()
    await state.clear()
    await message.answer("✅ Авторизация сохранена безопасно.\n\nТеперь можно пользоваться расписанием.", reply_markup=main_menu(message.from_user.id))


@dp.message(F.text.in_({"🏠 Главное меню", "⬅️ Назад"}))
async def back_main(message: Message, state: FSMContext):
    await state.clear(); await message.answer("Главное меню:", reply_markup=main_menu(message.from_user.id))


async def show_sports(message_or_callback, user_id, edit=False):
    schedule = await fetch_live_trainings(user_id)
    if not schedule:
        text = "Не удалось загрузить расписание.\n\nПроверь авторизацию через /start."
        if edit: await message_or_callback.edit_text(text)
        else: await message_or_callback.answer(text)
        return
    sports = sorted({x.get("title") for x in schedule if x.get("title")})
    sport_buttons = [InlineKeyboardButton(text=s, callback_data=f"sport:{i}") for i, s in enumerate(sports)]
    buttons = pair_inline_buttons(sport_buttons)
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh:sports")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "📅 <b>Расписание</b>\n\nВыбери секцию:"
    if edit: await message_or_callback.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else: await message_or_callback.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.message(F.text.in_({"📅 Расписание", "📅 Записаться", "📅 Записаться на спорт"}))
async def choose_sport(message: Message, state: FSMContext):
    await state.clear(); await show_sports(message, message.from_user.id)


@dp.callback_query(F.data == "refresh:sports")
async def refresh_sports(callback: CallbackQuery, state: FSMContext):
    await state.clear(); await show_sports(callback.message, callback.from_user.id, edit=True); await callback.answer("Расписание обновлено")


@dp.callback_query(F.data.startswith("sport:"))
async def select_sport(callback: CallbackQuery, state: FSMContext):
    try: index = int(callback.data.split(":", 1)[1])
    except ValueError: await callback.answer("Открой расписание заново.", show_alert=True); return
    schedule = await fetch_live_trainings(callback.from_user.id)
    sports = sorted({x.get("title") for x in schedule if x.get("title")})
    if index >= len(sports): await callback.answer("Расписание обновилось. Открой его ещё раз.", show_alert=True); return
    sport = sports[index]
    await state.update_data(current_sport=sport)
    await render_dates(callback.message, callback.from_user.id, sport)
    await callback.answer()


async def render_dates(message, user_id, sport, edit=False):
    schedule = await fetch_live_trainings(user_id)
    dates = sorted({x.get("start", "")[:10] for x in schedule if x.get("title") == sport and x.get("start")})
    if not dates:
        text = f"🏃 <b>{sport}</b>\n\nДля этой секции занятий нет."
        if edit: await message.edit_text(text, parse_mode="HTML")
        else: await message.answer(text)
        return
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    date_buttons = []
    for d in dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        date_buttons.append(InlineKeyboardButton(text=f"📅 {dt.strftime('%d.%m')} ({days[dt.weekday()]})", callback_data=f"date:{d}"))
    buttons = pair_inline_buttons(date_buttons)
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh:dates"), InlineKeyboardButton(text="⬅️ Секции", callback_data="back:sports")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = f"🏃 <b>{sport}</b>\n\nВыбери дату:"
    if edit: await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else: await message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data == "refresh:dates")
async def refresh_dates(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data(); sport = data.get("current_sport")
    if not sport: await callback.answer("Открой расписание заново.", show_alert=True); return
    await render_dates(callback.message, callback.from_user.id, sport, edit=True); await callback.answer("Расписание обновлено")


@dp.callback_query(F.data == "back:sports")
async def back_sports_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear(); await show_sports(callback.message, callback.from_user.id, edit=True); await callback.answer()


@dp.callback_query(F.data.startswith("date:"))
async def select_date(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split(":", 1)[1]
    data = await state.get_data(); sport = data.get("current_sport")
    if not sport: await callback.answer("Открой расписание заново.", show_alert=True); return
    await state.update_data(current_date=date_str)
    await render_times(callback.message, callback.from_user.id, sport, date_str)
    await callback.answer()


async def render_times(message, user_id, sport, date_str, edit=False):
    schedule = await fetch_live_trainings(user_id)
    action_buttons = []
    target_items = []
    for item in schedule:
        if item.get("title") != sport or not item.get("start", "").startswith(date_str): continue
        tid = training_id_of(item)
        if tid is None: continue
        target_items.append(item)
    target_items.sort(key=lambda x: x.get("start", ""))
    for item in target_items:
        tid = training_id_of(item); dt = parse_api_datetime(item.get("start", "")); status = training_status(item)
        label = dt.strftime("%H:%M") if dt else "--:--"
        status_label = {"open": "Записаться", "closed": "Запланировать", "full": "Мест нет", "booked": "Уже записан"}.get(status, "Подробнее")
        action_buttons.append(InlineKeyboardButton(text=f"🕐 {label} · {status_label}", callback_data=f"book:{tid}"))
        action_buttons.append(InlineKeyboardButton(text=f"🔔 Место · {label}", callback_data=f"track:{tid}"))
    buttons = pair_inline_buttons(action_buttons)
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh:times:{date_str}"), InlineKeyboardButton(text="⬅️ Даты", callback_data="back:dates")])
    text = f"🏃 <b>{sport}</b>\n📅 {datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')}\n\nВыбери занятие:"
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit: await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else: await message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data.startswith("refresh:times:"))
async def refresh_times(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split(":", 2)[2]; data = await state.get_data(); sport = data.get("current_sport")
    if not sport: await callback.answer("Открой расписание заново.", show_alert=True); return
    await render_times(callback.message, callback.from_user.id, sport, date_str, edit=True); await callback.answer("Обновлено")


@dp.callback_query(F.data == "back:dates")
async def back_dates_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data(); sport = data.get("current_sport")
    if not sport: await show_sports(callback.message, callback.from_user.id, edit=True); return
    await render_dates(callback.message, callback.from_user.id, sport, edit=True); await callback.answer()


@dp.callback_query(F.data.startswith("book:"))
async def confirm_booking(callback: CallbackQuery):
    try: tid = int(callback.data.split(":", 1)[1])
    except ValueError: await callback.answer("Некорректное занятие.", show_alert=True); return
    schedule = await fetch_live_trainings(callback.from_user.id)
    target = next((x for x in schedule if str(training_id_of(x)) == str(tid)), None)
    if not target: await callback.answer("Занятие больше недоступно. Обнови расписание.", show_alert=True); return
    detail, _ = await fetch_training_details(callback.from_user.id, tid)
    if detail:
        target = merge_training_detail(target, detail)
    status = training_status(target)
    if status == "booked": await callback.answer("Ты уже записан.", show_alert=True); return
    name = target.get("title") or "Тренировка"
    dt = parse_api_datetime(target.get("start", ""))
    props = target.get("extendedProps") or {}
    text = training_card(target) + "\n\n<b>Подтвердить автоматическую запись?</b>\nБот будет ждать открытия записи и проверит результат."
    buttons = [[InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm:{tid}")], [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:times")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm:"))
async def do_confirm_booking(callback: CallbackQuery):
    try: tid = int(callback.data.split(":", 1)[1])
    except ValueError: await callback.answer("Некорректное занятие.", show_alert=True); return
    schedule = await fetch_live_trainings(callback.from_user.id)
    target = next((x for x in schedule if str(training_id_of(x)) == str(tid)), None)
    if not target: await callback.answer("Занятие исчезло из расписания.", show_alert=True); return
    detail, detail_error = await fetch_training_details(callback.from_user.id, tid)
    if detail:
        target = merge_training_detail(target, detail)
    dt = parse_api_datetime(target.get("start", "")); name = target.get("title") or "Тренировка"
    if not dt: await callback.answer("Не удалось определить время занятия.", show_alert=True); return
    props = target.get("extendedProps") or {}
    group_id = props.get("group_id")
    trigger, open_time = plan_booking(bot, callback.message.chat.id, group_id, tid, dt, name)
    if trigger is None:
        await callback.message.edit_text(training_card(target) + "\n\n✅ <b>Ты уже записан на это занятие.</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back:times")]]))
        await callback.answer("Ты уже записан")
        return
    await callback.message.edit_text(f"⏳ <b>Запись запланирована</b>\n\n🏃 {escape(str(name))}\n📅 Тренировка: {fmt_dt(dt)}\n🔓 Открытие записи: {fmt_dt(open_time)}\n\nЯ автоматически проверю доступность в момент открытия и сообщу результат.", parse_mode="HTML")
    await callback.answer("Запись запланирована")


@dp.callback_query(F.data == "back:times")
async def back_times_from_card(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data(); sport = data.get("current_sport"); date_str = data.get("current_date")
    if not sport: await show_sports(callback.message, callback.from_user.id, edit=True); return
    if date_str: await render_times(callback.message, callback.from_user.id, sport, date_str, edit=True)
    else: await render_dates(callback.message, callback.from_user.id, sport, edit=True)
    await callback.answer()


async def render_my_bookings(message: Message, tg_id: int, edit: bool = False):
    # Всегда перечитываем БД и расписание после действия пользователя.
    # Важно: не отправляем новое сообщение при нажатии на отмену —
    # редактируем то же самое окно, чтобы оставшиеся записи не исчезали.
    db = get_db()
    try:
        plans = db.query(BookingPlan).filter(
            BookingPlan.telegram_id == tg_id,
            BookingPlan.status.in_(["waiting", "trying", "auth_error", "full", "error"])
        ).order_by(BookingPlan.training_start.asc()).all()
        # Сохраняем данные до закрытия Session, чтобы SQLAlchemy не пытался
        # лениво обновить detached-объекты при формировании текста.
        plan_rows = [(p.training_id, p.training_name, p.training_start, p.status) for p in plans]
    finally:
        db.close()

    enrollments = await fetch_my_current_enrollments(tg_id)
    lines = ["📋 <b>Мои записи</b>", ""]
    buttons = []
    status_names = {
        "waiting": "🕐 ожидает открытия",
        "trying": "⏳ идёт попытка",
        "auth_error": "🔐 ошибка авторизации",
        "full": "🔴 мест нет",
        "error": "⚠️ ошибка",
    }

    for training_id, training_name, training_start, status in plan_rows:
        lines.append(
            f"• {training_name}\n  📅 {training_start.strftime('%d.%m.%Y %H:%M')} · {status_names.get(status, status)}"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отменить · {training_name[:22]}",
                callback_data=f"cancel_plan:{training_id}"
            )
        ])

    if enrollments:
        if len(lines) > 2:
            lines.append("")
        lines.append("<b>Записи на сайте</b>")
        for item in enrollments:
            name = item["title"]
            tid = item["id"]
            lines.append(f"• {name} · {fmt_dt(item.get('start'))}")
            buttons.append([
                InlineKeyboardButton(
                    text=f"🗑 Отменить · {name[:22]}",
                    callback_data=f"drop:{tid}"
                )
            ])

    if len(lines) == 2:
        lines.append("Пока нет активных записей.")

    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    if edit:
        try:
            await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception as exc:
            # Например, если Telegram сообщает, что текст уже не изменился.
            if "message is not modified" not in str(exc).lower():
                raise
    else:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.message(F.text == "📋 Мои записи")
async def my_bookings(message: Message):
    await render_my_bookings(message, message.from_user.id, edit=False)


@dp.callback_query(F.data.startswith("cancel_plan:"))
async def cancel_plan_callback(callback: CallbackQuery):
    tid = int(callback.data.split(":", 1)[1])
    cancel_booking(callback.from_user.id, tid)
    await callback.answer("Запись отменена.", show_alert=True)
    await render_my_bookings(callback.message, callback.from_user.id, edit=True)


@dp.callback_query(F.data.startswith("drop:"))
async def drop_booking(callback: CallbackQuery):
    tid = int(callback.data.split(":", 1)[1])
    success, error = await cancel_live_enrollment(callback.from_user.id, tid)
    await callback.answer("Запись отменена." if success else f"Ошибка: {error}", show_alert=True)
    # После отмены заново получаем ВСЕ оставшиеся записи и обновляем
    # текущее сообщение, а не заменяем его экраном с пустым состоянием.
    await render_my_bookings(callback.message, callback.from_user.id, edit=True)


@dp.message(F.text == "🔔 Уведомления")
async def notification_center(message: Message):
    await render_notification_center(message, edit=False)


async def render_notification_center(message, edit=False):
    db = get_db()
    try:
        settings = db.query(NotificationSettings).filter(NotificationSettings.telegram_id == message.chat.id).first()
        trackers = db.query(SpotTracker).filter(SpotTracker.telegram_id == message.chat.id).all()
        if not settings:
            settings = NotificationSettings(telegram_id=message.chat.id, timezone=config.TIMEZONE)
            db.add(settings); db.commit()
        reminder = "включены" if settings.training_reminders else "выключены"
        free = "включены" if settings.free_spot_notifications else "выключены"
        auto = "включена" if settings.auto_book_free_spot else "выключена"
        lines = ["🔔 <b>Уведомления</b>", "", f"⏰ Напоминания о тренировках: <b>{reminder}</b>", f"🪑 Уведомления о местах: <b>{free}</b>", f"🤖 Автозапись при освободившемся месте: <b>{auto}</b>", ""]
        if trackers:
            lines.append("Ожидаемые места:")
            lines += [f"• {escape(str(x.training_name or 'Тренировка'))} · {escape(str(x.training_time or 'время неизвестно'))}" for x in trackers]
        buttons = [
            [InlineKeyboardButton(text="⏰ Вкл/выкл напоминания", callback_data="setnotify:reminders")],
            [InlineKeyboardButton(text="🪑 Вкл/выкл уведомления о местах", callback_data="setnotify:spots")],
            [InlineKeyboardButton(text="🤖 Вкл/выкл автозапись при месте", callback_data="setnotify:auto")],
        ]
        if trackers:
            buttons.append([InlineKeyboardButton(text="🗑 Очистить уведомления о местах", callback_data="cleartrack")])
    finally:
        db.close()
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit:
        await message.edit_text("\n".join(lines), reply_markup=markup, parse_mode="HTML")
    else:
        await message.answer("\n".join(lines), reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data.startswith("setnotify:"))
async def toggle_notify(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    attr = {"reminders": "training_reminders", "spots": "free_spot_notifications", "auto": "auto_book_free_spot"}.get(key)
    if not attr:
        await callback.answer("Неизвестная настройка.", show_alert=True); return
    db = get_db()
    try:
        settings = db.query(NotificationSettings).filter(NotificationSettings.telegram_id == callback.from_user.id).first()
        if not settings:
            settings = NotificationSettings(telegram_id=callback.from_user.id, timezone=config.TIMEZONE); db.add(settings)
        setattr(settings, attr, not getattr(settings, attr)); db.commit()
    finally:
        db.close()
    await callback.answer("Настройка обновлена")
    await render_notification_center(callback.message, edit=True)


@dp.callback_query(F.data == "cleartrack")
async def clear_trackers(callback: CallbackQuery):
    db = get_db()
    try:
        db.query(SpotTracker).filter(SpotTracker.telegram_id == callback.from_user.id).delete(synchronize_session=False); db.commit()
    finally:
        db.close()
    await callback.answer("Уведомления очищены")
    await render_notification_center(callback.message, edit=True)


@dp.callback_query(F.data.startswith("track:"))
async def track_spot(callback: CallbackQuery):
    try:
        tid = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное занятие.", show_alert=True); return
    schedule = await fetch_live_trainings(callback.from_user.id)
    target = next((x for x in schedule if str(training_id_of(x)) == str(tid)), None)
    if not target:
        await callback.answer("Занятие больше недоступно. Обнови расписание.", show_alert=True); return
    name = target.get("title") or "Тренировка"; dt = parse_api_datetime(target.get("start", ""))
    db = get_db()
    try:
        row = db.query(SpotTracker).filter(SpotTracker.telegram_id == callback.from_user.id, SpotTracker.training_id == tid).first()
        if not row:
            db.add(SpotTracker(telegram_id=callback.from_user.id, training_id=tid, training_name=name, training_time=fmt_dt(dt), auto_book=False)); db.commit()
        else:
            row.training_name = name; row.training_time = fmt_dt(dt); db.commit()
    finally:
        db.close()
    await callback.answer("Уведомление о месте включено.", show_alert=True)


@dp.message(F.text == "⚙️ Настройки")
async def settings(message: Message):
    db = get_db()
    try:
        row = db.query(NotificationSettings).filter(NotificationSettings.telegram_id == message.from_user.id).first()
        timezone = row.timezone if row else config.TIMEZONE
    finally:
        db.close()
    text = ("⚙️ <b>Настройки</b>\n\n"
            "Здесь находятся общие параметры бота. Уведомления вынесены в отдельный раздел.\n\n"
            f"🕐 Часовой пояс: <b>{escape(timezone)}</b>\n"
            "🔐 Cookie хранятся в БД в зашифрованном виде.")
    await message.answer(text, reply_markup=settings_menu(), parse_mode="HTML")


@dp.callback_query(F.data == "settings:notifications")
async def settings_notifications(callback: CallbackQuery):
    await render_notification_center(callback.message, edit=True); await callback.answer()


@dp.callback_query(F.data == "settings:auth")
async def settings_auth(callback: CallbackQuery):
    await callback.message.edit_text("🔐 <b>Авторизация</b>\n\nДля обновления sessionid и csrftoken используй /start. Старые значения заменяются новыми и сохраняются зашифрованными.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки", callback_data="back:settings")]]), parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "settings:timezone")
async def settings_timezone(callback: CallbackQuery):
    await callback.message.edit_text(f"🕐 <b>Часовой пояс</b>\n\nВсе даты и время бота нормализуются к <b>{escape(config.TIMEZONE)}</b> — часовому поясу Иннополиса. Это позволяет не зависеть от часового пояса сервера и переходов DST.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки", callback_data="back:settings")]]), parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "back:settings")
async def back_settings(callback: CallbackQuery):
    await settings(callback.message); await callback.answer()


# ---- Admin -----------------------------------------------------------
@dp.message(F.text == "🛠 Управление")
async def management(message: Message):
    if is_admin(message.from_user.id): await message.answer("🛠 <b>Управление</b>", reply_markup=management_menu(message.from_user.id), parse_mode="HTML")


@dp.message(F.text == "👤 Администраторы")
async def admin_management(message: Message):
    if not is_owner(message.from_user.id): await message.answer("🔒 Только владелец может управлять администраторами."); return
    await message.answer("👤 <b>Администраторы</b>", reply_markup=simple_nav(("➕ Назначить администратора",), ("➖ Снять администратора",), ("🏠 Главное меню",)), parse_mode="HTML")


@dp.message(F.text == "➕ Назначить администратора")
async def add_admin_start(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return
    await message.answer("Отправь Telegram ID или @username пользователя.", reply_markup=cancel_menu()); await state.set_state(AdminStates.waiting_for_add_admin)


@dp.message(AdminStates.waiting_for_add_admin)
async def add_admin(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))
        return
    if not is_owner(message.from_user.id): await state.clear(); return
    await grant_role(message, state, "admin")


@dp.message(F.text == "➖ Снять администратора")
async def remove_admin_start(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id): return
    await message.answer("Отправь Telegram ID или @username администратора.", reply_markup=cancel_menu()); await state.set_state(AdminStates.waiting_for_remove_admin)


@dp.message(AdminStates.waiting_for_remove_admin)
async def remove_admin(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))
        return
    if not is_owner(message.from_user.id): await state.clear(); return
    await revoke_role(message, state, "admin")


@dp.message(F.text == "➕ Выдать доступ")
async def grant_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await message.answer("Отправь Telegram ID или @username пользователя.", reply_markup=cancel_menu()); await state.set_state(AdminStates.waiting_for_grant)


@dp.message(AdminStates.waiting_for_grant)
async def grant_access(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))
        return
    if not is_admin(message.from_user.id): await state.clear(); return
    await grant_role(message, state, "user")


@dp.message(F.text == "➖ Забрать доступ")
async def revoke_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await message.answer("Отправь Telegram ID или @username пользователя.", reply_markup=cancel_menu()); await state.set_state(AdminStates.waiting_for_revoke)


@dp.message(AdminStates.waiting_for_revoke)
async def revoke_access(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))
        return
    if not is_admin(message.from_user.id): await state.clear(); return
    await revoke_role(message, state, "user")


async def resolve_target(db, value: str):
    value = value.strip()
    if value.isdigit():
        tid = int(value); return db.query(AllowedUser).filter(AllowedUser.telegram_id == tid).first(), tid, None
    username = normalize_username(value); return db.query(AllowedUser).filter(AllowedUser.username == username).first(), None, username


async def grant_role(message, state, role):
    db = get_db()
    try:
        row, tid, username = await resolve_target(db, message.text or "")
        if tid == config.OWNER_TELEGRAM_ID or (row and row.telegram_id == config.OWNER_TELEGRAM_ID):
            await state.clear()
            await message.answer("👑 Владелец уже имеет максимальные права.", reply_markup=management_menu(message.from_user.id)); return
        if row:
            if role == "admin": row.role = "admin"
            row.telegram_id = tid or row.telegram_id; row.username = username or row.username; row.granted_by = message.from_user.id; row.last_seen_at = row.last_seen_at
        else:
            row = AllowedUser(telegram_id=tid, username=username, role=role, granted_by=message.from_user.id); db.add(row)
        db.commit(); label = str(tid) if tid else "@" + username
        await message.answer(f"✅ {label}: {'назначен администратором' if role == 'admin' else 'доступ выдан'}.", reply_markup=management_menu(message.from_user.id))
    finally: db.close()
    await state.clear()


async def revoke_role(message, state, role):
    db = get_db()
    try:
        row, tid, username = await resolve_target(db, message.text or "")
        if not row:
            await state.clear()
            await message.answer("Пользователь не найден.", reply_markup=management_menu(message.from_user.id)); return
        if row.telegram_id == config.OWNER_TELEGRAM_ID: await message.answer("Нельзя изменить права владельца.", reply_markup=management_menu(message.from_user.id)); return
        if role == "admin":
            if row.role != "admin": await message.answer("У пользователя нет прав администратора.", reply_markup=management_menu(message.from_user.id)); return
            row.role = "user"
        else:
            if row.role == "admin" and not is_owner(message.from_user.id): await message.answer("🔒 Администратор не может забрать права другого администратора.", reply_markup=management_menu(message.from_user.id)); return
            db.delete(row)
        db.commit(); await message.answer("✅ Права пользователя обновлены.", reply_markup=management_menu(message.from_user.id))
    finally: db.close()
    await state.clear()


@dp.message(F.text == "🔎 Найти пользователя")
async def search_user_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await message.answer("Отправь Telegram ID или @username.", reply_markup=cancel_menu()); await state.set_state(AdminStates.waiting_for_search)


@dp.message(AdminStates.waiting_for_search)
async def search_user(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))
        return
    if not is_admin(message.from_user.id): await state.clear(); return
    db = get_db()
    try:
        row, tid, username = await resolve_target(db, message.text or "")
        if not row:
            await message.answer("Пользователь пока не найден в базе. Для выдачи доступа используй «➕ Выдать доступ».", reply_markup=management_menu(message.from_user.id)); return
        role = "Администратор" if row.role == "admin" else "Пользователь"
        name = f"@{row.username}" if row.username else "без username"
        lines = [f"👤 <b>{name}</b>", f"🆔 ID: <code>{row.telegram_id or 'ещё не заходил'}</code>", f"🔐 Роль: {role}", f"📅 Доступ выдан: {row.added_at.strftime('%d.%m.%Y %H:%M') if row.added_at else '—'}", f"🕐 Последняя активность: {row.last_seen_at.strftime('%d.%m.%Y %H:%M') if row.last_seen_at else '—'}"]
        await message.answer("\n".join(lines), reply_markup=management_menu(message.from_user.id), parse_mode="HTML")
    finally: db.close()
    await state.clear()


@dp.message(F.text == "📊 Пользователи")
async def users_stats(message: Message):
    if not is_admin(message.from_user.id): return
    db = get_db()
    try:
        rows = db.query(AllowedUser).order_by(AllowedUser.role.desc(), AllowedUser.added_at.asc()).all()
        admins = [r for r in rows if r.role == "admin"]; users = [r for r in rows if r.role != "admin"]
        lines = [f"📊 <b>Пользователи</b>", f"👑 Владелец: ID {config.OWNER_TELEGRAM_ID}", f"🛡 Администраторы: {len(admins)}", f"👤 Пользователи: {len(users)}", ""]
        for r in admins + users:
            name = "@" + r.username if r.username else "без username"; role = "🛡" if r.role == "admin" else "👤"; last = r.last_seen_at.strftime("%d.%m %H:%M") if r.last_seen_at else "—"
            lines.append(f"{role} {name} · ID {r.telegram_id or '—'} · {last}")
        await message.answer("\n".join(lines), reply_markup=management_menu(message.from_user.id), parse_mode="HTML")
    finally: db.close()


@dp.message(F.text == "📢 Рассылка")
async def broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await message.answer("📢 Отправь текст рассылки.", reply_markup=cancel_menu()); await state.set_state(AdminStates.waiting_for_broadcast)


@dp.message(F.text == "❌ Отмена")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear(); await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))


@dp.message(AdminStates.waiting_for_broadcast)
async def broadcast(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=management_menu(message.from_user.id) if is_admin(message.from_user.id) else main_menu(message.from_user.id))
        return
    if not is_admin(message.from_user.id): await state.clear(); return
    text = message.text or ""; await state.clear(); db = get_db(); success = failed = 0
    try:
        recipients = db.query(AllowedUser).filter(AllowedUser.telegram_id.isnot(None)).all()
        for row in recipients:
            try: await bot.send_message(row.telegram_id, f"📢 Сообщение от администрации:\n\n{text}"); success += 1; await asyncio.sleep(0.05)
            except Exception: failed += 1
    finally: db.close()
    await message.answer(f"✅ Рассылка завершена.\nОтправлено: {success}\nОшибок: {failed}", reply_markup=management_menu(message.from_user.id))


async def main():
    restore_pending_bookings()
    scheduler.add_job(keep_alive_session_job, trigger="interval", hours=3, id="system_keep_alive", replace_existing=True)
    scheduler.add_job(check_training_reminders, trigger="interval", minutes=10, id="system_reminders", replace_existing=True)
    scheduler.add_job(monitor_free_spots_job, trigger="interval", minutes=2, id="system_spot_monitor", replace_existing=True)
    scheduler.start()

    try:
        await dp.start_polling(bot)
    finally:
        # Сначала останавливаем планировщик, чтобы новые фоновые задачи
        # не стартовали во время закрытия Telegram HTTP-сессии.
        if scheduler.running:
            scheduler.pause()
            scheduler.shutdown(wait=True)

        # aiogram использует aiohttp внутри bot.session. Явно закрываем
        # сессию при любом выходе из polling.
        try:
            await bot.session.close()
        except Exception:
            logging.exception("Ошибка при закрытии Telegram-сессии")


if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logging.info("Бот остановлен.")
