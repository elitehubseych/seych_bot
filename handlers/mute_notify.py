"""
Листенер команды mute в PEER_BASE -> уведомление в PEER_ELITE.

Формат команды (в PEER_BASE):
    mute @user/link/id 30 минут
    Спам

Уведомление в PEER_ELITE:
    Разработчик / Администратор выдал мут Имя на срок.
    Причина: Спам

Не создаёт мут — только логирует и уведомляет.
"""

import logging
import re

from config import config
from utils.vk import send_plain, get_full_name, mention

logger = logging.getLogger(__name__)

_DURATION = (
    r"("
    r"секунд[уы]?|"
    r"минут[уы]?|"
    r"час(?:ов?[аеу]?|а)?|"
    r"день|дня|дней|"
    r"недел[юяи]|"
    r"месяц[аеу]?"
    r")"
)

_TARGET = (
    r"(?:"
    r"\[id(\d+)\|[^\]]*\]|"      # [id123|Name]
    r"\[club(\d+)\|[^\]]*\]|"    # [club123|Name]
    r"@(\w+)|"                   # @username
    r"id(\d+)|"                  # id123
    r"(vk\.com/\S+)|"            # vk.com/id123
    r"(\d+)"                     # 123456
    r")"
)

_MUTE_RE = re.compile(
    r"^mute\s+" + _TARGET + r"\s+(\d+)\s*" + _DURATION,
    re.IGNORECASE,
)


def check_mute_notify(vk_id, peer_id, text):
    if not config.PEER_BASE or not config.PEER_ELITE:
        return
    if peer_id != config.PEER_BASE:
        return

    lines = (text or "").strip().split("\n", 1)
    first_line = lines[0].strip() if lines else ""

    m = _MUTE_RE.match(first_line)
    if not m:
        return

    target_id = None
    if m.group(1):
        target_id = int(m.group(1))
    elif m.group(2):
        target_id = -int(m.group(2))
    elif m.group(3):
        try:
            from utils.vk import vk
            resolved = vk.users.get(user_ids=m.group(3))
            if resolved:
                target_id = int(resolved[0]["id"])
        except Exception:
            pass
    elif m.group(4):
        target_id = int(m.group(4))
    elif m.group(5):
        link_id = re.search(r"id(\d+)", m.group(5))
        if link_id:
            target_id = int(link_id.group(1))

    if not target_id:
        return

    number = int(m.group(7))
    unit = m.group(8).lower()
    if "секунд" in unit:
        duration = f"{number} сек."
    elif "минут" in unit:
        duration = f"{number} мин."
    elif "час" in unit:
        duration = f"{number} ч."
    elif "дн" in unit or "день" in unit:
        duration = f"{number} дн."
    elif "недел" in unit:
        duration = f"{number} нед."
    elif "месяц" in unit:
        duration = f"{number} мес."
    else:
        duration = f"{number} {unit}"

    reason = lines[1].strip() if len(lines) > 1 and lines[1].strip() else "Не указана"

    target_name = get_full_name(target_id)
    target_mention = mention(target_id, target_name)

    is_dev = str(vk_id) == str(config.DEV_ID)
    actor = mention(vk_id, "Разработчик") if is_dev else mention(vk_id, "Администратор")

    msg = (
        f"{actor} выдал мут {target_mention} на {duration}.\n"
        f"Причина: {reason}"
    )

    try:
        send_plain(config.PEER_ELITE, msg)
        logger.info("mute notify: actor=%s target=%s peer=%s reason=%r", vk_id, target_id, peer_id, reason)
    except Exception as e:
        logger.error("mute notify failed: %s", e)
