
import datetime
import logging
import threading
import time

import db
from config import config
from handlers.registry import command
from utils.parse import extract_target_id, format_amount
from utils.vk import display_name_by_vk_id, vk

logger = logging.getLogger(__name__)

DEV_ID = int(config.DEV_ID)

_MSK = datetime.timezone(datetime.timedelta(hours=3))

_ROLES_CACHE = {}          # peer_id -> (timestamp, owner_id, admin_ids)
_ROLES_TTL_SEC = 300
_ROLES_LOCK = threading.Lock()


def _chat_roles(peer_id):
    """owner_id и admin_ids беседы через VK API (кэш на 5 минут)."""
    with _ROLES_LOCK:
        cached = _ROLES_CACHE.get(peer_id)
        if cached and time.monotonic() - cached[0] < _ROLES_TTL_SEC:
            return cached[1], cached[2]
    try:
        raw = vk.messages.getConversationMembers(peer_id=peer_id)
        settings = raw.get("chat_settings") or {}
        owner_id = int(settings.get("owner_id") or 0)
        admin_ids = {int(x) for x in (settings.get("admin_ids") or [])}
    except Exception as error:
        logger.warning("Не удалось получить роли беседы %s: %s", peer_id, error)
        owner_id, admin_ids = None, set()
    with _ROLES_LOCK:
        _ROLES_CACHE[peer_id] = (time.monotonic(), owner_id, admin_ids)
    return owner_id, admin_ids


def _role_name(peer_id, vk_id):
    if vk_id == DEV_ID:
        return "Разработчик"
    owner_id, admin_ids = _chat_roles(peer_id)
    if owner_id is not None:
        if vk_id == owner_id:
            return "Владелец"
        if vk_id in admin_ids:
            return "Администратор"
    return "Бездарь"


def _title_display(title_key):
    from utils.items import TITLES
    meta = TITLES.get(title_key)
    return meta["name"] if meta else title_key


@command("инфо")
def cmd_info(user, args, message):
    peer_id = message.get("peer_id")
    if not peer_id or peer_id < 2000000000:
        return "ℹ️ Профили доступны только в беседе."

    target_id, remaining = extract_target_id(args, message.get("reply_message"))
    if remaining.strip():
        return None
    target_id = target_id if target_id and target_id > 0 else user["vk_id"]

    target = db.get_user_readonly(target_id)
    if target is None:
        return "Этот пользователь ещё не знаком с ботом 🤷"

    member = db.get_chat_member_info(peer_id, target_id) or {}
    messages_count = member.get("message_count") or 0
    joined_at = member.get("joined_at")

    title_key = db.get_active_title(target_id)
    title_text = _title_display(title_key) if title_key else "Отсутствует"

    marriage = db.get_active_marriage_for(peer_id, target_id)
    if marriage:
        partner_id = marriage["user2_id"] if marriage["user1_id"] == target_id else marriage["user1_id"]
        partner = "❤️ " + display_name_by_vk_id(partner_id)
    else:
        partner = "Отсутствует"

    joined_text = joined_at.astimezone(_MSK).strftime("%d.%m.%Y") if joined_at else "—"

    lines = [
        "ℹ️ Информация о %s" % display_name_by_vk_id(target_id),
        "",
        "👤 Должность: %s" % _role_name(peer_id, target_id),
        "🎖 Титул: %s" % title_text,
        "💬 Личный актив: %s сообщ." % format_amount(messages_count),
        "💰 Общие элиты: %s 💎" % format_amount(target.get("total_earned") or 0),
        "💎 Элиты: %s 💎" % format_amount(target["balance"]),
        "📉 Потрачено элитов: %s 💎" % format_amount(target.get("total_spent") or 0),
        "💍 Брак: %s" % partner,
        "📅 Регистрация в боте: %s" % joined_text,
    ]

    from handlers.business import BIZ, owned_kinds as _owned_kinds
    kinds = _owned_kinds(peer_id, target_id)
    if kinds:
        biz_names = ", ".join(BIZ[k]["emoji"] + " " + BIZ[k]["name"] for k in kinds if k in BIZ)
        lines.append("💼 Бизнес: %s" % biz_names)

    return "\n".join(lines)
