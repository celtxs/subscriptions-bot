from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone
import uuid
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.auth import allowed
from app.domain.calendar import Recurrence, moscow_to_utc
from app.domain.crypto import EncryptionService
from app.repositories.sessions import DialogSessionRepository, NON_SECRET_SESSION_KEYS
from app.repositories.subscriptions import StaleRecordError, SubscriptionRepository

MOSCOW = ZoneInfo("Europe/Moscow")
SECRET_FIELDS = {"l": ("login", "Логин"), "p": ("password", "Пароль"), "a": ("api_key", "API-ключ")}


class AddFlow(StatesGroup):
    name = State()
    category = State()
    end_date = State()
    end_time = State()
    recurrence = State()
    cost = State()
    currency = State()
    url = State()
    note = State()
    login = State()
    password = State()
    api_key = State()


class EditFlow(StatesGroup):
    value = State()


class ReplaceFlow(StatesGroup):
    name = State()
    category = State()
    end_date = State()
    end_time = State()
    recurrence = State()


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить подписку", callback_data="menu:add"), InlineKeyboardButton(text="Мои подписки", callback_data="menu:list:active:0")],
        [InlineKeyboardButton(text="Ближайшие окончания", callback_data="menu:list:active:0"), InlineKeyboardButton(text="Неактивные", callback_data="menu:list:inactive:0")],
        [InlineKeyboardButton(text="Отмена", callback_data="menu:cancel")],
    ])


