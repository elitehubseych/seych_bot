
import datetime
import json
import logging
import random
import threading
import uuid
from zoneinfo import ZoneInfo

import re

import db
from config import config
from handlers.coin import _send_event_answer, _temp_chat_message
from handlers.registry import DEAD_SESSION, command
from utils import items
from utils.items import (
    CASES,
    CASE_ALIASES,
    CASE_ORDER,
    ITEMS,
    KEY_PRICE,
    TITLES,
)
from utils.parse import extract_target_id, format_amount
from utils.vk import bot_mention, display_name_by_vk_id, group_info, mention, send_plain, vk

logger = logging.getLogger(__name__)


def _m(user_id):
    return display_name_by_vk_id(user_id)

SESSION_TTL_SECONDS = 600
PAGE_SIZE = 5

SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()

MAGNET_BUFF = set()
_MAGNET_LOCK = threading.Lock()

NOT_YOUR_PHRASES = [
    "Не трогай чужие кейсы 🤨",
    "Это не твой инвентарь 🚶",
    "Чужие предметы не открываем ✋",
    "Мимо, солнышко 😏",
    "Своя раздача есть? Вот и иди с ней 💳",
]

_CASE_ALIAS_CANONICAL = {
    "case_bomzh": "бомж",
    "case_normal": "обычный",
    "case_legendary": "легендарный",
    "case_elite": "элитный",
}

MSK = ZoneInfo("Europe/Moscow")

DEV_ID = int(str(config.DEV_ID).strip()) if str(config.DEV_ID).strip().isdigit() else 0


def _is_dev(vk_id):
    return DEV_ID and int(vk_id) == DEV_ID


_CASE_TYPE = {
    "case_bomzh": "Обычный",
    "case_normal": "Обычный",
    "case_legendary": "Легендарный",
    "case_elite": "Элитный",
}

_TYPE_EMOJI = {
    "Обычный": "📦",
    "Мифический": "🌀",
    "Легендарный": "⚡",
    "Элитный": "💎",
}

# Контекст текущего клика: снекбары шлём активным вызовом
# messages.sendMessageEventAnswer (как в coin._reject), а не через
# тело ответа на callback — так надёжнее.
_EVENT_CTX = threading.local()

# ── кеш статистики титулов (чтобы не долбить COUNT(*) в цикле) ──
_TITLE_STATS_CACHE = {}  # title_key -> (ts, pct, owners)
_TITLE_STATS_LOCK = threading.Lock()
_TITLE_STATS_TTL = 60

# ── кеш инвентаря для batch-проверок (короткий ttl на запрос) ──
_INV_CACHE = {}  # vk_id -> (ts, {key:qty})
_INV_CACHE_LOCK = threading.Lock()
_INV_CACHE_TTL = 3


def _snack(text):
    ctx = getattr(_EVENT_CTX, "event", None)
    if ctx:
        try:
            delivered = _send_event_answer(
                ctx.get("event_id"), ctx.get("user_id"), ctx.get("peer_id"), text
            )
        except Exception:
            delivered = False
        if not delivered:
            _temp_chat_message(ctx.get("peer_id"), text)
    return {"type": "show_snackbar", "text": text}


def _kb(sid, rows):
    buttons = []
    for row in rows:
        line = []
        for item in row:
            label, act = item[0], item[1]
            color = item[2] if len(item) > 2 else "secondary"
            payload = {"type": "inv", "sid": sid, "act": act}
            if len(item) > 3:
                payload["arg"] = item[3]
            line.append({
                "action": {
                    "type": "callback",
                    "label": label,
                    "payload": json.dumps(payload),
                },
                "color": color,
            })
        buttons.append(line)
    return {"inline": True, "buttons": buttons}


# ---------------------------------------------------------------- экраны


def _inventory_pages(vk_id):
    rows = db.get_inventory(vk_id)
    entries = []
    for row in rows:
        key = row["item_key"]
        if key in items.CASES or key.startswith("title_") or key == "key":
            continue
        name = items.display_name(key)
        qty = int(row["qty"])
        entries.append((key, name, qty))
    pages = []
    for start in range(0, max(len(entries), 1), PAGE_SIZE):
        pages.append(entries[start:start + PAGE_SIZE])
    return pages


def _inventory_view(session):
    vk_id = session["vk"]
    pages = session.setdefault("pages", _inventory_pages(vk_id))
    page = min(session.get("page", 0), max(len(pages) - 1, 0))
    session["page"] = page
    current = pages[page] if pages else []

    lines = [
        "🎒 Инвентарь %s" % _m(vk_id),
        "",
        "📦 Предметы:",
    ]
    if current:
        for i, (_key, name, qty) in enumerate(current, 1):
            lines.append("%d. %s ×%d" % (i, name, qty))
    else:
        lines.append("😴 Пока пусто — заглядывай в адвент и кейсы!")
    lines.append("")
    lines.append("📄 Стр. %d/%d" % (page + 1, max(len(pages), 1)))

    kb_rows = []
    for key, name, _qty in current:
        kb_rows.append([(name[:40], "use", "secondary", key)])
    if len(pages) > 1:
        nav = []
        if page > 0:
            nav.append(("◀ Назад", "page_prev"))
        if page < len(pages) - 1:
            nav.append(("Вперёд ▶", "page_next"))
        kb_rows.append(nav)
    kb_rows.append([("❌ Закрыть", "close", "negative")])
    return "\n".join(lines), _kb(session["sid"], kb_rows)


def _keys_view(session):
    vk_id = session["vk"]
    qty = db.item_qty(vk_id, "key")
    text = (
        "🔑 Ключи\n\n"
        "%s, у тебя %s\n\n"
        "Ключи открывают кейсы 🎁\n"
        "Команда «кейсы» — и в путь!"
        % (
            _m(vk_id),
            _plural_keys(qty),
        )
    )
    kb = _kb(session["sid"], [
        [("🛒 Купить ключи", "keys_buy_menu", "primary")],
        [("❌ Закрыть", "close", "negative")],
    ])
    return text, kb


def _plural_keys(n):
    if n % 10 == 1 and n % 100 != 11:
        return "%d ключ" % n
    if n % 10 in (2, 3, 4) and not (n % 100 in (12, 13, 14)):
        return "%d ключа" % n
    return "%d ключей" % n


def _keys_buy_view(session):
    rows = [
        [
            ("🔑 1 — %s" % format_amount(KEY_PRICE), "buy_keys", "primary", 1),
            ("🔑 3 — %s" % format_amount(KEY_PRICE * 3), "buy_keys", "primary", 3),
        ],
        [
            ("🔑 5 — %s" % format_amount(KEY_PRICE * 5), "buy_keys", "primary", 5),
            ("🔑 10 — %s" % format_amount(KEY_PRICE * 10), "buy_keys", "primary", 10),
        ],
        [
            ("⬅ Назад", "keys"),
            ("❌ Закрыть", "close", "negative"),
        ],
    ]
    text = (
        "🛒 Магазин ключей\n\n"
        "Выбирай сколько нужно 👇\n"
        "💰 Цена: %s 💎 за 1 ключ"
        % format_amount(KEY_PRICE)
    )
    return text, _kb(session["sid"], rows)


