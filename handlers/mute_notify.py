import re

from config import config
from utils.vk import send_plain, get_full_name, mention

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
    r"\[id(\d+)\|[^\]]*\]|"
    r"\[club(\d+)\|[^\]]*\]|"
    r"@(\w+)|"
    r"id(\d+)|"
    r"(vk\.com/\S+)|"
    r"(\d+)"
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

    def _decl(n, one, two, five):
        if n % 10 == 1 and n % 100 != 11:
            return one
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return two
        return five

    if "секунд" in unit:
        duration = "%d %s" % (number, _decl(number, "секунду", "секунды", "секунд"))
    elif "минут" in unit:
        duration = "%d %s" % (number, _decl(number, "минуту", "минуты", "минут"))
    elif "час" in unit:
        duration = "%d %s" % (number, _decl(number, "час", "часа", "часов"))
    elif "дн" in unit or "день" in unit:
        duration = "%d %s" % (number, _decl(number, "день", "дня", "дней"))
    elif "недел" in unit:
        duration = "%d %s" % (number, _decl(number, "неделю", "недели", "недель"))
    elif "месяц" in unit:
        duration = "%d %s" % (number, _decl(number, "месяц", "месяца", "месяцев"))
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
    except Exception:
        pass