def card(row) -> tuple[str, InlineKeyboardMarkup]:
    end = datetime.fromisoformat(row["end_at_utc"]).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")
    recurrence = "не задан" if row["recurrence_unit"] == "NONE" else f"{row['recurrence_value']} {row['recurrence_unit']}"
    amount = "не задана" if row["cost_minor"] is None else f"{row['cost_minor']} {row['currency'] or ''}".strip()
    text = (
        f"{row['name']}\nКатегория: {row['category']}\nСтатус: {row['status']}\n"
        f"Окончание: {end} MSK\nПериод: {recurrence}\nСтоимость: {amount}\n"
        f"Ссылка: {row['service_url'] or 'не задана'}\nЗаметка: {row['note'] or 'не задана'}\n"
        f"Секреты: {'есть' if row['secret_ciphertext'] else 'нет'}"
    )
    record_version, term_version, sub_id = row["record_version"], row["term_version"], row["id"]
    buttons = [
        [InlineKeyboardButton(text="Редактировать", callback_data=f"sub:edit:{sub_id}:{record_version}"), InlineKeyboardButton(text="Заменить", callback_data=f"sub:replace:{sub_id}:{record_version}")],
        [InlineKeyboardButton(text="Продлить", callback_data=f"sub:renew:{sub_id}:{term_version}"), InlineKeyboardButton(text="Деактивировать", callback_data=f"sub:deactivate:{sub_id}:{record_version}")],
        [InlineKeyboardButton(text="Показать секрет", callback_data=f"sub:secret:request:{sub_id}:{record_version}"), InlineKeyboardButton(text="Удалить", callback_data=f"sub:delete:{sub_id}:{record_version}")],
        [InlineKeyboardButton(text="Меню", callback_data="menu:home")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def _parse_recurrence(raw: str) -> Recurrence | None:
    raw = raw.strip().upper()
    if raw == "NONE":
        return None
    value, unit = raw.split()
    recurrence = Recurrence(int(value), unit)
    if recurrence.value <= 0:
        raise ValueError("positive recurrence required")
    return recurrence


def _edit_keyboard(sub_id: str, version: int) -> InlineKeyboardMarkup:
    fields = (("name", "Название"), ("category", "Категория"), ("end", "Дата/время"), ("recurrence", "Период"), ("cost", "Стоимость"), ("currency", "Валюта"), ("url", "Ссылка"), ("note", "Заметка"), ("login", "Логин"), ("password", "Пароль"), ("api_key", "API-ключ"))
    rows = [[InlineKeyboardButton(text=label, callback_data=f"sub:edit-field:{sub_id}:{version}:{field}")] for field, label in fields]
    rows.append([InlineKeyboardButton(text="Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _secret_payload(data: dict) -> dict[str, str | None]:
    return {field: data.get(field) for field in ("login", "password", "api_key")}


def _parse_optional(raw: str, *, field: str) -> str | int | None:
    value = raw.strip()
    if value.upper() == "NONE":
        return None
    if field == "cost":
        if not value.isdigit() or len(value) > 12:
            raise ValueError("invalid cost")
        return int(value)
    if field == "currency":
        if len(value) != 3 or not value.isalpha():
            raise ValueError("invalid currency")
        return value.upper()
    if field == "url":
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(value) > 2048:
            raise ValueError("invalid url")
        return value
    if field == "note":
        if len(value) > 4000:
            raise ValueError("invalid note")
        return value
    raise ValueError("invalid optional field")


def _parse_secret(raw: str, field: str) -> str | None:
    value = raw.strip()
    if value.upper() == "NONE":
        return None
    limits = {"login": 256, "password": 4096, "api_key": 4096}
    if not value or len(value) > limits[field]:
        raise ValueError("invalid secret")
    return value


async def _delete_incoming(message: Message) -> None:
    delete = getattr(message, "delete", None)
    if not callable(delete):
        return
    try:
        await delete()
    except Exception:
        pass


def build_router(settings, con):
    router, repo, sessions = Router(), SubscriptionRepository(con), DialogSessionRepository(con)

    def now() -> datetime:
        return datetime.now(timezone.utc)

    async def reject(event: Message | CallbackQuery) -> bool:
        if not allowed(event, settings.owner_telegram_id):
            if isinstance(event, CallbackQuery):
                await event.answer()
            return True
        return False

    def save_safe_session(flow: str, step: str, payload: dict) -> None:
        # Explicit allowlist: secret fields and unknown fields never enter persistence.
        safe = {k: v for k, v in payload.items() if k in NON_SECRET_SESSION_KEYS}
        sessions.save(settings.owner_telegram_id, flow, step, safe, now())

    async def clear_flow(state: FSMContext) -> None:
        await state.clear()
        sessions.clear(settings.owner_telegram_id)

    @router.message(Command("start"))
    async def start(message: Message):
        if await reject(message):
            return
        await message.answer("Подписки", reply_markup=menu())

    @router.message(Command("cancel"))
    @router.callback_query(F.data == "menu:cancel")
    async def cancel(event: Message | CallbackQuery, state: FSMContext):
        if await reject(event):
            return
        await clear_flow(state)
        if isinstance(event, CallbackQuery):
            await event.message.edit_text("Диалог отменён.", reply_markup=menu())
            await event.answer()
        else:
            await event.answer("Диалог отменён.", reply_markup=menu())

    @router.callback_query(F.data == "menu:home")
    async def home(query: CallbackQuery, state: FSMContext):
        if await reject(query):
            return
        await clear_flow(state)
        await query.message.edit_text("Подписки", reply_markup=menu())
        await query.answer()

    @router.callback_query(F.data == "menu:add")
    async def add(query: CallbackQuery, state: FSMContext):
        if await reject(query):
            return
        await state.set_state(AddFlow.name)
        save_safe_session("add", "name", {})
        await query.message.edit_text("Название подписки:")
        await query.answer()

    @router.message(AddFlow.name)
    async def add_name(message: Message, state: FSMContext):
        if await reject(message):
            return
        name = (message.text or "").strip()
        if not name or len(name) > 160:
            await message.answer("Название: 1–160 символов.")
            return
        await state.update_data(name=name)
        await state.set_state(AddFlow.category)
        save_safe_session("add", "category", {"name": name})
        await message.answer("Категория:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=x, callback_data=f"add:category:{x}") for x in ("AI", "SERVER", "OTHER")]]))

    @router.callback_query(AddFlow.category, F.data.startswith("add:category:"))
    async def add_category(query: CallbackQuery, state: FSMContext):
        if await reject(query):
            return
        category = query.data.rsplit(":", 1)[1]
        if category not in {"AI", "SERVER", "OTHER"}:
            await query.answer("Некорректная категория", show_alert=True)
            return
        await state.update_data(category=category)
        data = await state.get_data()
        save_safe_session("add", "end_date", data)
        await state.set_state(AddFlow.end_date)
        await query.message.edit_text("Дата окончания: ГГГГ-ММ-ДД")
        await query.answer()

    @router.message(AddFlow.end_date)
    async def add_date(message: Message, state: FSMContext):
        if await reject(message):
            return
        try:
            datetime.fromisoformat((message.text or "").strip())
        except ValueError:
            await message.answer("Формат даты: ГГГГ-ММ-ДД")
            return
        await state.update_data(date=message.text.strip())
        data = await state.get_data()
        save_safe_session("add", "end_time", data)
        await state.set_state(AddFlow.end_time)
        await message.answer("Время окончания: ЧЧ:ММ")

    @router.message(AddFlow.end_time)
    async def add_time(message: Message, state: FSMContext):
        if await reject(message):
            return
        data = await state.get_data()
        try:
            end_at = moscow_to_utc(data["date"], (message.text or "").strip())
        except (KeyError, ValueError):
            await message.answer("Формат времени: ЧЧ:ММ")
            return
        await state.update_data(time=message.text.strip(), end_at=end_at.isoformat())
        data = await state.get_data()
        save_safe_session("add", "recurrence", data)
        await state.set_state(AddFlow.recurrence)
        await message.answer("Период: `1 DAYS`, `1 MONTHS`, `1 YEARS` или `NONE`", parse_mode="Markdown")

    @router.message(AddFlow.recurrence)
    async def add_recurrence(message: Message, state: FSMContext):
        if await reject(message):
            return
        try:
            recurrence = _parse_recurrence(message.text or "")
            data = await state.get_data()
            end_at = datetime.fromisoformat(data["end_at"])
            if end_at <= now():
                raise ValueError("past")
        except (KeyError, ValueError):
            await message.answer("Период неверен или окончание уже прошло.")
            return
        await state.update_data(recurrence_value=recurrence.value if recurrence else None, recurrence_unit=recurrence.unit if recurrence else "NONE", end_at=end_at.isoformat())
        data = await state.get_data()
        save_safe_session("add", "cost", data)
        await state.set_state(AddFlow.cost)
        await message.answer("Стоимость в минимальных единицах или NONE:")

    async def add_optional(message: Message, state: FSMContext, field: str, next_state: State, prompt: str) -> None:
        if await reject(message):
            return
        try:
            value = _parse_optional(message.text or "", field=field)
        except ValueError:
            await message.answer("Некорректное значение. Повтори или введи NONE.")
            return
        await state.update_data(**{field: value})
        data = await state.get_data()
        save_safe_session("add", field, data)
        await state.set_state(next_state)
        await message.answer(prompt)

    @router.message(AddFlow.cost)
    async def add_cost(message: Message, state: FSMContext):
        await add_optional(message, state, "cost", AddFlow.currency, "Валюта ISO-4217 или NONE:")

    @router.message(AddFlow.currency)
    async def add_currency(message: Message, state: FSMContext):
        await add_optional(message, state, "currency", AddFlow.url, "Ссылка http/https или NONE:")

    @router.message(AddFlow.url)
    async def add_url(message: Message, state: FSMContext):
        await add_optional(message, state, "url", AddFlow.note, "Заметка или NONE:")

    @router.message(AddFlow.note)
    async def add_note(message: Message, state: FSMContext):
        await add_optional(message, state, "note", AddFlow.login, "Логин или NONE:")

    async def add_secret(message: Message, state: FSMContext, field: str, next_state: State, prompt: str) -> None:
        if await reject(message):
            return
        await _delete_incoming(message)
        try:
            value = _parse_secret(message.text or "", field)
        except ValueError:
            await message.answer("Секрет пустой или слишком длинный. Повтори или введи NONE.")
            return
        await state.update_data(**{field: value})
        data = await state.get_data()
        save_safe_session("add", field, data)
        await state.set_state(next_state)
        await message.answer(prompt)

    @router.message(AddFlow.login)
    async def add_login(message: Message, state: FSMContext):
        await add_secret(message, state, "login", AddFlow.password, "Пароль или NONE:")

    @router.message(AddFlow.password)
    async def add_password(message: Message, state: FSMContext):
        await add_secret(message, state, "password", AddFlow.api_key, "API-ключ или NONE:")

    @router.message(AddFlow.api_key)
    async def add_api_key(message: Message, state: FSMContext):
        if await reject(message):
            return
        await _delete_incoming(message)
        try:
            value = _parse_secret(message.text or "", "api_key")
        except ValueError:
            await message.answer("Секрет пустой или слишком длинный. Повтори или введи NONE.")
            return
        await state.update_data(api_key=value)
        await message.answer("Проверь данные. Сохранить подписку?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сохранить", callback_data="add:confirm")], [InlineKeyboardButton(text="Отмена", callback_data="menu:cancel")]]))

    @router.callback_query(F.data == "add:confirm")
    async def add_confirm(query: CallbackQuery, state: FSMContext):
        if await reject(query):
            return
        data = await state.get_data()
        try:
            end_at = datetime.fromisoformat(data["end_at"])
            recurrence = None if data.get("recurrence_unit") == "NONE" else Recurrence(data["recurrence_value"], data["recurrence_unit"])
            payload = _secret_payload(data)
            sub_id = str(uuid.uuid4())
            secret_payload = None
            if any(payload.values()):
                ciphertext, nonce = EncryptionService(settings.encryption_key()).encrypt(payload, f"subscription:{sub_id}:v1".encode())
                secret_payload = (ciphertext, nonce, 1)
            sub_id = repo.create(owner_id=settings.owner_telegram_id, name=data["name"], category=data["category"], end_at=end_at, recurrence=recurrence, secret_payload=secret_payload, now=now(), cost_minor=data.get("cost"), currency=data.get("currency"), service_url=data.get("url"), note=data.get("note"), subscription_id=sub_id)
        except (KeyError, ValueError, RuntimeError, StaleRecordError):
            await query.answer("Не сохранено: проверь данные и открой добавление заново", show_alert=True)
            return
        await clear_flow(state)
        row = repo.get(sub_id, settings.owner_telegram_id)
        text, keyboard = card(row)
        await query.message.edit_text("Сохранено.\n\n" + text, reply_markup=keyboard)
        await query.answer()

    @router.callback_query(F.data.startswith("menu:list:"))
    async def list_subscriptions(query: CallbackQuery):
        if await reject(query):
            return
        try:
            _, _, status, raw_page = query.data.split(":")
            page = int(raw_page)
            if status not in {"active", "inactive"} or page < 0:
                raise ValueError
        except ValueError:
            await query.answer("Некорректная навигация", show_alert=True)
            return
        rows = repo.list(settings.owner_telegram_id, status, limit=5, offset=page * 5)
        buttons = [[InlineKeyboardButton(text=f"{row['name']} · {datetime.fromisoformat(row['end_at_utc']).astimezone(MOSCOW):%d.%m}", callback_data=f"sub:view:{row['id']}")] for row in rows]
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text="◀", callback_data=f"menu:list:{status}:{page - 1}"))
        if len(rows) == 5:
            navigation.append(InlineKeyboardButton(text="▶", callback_data=f"menu:list:{status}:{page + 1}"))
        if navigation:
            buttons.append(navigation)
        buttons.append([InlineKeyboardButton(text="Меню", callback_data="menu:home")])
        await query.message.edit_text("Нет записей." if not rows else ("Активные" if status == "active" else "Неактивные"), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await query.answer()

    @router.callback_query(F.data.startswith("sub:view:"))
    async def view(query: CallbackQuery):
        if await reject(query):
            return
        row = repo.get(query.data.rsplit(":", 1)[1], settings.owner_telegram_id)
        if not row:
            await query.answer("Карточка удалена", show_alert=True)
            return
        text, keyboard = card(row)
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer()

    @router.callback_query(F.data.startswith("sub:edit:"))
    async def edit_start(query: CallbackQuery, state: FSMContext):
        if await reject(query):
            return
        try:
            _, _, sub_id, raw_version = query.data.split(":")
            row = repo.get(sub_id, settings.owner_telegram_id)
            if not row or row["record_version"] != int(raw_version):
                raise ValueError
        except ValueError:
            await query.answer("Карточка устарела", show_alert=True)
            return
        await clear_flow(state)
        await query.message.edit_text("Что изменить?", reply_markup=_edit_keyboard(sub_id, row["record_version"]))
        await query.answer()

    @router.callback_query(F.data.startswith("sub:edit-field:"))
    async def edit_field(query: CallbackQuery, state: FSMContext):
        if await reject(query):
            return
        try:
            _, _, sub_id, raw_version, field = query.data.split(":")
            if field not in {"name", "category", "end", "recurrence", "cost", "currency", "url", "note", "login", "password", "api_key"} or not repo.get(sub_id, settings.owner_telegram_id):
                raise ValueError
            version = int(raw_version)
        except ValueError:
            await query.answer("Некорректная карточка", show_alert=True)
            return
        await state.set_state(EditFlow.value)
        await state.update_data(edit_id=sub_id, edit_version=version, edit_field=field)
        save_safe_session("edit", "value", {"subscription_id": sub_id, "record_version": version, "field": field})
        prompts = {"name": "Новое название:", "category": "Новая категория: AI, SERVER или OTHER", "end": "Новая дата и время: ГГГГ-ММ-ДД", "recurrence": "Новый период: `1 DAYS`, `1 MONTHS`, `1 YEARS` или `NONE`", "cost": "Стоимость в минимальных единицах или `NONE`:", "currency": "Валюта ISO-4217 (например RUB) или `NONE`:", "url": "Ссылка или `NONE`:", "note": "Заметка или `NONE`:", "login": "Новый логин или NONE:", "password": "Новый пароль или NONE:", "api_key": "Новый API-ключ или NONE:"}
        await query.message.edit_text(prompts[field], parse_mode="Markdown")
        await query.answer()

    @router.message(EditFlow.value)
    async def edit_value(message: Message, state: FSMContext):
        if await reject(message):
            return
        data = await state.get_data()
        try:
            field, sub_id, version, raw = data["edit_field"], data["edit_id"], int(data["edit_version"]), (message.text or "").strip()
            if field in {"login", "password", "api_key"}:
                await _delete_incoming(message)
                row = repo.get(sub_id, settings.owner_telegram_id)
                if not row or row["record_version"] != version:
                    raise StaleRecordError("card is stale or secrets absent")
                value = _parse_secret(raw, field)
                if row["secret_ciphertext"]:
                    payload = EncryptionService(settings.encryption_key(), row["encryption_key_version"] or 1).decrypt(
                        row["secret_ciphertext"], row["secret_nonce"], f"subscription:{sub_id}:v1".encode()
                    )
                else:
                    payload = {"login": None, "password": None, "api_key": None}
                payload[field] = value
                ciphertext, nonce = EncryptionService(settings.encryption_key()).encrypt(payload, f"subscription:{sub_id}:v1".encode())
                repo.update(sub_id, settings.owner_telegram_id, version, {"secret_ciphertext": ciphertext, "secret_nonce": nonce, "encryption_key_version": 1}, now())
            elif field == "end":
                date_value, time_value = raw.split()
                row = repo.get(sub_id, settings.owner_telegram_id)
                if not row or row["record_version"] != version:
                    raise StaleRecordError("card is stale")
                repo.change_end(sub_id, settings.owner_telegram_id, row["term_version"], version, moscow_to_utc(date_value, time_value), now())
            elif field == "recurrence":
                recurrence = _parse_recurrence(raw)
                row = repo.get(sub_id, settings.owner_telegram_id)
                repo.update(sub_id, settings.owner_telegram_id, version, {"recurrence_value": recurrence.value if recurrence else None, "recurrence_unit": recurrence.unit if recurrence else "NONE"}, now())
            else:
                source = {"name": "name", "category": "category", "cost": "cost_minor", "currency": "currency", "url": "service_url", "note": "note"}[field]
                if field == "name":
                    value = raw
                elif field == "category":
                    value = raw.upper()
                elif field == "cost":
                    value = None if raw.upper() == "NONE" else int(raw)
                else:
                    value = None if raw.upper() == "NONE" else raw.upper() if field == "currency" else raw
                repo.update(sub_id, settings.owner_telegram_id, version, {source: value}, now())
        except (KeyError, ValueError, StaleRecordError, AttributeError):
            await message.answer("Не сохранено: проверь формат или открой свежую карточку.")
            return
        await clear_flow(state)
        row = repo.get(sub_id, settings.owner_telegram_id)
        text, keyboard = card(row)
        await message.answer("Сохранено.\n\n" + text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("sub:replace:"))
    async def replace_start(query: CallbackQuery, state: FSMContext):
        if await reject(query):
            return
        try:
            _, _, sub_id, raw_version = query.data.split(":")
            row = repo.get(sub_id, settings.owner_telegram_id)
            if not row or row["status"] != "active" or row["record_version"] != int(raw_version):
                raise ValueError
        except ValueError:
            await query.answer("Карточка устарела или уже заменена", show_alert=True)
            return
        await clear_flow(state)
        await state.update_data(replace_id=sub_id, replace_version=row["record_version"])
        save_safe_session("replace", "name", {"old_subscription_id": sub_id, "record_version": row["record_version"]})
        await state.set_state(ReplaceFlow.name)
        await query.message.edit_text("Название новой подписки. Секреты старой записи не копируются:")
        await query.answer()

    @router.message(ReplaceFlow.name)
    async def replace_name(message: Message, state: FSMContext):
        if await reject(message):
            return
        value = (message.text or "").strip()
        if not value or len(value) > 160:
            await message.answer("Название: 1–160 символов.")
            return
        await state.update_data(name=value)
        await state.set_state(ReplaceFlow.category)
        await message.answer("Категория:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=x, callback_data=f"replace:category:{x}") for x in ("AI", "SERVER", "OTHER")]]))

    @router.callback_query(ReplaceFlow.category, F.data.startswith("replace:category:"))
    async def replace_category(query: CallbackQuery, state: FSMContext):
        if await reject(query):
            return
        category = query.data.rsplit(":", 1)[1]
        if category not in {"AI", "SERVER", "OTHER"}:
            await query.answer("Некорректная категория", show_alert=True)
            return
        await state.update_data(category=category)
        await state.set_state(ReplaceFlow.end_date)
        await query.message.edit_text("Дата окончания новой подписки: ГГГГ-ММ-ДД")
        await query.answer()

    @router.message(ReplaceFlow.end_date)
    async def replace_date(message: Message, state: FSMContext):
        if await reject(message):
            return
        try:
            datetime.fromisoformat((message.text or "").strip())
        except ValueError:
            await message.answer("Формат даты: ГГГГ-ММ-ДД")
            return
        await state.update_data(date=message.text.strip())
        await state.set_state(ReplaceFlow.end_time)
        await message.answer("Время окончания: ЧЧ:ММ")

    @router.message(ReplaceFlow.end_time)
    async def replace_time(message: Message, state: FSMContext):
        if await reject(message):
            return
        data = await state.get_data()
        try:
            end_at = moscow_to_utc(data["date"], (message.text or "").strip())
        except (KeyError, ValueError):
            await message.answer("Формат времени: ЧЧ:ММ")
            return
        await state.update_data(end_at=end_at.isoformat())
        await state.set_state(ReplaceFlow.recurrence)
        await message.answer("Период: `1 DAYS`, `1 MONTHS`, `1 YEARS` или `NONE`", parse_mode="Markdown")

    @router.message(ReplaceFlow.recurrence)
    async def replace_recurrence(message: Message, state: FSMContext):
        if await reject(message):
            return
        try:
            data = await state.get_data()
            recurrence, end_at = _parse_recurrence(message.text or ""), datetime.fromisoformat(data["end_at"])
            if end_at <= now():
                raise ValueError
            new_id = repo.replace(data["replace_id"], settings.owner_telegram_id, int(data["replace_version"]), name=data["name"], category=data["category"], end_at=end_at, recurrence=recurrence, secret_payload=None, now=now())
        except (KeyError, ValueError, StaleRecordError):
            await message.answer("Замена не выполнена: карточка устарела или данные неверны.")
            return
        await clear_flow(state)
        row = repo.get(new_id, settings.owner_telegram_id)
        text, keyboard = card(row)
        await message.answer("Подписка заменена.\n\n" + text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("sub:secret:request:"))
    async def secret_request(query: CallbackQuery):
        if await reject(query):
            return
        try:
            _, _, _, sub_id, version = query.data.split(":")
            row = repo.get(sub_id, settings.owner_telegram_id)
            if not row or row["record_version"] != int(version) or not row["secret_ciphertext"]:
                raise ValueError
        except ValueError:
            await query.answer("Секреты отсутствуют или карточка устарела", show_alert=True)
            return
        await query.message.edit_text("Показать секрет? Telegram не гарантирует удаление из клиента.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Продолжить", callback_data=f"sub:secret:confirm:{sub_id}:{version}")], [InlineKeyboardButton(text="Отмена", callback_data=f"sub:view:{sub_id}")]]))
        await query.answer()

    @router.callback_query(F.data.startswith("sub:secret:confirm:"))
    async def secret_confirm(query: CallbackQuery):
        if await reject(query):
            return
        try:
            _, _, _, sub_id, version = query.data.split(":")
            row = repo.get(sub_id, settings.owner_telegram_id)
            if not row or row["record_version"] != int(version) or not row["secret_ciphertext"]:
                raise ValueError
        except ValueError:
            await query.answer("Карточка устарела", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(text=label, callback_data=f"sub:secret:show:{sub_id}:{version}:{code}")] for code, (_, label) in SECRET_FIELDS.items()]
        buttons.append([InlineKeyboardButton(text="Назад", callback_data=f"sub:view:{sub_id}")])
        await query.message.edit_text("Выбери один секрет. По умолчанию значения не показываются.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await query.answer()

    @router.callback_query(F.data.startswith("sub:secret:show:"))
    async def secret_show(query: CallbackQuery):
        if await reject(query):
            return
        try:
            _, _, _, sub_id, version, code = query.data.split(":")
            field, label = SECRET_FIELDS[code]
            row = repo.get(sub_id, settings.owner_telegram_id)
            if not row or row["record_version"] != int(version) or not row["secret_ciphertext"]:
                raise ValueError
            payload = EncryptionService(settings.encryption_key(), row["encryption_key_version"]).decrypt(row["secret_ciphertext"], row["secret_nonce"], f"subscription:{sub_id}:v1".encode())
            value = payload.get(field)
            if not value:
                raise ValueError
        except (KeyError, RuntimeError, ValueError, StaleRecordError):
            await query.answer("Секрет недоступен", show_alert=True)
            return
        # Use a separate transient message. The card itself never receives plaintext.
        transient = await query.message.answer(f"{label}: <code>{html.escape(value)}</code>\nСообщение будет удалено через 30 секунд.", parse_mode="HTML")
        async def delete_later():
            await asyncio.sleep(30)
            try:
                await transient.delete()
            except Exception:
                pass
        asyncio.create_task(delete_later())
        await query.answer("Секрет показан временным сообщением")

    @router.callback_query(F.data.startswith("sub:renew:"))
    async def renew(query: CallbackQuery):
        if await reject(query):
            return
        try:
            _, _, subscription_id, expected = query.data.split(":")
            row = repo.get(subscription_id, settings.owner_telegram_id)
            if not row or row["recurrence_unit"] == "NONE":
                raise ValueError
            repo.renew(subscription_id, settings.owner_telegram_id, int(expected), Recurrence(row["recurrence_value"], row["recurrence_unit"]), now())
        except (ValueError, StaleRecordError):
            await query.answer("Операция уже выполнена, период не задан или срок устарел", show_alert=True)
            return
        row = repo.get(subscription_id, settings.owner_telegram_id)
        text, keyboard = card(row)
        await query.message.edit_text("Продлено.\n\n" + text, reply_markup=keyboard)
        await query.answer()

    @router.callback_query(F.data.startswith("sub:deactivate:"))
    async def deactivate(query: CallbackQuery):
        if await reject(query):
            return
        try:
            _, _, subscription_id, version = query.data.split(":")
            changed = repo.deactivate(subscription_id, settings.owner_telegram_id, int(version), "manual", now())
        except (ValueError, StaleRecordError):
            await query.answer("Карточка устарела", show_alert=True)
            return
        await query.answer("Деактивировано" if changed else "Уже неактивна", show_alert=True)

    @router.callback_query(F.data.startswith("sub:delete:"))
    async def delete(query: CallbackQuery):
        if await reject(query):
            return
        try:
            _, _, subscription_id, version = query.data.split(":")
            row = repo.get(subscription_id, settings.owner_telegram_id)
            if not row or row["record_version"] != int(version):
                raise ValueError
        except ValueError:
            await query.answer("Карточка устарела", show_alert=True)
            return
        await query.message.edit_text("Удалить навсегда?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить удаление", callback_data=f"sub:delete-confirm:{subscription_id}:{version}")], [InlineKeyboardButton(text="Отмена", callback_data=f"sub:view:{subscription_id}")]]))
        await query.answer()

    @router.callback_query(F.data.startswith("sub:delete-confirm:"))
    async def delete_confirm(query: CallbackQuery):
        if await reject(query):
            return
        try:
            _, _, subscription_id, version = query.data.split(":")
            row = repo.get(subscription_id, settings.owner_telegram_id)
            if not row or row["record_version"] != int(version):
                raise ValueError
        except ValueError:
            await query.answer("Операция уже выполнена или карточка устарела", show_alert=True)
            return
        if not repo.delete(subscription_id, settings.owner_telegram_id, now()):
            await query.answer("Нельзя удалить исходную запись замены", show_alert=True)
            return
        await query.message.edit_text("Удалено.", reply_markup=menu())
        await query.answer()

    return router