def _owned_cases(vk_id):
    owned = []
    for ckey in CASE_ORDER:
        qty = db.item_qty(vk_id, ckey)
        if qty > 0:
            owned.append((ckey, qty))
    return owned


def _cases_view(session):
    vk_id = session["vk"]
    owned = _owned_cases(vk_id)
    keys_qty = db.item_qty(vk_id, "key")
    lines = [
        "🎁 ELITE CASE",
        "",
        "📦 Твои кейсы:",
    ]
    if owned:
        for i, (ckey, qty) in enumerate(owned, 1):
            lines.append("%d. %s ×%d" % (i, CASES[ckey]["name"], qty))
    else:
        lines.append("😴 Пока нет ни одного кейса — купи ключи и открой первый!")
    lines.append("")
    lines.append("🔑 Ключей: %d" % keys_qty)

    kb_rows = []
    for ckey, _qty in owned:
        kb_rows.append([(CASES[ckey]["name"][:40], "case_select", "primary", ckey)])
    kb_rows.append([
        ("🔑 Ключи", "keys"),
        ("❌ Закрыть", "close", "negative"),
    ])
    return "\n".join(lines), _kb(session["sid"], kb_rows)


def _case_select_view(session, ckey):
    vk_id = session["vk"]
    case = CASES[ckey]
    have_case = db.item_qty(vk_id, ckey)
    keys_qty = db.item_qty(vk_id, "key")
    enough = keys_qty >= case["keys"] and have_case > 0
    text = (
        "Вы выбрали кейс «%s»\n\n"
        "Что вы собираетесь с ним сделать?\n\n"
        "🔑 Для открытия нужно: %s (у вас %s)\n"
        "📦 В наличии: %d шт."
        % (
            case["name"].replace("📦 ", ""),
            _plural_keys(case["keys"]),
            _plural_keys(keys_qty),
            have_case,
        )
    )
    kb_rows = [
        [("🔓 Открыть", "case_open", "positive" if enough else "secondary", ckey)],
        [("🎁 Подарить", "case_gift_hint", "primary", ckey)],
        [("❓ Что внутри?", "case_inside", "secondary", ckey)],
        [("⬅ Назад", "cases")],
    ]
    return text, _kb(session["sid"], kb_rows)


def _case_inside_view(session, ckey):
    """Показать содержимое кейса с шансами."""
    from utils.items import _ROLLS, TITLES as TITLES_MAP
    case = CASES[ckey]
    rolls = _ROLLS.get(ckey, [])
    total = sum(w for _, _, w in rolls)
    lines = [
        "📦 %s" % case["name"].replace("📦 ", ""),
        "",
        "Содержимое:",
    ]
    for kind, value, weight in rolls:
        pct = round(weight / total * 100, 1) if total else 0
        if kind == "elite":
            label = "%s элитов 💎" % format_amount(value)
        elif kind == "title":
            meta = TITLES_MAP.get(value, {})
            ttype = meta.get("type", "")
            label = "%s (титул · %s)" % (meta.get("name", value), ttype)
        else:
            label = items.display_name(value)
        lines.append("• %s — %s%%" % (label, pct))
    lines.append("")
    lines.append("🔑 Для открытия: %s" % _plural_keys(case["keys"]))
    kb_rows = [
        [("❓ Что внутри?", "case_inside", "secondary", ckey)],
        [("⬅ Назад", "cases")],
    ]
    return "\n".join(lines), _kb(session["sid"], kb_rows)


def _prize_line(kind, value):
    if kind == "elite":
        return "%s элитов 💎" % format_amount(value)
    if kind == "title":
        return "%s (титул)" % TITLES[value]["name"]
    return items.display_name(value)


def _apply_prize(vk_id, kind, value):
    if kind == "elite":
        db.update_balance(vk_id, value)
    elif kind == "title":
        db.add_item(vk_id, value, 1)
    else:
        db.add_item(vk_id, value, 1)


def _case_open_view(session, ckey):
    """Открывает кейс: списывает ключи, выдаёт приз."""
    vk_id = session["vk"]
    case = CASES[ckey]
    keys_qty = db.item_qty(vk_id, "key")

    if db.item_qty(vk_id, ckey) <= 0:
        return None, _snack("Такого кейса у вас больше нет 🤷")
    if keys_qty < case["keys"]:
        return None, _snack(
            "Не хватает ключей! Нужно %s, у вас %s"
            % (_plural_keys(case["keys"]), _plural_keys(keys_qty))
        )

    db.take_item(vk_id, "key", case["keys"])
    db.take_item(vk_id, ckey, 1)

    buffed = False
    with _MAGNET_LOCK:
        buffed = vk_id in MAGNET_BUFF
        if buffed:
            MAGNET_BUFF.discard(vk_id)

    kind, value = items.roll_case(ckey)
    note = ""
    if buffed and kind == "elite":
        value *= 2
        note = "\n🧲 Магнит удвоил выигрыш!"
    elif buffed:
        with _MAGNET_LOCK:
            MAGNET_BUFF.add(vk_id)
        note = "\n🧲 Магнит сработает на элитовом выпадении."

    _apply_prize(vk_id, kind, value)

    text = (
        "🔓 Кейс «%s» открыт!\n\n"
        "%s, вам выпало:\n%s%s"
        % (
            case["name"].replace("📦 ", ""),
            _m(vk_id),
            _prize_line(kind, value),
            note,
        )
    )
    kb = _kb(session["sid"], [
        [("🔁 Открыть ещё", "case_open", "primary", ckey)],
        [("⬅ К кейсам", "cases")],
        [("🎒 Инвентарь", "inventory")],
    ])
    return (text, kb), None


def _advent_state(vk_id):
    adv = db.get_advent(vk_id)
    today = datetime.datetime.now(MSK).date()
    claimed_today = adv["last_claim"] is not None and adv["last_claim"] == today
    return adv, today, claimed_today


def _advent_day_reward(vk_id, cycle, day):
    return items.advent_reward(
        vk_id, cycle, day,
        owns_title=lambda t: db.item_qty(vk_id, t) > 0 or db.get_active_title(vk_id) == t,
    )


def _reward_names(vk_id, cycle, day):
    return ", ".join(_prize_line(k, v) for k, v in _advent_day_reward(vk_id, cycle, day))


def _advent_view(session):
    vk_id = session["vk"]
    adv, today, claimed_today = _advent_state(vk_id)
    cycle, claimed = adv["cycle"], adv["claimed"]

    lines = [
        "%s, Ваш личный адвент календарь!" % _m(vk_id),
        "Собирайте каждый день новые предметы.",
        "",
        "%d дней — %d предметов." % (10, 10),
        "",
    ]
    next_idx = claimed if not claimed_today else None
    for day in range(1, 11):
        idx = day - 1
        names = _reward_names(vk_id, cycle, day)
        if idx < claimed:
            mark = "✅"
        elif idx == next_idx:
            mark = "🎁"
        else:
            mark = "🔒"
        lines.append("%d. %s %s — %s" % (day, mark, "День %d" % day, names))

    lines.append("")
    if claimed_today:
        lines.append("🗓 Новое окошко откроется завтра")
    elif next_idx is not None:
        lines.append("🎁 Окошко готово к открытию!")

    kb_rows = []
    if next_idx is not None:
        kb_rows.append([("🎁 Забрать", "advent_claim", "primary")])
    kb_rows.append([("❌ Закрыть", "close", "negative")])
    return "\n".join(lines), _kb(session["sid"], kb_rows)


