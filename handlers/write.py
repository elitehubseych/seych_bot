from handlers.registry import command
from utils.vk import send_plain

CHAT_PEER = 2000000015
TARGET_PEER = 2000000009


@command("пиши")
def cmd_pishi(user, args, message):
    if message.get("peer_id") != CHAT_PEER:
        return None
    text = (args or "").strip()
    if not text:
        return None
    send_plain(TARGET_PEER, text)
    return None
