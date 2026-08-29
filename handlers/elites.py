
import db
from config import config
from handlers.registry import command
from utils.parse import extract_target_id, format_amount
from utils.vk import display_name

EMOJI = "💎"


@command("элиты")
def cmd_elites(user, args, message):
    reply_message = message.get("reply_message")
    target_id, remaining = extract_target_id(args, reply_message)
    if remaining.strip():
        return None
    allowed_group_id = -int(config.ID_GROUP)

    if target_id is None or target_id == user["vk_id"]:
        name = display_name(user)
        return f"{name} - у тебя {format_amount(user['balance'])} элитов {EMOJI}"

    if target_id < 0 and target_id != allowed_group_id:
        return "Эта команда не доступна на сообществах 😕"

    target = db.get_user_readonly(target_id)
    if target is None:
        return "Этот пользователь ещё не знаком с ботом 🤷"

    name = display_name(target)
    return f"У {name} - {format_amount(target['balance'])} элитов {EMOJI}"
