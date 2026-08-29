
import logging
import threading
import time

import db
from config import config
from handlers.mute_notify import check_mute_notify
from handlers.registry import COMMANDS
from utils.users import ensure_users, extract_mentioned_ids
from utils.vk import send_plain, send_reply

logger = logging.getLogger(__name__)

CHAT_PEER_ID_MIN = 2000000000

# Команды, ответы на которые уходят реплаем на сообщение-триггер
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

    # ── листенер mute-команды в PEER_BASE → уведомление в PEER_ELITE ──
    try:
        check_mute_notify(vk_id, peer_id, text)
    except Exception:
        logger.exception("mute_notify check failed")

    if peer_id < CHAT_PEER_ID_MIN:
        logger.info("ЛС: from=%s text=%r", vk_id, (text or "")[:60])

    try:
        user = db.get_user(vk_id)
        if user is None:
            logger.error("Не удалось получить пользователя %s", vk_id)
            return

        # ── мгновенный ответ: сначала ищем команду, тяжёлую статистику — в фон ──
        command, args = split_command(text)
        handler = COMMANDS.get(command)

        # Фон: ensure_users / ensure_chat_member / bump / +1 элита — не блокируем ответ
        _mids = extract_mentioned_ids(text)
        _is_chat = peer_id >= CHAT_PEER_ID_MIN
        _uid = vk_id
        _pid = peer_id
        def _bg_bookkeep():
            try:
                if _mids:
                    ensure_users(_mids)
            except Exception:
                logger.exception("ensure_users bg failed")
            if _is_chat:
                try:
                    db.ensure_chat_member(_pid, _uid)
                except Exception:
                    logger.exception("ensure_chat_member bg failed")
                try:
                    db.bump_messages(_pid, _uid)
                except Exception:
                    logger.exception("bump_messages bg failed")
                try:
                    db.update_balance(_uid, 1)
                except Exception:
                    logger.exception("update_balance bg failed")
        threading.Thread(target=_bg_bookkeep, daemon=True).start()

        if handler is None:
            return

        started = time.monotonic()
        try:
            answer = handler(user, args, message)
        except Exception:
            logger.exception("Ошибка команды %s", command)
            answer = "⚠️ Ошибка, попробуй ещё раз"
        took_ms = int((time.monotonic() - started) * 1000)
        if took_ms > 1500:
            logger.warning("Медленная команда %s: %s мс", command, took_ms)
        if answer:
            if command in REPLY_COMMANDS:
                send_reply(peer_id, conversation_message_id, answer)
            else:
                # Обычное сообщение без reply: реплаи падают и нагружают чат
                send_plain(peer_id, answer)
    except Exception as error:
        logger.exception("Ошибка обработки сообщения от %s: %s", vk_id, error)