# ---------------------------------------------------------------- движок


def _new_session(user, message):
    peer = message.get("peer_id")
    sid = uuid.uuid4().hex[:10]
    session = {
        "sid": sid, "peer": peer, "vk": user["vk_id"],
        "status": "active", "cmid": None,
        # Экраны шлём обычными сообщениями: reply нагружает чат и VK
        "reply_to": None,
        "lock": threading.Lock(),
    }
    with _SESSIONS_LOCK:
        SESSIONS[sid] = session
    _arm_timer(sid)
    return session


ASYNC_SEND = True


def _send_view(session, built):
    text, kb = built
    try:
        kwargs = {
            "peer_ids": [session["peer"]],
            "message": text,
            "random_id": random.randrange(2 ** 31),
            "keyboard": json.dumps(kb),
        }
        reply_to = session.get("reply_to")
        if reply_to:
            kwargs["reply_to"] = reply_to
    except Exception:
        logger.exception("Не удалось собрать экран инвентаря")
        return

    def _work():
        # Сначала пробуем отредактировать существующее сообщение — меньше спама.
        cmid = session.get("cmid")
        if cmid:
            try:
                edit_kwargs = {
                    "peer_id": session["peer"],
                    "conversation_message_id": cmid,
                    "message": text,
                    "keyboard": kwargs["keyboard"],
                }
                if vk.messages.edit(**edit_kwargs):
                    return
            except Exception:
                pass
        _delete_message(session)
        try:
            try:
                sent = vk.messages.send(**kwargs)
            except Exception:
                if "reply_to" not in kwargs:
                    raise
                # Сообщение, на которое отвечали, могло быть удалено —
                # повторяем без reply, чтобы экран всё равно ушёл
                logger.warning("Отправка с reply_to=%s не удалась, повторяем без ответа", kwargs.pop("reply_to"))
                kwargs["random_id"] = random.randrange(2 ** 31)
                sent = vk.messages.send(**kwargs)
            try:
                first = sent[0] if isinstance(sent, list) else sent
                session["cmid"] = first.get("conversation_message_id") or session.get("cmid")
            except Exception:
                pass
        except Exception:
            logger.exception("Не удалось отправить экран инвентаря")

    if ASYNC_SEND:
        # VK ждёт ответ на callback 3 секунды — отправка в фоне,
        # иначе снекбары не доходят и кнопка «грузит»
        threading.Thread(target=_work, daemon=True).start()
    else:
        _work()


def _delete_message(session):
    cmid = session.get("cmid")
    if not cmid:
        return
    try:
        vk.messages.delete(
            peer_id=session["peer"],
            conversation_message_ids=[cmid],
            delete_for_all=1,
        )
    except Exception:
        pass
    session["cmid"] = None


def _arm_timer(sid):
    def _expire():
        with _SESSIONS_LOCK:
            session = SESSIONS.pop(sid, None)
        if session:
            with session["lock"]:
                if session.get("status") == "active":
                    session["status"] = "closed"
                    _delete_message(session)

    timer = threading.Timer(SESSION_TTL_SECONDS, _expire)
    timer.daemon = True
    timer.start()


# ---------------------------------------------------------------- команды


@command("инвентарь", "инв")
def cmd_inventory(user, args, message):
    if args.strip():
        return None
    session = _new_session(user, message)
    _send_view(session, _inventory_view(session))
    return None


def _titles_view(session):
    vk_id = session["owner"]
    active = db.get_active_title(vk_id)
    mine = session["vk"] == vk_id
    lines = [
        "🏆 Титулы %s" % display_name_by_vk_id(vk_id),
        "",
    ]
    kb_rows = []
    row = []
    # batch: один запрос вместо 8 item_qty + кеш на pct/owners
    inv_map = _get_inventory_map(vk_id)
    for tkey, meta in TITLES.items():
        qty = inv_map.get(tkey, 0)
        if qty <= 0:
            continue
        equipped = active == tkey
        title_type = meta.get("type", "Обычный")
        pct, owners = _cached_title_stats(tkey)
        value = meta.get("value", 0)
        status = " (надет)" if equipped else ""
        lines.append("%s%s" % (meta["name"], status))
        lines.append("   Тип: %s · Редкость: %d%% (%d чел.) · Ценность: ~%s 💎" % (
            title_type, pct, owners, format_amount(value)))
        if mine:
            btn_color = "positive" if equipped else "secondary"
            row.append((meta["name"][:40], "title_toggle", btn_color, tkey))
            if len(row) == 2:
                kb_rows.append(row)
                row = []
    if row:
        kb_rows.append(row)
    if not any(l.strip() for l in lines[2:]):
        lines.append("😴 Пока нет ни одного титула — открывай кейсы!")
    if mine:
        kb_rows.append([("❌ Закрыть", "close", "negative")])
    elif not kb_rows:
        kb_rows.append([("❌ Закрыть", "close", "negative")])
    return "\n".join(lines), _kb(session["sid"], kb_rows)


@command("титулы", "титул")
def cmd_titles(user, args, message):
    rest = (args or "").strip().lower()
    # Подкоманды
    if rest.startswith("подарить"):
        return cmd_title_gift(user, args[len("подарить"):].strip(), message)
    if rest.startswith("продать"):
        return cmd_title_sell(user, args[len("продать"):].strip(), message)

    target_id = None
    if rest:
        target_id, remaining = extract_target_id(rest, message.get("reply_message"))
        if remaining.strip():
            return None
    if not target_id or target_id <= 0:
        target_id = user["vk_id"]
    session = _new_session(user, message)
    session["owner"] = target_id
    built = _titles_view(session)
    _send_view(session, built)
    return None


# ── титул подарить ─────────────────────────────────────────────────────────────


