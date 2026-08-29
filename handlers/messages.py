import threading
import time

import db
from config import config
from handlers.mute_notify import check_mute_notify
from handlers.registry import COMMANDS
from utils.users import ensure_users, extract_mentioned_ids
from utils.vk import send_plain, send_reply

CHAT_PEER_ID_MIN = 2000000000

REPLY_COMMANDS = {"бонус", "ежедневный", "банк", "промо", "промокод"}


def split_command(text):
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return None, ""
    return parts[0].lower(), parts[1] if len(parts) > 1 else ""


def is_allowed_group(data):
    allowed_group_id = str(config.ID_GROUP).strip()
    if not allowed_group_id:
        return False

    obj = data.get("object") or {}
    group_id = data.get("group_id")
    if group_id is None:
        group_id = obj.get("group_id")
    if group_id is None:
        return False

    return str(group_id) == allowed_group_id


def handle_message_new(data):
    if not is_allowed_group(data):
        return

    obj = data.get("object") or {}
    message = obj.get("message", obj)

    vk_id = message.get("from_id")
    peer_id = message.get("peer_id")
    conversation_message_id = message.get("conversation_message_id") or message.get("id")
    text = message.get("text", "")

    if not vk_id or not peer_id:
        return

    if vk_id < 0:
        return

    try:
        check_mute_notify(vk_id, peer_id, text)
    except Exception:
        pass

    try:
        user = db.get_user(vk_id)
        if user is None:
            return

        command, args = split_command(text)
        handler = COMMANDS.get(command)

        _mids = extract_mentioned_ids(text)
        _is_chat = peer_id >= CHAT_PEER_ID_MIN
        _uid = vk_id
        _pid = peer_id
        def _bg_bookkeep():
            try:
                if _mids:
                    ensure_users(_mids)
            except Exception:
                pass
            if _is_chat:
                try:
                    db.ensure_chat_member(_pid, _uid)
                except Exception:
                    pass
                try:
                    db.bump_messages(_pid, _uid)
                except Exception:
                    pass
                try:
                    db.update_balance(_uid, 1)
                except Exception:
                    pass
        threading.Thread(target=_bg_bookkeep, daemon=True).start()

        if handler is None:
            return

        started = time.monotonic()
        try:
            answer = handler(user, args, message)
        except Exception:
            answer = "⚠️ Ошибка, попробуй ещё раз"
        took_ms = int((time.monotonic() - started) * 1000)
        if answer:
            if command in REPLY_COMMANDS:
                send_reply(peer_id, conversation_message_id, answer)
            else:
                send_plain(peer_id, answer)
    except Exception:
        pass
