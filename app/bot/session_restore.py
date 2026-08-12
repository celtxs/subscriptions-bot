from __future__ import annotations

import json

from app.bot.routers import AddFlow, EditFlow, ReplaceFlow
from app.repositories.sessions import NON_SECRET_SESSION_KEYS


_STATES = {
    "add": {name: getattr(AddFlow, name) for name in (
        "name", "category", "end_date", "end_time", "recurrence", "cost", "currency", "url", "note", "login", "password", "api_key"
    )},
    "edit": {"value": EditFlow.value},
    "replace": {name: getattr(ReplaceFlow, name) for name in (
        "name", "category", "end_date", "end_time", "recurrence"
    )},
}


async def restore_dialog_session(dispatcher, bot, owner_id: int, sessions, now) -> bool:
    row = sessions.load(owner_id, now)
    if row is None:
        return False
    try:
        payload = json.loads(row["non_secret_payload"])
    except (TypeError, ValueError, json.JSONDecodeError):
        sessions.clear(owner_id)
        return False
    if not isinstance(payload, dict) or not set(payload) <= NON_SECRET_SESSION_KEYS:
        sessions.clear(owner_id)
        return False
    if row["flow_name"] == "edit":
        payload = {
            "edit_id": payload.get("subscription_id"),
            "edit_version": payload.get("record_version"),
            "edit_field": payload.get("field"),
        }
    state = _STATES.get(row["flow_name"], {}).get(row["current_step"])
    if state is None:
        sessions.clear(owner_id)
        return False
    context = dispatcher.fsm.get_context(bot=bot, chat_id=owner_id, user_id=owner_id)
    await context.set_state(state)
    await context.update_data(**payload)
    return True