def cmd_title_gift(user, args, message):
    raw = (args or "").strip()
    # ── бот: титул подарить бот @user [peer]\nсообщение — все титулы, от имени бота ──
    if raw.lower().startswith("бот"):
        if not _is_dev(user["vk_id"]):
            return None
        after_bot = raw[3:].strip()
        target_id, rest = extract_target_id(after_bot, message.get("reply_message"))
        if not target_id or target_id <= 0:
            return "Формат: титул подарить бот @user [peer_id]\nПример: титул подарить бот @user 2000000001"
        if target_id < 0:
            return "Сообществам титулы не нужны 😕"
        tail = rest or ""
        peer_id, msg_text, err = _parse_bot_title_tail(tail)
        if err == "peer_format":
            return "peer_id должен быть числом (например 2000000001)"
        # все титулы мира
        titles = list(TITLES.items())
        session = _new_session(user, message)
        session["owner"] = user["vk_id"]
        session["gift_target"] = target_id
        session["gift_step"] = "pick"
        session["gift_bot"] = True
        session["gift_peer"] = peer_id
        session["gift_msg"] = msg_text
        lines = [
            "🎁 Подарок от бота для %s" % display_name_by_vk_id(target_id),
            "",
            "Какой титул Вы хотите подарить от имени бота?",
        ]
        kb_rows = []
        row = []
        for tkey, meta in titles:
            row.append((meta["name"][:40], "bgift_pick", "primary", tkey))
            if len(row) == 2:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        kb_rows.append([("❌ Отмена", "close", "negative")])
        _send_view(session, ("\n".join(lines), _kb(session["sid"], kb_rows)))
        return None

    target_id, rest = extract_target_id(args or "", message.get("reply_message"))
    if not target_id or target_id <= 0:
        return "Формат: титул подарить @получатель"
    if target_id == user["vk_id"]:
        return "Нельзя дарить самому себе 😏"
    vk_id = user["vk_id"]
    # batch: один SELECT вместо N
    inv_map = _get_inventory_map(vk_id)
    titles = []
    for tkey, meta in TITLES.items():
        if inv_map.get(tkey, 0) > 0:
            titles.append((tkey, meta))
    if not titles:
        return "У тебя нет титулов для подарка 🤷"
    session = _new_session(user, message)
    session["owner"] = vk_id
    session["gift_target"] = target_id
    session["gift_step"] = "pick"
    lines = [
        "🎁 Подарок для %s" % display_name_by_vk_id(target_id),
        "",
        "Какой титул будем дарить?",
    ]
    kb_rows = []
    row = []
    for tkey, meta in titles:
        row.append((meta["name"][:40], "tgift_pick", "primary", tkey))
        if len(row) == 2:
            kb_rows.append(row)
            row = []
    if row:
        kb_rows.append(row)
    kb_rows.append([("❌ Отмена", "close", "negative")])
    _send_view(session, ("\n".join(lines), _kb(session["sid"], kb_rows)))
    return None


def cmd_title_sell(user, args, message):
    parts = (args or "").strip()
    target_id, rest = extract_target_id(parts, message.get("reply_message"))
    if not target_id or target_id <= 0:
        return "Формат: титул продать @получатель <цена>"
    if target_id == user["vk_id"]:
        return "Нельзя продать самому себе 😏"
    tail = rest.strip()
    price = 0
    if tail:
        digits = "".join(c for c in tail.replace(" ", "") if c.isdigit())
        if digits:
            price = int(digits)
    if price <= 0:
        return "Формат: титул продать @получатель <цена>\nПример: титул продать @user 5000"
    vk_id = user["vk_id"]
    titles = []
    for tkey, meta in TITLES.items():
        if db.item_qty(vk_id, tkey) > 0:
            titles.append((tkey, meta))
    if not titles:
        return "У тебя нет титулов для продажи 🤷"
    session = _new_session(user, message)
    session["owner"] = vk_id
    session["sell_target"] = target_id
    session["sell_price"] = price
    session["sell_step"] = "pick"
    lines = [
        "💰 Продажа титула",
        "",
        "Покупатель: %s" % display_name_by_vk_id(target_id),
        "Цена: %s 💎" % format_amount(price),
        "",
        "Какой титул продаём?",
    ]
    kb_rows = []
    row = []
    for tkey, meta in titles:
        row.append((meta["name"][:40], "tsell_pick", "primary", tkey))
        if len(row) == 2:
            kb_rows.append(row)
            row = []
    if row:
        kb_rows.append(row)
    kb_rows.append([("❌ Отмена", "close", "negative")])
    _send_view(session, ("\n".join(lines), _kb(session["sid"], kb_rows)))
    return None


@command("ключи")
def cmd_keys(user, args, message):
    if args.strip():
        return None
    session = _new_session(user, message)
    _send_view(session, _keys_view(session))
    return None


@command("кейсы")
def cmd_cases(user, args, message):
    if args.strip():
        return None
    session = _new_session(user, message)
    _send_view(session, _cases_view(session))
    return None


@command("адвент")
def cmd_advent(user, args, message):
    if args.strip():
        return None
    session = _new_session(user, message)
    _send_view(session, _advent_view(session))
    return None


# ---------------------------------------------------------------- гифты

_GIFT_ITEM_ALIASES = {
    "ключ": "key", "ключи": "key", "key": "key",
    "подарок": "gift_box",
    "магнит": "magnet",
    "vip": "vip_ticket", "вип": "vip_ticket",
}


def _find_item(query):
    """Точное совпадение по алиасу или названию — без «похожих» слов."""
    q = query.strip().lower()
    if not q:
        return None
    if q in _GIFT_ITEM_ALIASES:
        return _GIFT_ITEM_ALIASES[q]
    for key, meta in list(ITEMS.items()) + list(TITLES.items()):
        if q == meta["name"].lower():
            return key
    return None


def _async_plain(peer_id, text):
    def _w():
        try:
            send_plain(peer_id, text)
        except Exception:
            logger.exception("async send_plain failed peer=%s", peer_id)
    threading.Thread(target=_w, daemon=True).start()


def _parse_bot_item_tail(tail_raw):
    """Хвост вида '<предмет> [peer_id]\\nсообщение'. Возвращает (item_query, peer|None, msg|None, err)."""
    raw = (tail_raw or "").strip()
    if not raw:
        return None, None, None, "no_item"
    if "\n" in raw:
        first, msg = raw.split("\n", 1)
        msg = msg.strip()[:2000] or None
    else:
        first, msg = raw, None
    first = first.strip()
    if not first:
        return None, None, msg, "no_item"
    # кавычки
    m = re.search(r'"([^"]+)"', first)
    if m:
        item_q = m.group(1).strip()
        rest = first.replace(m.group(0), "").strip()
        peer_tok = rest.split()[0] if rest else ""
        if rest and len(rest.split()) > 1:
            return None, None, msg, "format"
    elif "«" in first and "»" in first:
        m2 = re.search(r'«([^»]+)»', first)
        if m2:
            item_q = m2.group(1).strip()
            rest = first.replace(m2.group(0), "").strip()
            peer_tok = rest.split()[0] if rest else ""
            if rest and len(rest.split()) > 1:
                return None, None, msg, "format"
        else:
            return None, None, msg, "format"
    else:
        parts = first.split()
        if len(parts) == 1:
            item_q = parts[0].strip('"\'«»')
            peer_tok = ""
        elif len(parts) == 2:
            if parts[1].isdigit():
                item_q = parts[0].strip('"\'«»')
                peer_tok = parts[1]
            else:
                return None, None, msg, "format"
        else:
            # последний токен peer?
            if parts[-1].isdigit():
                peer_tok = parts[-1]
                item_q = " ".join(parts[:-1]).strip('"\'«»')
                if len(item_q.split()) != 1:
                    return None, None, msg, "format"
            else:
                return None, None, msg, "format"
    if not item_q:
        return None, None, msg, "no_item"
    peer = None
    if peer_tok:
        if not peer_tok.isdigit():
            return None, None, msg, "peer_format"
        try:
            peer = int(peer_tok)
        except Exception:
            return None, None, msg, "peer_format"
    return item_q, peer, msg, None


def _parse_bot_title_tail(tail_raw):
    """Хвост для титул подарить бот: '[peer_id]\\nсообщение'."""
    raw = (tail_raw or "").strip()
    if not raw:
        return None, None, None
    if "\n" in raw:
        first, msg = raw.split("\n", 1)
        msg = msg.strip()[:2000] or None
    else:
        first, msg = raw, None
    first = first.strip()
    peer = None
    if first:
        # may contain peer plus extra spaces? only peer allowed
        parts = first.split()
        if len(parts) != 1 or not parts[0].isdigit():
            return None, None, "peer_format"
        peer = int(parts[0])
    return peer, msg, None


