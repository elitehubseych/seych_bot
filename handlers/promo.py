import datetime
import json

import db
from handlers.registry import command
from utils.items import CASES, ITEMS, TITLES, CASE_ALIASES
from utils.parse import format_amount

_TYPE_EMOJI = {
    "Обычный": "📦",
    "Мифический": "🌀",
    "Легендарный": "⚡",
    "Элитный": "💎",
}

MSK = datetime.timezone(datetime.timedelta(hours=3))


def _item_name(item_key, qty):
    if item_key in CASES:
        base = CASES[item_key]["name"].replace("📦 ", "")
        return f"📦 {base}"
    if item_key in TITLES:
        base = TITLES[item_key]["name"]
        typ = TITLES[item_key].get("type", "Обычный")
        return f"{_TYPE_EMOJI.get(typ, '⭐')} {base}"
    if item_key in ITEMS:
        return ITEMS[item_key]["name"]
    return f"🎁 {item_key}"


def _reward_line(reward, idx):
    rtype = reward.get("type")
    qty = reward.get("qty", 1)
    if rtype == "elite":
        amount = reward.get("amount", 0)
        return f"{idx}. 💎 {format_amount(amount)} элитов"
    if rtype == "key":
        return f"{idx}. 🔑 {qty} ключей"
    if rtype == "item":
        key = reward.get("key", "")
        return f"{idx}. {_item_name(key, qty)}" + (f" ×{qty}" if qty > 1 else "")
    return f"{idx}. 🎁 {reward.get('label', 'Подарок')}"


def _type_display(typ, fallback="⭐"):
    return _TYPE_EMOJI.get(typ, fallback)


def _reward_texts(rewards):
    out = []
    for i, r in enumerate(rewards, 1):
        out.append(_reward_line(r, i))
    return out


def _apply_rewards(vk_id, rewards):
    given = []
    for i, r in enumerate(rewards or [], 1):
        rtype = r.get("type")
        qty = int(r.get("qty", 1) or 1)
        try:
            if rtype == "elite":
                db.update_balance(vk_id, int(r.get("amount", 0)))
            elif rtype == "key":
                db.add_item(vk_id, "key", qty)
            elif rtype == "item":
                db.add_item(vk_id, r.get("key", ""), qty)
        except Exception:
            continue
        given.append(_reward_line(r, i))
    return given


@command("промо", "промокод")
def cmd_promo(user, args, message):
    raw = (args or "").strip().strip('"«» ')

    if raw.lower().startswith("создать"):
        from config import config
        from handlers.inventory import _is_dev, bot_mention
        if not _is_dev(user["vk_id"]):
            return "❌ Только разработчик может создавать промокоды"
        lines = raw[len("создать"):].strip().splitlines()
        if not lines:
            return (
                "📝 Формат: промо создать <КОД>\n"
                "<лимит активаций>\n<часы действия>\n"
                "<награды JSON на след. строке>\n"
                "Пример: see промо создать\n"
                "SEYCH\n10\n5\n"
                '[{"type":"elite","amount":500000},'
                '{"type":"item","key":"case_elite","qty":1},'
                '{"type":"key","qty":10}]'
            )
        code = lines[0].strip().upper()
        max_uses = 1
        hours = None
        rewards = []
        if len(lines) > 1:
            try:
                max_uses = int(lines[1].strip())
            except Exception:
                max_uses = 1
        if len(lines) > 2:
            try:
                hours = float(lines[2].strip())
            except Exception:
                hours = None
        if len(lines) > 3:
            try:
                rewards = json.loads(lines[3].strip())
                if not isinstance(rewards, list):
                    rewards = []
            except Exception:
                rewards = []
        if not code or not rewards:
            return "❌ Укажи код и награды. Формат: промо создать <КОД>\n<лимит>\n<часы>\n<JSON>"
        store = db.get_connection()
        cur = store.cursor()
        try:
            cur.execute(
                "INSERT INTO promo_codes (code, rewards, max_uses, used_count, expires_at) "
                "VALUES (%s, %s, %s, 0, "
                "CASE WHEN %s IS NULL THEN NULL ELSE CURRENT_TIMESTAMP + (%s || ' hours')::interval END) "
                "ON CONFLICT (code) DO UPDATE SET "
                "  rewards = EXCLUDED.rewards, "
                "  max_uses = EXCLUDED.max_uses, "
                "  expires_at = EXCLUDED.expires_at",
                (code.upper(), json.dumps(rewards, ensure_ascii=False), max_uses,
                 hours, hours),
            )
            store.commit()
        finally:
            store.close()
        gift_text = "\n".join(_reward_texts(rewards)) or "🎁 —"
        limit_txt = f"{max_uses} акт." if max_uses else "∞"
        exp_txt = f"5.0 ч" if hours else "∞"
        return (
            f"✅ {bot_mention()} создал промокод «{code}»\n"
            f"🔢 Лимит: {limit_txt}\n"
            f"⏳ Действует: {exp_txt}\n"
            f"\n🎁 Внутри:\n{gift_text}"
        )

    code = raw.split()[0].upper() if raw else ""
    if not code:
        return (
            "🎟️ Укажи промокод!\n\n"
            "Пример: «промо SEYCH»\n"
            "Команда выдаёт тебе подарки от бота 🎁"
        )

    promo = db.promo_get(code)
    if promo is None:
        return "❌ Этого промокода не существует."

    if promo.get("expires_at") and promo["expires_at"] <= datetime.datetime.now(datetime.timezone.utc):
        return "⏰ Этот промокод недействителен — срок вышел."

    if promo["used_count"] >= promo["max_uses"]:
        return "🚫 Этот промокод уже использован все разы."

    if db.promo_already_claimed(code, user["vk_id"]):
        return "⚠️ Вы уже вводили этот промокод."

    if not db.promo_take_claim(code):
        return "🚫 Этот промокод недействителен."

    db.promo_mark_claimed(code, user["vk_id"])
    given = _apply_rewards(user["vk_id"], promo.get("rewards", []))
    if not given:
        return "❌ Не удалось выдать награды промокода 😕"

    return (
        f"🎉 Вы использовали промокод «{code}».\n"
        f"\n🎁 Подарки:\n" + "\n".join(given)
    )
