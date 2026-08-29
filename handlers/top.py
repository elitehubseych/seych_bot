
import db
from config import config
from handlers.registry import command
from utils.parse import format_amount
from utils.vk import get_full_name, mention, prefetch_full_names


@command("топ")
def cmd_top(user, args, message):
    if args.strip().lower() != "элиты":
        return None

    peer_id = message.get("peer_id")
    if not peer_id or peer_id < 2000000000:
        return "Эта команда работает только в беседе."

    db.ensure_chat_member(peer_id, user["vk_id"])
    top = db.get_chat_top(peer_id, limit=30)
    if not top:
        return "В беседе пока нет участников с элитами."

    prefetch_full_names(row["vk_id"] for row in top if not row.get("nickname"))

    lines = ["Статистика по элитам в чате:"]
    for index, row in enumerate(top, start=1):
        if row.get("nickname"):
            shown = row["nickname"]
        else:
            shown = get_full_name(row["vk_id"]).split(" ")[0]
        display = mention(row["vk_id"], shown)
        lines.append(f"{index}. {display} -- {format_amount(row['balance'])} 💎")

    total = db.get_chat_total_balance(peer_id)
    lines.append("")
    lines.append(f"Общие элиты беседы: {format_amount(total)} 💎")
    return "\n".join(lines)


@command("ники")
def cmd_nicknames(user, args, message):
    if args.strip():
        return None

    peer_id = message.get("peer_id")
    if not peer_id or peer_id < 2000000000:
        return "Эта команда работает только в беседе."

    db.ensure_chat_member(peer_id, user["vk_id"])
    members = db.get_chat_nicknames(peer_id)
    if not members:
        return "В беседе пока нет никнеймов."

    prefetch_full_names(row["vk_id"] for row in members)

    lines = ["Список никнеймов:"]
    seen = 0
    for index, row in enumerate(members, start=1):
        nick = (row.get("nickname") or "").strip()
        if not nick:
            continue
        seen += 1
        full_name = get_full_name(row["vk_id"])
        display_nick = mention(row["vk_id"], nick)
        lines.append(f"{seen}. {display_nick} - {full_name}")
    if seen == 0:
        return "В беседе пока нет никнеймов."
    return "\n".join(lines)


@command("топэлиты")
def cmd_top_elites(user, args, message):
    return cmd_top(user, "элиты", message)