def _bot_item_announce(target_id, item_key, case_key, peer_id, msg_text):
    bot = bot_mention()
    taker = _m(target_id)
    if case_key:
        cname = CASES[case_key]["name"]  # "📦 Кейс «Элитный»"
        boxname = cname.replace("📦 ", "").strip()
        typ = _CASE_TYPE.get(case_key, "Обычный")
        typ_emoji = _TYPE_EMOJI.get(typ, "⭐")
        text = (
            f"🎁 {bot} подарил кейс «{boxname}» {taker}\n"
            f"{typ_emoji} Тип: {typ}"
        )
    else:
        iname = ITEMS[item_key]["name"] if item_key in ITEMS else item_key
        typ = "Обычный"
        if item_key in TITLES:
            typ = TITLES[item_key].get("type", "Обычный")
        typ_emoji = _TYPE_EMOJI.get(typ, "⭐")
        text = (
            f"🎁 {bot} подарил \"{iname}\" {taker}\n"
            f"{typ_emoji} Тип: {typ}"
        )
    if msg_text:
        text += f"\n💬 Сообщение: {msg_text}"
    if peer_id:
        _async_plain(peer_id, text)
    dm = text
    if case_key:
        dm += "\n\n📦 Для использования: кейсы"
    _async_plain(target_id, dm)


def _bot_title_announce(target_id, title_key, peer_id, msg_text):
    bot = bot_mention()
    taker = _m(target_id)
    meta = TITLES[title_key]
    pct, owners = _cached_title_stats(title_key)
    ttype = meta.get("type", "Обычный")
    typ_emoji = _TYPE_EMOJI.get(ttype, "⭐")
    text = (
        f"👑 {bot} присвоил титул {meta['name']} {taker}.\n"
        f"{typ_emoji} Тип: {ttype}\n"
        f"💎 Ценность: {format_amount(meta.get('value', 0))}\n"
        f"🔥 Редкость: {pct}% ({owners} чел.)"
    )
    if msg_text:
        text += f"\n💬 Сообщение: {msg_text}"
    if peer_id:
        _async_plain(peer_id, text)
    _async_plain(target_id, text)


def _gift_case_to(user, target_id, ckey):
    """Передаёт кейс target_id, возвращает текст результата."""
    if target_id == user["vk_id"]:
        return "Себе кейс не подарить 🙃"
    if target_id < 0:
        return "Сообществам кейсы не нужны 😕"

    short = CASES[ckey]["name"].replace("📦 ", "")

    if not db.take_item(user["vk_id"], ckey, 1):
        return "У тебя нет кейса «%s» 🤷" % CASES[ckey]["name"]

    db.get_user(target_id)
    db.add_item(target_id, ckey, 1)

    giver = _m(user["vk_id"])
    taker = _m(target_id)
    return (
        '%s подарил кейс "%s" %s.\n'
        "Для использования: кейсы"
        % (giver, short, taker)
    )


@command("гифт")
def cmd_gift(user, args, message):
    # ── бот-подарки: гифт бот @user предмет [peer]\nсообщение ──
    raw = args or ""
    if raw.strip().lower().startswith("бот"):
        if not _is_dev(user["vk_id"]):
            return None
        after_bot = raw.strip()[3:].strip()
        target_id, rest = extract_target_id(after_bot, message.get("reply_message"))
        if not target_id or target_id <= 0:
            return "Формат: гифт бот @user <предмет> [peer_id]\nПример: гифт бот @user ключ 2000000001"
        if target_id < 0:
            return "Сообществам подарки не нужны 😕"
        tail = rest or ""
        # хвост может содержать перенос строки уже — rest его сохранил
        # но если команда была 'гифт бот @user ключ\nтекст', extract_target_id вернёт rest='ключ\nтекст'
        item_q, peer_id, msg_text, err = _parse_bot_item_tail(tail)
        if err == "no_item":
            return "Укажи предмет: гифт бот @user ключ"
        if err == "format":
            return "Формат: гифт бот @user <предмет> [peer_id]\nПример: гифт бот @user ключ 2000000001"
        if err == "peer_format":
            return "peer_id должен быть числом (например 2000000001)"
        # определяем что дарим: кейс или предмет
        ckey = CASE_ALIASES.get((item_q or "").lower().strip("\\.,!?").rstrip("\\").strip())
        if ckey is not None:
            db.get_user(target_id)
            db.add_item(target_id, ckey, 1)
            _bot_item_announce(target_id, None, ckey, peer_id, msg_text)
            taker = _m(target_id)
            bot = bot_mention()
            cname = CASES[ckey]["name"].replace("📦 ", "")
            typ = _CASE_TYPE.get(ckey, "Обычный")
            base = f"🎁 {bot} подарил кейс «{cname}» {taker}\n{_TYPE_EMOJI.get(typ,'⭐')} Тип: {typ}"
            if msg_text:
                base += f"\n💬 Сообщение: {msg_text}"
            return "✅ " + base
        item_key = _find_item(item_q)
        if item_key is None:
            return 'Предмет «%s» не найден. Пример: гифт бот @user ключ' % (item_q or "")
        db.get_user(target_id)
        db.add_item(target_id, item_key, 1)
        _bot_item_announce(target_id, item_key, None, peer_id, msg_text)
        taker = _m(target_id)
        bot = bot_mention()
        iname = ITEMS[item_key]["name"] if item_key in ITEMS else item_key
        typ = TITLES[item_key].get("type", "Обычный") if item_key in TITLES else "Обычный"
        base = f"🎁 {bot} подарил \"{iname}\" {taker}\n{_TYPE_EMOJI.get(typ,'⭐')} Тип: {typ}"
        if msg_text:
            base += f"\n💬 Сообщение: {msg_text}"
        return "✅ " + base

    target_id, rest = extract_target_id(args, message.get("reply_message"))
    if target_id is None:
        return "Укажи получателя и предмет: гифт @user ключ"
    target_id, rest = target_id, (rest or "").strip()
    # Ровно одно слово-предмет: «гифт @user ключ привет» — ошибка формата
    if len(rest.split()) != 1:
        return 'Формат: гифт @user <предмет> (одно слово)'
    if not rest:
        return 'Укажи предмет: гифт @user "ключ"'

    word = rest.lower().strip("\\.,!?")
    case_key = CASE_ALIASES.get(word.rstrip("\\").strip())
    if case_key is not None:
        # «гифт @user обычный» — это подарок кейса
        return _gift_case_to(user, target_id, case_key)

    if target_id == user["vk_id"]:
        return "Себе дарить нечего 🙃"
    if target_id < 0:
        return "Сообществам подарки не нужны 😕"

    item_key = _find_item(rest)
    if item_key is None:
        return 'Предмет «%s» не найден. Пример: гифт @user ключ' % rest.split()[0]

    if not db.take_item(user["vk_id"], item_key, 1):
        return "У тебя нет предмета «%s» 🤷" % ITEMS[item_key]["name"]

    db.get_user(target_id)
    db.add_item(target_id, item_key, 1)

    giver = _m(user["vk_id"])
    taker = _m(target_id)
    return (
        '%s подарил предмет "%s" %s\n'
        "Предмет был внесен в инвентарь."
        % (giver, ITEMS[item_key]["name"], taker)
    )


