
import datetime

import db
from handlers.registry import command
from utils.parse import format_amount

DAILY_AMOUNT = 500

MSK = datetime.timezone(datetime.timedelta(hours=3))


def _when_text(ready_at):
    """«завтра в 15:00 по МСК» / «сегодня в ...» / «05.09 в ...»."""
    if ready_at is None:
        return "завтра"
    local = ready_at.astimezone(MSK)
    now = datetime.datetime.now(MSK)
    hhmm = local.strftime("%H:%M")
    days = (local.date() - now.date()).days
    if days <= 0:
        return f"сегодня в {hhmm} (мск)"
    if days == 1:
        return f"завтра в {hhmm} (мск)"
    return f"{local.strftime('%d.%m')} в {hhmm} (мск)"


@command("бонус", "ежедневный")
def cmd_daily(user, args, message):
    if args.strip():
        return None

    new_balance = db.claim_daily(user["vk_id"], DAILY_AMOUNT)
    if new_balance is None:
        ready = db.daily_ready_at(user["vk_id"])
        return (
            "⏳ Бонус уже забран.\n"
            "Приходи %s ⏰" % _when_text(ready)
        )
    return (
        "🎁 Бонус забран: +%s 💎\n"
        "Баланс: %s\n"
        "Приходи %s ⏰" % (
            format_amount(DAILY_AMOUNT),
            format_amount(new_balance),
            _when_text(db.daily_ready_at(user["vk_id"])),
        )
    )
