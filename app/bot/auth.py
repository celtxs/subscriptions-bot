from aiogram.types import CallbackQuery, Message

def allowed(event: Message | CallbackQuery, owner_id: int) -> bool:
    user=event.from_user
    chat=event.message.chat if isinstance(event, CallbackQuery) else event.chat
    return bool(user and user.id == owner_id and chat.type == "private")