@command("гифткейс")
def cmd_gift_case(user, args, message):
    raw = args or ""
    # ── бот: гифткейс бот @user бомж [peer]\nсообщение / с кавычками ──
    if raw.strip().lower().startswith("бот"):
        if not _is_dev(user["vk_id"]):
            return None
        after_bot = raw.strip()[3:].strip()
        target_id, rest = extract_target_id(after_bot, message.get("reply_message"))
        if not target_id or target_id <= 0:
            return "Формат: гифткейс бот @user <кейс> [peer_id]\nПример: гифткейс бот @user бомж 2000000001"
        if target_id < 0:
            return "Сообществам кейсы не нужны 😕"
        tail = rest or ""
        item_q, peer_id, msg_text, err = _parse_bot_item_tail(tail)
        if err == "no_item":
            return "Укажи кейс: гифткейс бот @user бомж"
        if err in ("format", "peer_format"):
            return "Формат: гифткейс бот @user бомж|обычный|легендарный|элитный [peer_id]"
        ckey = CASE_ALIASES.get((item_q or "").lower().strip("\\.,!?").rstrip("\\").strip())
        if ckey is None:
            return "Не понял какой кейс. Формат: гифткейс бот @user бомж\\обычный\\легендарный\\элитный"
        db.get_user(target_id)
        db.add_item(target_id, ckey, 1)
        _bot_item_announce(target_id, None, ckey, peer_id, msg_text)
        bot = bot_mention()
        taker = _m(target_id)
        cname = CASES[ckey]["name"].replace("📦 ", "")
        typ = _CASE_TYPE.get(ckey, "Обычный")
        base = f"🎁 {bot} подарил кейс «{cname}» {taker}\n{_TYPE_EMOJI.get(typ,'⭐')} Тип: {typ}"
        if msg_text:
            base += f"\n💬 Сообщение: {msg_text}"
        return "✅ " + base
    target_id, rest = extract_target_id(args, message.get("reply_message"))
    tail = (rest or "").strip().lower()
    if target_id is None or not tail:
        return "Формат: гифткейс @user бомж\\обычный\\легендарный\\элитный"
    # Ровно одно слово-тип кейса, лишний текст — отказ
    if len(tail.split()) != 1:
        return "Формат: гифткейс @user бомж\\обычный\\легендарный\\элитный"

    word = tail.strip("\\.,!?")
    ckey = CASE_ALIASES.get(word.rstrip("\\").strip())
    if ckey is None:
        return "Не понял какой кейс. Формат: гифткейс @user бомж\\обычный\\легендарный\\элитный"

    return _gift_case_to(user, target_id, ckey)


# ---------------------------------------------------------------- колбеки


def handle_message_event(data):
    obj = data.get("object") or {}
    payload_raw = obj.get("payload")
    if not payload_raw:
        return None
    try:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
    except Exception:
        return None
    if payload.get("type") != "inv":
        return None

    sid = payload.get("sid")
    act = payload.get("act")
    arg = payload.get("arg")
    user_id = data.get("user_id") or obj.get("user_id")

    _EVENT_CTX.event = {
        "event_id": obj.get("event_id") or data.get("event_id"),
        "user_id": user_id,
        "peer_id": data.get("peer_id") or obj.get("peer_id"),
    }
    try:
        with _SESSIONS_LOCK:
            session = SESSIONS.get(sid)
        if session is None or session.get("status") != "active":
            return DEAD_SESSION

        clicked_cmid = obj.get("conversation_message_id")
        if clicked_cmid:
            session["cmid"] = clicked_cmid

        with session["lock"]:
            if session.get("status") != "active":
                return None
            if user_id != session["vk"]:
                return _snack(random.choice(NOT_YOUR_PHRASES))
            try:
                return _route(session, act, arg)
            except Exception:
                logger.exception("Ошибка инвентаря act=%s", act)
                return _snack("Что-то сломалось, попробуй ещё раз 🛠")
    finally:
        _EVENT_CTX.event = None


def _route(session, act, arg):
    vk_id = session["vk"]

    if act == "close":
        session["status"] = "closed"
        _delete_message(session)
        with _SESSIONS_LOCK:
            SESSIONS.pop(session["sid"], None)
        return None

    if act == "inventory":
        session["pages"] = _inventory_pages(vk_id)
        session["page"] = 0
        _send_view(session, _inventory_view(session))
        return None

    if act == "page_next":
        pages = session.get("pages") or _inventory_pages(vk_id)
        session["page"] = min(session.get("page", 0) + 1, len(pages) - 1)
        _send_view(session, _inventory_view(session))
        return None

    if act == "page_prev":
        session["page"] = max(session.get("page", 0) - 1, 0)
        _send_view(session, _inventory_view(session))
        return None

    if act == "use":
        return _use_item(session, arg)

    if act == "keys":
        _send_view(session, _keys_view(session))
        return None

    if act == "keys_buy_menu":
        _send_view(session, _keys_buy_view(session))
        return None

    if act == "buy_keys":
        return _buy_keys(session, arg)

    if act == "cases":
        _send_view(session, _cases_view(session))
        return None

    if act == "case_select":
        _send_view(session, _case_select_view(session, arg))
        return None

    if act == "case_inside":
        _send_view(session, _case_inside_view(session, arg))
        return None

    if act == "case_gift_hint":
        alias = _CASE_ALIAS_CANONICAL.get(arg, arg)
        return _snack(
            "Для этого воспользуйтесь командой:\n"
            "гифткейс @получатель %s" % alias
        )

    if act == "case_open":
        result, snack = _case_open_view(session, arg)
        if snack:
            return snack
        _send_view(session, result)
        return None

    if act == "advent_claim":
        return _claim_advent(session)

    if act == "title_toggle":
        owner = session.get("owner", vk_id)
        if owner != vk_id:
            return _snack("Можно менять только свой титул 😉")
        title_key = arg
        if title_key not in TITLES or db.item_qty(vk_id, title_key) <= 0:
            return _snack("У тебя нет этого титула 🤷")
        current = db.get_active_title(vk_id)
        if current == title_key:
            db.set_active_title(vk_id, None)
            _send_view(session, _titles_view(session))
            return _snack("Титул «%s» снят" % TITLES[title_key]["name"])
        db.set_active_title(vk_id, title_key)
        _send_view(session, _titles_view(session))
        return _snack("👑 Титул «%s» надет!" % TITLES[title_key]["name"])

    if act == "bgift_pick":
        return _bot_tgift_pick(session, arg)

    if act == "bgift_confirm":
        return _bot_tgift_confirm(session)

    if act == "tgift_pick":
        return _tgift_pick(session, arg)

    if act == "tgift_confirm":
        return _tgift_confirm(session)

    if act == "tsell_pick":
        return _tsell_pick(session, arg)

    if act == "tsell_confirm":
        return _tsell_confirm(session)

    if act == "tsell_buy":
        return _tsell_buy(session)

    return None


