
import db
from handlers.registry import command
from utils.vk import display_name, mention


@command("+ник")
def cmd_set_nick(user, args, message):
    new_nick = args.strip()
    if not new_nick:
        return "Укажи ник: +ник <текст>"

    old_display = display_name(user)
    db.set_nickname(user["vk_id"], new_nick)
    new_display = mention(user["vk_id"], new_nick)
    return f"{old_display}, ты теперь {new_display}"


@command("-ник")
def cmd_clear_nick(user, args, message):
    if args.strip():
        return None

    db.clear_nickname(user["vk_id"])
    user_no_nick = dict(user, nickname=None)
    return f"Ник удалён. Теперь ты снова {display_name(user_no_nick)}"