def _cached_title_stats(title_key):
    import time as _t
    now = _t.monotonic()
    with _TITLE_STATS_LOCK:
        ent = _TITLE_STATS_CACHE.get(title_key)
        if ent and now - ent[0] < _TITLE_STATS_TTL:
            return ent[1], ent[2]
    pct = db.title_rarity_percent(title_key)
    owners = db.title_owners_count(title_key)
    with _TITLE_STATS_LOCK:
        _TITLE_STATS_CACHE[title_key] = (now, pct, owners)
    return pct, owners


def _title_rarity_line(title_key):
    """Строка редкости титула."""
    from utils.items import TITLE_TYPE_ORDER
    meta = TITLES[title_key]
    title_type = meta.get("type", "Обычный")
    pct, owners = _cached_title_stats(title_key)
    return title_type, pct, owners


def _get_inventory_map(vk_id):
    """Batch: один SELECT вместо N item_qty. Короткий кеш на 3 сек."""
    import time as _t
    now = _t.monotonic()
    with _INV_CACHE_LOCK:
        ent = _INV_CACHE.get(vk_id)
        if ent and now - ent[0] < _INV_CACHE_TTL:
            return ent[1]
    rows = db.get_inventory(vk_id)
    mp = {r["item_key"]: int(r["qty"]) for r in rows}
    with _INV_CACHE_LOCK:
        _INV_CACHE[vk_id] = (now, mp)
    return mp


def _bot_tgift_pick(session, title_key):
    # только dev может жать свои кнопки
    if not _is_dev(session.get("vk")):
        return _snack("Только разработчик может дарить от бота")
    target_id = session.get("gift_target")
    if not target_id or title_key not in TITLES:
        return _snack("Что-то пошло не так 🤷")
    if not session.get("gift_bot"):
        return _snack("Сессия не для бота")
    meta = TITLES[title_key]
    title_type, pct, owners = _title_rarity_line(title_key)
    session["gift_title"] = title_key
    session["gift_step"] = "confirm"
    lines = [
        "🎁 Подарок от бота для %s" % display_name_by_vk_id(target_id),
        "",
        "Титул: %s" % meta["name"],
        "Тип: %s" % title_type,
        "Редкость: %d%% (%d чел.)" % (pct, owners),
        "Ценность: ~%s 💎" % format_amount(meta.get("value", 0)),
        "",
        "Подарить этот титул от имени бота?",
    ]
    kb_rows = [
        [("✅ Подарить от бота", "bgift_confirm", "positive")],
        [("❌ Отмена", "close", "negative")],
    ]
    _send_view(session, ("\n".join(lines), _kb(session["sid"], kb_rows)))
    return None


def _bot_tgift_confirm(session):
    if not _is_dev(session.get("vk")):
        return _snack("Только разработчик")
    target_id = session.get("gift_target")
    title_key = session.get("gift_title")
    if not target_id or not title_key:
        return _snack("Что-то пошло не так 🤷")
    if title_key not in TITLES:
        return _snack("Титул не найден")
    # от имени бота — просто выдаём, без списания у дарителя
    db.get_user(target_id)
    ok = db.add_item(target_id, title_key, 1)
    if not ok:
        return _snack("Не удалось выдать титул 😕")
    meta = TITLES[title_key]
    peer_id = session.get("gift_peer")
    msg_text = session.get("gift_msg")
    # анонсы: в чат peer_id + ЛС
    _bot_title_announce(target_id, title_key, peer_id, msg_text)
    # закрываем экран подарка
    session["status"] = "closed"
    _delete_message(session)
    with _SESSIONS_LOCK:
        SESSIONS.pop(session["sid"], None)
    # инвалидируем кеш титулов на всякий
    with _TITLE_STATS_LOCK:
        _TITLE_STATS_CACHE.pop(title_key, None)
    with _INV_CACHE_LOCK:
        _INV_CACHE.pop(target_id, None)
    return _snack("👑 Титул %s выдан %s от бота!" % (meta["name"], display_name_by_vk_id(target_id)))


def _tgift_pick(session, title_key):
    vk_id = session["vk"]
    target_id = session.get("gift_target")
    if not target_id or title_key not in TITLES:
        return _snack("Что-то пошло не так 🤷")
    if db.item_qty(vk_id, title_key) <= 0:
        return _snack("У тебя нет этого титула 🤷")
    meta = TITLES[title_key]
    title_type, pct, owners = _title_rarity_line(title_key)
    session["gift_title"] = title_key
    session["gift_step"] = "confirm"
    lines = [
        "🎁 Подарок для %s" % display_name_by_vk_id(target_id),
        "",
        "Титул: %s" % meta["name"],
        "Тип: %s" % title_type,
        "Редкость: %d%% (%d чел.)" % (pct, owners),
        "",
        "Подарить этот титул?",
    ]
    kb_rows = [
        [("✅ Подарить", "tgift_confirm", "positive")],
        [("❌ Отмена", "close", "negative")],
    ]
    _send_view(session, ("\n".join(lines), _kb(session["sid"], kb_rows)))
    return None


def _tgift_confirm(session):
    vk_id = session["vk"]
    target_id = session.get("gift_target")
    title_key = session.get("gift_title")
    if not target_id or not title_key:
        return _snack("Что-то пошло не так 🤷")
    if db.item_qty(vk_id, title_key) <= 0:
        return _snack("Титул уже потерян 🤷")
    meta = TITLES[title_key]
    ok = db.transfer_item(vk_id, target_id, title_key)
    if not ok:
        return _snack("Не удалось передать титул 😕")
    _send_view(session, _titles_view(session))
    try:
        send_plain(
            target_id,
            "🎉 %s подарил(а) тебе титул «%s»!\n"
            "Посмотри: титулы" % (display_name_by_vk_id(vk_id), meta["name"]),
        )
    except Exception:
        pass
    return _snack("🎁 Титул «%s» подарлен %s!" % (
        meta["name"], display_name_by_vk_id(target_id)))


def _tsell_pick(session, title_key):
    vk_id = session["vk"]
    target_id = session.get("sell_target")
    price = session.get("sell_price", 0)
    if not target_id or title_key not in TITLES or price <= 0:
        return _snack("Что-то пошло не так 🤷")
    if db.item_qty(vk_id, title_key) <= 0:
        return _snack("У тебя нет этого титула 🤷")
    meta = TITLES[title_key]
    title_type, pct, owners = _title_rarity_line(title_key)
    buyer = db.get_user_readonly(target_id)
    buyer_balance = buyer["balance"] if buyer else 0
    session["sell_title"] = title_key
    session["sell_step"] = "confirm"
    enough = buyer_balance >= price
    lines = [
        "💰 Продажа титула",
        "",
        "Титул: %s" % meta["name"],
        "Тип: %s" % title_type,
        "Редкость: %d%% (%d чел.)" % (pct, owners),
        "Ценность: ~%s 💎" % format_amount(meta.get("value", 0)),
        "",
        "Покупатель: %s" % display_name_by_vk_id(target_id),
        "Цена: %s 💎" % format_amount(price),
        "",
        "⚠️ Покупатель должен подтвердить покупку.",
    ]
    if not enough:
        lines.append(
            "⚠️ У покупателя недостаточно элитов (%s 💎)"
            % format_amount(buyer_balance)
        )
    kb_rows = [
        [("✅ Продать", "tsell_confirm", "positive" if enough else "secondary")],
        [("❌ Отмена", "close", "negative")],
    ]
    _send_view(session, ("\n".join(lines), _kb(session["sid"], kb_rows)))
    return None


def _tsell_confirm(session):
    vk_id = session["vk"]
    target_id = session.get("sell_target")
    title_key = session.get("sell_title")
    price = session.get("sell_price", 0)
    if not target_id or not title_key or price <= 0:
        return _snack("Что-то пошло не так 🤷")
    if db.item_qty(vk_id, title_key) <= 0:
        return _snack("Титул уже потерян 🤷")
    meta = TITLES[title_key]
    title_type, pct, owners = _title_rarity_line(title_key)
    buyer = db.get_user_readonly(target_id)
    buyer_balance = buyer["balance"] if buyer else 0
    if buyer_balance < price:
        return _snack(
            "❌ У покупателя недостаточно элитов! (%s 💎)"
            % format_amount(buyer_balance)
        )
    buyer_name = display_name_by_vk_id(target_id)
    seller_name = display_name_by_vk_id(vk_id)
    lines = [
        "💰 Покупка титула",
        "",
        "%s, вы собрались купить титул" % buyer_name,
        "«%s» у %s за %s 💎" % (meta["name"], seller_name, format_amount(price)),
        "",
        "Тип: %s" % title_type,
        "Редкость: %d%% (%d чел.)" % (pct, owners),
        "Ценность: ~%s 💎" % format_amount(meta.get("value", 0)),
    ]
    session["sell_step"] = "buy"
    session["sell_seller"] = vk_id
    kb_rows = [
        [("✅ Купить", "tsell_buy", "positive")],
        [("❌ Отмена", "close", "negative")],
    ]
    try:
        buyer_session = {
            "sid": uuid.uuid4().hex[:10],
            "peer": target_id,
            "vk": target_id,
            "status": "active",
            "cmid": None,
            "reply_to": None,
            "lock": threading.Lock(),
            "sell_target": target_id,
            "sell_title": title_key,
            "sell_price": price,
            "sell_seller": vk_id,
            "sell_step": "buy",
        }
        with _SESSIONS_LOCK:
            SESSIONS[buyer_session["sid"]] = buyer_session
        _send_view(buyer_session, (
            "\n".join(lines),
            _kb(buyer_session["sid"], kb_rows),
        ))
    except Exception:
        pass
    return _snack("⏳ Ожидаем подтверждения от %s..." % buyer_name)


def _tsell_buy(session):
    vk_id = session["vk"]
    target_id = session.get("sell_target")
    title_key = session.get("sell_title")
    price = session.get("sell_price", 0)
    seller_id = session.get("sell_seller")
    if not target_id or not title_key or price <= 0 or not seller_id:
        return _snack("Что-то пошло не так 🤷")
    if vk_id != target_id:
        return _snack("Это не для тебя 🤷")
    if db.item_qty(seller_id, title_key) <= 0:
        return _snack("Продавец уже продал этот титул 😕")
    buyer = db.get_user_readonly(vk_id)
    if not buyer or buyer["balance"] < price:
        return _snack(
            "❌ Недостаточно элитов! Нужно %s 💎" % format_amount(price)
        )
    meta = TITLES[title_key]
    ok = db.transfer_item(seller_id, vk_id, title_key)
    if not ok:
        return _snack("Не удалось передать титул 😕")
    db.update_balance(vk_id, -price)
    db.update_balance(seller_id, price)
    _send_view(session, _titles_view(session))
    try:
        send_plain(
            seller_id,
            "💰 %s купил(а) у тебя титул «%s» за %s 💎!" % (
                display_name_by_vk_id(vk_id), meta["name"], format_amount(price)),
        )
    except Exception:
        pass
    return _snack("✅ Титул «%s» куплен у %s за %s 💎!" % (
        meta["name"], display_name_by_vk_id(seller_id), format_amount(price)))


def _buy_keys(session, amount):
    amount = int(amount)
    cost = KEY_PRICE * amount
    balance = db.get_user(session["vk"])["balance"]
    if balance < cost:
        return _snack(
            "❌ Не хватает элитов! Нужно %s, у тебя %s"
            % (format_amount(cost), format_amount(balance))
        )
    db.update_balance(session["vk"], -cost)
    db.add_item(session["vk"], "key", amount)
    _send_view(session, _keys_view(session))
    return _snack("✅ Куплено %s за %s 💎" % (_plural_keys(amount), format_amount(cost)))


def _use_item(session, item_key):
    vk_id = session["vk"]
    if db.item_qty(vk_id, item_key) <= 0:
        return _snack("Этого предмета больше нет 🤷")

    if item_key in TITLES:
        current = db.get_active_title(vk_id)
        if current == item_key:
            db.set_active_title(vk_id, None)
            _send_view(session, _inventory_view(session))
            return _snack('Титул «%s» снят' % TITLES[item_key]["name"])
        db.set_active_title(vk_id, item_key)
        _send_view(session, _inventory_view(session))
        return _snack('👑 Титул «%s» надет!' % TITLES[item_key]["name"])

    if item_key == "gift_box":
        db.take_item(vk_id, "gift_box", 1)
        prize = random.choice((200, 300, 500, 700, 1000, "key"))
        if prize == "key":
            db.add_item(vk_id, "key", 1)
            won = "🔑 Ключ!"
        else:
            db.update_balance(vk_id, prize)
            won = "%s элитов 💎" % format_amount(prize)
        _send_view(session, _inventory_view(session))
        return _snack("🎁 Подарок открыт: %s" % won)

    if item_key == "magnet":
        db.take_item(vk_id, "magnet", 1)
        with _MAGNET_LOCK:
            MAGNET_BUFF.add(vk_id)
        return _snack("🧲 Магнит активен: следующий кейс удачнее!")

    flavor = {
        "key": "Ключ используется в ELITE CASE → команда «кейсы»",
        "vip_ticket": "🎫 Пригодится для VIP-казино. Скоро...",
        "medal_bronze": "🥉 Красивая медалька для коллекции!",
        "medal_silver": "🥈 Блестит почти как золото!",
        "medal_gold": "🥇 Настоящее золото ELITE!",
        "crown": "👑 Корона чата — предмет престижа!",
    }
    return _snack(flavor.get(item_key, "Этот предмет пока нельзя использовать"))


def _claim_advent(session):
    vk_id = session["vk"]
    adv, today, claimed_today = _advent_state(vk_id)
    if claimed_today:
        return _snack("Сегодня уже забрал! Жди 00:00 МСК ⏰")

    cycle, day = adv["cycle"], adv["claimed"] + 1
    grants = _advent_day_reward(vk_id, cycle, day)
    new_cycle = adv["cycle"]
    new_claimed = adv["claimed"] + 1
    if new_claimed >= 10:
        new_cycle += 1
        new_claimed = 0

    db.save_advent_claim(vk_id, new_cycle, new_claimed, today)
    for kind, value in grants:
        _apply_prize(vk_id, kind, value)

    _send_view(session, _advent_view(session))
    won = ", ".join(_prize_line(k, v) for k, v in grants)
    extra = " 🎉 Календарь обновлён!" if new_claimed == 0 else ""
    return _snack("🎁 День %d забран: %s%s" % (day, won, extra))
