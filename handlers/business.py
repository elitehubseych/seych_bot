
import datetime
import json
import random
import threading
import time
import uuid

import db
from config import config
from handlers.registry import command
from utils.parse import extract_target_id, format_amount, parse_amount
from utils.vk import display_name_by_vk_id, mention, send_plain, vk

CHAT_PEER_ID_MIN = 2000000000
BOT_OWNER = 0
SESSION_TTL_SECONDS = 600

_NOT_YOUR_PHRASES = [
    "Это разве твой бизнес? 🤨",
    "Сначала купи бизнес, а потом тыкай 😏",
    "Куда тыкнул? Своё открывай! 🚫",
    "Руки прочь от чужого бизнеса! ✋",
    "А свой бизнес когда? 😐",
    "Ты тут не владелец, проходи мимо 🚶",
    "Эта кнопка тебя не зовала 👀",
    "Не твой бизнес — не твоя кнопка 🙅",
    "Чужие кнопки не нажимай! 🙈",
    "Тут надо быть владельцем, а ты кто? 🤡",
    "Бизнес не твой, кнопка тоже 🚧",
    "Арендуешь? Нет? Тогда не тыкай 🏠",
    "Лапки вверх, руки от бизнеса! 🐱",
    "Ты в чужом бизнесе как鸡 в чужом супе 🍲",
    "Купи бизнес, потом приходи 😎",
]

PAY_WINDOW_DAYS = 4
GRACE_DAYS = 3
SALE_COMMISSION_PCT = 2
BOT_GOVERNMENT_PRICE_PCT = 50

try:
    _BOT_VK_ID = -abs(int(str(config.ID_GROUP).strip()))
except Exception:
    _BOT_VK_ID = 0

def _is_bot(vk_id):
    return _BOT_VK_ID != 0 and int(vk_id) == _BOT_VK_ID

BASE_PCT = 15

UPGRADES = [
    ("pct", 30, 50_000),
    ("subsidy", -35, 100_000),
    ("pct", 35, 125_000),
    ("pct", 45, 150_000),
    ("pct", 50, 350_000),
]

BIZ = {
    "bank": {
        "kind": "bank",
        "name": "Elite Bank",
        "emoji": "🏦",
        "price": 5_000_000,
        "fee": 35_000,
        "income_desc": "процент с каждого выплаченного кредита и изъятого при просрочке",
    },
    "blackjack": {
        "kind": "blackjack",
        "name": "BlackJack",
        "emoji": "🃏",
        "price": 7_000_000,
        "fee": 50_000,
        "income_desc": "процент с каждой раздачи за столом",
    },
    "coin": {
        "kind": "coin",
        "name": "Монетка",
        "emoji": "🪙",
        "price": 2_500_000,
        "fee": 25_000,
        "income_desc": "процент от банка каждой дуэли орёл/решка",
    },
    "roulette": {
        "kind": "roulette",
        "name": "Рулетка",
        "emoji": "🎡",
        "price": 3_500_000,
        "fee": 30_000,
        "income_desc": "процент с оборота каждого спина",
    },
}

SELF_LOAN_PHRASES = [
    "Брать кредит у самого себя — очень оригинально 🤡",
    "Ты думаешь обыграть систему? Система — это ты 😏",
    "Левая рука не даст в долг правой ✋",
    "Свой банкир остаётся без кредита 🙅",
    "Кредит у себя? Скажи это своему юристу 💼",
    "Интересно было бы посмотреть на этот договор 📜",
    "Банк не выдаёт кредиты собственному директору 🎩",
    "Попробуй занять у кого-нибудь другого 😄",
    "Так можно весь банк забрать себе! Почти вышло 🤏",
    "Конфликт интересов детектед ⚠️",
    "Ты и владелец, и клиент? Смело. Но нет ❌",
    "Директор банка, касса закрыта для тебя 🔒",
    "Сначала продай банк, потом бери в нём кредит 😉",
    "Сам себе проценты капать не будут 🚫",
    "Гениальный план, но нет 💅",
]

SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()
_EVENT_CTX = threading.local()

DEALS = {}
_DEALS_LOCK = threading.Lock()
_DEAL_TTL_SECONDS = 900

_last_maintenance = {}
_MAINTENANCE_LOCK = threading.Lock()


def current_pct(upgrades):
    pct = BASE_PCT
    for i in range(min(int(upgrades or 0), len(UPGRADES))):
        kind, value, _price = UPGRADES[i]
        if kind == "pct":
            pct = value
    return pct


def has_subsidy(upgrades):
    return int(upgrades or 0) >= 2


def monthly_fee(upgrades, kind=None):
    fee = BIZ[kind]["fee"] if kind else 0
    if has_subsidy(upgrades):
        fee = fee * (100 + UPGRADES[1][1]) // 100
    return fee


def _add_month(dt, months=1):
    month_index = dt.year * 12 + dt.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(dt.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    if dt.tzinfo is not None:
        return dt.replace(year=year, month=month, day=day)
    return dt.replace(year=year, month=month, day=day)


def _fmt_date(dt):
    return dt.astimezone().strftime("%d.%m")


def _fmt_datetime(dt):
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def charge(chat_id, kind, turnover, client_ids):
    try:
        if not chat_id or chat_id < CHAT_PEER_ID_MIN or turnover <= 0:
            return
        biz = db.biz_get(chat_id, kind)
        if biz is None:
            biz = db.biz_ensure(chat_id, kind)
        if biz is None:
            return
        owner_id = int(biz.get("owner_vk") or 0)
        ids = [v for v in (client_ids or []) if v and int(v) > 0]
        if owner_id:
            ids = [v for v in ids if int(v) != owner_id]
        if not ids:
            return
        cut = int(turnover) * current_pct(biz["upgrades"]) // 100
        db.biz_turnover(chat_id, kind, ids, cut)
    except Exception:
        pass
    finally:
        try:
            maybe_maintenance(chat_id, throttled=True)
        except Exception:
            pass


def is_bank_owner(chat_id, vk_id):
    biz = db.biz_get(chat_id, "bank")
    return bool(biz and biz["owner_vk"] == vk_id)


def self_loan_phrase():
    return random.choice(SELF_LOAN_PHRASES)


def _load_meta(biz):
    try:
        meta = json.loads(biz["sale_info"] or "{}")
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta


def _save_meta(chat_id, kind, meta):
    db.biz_update(chat_id, kind, sale_info=meta)


def _seize_business(chat_id, kind, biz):
    owner = biz["owner_vk"]
    fine = BIZ[kind]["fee"]
    from db import get_user_readonly, update_balance

    pocket_taken = 0
    try:
        pocket_taken = int(db.biz_withdraw_pocket(chat_id, kind) or 0)
    except Exception:
        pass
    remaining = max(fine - pocket_taken, 0)

    user = get_user_readonly(owner) or {}
    cash_all = int(user.get("balance") or 0)

    cash = min(max(cash_all, 0), remaining)
    if cash > 0:
        update_balance(owner, -cash)

    remaining -= cash

    bank_taken = 0
    info = None
    try:
        from db import get_bank_info, bank_withdraw

        info = get_bank_info(owner)
        bank_now = int((info or {}).get("balance") or 0)
        bank_taken = min(bank_now, remaining)
        if bank_taken > 0:
            bank_withdraw(owner, bank_taken)
    except Exception:
        pass

    debt = remaining - bank_taken

    final_balance = None
    if debt > 0:
        final_balance = update_balance(owner, -debt)

    db.biz_update(chat_id, kind, owner_vk=BOT_OWNER, upgrades=0, pocket=0, paid_until=None)

    name = BIZ[kind]["emoji"] + " " + BIZ[kind]["name"]
    text = (
        "⚠️ {name}: бизнес изъят за неуплату!\n\n"
        "💰 Штраф: {fine} элитов 💎\n"
        "🏪 Погашено из кассы бизнеса: {pocket}\n"
        "🏦 Списано с наличных: {cash}\n"
        "💳 Списано со счёта: {bank}\n"
        "🔴 Долг на балансе: {debt}\n\n"
        "📉 Улучшения сброшены.\n"
        "Бизнес снова продаётся — не пропустите оплату в следующий раз!"
    ).format(
        name=name,
        fine=format_amount(fine),
        pocket=format_amount(pocket_taken),
        cash=format_amount(cash),
        bank=format_amount(bank_taken),
        debt=format_amount(debt),
    )
    try:
        from utils.vk import send_plain

        send_plain(owner, text)
    except Exception:
        pass


def _remind_owner(chat_id, kind, biz, left):
    owner = biz["owner_vk"]
    meta = _load_meta(biz)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    rem = meta.get("rem") or {}
    if str(rem.get("last")) == today:
        return
    rem["last"] = today
    meta["rem"] = rem
    _save_meta(chat_id, kind, meta)

    fee = monthly_fee(biz["upgrades"], kind)
    days = max(int(left.total_seconds() // 86400), 0)
    urgency = "⏰ Оплата нужна через %d дн." % days if days > 0 else "🚨 Оплата нужна СЕГОДНЯ!"
    keyboard = {
        "inline": True,
        "buttons": [[{"action": {"type": "callback", "label": "💸 Оплата бизнеса",
                                 "payload": json.dumps({"type": "biz", "act": "pay", "chat": chat_id, "kind": kind}, ensure_ascii=False)}}]],
    }
    try:
        vk.messages.send(
            user_id=owner,
            random_id=random.randrange(2 ** 31),
            message=(
                "🔔 Напоминание об оплате бизнеса\n\n"
                "%s «%s»\n"
                "💬 Беседа: «%s»\n"
                "📅 Дата оплаты: %s\n"
                "💸 К оплате: %s элитов 💎\n\n"
                "%s Неуплата через 3 дня после срока = штраф и потеря бизнеса!"
                % (BIZ[kind]["emoji"], BIZ[kind]["name"],
                   _chat_title(chat_id) or "неизвестная беседа",
                   _fmt_date(biz["paid_until"]), format_amount(fee), urgency)
            ),
            keyboard=json.dumps(keyboard),
        )
    except Exception as error:
        pass


def maybe_maintenance(chat_id, throttled=False, force=False):
    now = time.monotonic()
    with _MAINTENANCE_LOCK:
        last = _last_maintenance.get(chat_id, 0.0)
        if not force and throttled and now - last < 60.0:
            return
        _last_maintenance[chat_id] = now

    for kind in BIZ:
        biz = db.biz_get(chat_id, kind)
        if not biz or not biz["owner_vk"]:
            continue
        paid_until = biz["paid_until"]
        if not paid_until:
            continue
        if paid_until.tzinfo is None:
            paid_until = paid_until.replace(tzinfo=datetime.timezone.utc)
        left = paid_until - datetime.datetime.now(datetime.timezone.utc)
        if left.days <= PAY_WINDOW_DAYS:
            _remind_owner(chat_id, kind, biz, left)
        grace_deadline = paid_until + datetime.timedelta(days=GRACE_DAYS)
        if datetime.datetime.now(datetime.timezone.utc) > grace_deadline:
            _seize_business(chat_id, kind, biz)


def _send_event_answer(event_id, user_id, peer_id, text):
    import requests

    params = {
        "event_id": event_id,
        "user_id": user_id,
        "peer_id": peer_id,
        "event_data": json.dumps(
            {"type": "show_snackbar", "text": text}, ensure_ascii=False
        ),
        "access_token": config.TOKEN_GROUP,
        "v": "5.199",
    }
    for attempt in (1, 2):
        try:
            resp = requests.post(
                "https://api.vk.com/method/messages.sendMessageEventAnswer",
                data=params,
                timeout=5,
            )
            if '"response":1' in (resp.text or ""):
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False


def _temp_chat_message(peer_id, text):
    try:
        sent = vk.messages.send(peer_id=peer_id, message=text, random_id=random.randrange(2 ** 31))
        mid = _extract_message_id(sent)

        def _cleanup():
            try:
                vk.messages.delete(message_ids=mid, delete_for_all=1)
            except Exception:
                pass

        timer = threading.Timer(7, _cleanup)
        timer.daemon = True
        timer.start()
    except Exception:
        pass


def _snack(event_id, user_id, peer_id, text):
    delivered = False
    if event_id and user_id and peer_id:
        delivered = _send_event_answer(event_id, user_id, peer_id, text[:90])
    if not delivered:
        _temp_chat_message(peer_id, text[:90])
    return {"type": "show_snackbar", "text": text[:90]}


def _extract_message_id(sent):
    if isinstance(sent, dict):
        return sent.get("conversation_message_id") or sent.get("message_id") or sent.get("id")
    if isinstance(sent, list) and sent:
        first = sent[0]
        if isinstance(first, dict):
            return first.get("conversation_message_id") or first.get("message_id") or first.get("id")
        if isinstance(first, int):
            return first
    if isinstance(sent, int):
        return sent
    return None


def _delete_view(session):
    cmid = session.get("cmid")
    if not cmid or session.get("peer") is None:
        session["cmid"] = None
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


def _close_session(session):
    session["status"] = "done"
    timer = session.pop("timer", None)
    if timer:
        timer.cancel()
    with _SESSIONS_LOCK:
        if SESSIONS.get(session["sid"]) is session:
            SESSIONS.pop(session["sid"], None)
    _delete_view(session)


def _arm_timer(session):
    def _expire():
        with _SESSIONS_LOCK:
            if SESSIONS.get(session["sid"]) is session:
                SESSIONS.pop(session["sid"], None)
        if session.get("status") == "active":
            session["status"] = "expired"
            _delete_view(session)

    timer = threading.Timer(SESSION_TTL_SECONDS, _expire)
    timer.daemon = True
    timer.start()
    session["timer"] = timer


def _kb(sid, rows):
    buttons = []
    for row in rows:
        line = []
        for item in row:
            label, act = item[0], item[1]
            color = item[2] if len(item) > 2 else "secondary"
            line.append({
                "action": {
                    "type": "callback",
                    "label": label,
                    "payload": json.dumps({"type": "biz", "sid": sid, "act": act}, ensure_ascii=False),
                },
                "color": color,
            })
        buttons.append(line)
    return {"inline": True, "buttons": buttons}


def _send_view(session, text, keyboard=None):

    def _work():
        cmid = session.get("cmid")
        if cmid and session.get("peer") is not None:
            try:
                edit_kwargs = {
                    "peer_id": session["peer"],
                    "conversation_message_id": cmid,
                    "message": text,
                }
                if keyboard is not None:
                    edit_kwargs["keyboard"] = json.dumps(keyboard)
                result = vk.messages.edit(**edit_kwargs)
                return
            except Exception as error:
                pass
        _delete_view(session)
        try:
            kwargs = {
                "peer_ids": [session["peer"]],
                "message": text,
                "random_id": random.randrange(2 ** 31),
            }
            if keyboard is not None:
                kwargs["keyboard"] = json.dumps(keyboard)
            sent = vk.messages.send(**kwargs)
            new_cmid = _extract_message_id(sent)
            if new_cmid:
                session["cmid"] = new_cmid
        except Exception:
            pass

    _work()


def _new_session(user, message):
    peer = message.get("peer_id")
    sid = uuid.uuid4().hex[:10]
    session = {
        "sid": sid, "peer": peer, "vk": user["vk_id"],
        "status": "active", "cmid": None, "lock": threading.Lock(),
    }
    with _SESSIONS_LOCK:
        SESSIONS[sid] = session
    _arm_timer(session)
    return session


_KIND_ORDER = ("bank", "blackjack", "coin", "roulette")


def _owner_line(biz):
    if not biz or not biz["owner_vk"]:
        return "👤 Без владельца"
    return "👑 Владелец: %s" % display_name_by_vk_id(int(biz["owner_vk"]))


def _stats_block(stats):
    e, c = stats["earn"], stats["clients"]
    return (
        "\n📊 Заработок:"
        "\n• Сегодня: %s 💎"
        "\n• За неделю: %s 💎"
        "\n• За всё время: %s 💎"
        "\n• Средний заработок: %s 💎"
        "\n👥 Клиенты: сегодня %d · неделя %d · всего %d"
        % (
            format_amount(e["today"]), format_amount(e["week"]),
            format_amount(e["all"]), format_amount(e.get("avg", 0)),
            c["today"], c["week"], c["all"],
        )
    )


def _due_line(biz):
    paid_until = biz.get("paid_until")
    if not biz["owner_vk"]:
        return None
    if not paid_until:
        return "📅 Оплата: не назначена"
    if paid_until.tzinfo is None:
        paid_until = paid_until.replace(tzinfo=datetime.timezone.utc)
    left = paid_until - datetime.datetime.now(datetime.timezone.utc)
    fee = monthly_fee(biz["upgrades"], biz["kind"])
    line = "📅 Оплата: %s элитов до %s" % (format_amount(fee), _fmt_date(paid_until))
    if left.days < 0:
        line += " 🚨 ПРОСРОЧЕНО (льгота %d дн.)" % max(GRACE_DAYS + left.days, 0)
    elif left.days <= PAY_WINDOW_DAYS:
        line += " ⏰ пора платить!"
    return line


_CHAT_TITLES = {}
_CHAT_TITLES_LOCK = threading.Lock()


def _chat_title(peer_id):
    if not peer_id:
        return ""
    with _CHAT_TITLES_LOCK:
        cached = _CHAT_TITLES.get(peer_id)
    if cached:
        return cached
    title = ""
    try:
        result = vk.messages.getConversationsById(peer_ids=[peer_id])
        items = result.get("items") or []
        if items:
            chat_settings = items[0].get("chat_settings") or {}
            title = chat_settings.get("title") or ""
    except Exception as error:
        pass
    if title:
        with _CHAT_TITLES_LOCK:
            _CHAT_TITLES[peer_id] = title
    return title


def check_text(chat_id, user, chat_title=""):
    lines = ["💼 Бизнесы чата" + (" «%s»" % chat_title if chat_title else ""), ""]
    free = []
    for kind in _KIND_ORDER:
        spec = BIZ[kind]
        biz = db.biz_ensure(chat_id, kind)
        owned = bool(biz and biz["owner_vk"])
        mark = spec["emoji"] + " " + spec["name"]
        if owned:
            owner = int(biz["owner_vk"])
            lines.append("%s — %s" % (mark, display_name_by_vk_id(owner)))
        else:
            lines.append("%s — 👤 Без владельца" % mark)
            free.append(kind)
    lines.append("")
    if free:
        lines.append("Свободные бизнесы можно приобрести! 👇")
    else:
        lines.append("Все бизнесы уже заняты 😎")
    return "\n".join(lines)


def check_kb(chat_id, sid):
    rows = []
    row = []
    for kind in _KIND_ORDER:
        biz = db.biz_get(chat_id, kind)
        if biz and biz["owner_vk"]:
            continue
        spec = BIZ[kind]
        row.append((spec["name"], "info_" + kind, "positive"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("❌ Закрыть", "close")])
    return _kb(sid, rows)


def info_text(chat_id, kind):
    spec = BIZ[kind]
    biz = db.biz_ensure(chat_id, kind)
    upgrades = int((biz or {}).get("upgrades") or 0)
    stats = db.biz_stats_summary((biz or {}).get("sale_info") or "{}")
    pct = current_pct(upgrades)
    owned = bool(biz and biz["owner_vk"])
    fee = monthly_fee(upgrades, kind) if owned else spec["fee"]
    fee_note = " · 🎟 субсидия −35%" if owned and has_subsidy(upgrades) else ""
    lines = [
        ("%s Информация о бизнесе" % spec["emoji"])
        if owned else
        "%s %s" % (spec["emoji"], spec["name"]),
    ]
    if owned:
        lines += ["", "Бизнес: %s" % spec["name"]]
    lines += [
        "━━━━━━━━━━━━━━━",
        _owner_line(biz),
        "",
        "💸 Доход владельца: %d%% (%s)" % (pct, spec["income_desc"]),
        _stats_block(stats),
        "",
        "🏷️ Цена: %s элитов 💎" % format_amount(spec["price"]),
        "📅 Оплата: %s элитов раз в месяц%s" % (format_amount(fee), fee_note),
    ]
    due = _due_line(biz)
    if due:
        lines.append(due)
    return "\n".join(lines)


def info_kb(chat_id, kind, sid, vk_id):
    biz = db.biz_get(chat_id, kind)
    spec = BIZ[kind]
    rows = []
    owned = bool(biz and biz["owner_vk"])
    mine = owned and int(biz["owner_vk"]) == int(vk_id)
    if not owned:
        rows.append([("💳 Купить за %s" % format_amount(spec["price"]), "buy_" + kind, "positive")])
    elif mine:
        rows.append([("⚙️ Управление", "manage_" + kind, "primary")])
    rows.append([("⬅ Назад", "check"), ("❌ Закрыть", "close")])
    return _kb(sid, rows)


def manage_text(chat_id, kind, vk_id):
    spec = BIZ[kind]
    biz = db.biz_get(chat_id, kind) or {}
    meta = _load_meta(biz)
    stats = db.biz_stats_summary(biz.get("sale_info") or "{}")
    pocket = int(biz.get("pocket") or 0)
    pct = current_pct(biz.get("upgrades"))
    next_upg = UPGRADES[min(int(biz.get("upgrades") or 0), len(UPGRADES))] if int(biz.get("upgrades") or 0) < len(UPGRADES) else None
    lines = [
        "%s Ваш бизнес «%s»" % (spec["emoji"], spec["name"]),
        "👑 Владелец: %s" % display_name_by_vk_id(vk_id),
        "━━━━━━━━━━━━━━━",
        "💸 Доход: %d%% (%s)" % (pct, spec["income_desc"]),
        "📊 Заработок:",
        "• Сегодня: %s 💎" % format_amount(stats["earn"]["today"]),
        "• За неделю: %s 💎" % format_amount(stats["earn"]["week"]),
        "• За всё время: %s 💎" % format_amount(stats["earn"]["all"]),
        "• Средний заработок: %s 💎" % format_amount(stats["earn"].get("avg", 0)),
        "👥 Клиенты: сегодня %d · неделя %d · всего %d"
        % (stats["clients"]["today"], stats["clients"]["week"], stats["clients"]["all"]),
        "",
        "🏦 В кассе бизнеса: %s элитов 💎" % format_amount(pocket),
        _due_line(biz) or "",
        "",
        "🏷️ Для продажи: `бизнес продать @юзер сумма`",
    ]
    return "\n".join(lines)


def manage_kb(chat_id, kind, sid, vk_id):
    biz = db.biz_get(chat_id, kind) or {}
    pocket = int(biz.get("pocket") or 0)
    rows = []
    up_row = [("⬆ Улучшения", "upgr_" + kind)]
    if pocket > 0:
        up_row.append(("💰 Вывести %s" % format_amount(pocket), "withdraw_" + kind, "positive"))
    rows.append(up_row)

    paid_until = biz.get("paid_until")
    can_pay = False
    if paid_until:
        if paid_until.tzinfo is None:
            paid_until = paid_until.replace(tzinfo=datetime.timezone.utc)
        left = paid_until - datetime.datetime.now(datetime.timezone.utc)
        can_pay = left.days <= PAY_WINDOW_DAYS
    if not paid_until or can_pay:
        fee = monthly_fee(int(biz.get("upgrades") or 0), kind)
        rows.append([("💸 Оплатить %s" % format_amount(fee), "pay_" + kind, "primary")])

    if len(owned_kinds(chat_id, vk_id)) <= 1:
        rows.append([("❌ Закрыть", "close")])
    else:
        rows.append([("⬅ Назад", "mine")])
    return _kb(sid, rows)


def upgrades_text(chat_id, kind):
    spec = BIZ[kind]
    biz = db.biz_get(chat_id, kind) or {}
    bought = int(biz.get("upgrades") or 0)
    lines = [
        "%s Улучшения «%s»" % (spec["emoji"], spec["name"]),
        "Открываются строго поэтапно 👇",
        "",
    ]
    labels = []
    for i, (utype, value, price) in enumerate(UPGRADES):
        if utype == "subsidy":
            label = "Субсидия (−35% к оплате)"
        else:
            label = "Доход %d%%" % value
        if i < bought:
            status = "✅"
        elif i == bought:
            status = "💰"
        else:
            status = "🔒"
        lines.append("%s %d. %s — %s" % (status, i + 1, label, format_amount(price)))
    pct = current_pct(bought)
    lines += [
        "",
        "💸 Текущий доход: %d%%. Максимум — 50%%." % pct,
    ]
    if has_subsidy(bought):
        lines.append("🎟 Субсидия активна: оплата снижена на 35%.")
    return "\n".join(lines)


def upgrades_kb(chat_id, kind, sid, vk_id):
    biz = db.biz_get(chat_id, kind) or {}
    bought = int(biz.get("upgrades") or 0)
    rows = []
    if bought < len(UPGRADES):
        utype, value, price = UPGRADES[bought]
        label = "Субсидия" if utype == "subsidy" else "Доход %d%%" % value
        rows.append([("⬆ Улучшить: %s (%s)" % (label, format_amount(price)), "upgrade_buy_" + kind, "positive")])
    rows.append([("⬅ Назад", "manage_" + kind)])
    return _kb(sid, rows)


def _balance(vk_id):
    from db import get_user_readonly

    return int((get_user_readonly(vk_id) or {}).get("balance") or 0)


def _do_buy_from_bot(session, kind):
    spec = BIZ[kind]
    chat_id = session["peer"]
    vk_id = session["vk"]
    biz = db.biz_get(chat_id, kind) or {}
    if biz.get("owner_vk"):
        _send_view(session, info_text(chat_id, kind), info_kb(chat_id, kind, session["sid"], vk_id))
        return "❌ Этот бизнес уже купили!"
    price = spec["price"]
    if _balance(vk_id) < price:
        return "%s, не хватает на бизнес! Нужно %s 💎" % (display_name_by_vk_id(vk_id), format_amount(price))
    from db import update_balance

    update_balance(vk_id, -price)
    paid_until = _add_month(datetime.datetime.now(datetime.timezone.utc))
    db.biz_update(chat_id, kind, owner_vk=vk_id, upgrades=0, pocket=0,
                  paid_until=paid_until)
    _send_view(session, manage_text(chat_id, kind, vk_id), manage_kb(chat_id, kind, session["sid"], vk_id))
    return "🎉 Поздравляю с покупкой «%s»!" % spec["name"]


def _do_upgrade(session, kind):
    chat_id = session["peer"]
    vk_id = session["vk"]
    biz = db.biz_get(chat_id, kind) or {}
    if int(biz.get("owner_vk") or 0) != int(vk_id):
        return "Это не ваш бизнес!"
    bought = int(biz.get("upgrades") or 0)
    if bought >= len(UPGRADES):
        return "Максимальный уровень уже достигнут! 🏆"
    utype, value, price = UPGRADES[bought]
    if _balance(vk_id) < price:
        return "Не хватает на улучшение! Нужно %s 💎" % format_amount(price)
    from db import update_balance

    update_balance(vk_id, -price)
    db.biz_update(chat_id, kind, upgrades=bought + 1)
    label = "Субсидия (−35% к оплате)" if utype == "subsidy" else "доход %d%%" % value
    _send_view(session, upgrades_text(chat_id, kind), upgrades_kb(chat_id, kind, session["sid"], vk_id))
    return "⬆ Улучшение куплено: %s!" % label


def _do_withdraw(session, kind):
    chat_id = session["peer"]
    vk_id = session["vk"]
    biz = db.biz_get(chat_id, kind) or {}
    if int(biz.get("owner_vk") or 0) != int(vk_id):
        return "Это не ваш бизнес!"
    amount = int(biz.get("pocket") or 0)
    if amount <= 0:
        return "Касса пуста 😢"
    got = db.biz_withdraw_pocket(chat_id, kind)
    if not got:
        return "Касса пуста 😢"
    from db import update_balance

    update_balance(vk_id, int(got))
    _send_view(session, manage_text(chat_id, kind, vk_id), manage_kb(chat_id, kind, session["sid"], vk_id))
    return "💰 В кассу вывели %s элитов 💎" % format_amount(int(got))


def _do_pay_fee(session, kind):
    chat_id = session["peer"]
    vk_id = session["vk"]
    biz = db.biz_get(chat_id, kind) or {}
    if int(biz.get("owner_vk") or 0) != int(vk_id):
        return "Это не ваш бизнес!"
    fee = monthly_fee(int(biz.get("upgrades") or 0), kind)
    if _balance(vk_id) < fee:
        return "Не хватает на оплату! Нужно %s 💎" % format_amount(fee)
    paid_until = biz.get("paid_until")
    now = datetime.datetime.now(datetime.timezone.utc)
    if paid_until and paid_until.tzinfo is None:
        paid_until = paid_until.replace(tzinfo=datetime.timezone.utc)
    if not paid_until:
        new_due = _add_month(now)
    elif paid_until <= now:
        new_due = _add_month(now)
    else:
        new_due = _add_month(paid_until)
    from db import update_balance

    update_balance(vk_id, -fee)
    db.biz_update(chat_id, kind, paid_until=new_due)
    meta = _load_meta(db.biz_get(chat_id, kind))
    rem = meta.get("rem") or {}
    rem["last"] = None
    meta["rem"] = rem
    _save_meta(chat_id, kind, meta)
    _send_view(session, manage_text(chat_id, kind, vk_id), manage_kb(chat_id, kind, session["sid"], vk_id))
    return "✅ Бизнес оплачен до %s!" % _fmt_date(new_due)


def parse_sell_args(user, rest, message):
    target_id, remaining = extract_target_id(rest, reply_message=message.get("reply_message"))
    amount = None
    for token in remaining.split():
        parsed = parse_amount(token, default=None)
        if parsed is not None and parsed > 0:
            amount = parsed
            break
    if not target_id or (target_id < 0 and not _is_bot(target_id)):
        return None, None, "Укажите покупателя: `бизнес продать @юзер сумма`"
    if target_id == user["vk_id"]:
        return None, None, "Продать бизнес самому себе? Оригинально 🤡 Но нет."
    if _is_bot(target_id):
        return target_id, 0, None
    if amount is None:
        return None, None, "Укажите сумму: `бизнес продать @юзер 1.000.000`"
    return target_id, amount, None


def owned_kinds(chat_id, vk_id):
    result = []
    for kind in _KIND_ORDER:
        biz = db.biz_get(chat_id, kind)
        if biz and int(biz["owner_vk"] or 0) == int(vk_id):
            result.append(kind)
    return result


def sell_ask_text(chat_id, kind, buyer_id, amount):
    spec = BIZ[kind]
    commission = max(amount * SALE_COMMISSION_PCT // 100, 0)
    net = amount - commission
    return (
        "⚠️ Подтверждение продажи\n\n"
        "Вы уверены, что хотите продать бизнес\n"
        "%s «%s»\n%s за %s элитов 💎?\n\n"
        "📉 Улучшения при продаже сбрасываются!\n"
        "🏦 Касса автоматически выведется вам.\n"
        "🧾 Комиссия сделки: %d%% (%s)"
        % (spec["emoji"], spec["name"],
           display_name_by_vk_id(buyer_id),
           format_amount(amount), SALE_COMMISSION_PCT, format_amount(commission))
    )


def sell_ask_kb(kind, buyer_id, amount, sid):
    rows = [[
        ("✅ Подтвердить", "sell_go_%s_%s_%s" % (kind, buyer_id, amount), "positive"),
        ("❌ Отмена", "manage_" + kind),
    ]]
    return _kb(sid, rows)


def _bot_sell_price(kind):
    return BIZ[kind]["price"] * BOT_GOVERNMENT_PRICE_PCT // 100


def _sell_text(chat_id, kind, buyer_id, amount):
    spec = BIZ[kind]
    if _is_bot(buyer_id):
        gov = _bot_sell_price(kind)
        return (
            "🏛 Продажа государству\n\n"
            "Вы уверены, что хотите продать бизнес\n"
            "%s «%s» государству?\n\n"
            "💰 Гос.цена: %s элитов 💎\n"
            "📉 Улучшения при продаже сбрасываются!\n"
            "🏦 Касса автоматически выведется вам.\n"
            "✅ Подтверждаете продажу?"
            % (spec["emoji"], spec["name"], format_amount(gov))
        )
    commission = max(amount * SALE_COMMISSION_PCT // 100, 0)
    return (
        "⚠️ Подтверждение продажи\n\n"
        "Вы уверены, что хотите продать бизнес\n"
        "%s «%s»\n%s за %s элитов 💎?\n\n"
        "📉 Улучшения при продаже сбрасываются!\n"
        "🏦 Касса автоматически выведется вам.\n"
        "🧾 Комиссия сделки: %d%% (%s)"
        % (spec["emoji"], spec["name"],
           display_name_by_vk_id(buyer_id),
           format_amount(amount), SALE_COMMISSION_PCT, format_amount(commission))
    )


def _sell_kb(kind, buyer_id, amount, sid):
    actual = _bot_sell_price(kind) if _is_bot(buyer_id) else amount
    rows = [[
        ("✅ Подтвердить", "sell_go_%s_%s_%s" % (kind, buyer_id, actual), "positive"),
        ("❌ Отмена", "manage_" + kind),
    ]]
    return _kb(sid, rows)


def _sell_to_bot(session, kind):
    chat_id = session["peer"]
    seller_id = session["vk"]
    biz = db.biz_get(chat_id, kind) or {}
    if int(biz.get("owner_vk") or 0) != int(seller_id):
        return {"snack": "Это уже не ваш бизнес!"}
    price = _bot_sell_price(kind)
    pocket = int(biz.get("pocket") or 0)
    if pocket > 0:
        db.biz_withdraw_pocket(chat_id, kind)
    from db import update_balance
    update_balance(seller_id, price)
    if pocket > 0:
        update_balance(seller_id, pocket)
    db.biz_update(chat_id, kind, owner_vk=BOT_OWNER, upgrades=0, pocket=0, paid_until=None)
    spec = BIZ[kind]
    _close_session(session)
    return {
        "snack": (
            "🏛 Бизнес %s «%s» продан государству за %s элитов 💎"
            + ("\n🏦 Касса %s 💎 выведена." % format_amount(pocket) if pocket else "")
        ) % (spec["emoji"], spec["name"], format_amount(price))
    }


DEAL_TTL_SECONDS = 120


def _edit_offer(deal, text):
    cmid = deal.get("cmid")
    if not cmid:
        return
    try:
        vk.messages.edit(
            peer_id=deal["chat"],
            conversation_message_id=cmid,
            message=text,
            keyboard="",
        )
    except Exception as error:
        pass


def start_deal(session, kind, buyer_id, amount):
    chat_id = session["peer"]
    seller_id = session["vk"]
    deal_id = uuid.uuid4().hex[:8]
    spec = BIZ[kind]
    keyboard = {
        "inline": True,
        "buttons": [[
            {"action": {"type": "callback",
                        "label": "✅ Купить «%s» за %s" % (spec["name"], format_amount(amount)),
                        "payload": json.dumps({"type": "biz", "act": "deal", "id": deal_id}, ensure_ascii=False)},
             "color": "positive"},
            {"action": {"type": "callback",
                        "label": "❌ Отмена",
                        "payload": json.dumps({"type": "biz", "act": "deal_cancel", "id": deal_id}, ensure_ascii=False)},
             "color": "negative"},
        ]],
    }
    cmid = None
    try:
        sent = vk.messages.send(
            peer_ids=[chat_id],
            random_id=random.randrange(2 ** 31),
            message=(
                "🤝 %s, вам предлагают купить бизнес!\n\n"
                "%s «%s» от %s\n"
                "💰 Цена: %s элитов 💎\n"
                "📉 Улучшения сбросятся. Касса уйдёт продавцу.\n\n"
                "Нажмите кнопку для подтверждения 👇"
                % (display_name_by_vk_id(buyer_id),
                   spec["emoji"], spec["name"],
                   display_name_by_vk_id(seller_id),
                   format_amount(amount))
            ),
            keyboard=json.dumps(keyboard),
        )
        cmid = _extract_message_id(sent)
    except Exception:
        pass
    with _DEALS_LOCK:
        DEALS[deal_id] = {
            "chat": chat_id, "kind": kind,
            "seller": seller_id, "buyer": buyer_id,
            "amount": amount, "created": time.time(),
            "cmid": cmid,
        }

    def _expire():
        with _DEALS_LOCK:
            gone = DEALS.pop(deal_id, None)
        if gone and gone.get("cmid"):
            try:
                vk.messages.delete(
                    peer_id=gone["chat"],
                    conversation_message_ids=[gone["cmid"]],
                    delete_for_all=1,
                )
            except Exception:
                pass

    timer = threading.Timer(DEAL_TTL_SECONDS, _expire)
    timer.daemon = True
    timer.start()
    return "Предложение отправлено покупателю ⏳"


def cancel_deal(deal_id, actor_id, peer_id):
    with _DEALS_LOCK:
        deal = DEALS.get(deal_id)
    if not deal or deal["chat"] != peer_id:
        return "Предложение уже неактуально"
    if actor_id != deal["buyer"]:
        return "Отменить может только покупатель 😉"
    with _DEALS_LOCK:
        DEALS.pop(deal_id, None)
    _edit_offer(
        deal,
        "❌ %s отказался от покупки. Предложение отменено." % display_name_by_vk_id(actor_id),
    )
    return "Сделка отменена"


def confirm_deal(deal_id, actor_id, peer_id, event_id=None, user_id=None):
    with _DEALS_LOCK:
        deal = DEALS.get(deal_id)
    if not deal:
        return None
    if deal["chat"] != peer_id or actor_id != deal["buyer"]:
        return None
    with _DEALS_LOCK:
        DEALS.pop(deal_id, None)

    chat_id, kind = deal["chat"], deal["kind"]
    seller_id, buyer_id, amount = deal["seller"], deal["buyer"], deal["amount"]
    spec = BIZ[kind]

    biz = db.biz_get(chat_id, kind) or {}
    if int(biz.get("owner_vk") or 0) != int(seller_id):
        _edit_offer(deal, "❌ Сделка отменена: бизнес больше не принадлежит продавцу.")
        return None
    if buyer_id not in (db.get_chat_member_ids(chat_id) or []):
        _edit_offer(deal, "❌ Сделка отменена: покупателя нет в беседе.")
        return None
    if _balance(buyer_id) < amount:
        _edit_offer(deal, "❌ Сделка отменена: у покупателя не хватает элитов.")
        return None

    pocket = int(biz.get("pocket") or 0)
    if pocket > 0:
        db.biz_withdraw_pocket(chat_id, kind)

    commission = max(amount * SALE_COMMISSION_PCT // 100, 0)
    net = amount - commission
    from db import update_balance

    update_balance(buyer_id, -amount)
    update_balance(seller_id, net)
    if pocket > 0:
        update_balance(seller_id, pocket)

    db.biz_update(chat_id, kind, owner_vk=buyer_id, upgrades=0, pocket=0)

    _edit_offer(
        deal,
        (
            "✅ Сделка совершена!\n\n"
            "%s «%s»: %s → %s\n"
            "💰 %s элитов 💎\n"
            "🧾 Комиссия: %s\n"
            "💵 Продавцу: %s"
            % (spec["emoji"], spec["name"],
               display_name_by_vk_id(seller_id),
               display_name_by_vk_id(buyer_id),
               format_amount(amount),
               format_amount(commission), format_amount(net))
        ),
    )
    try:
        from utils.receipt import send_business_receipts

        send_business_receipts(seller_id, buyer_id, spec["name"], amount, commission, net)
    except Exception:
        pass
    return None


def _route(session, act):
    chat_id, vk_id, sid = session["peer"], session["vk"], session["sid"]

    if act == "close":
        _close_session(session)
        return None

    if act == "check":
        _send_view(session, check_text(chat_id, session, chat_title=_chat_title(chat_id)), check_kb(chat_id, sid))
        return None

    if act == "mine":
        kinds = owned_kinds(chat_id, vk_id)
        if not kinds:
            _send_view(session, check_text(chat_id, session, chat_title=_chat_title(chat_id)), check_kb(chat_id, sid))
            return None
        if len(kinds) == 1:
            kind = kinds[0]
            _send_view(session, manage_text(chat_id, kind, vk_id), manage_kb(chat_id, kind, sid, vk_id))
            return None
        lines = ["💼 %s, ваши бизнесы:" % display_name_by_vk_id(vk_id), ""]
        for kind in kinds:
            lines.append("%s «%s»" % (BIZ[kind]["emoji"], BIZ[kind]["name"]))
        lines.append("")
        lines.append("Выберите бизнес для управления 👇")
        rows = []
        row = []
        for kind in kinds:
            row.append((BIZ[kind]["name"], "manage_" + kind, "secondary"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([("❌ Закрыть", "close")])
        _send_view(session, "\n".join(lines), _kb(sid, rows))
        return None

    if act.startswith("info_"):
        kind = act.split("_", 1)[1]
        if kind in BIZ:
            _send_view(session, info_text(chat_id, kind), info_kb(chat_id, kind, sid, vk_id))
        return None

    if act.startswith("buy_go_"):
        snack = _do_buy_from_bot(session, act.split("_", 2)[2])
        return {"snack": snack}

    if act.startswith("buy_"):
        kind = act.split("_", 1)[1]
        if kind in BIZ:
            spec = BIZ[kind]
            text = (
                "⚠️ Подтверждение покупки\n\n"
                "Купить бизнес %s «%s» за %s элитов 💎?\n\n"
                "📅 Первая оплата через месяц: %s элитов"
                % (spec["emoji"], spec["name"], format_amount(spec["price"]), format_amount(spec["fee"]))
            )
            _send_view(
                session, text,
                _kb(sid, [
                    [("✅ Купить", "buy_go_" + kind, "positive"), ("❌ Отмена", "info_" + kind)],
                ]),
            )
        return None

    if act.startswith("manage_"):
        kind = act.split("_", 1)[1]
        biz = db.biz_get(chat_id, kind) or {}
        if int(biz.get("owner_vk") or 0) != int(vk_id):
            return {"snack": "Это не ваш бизнес!"}
        _send_view(session, manage_text(chat_id, kind, vk_id), manage_kb(chat_id, kind, sid, vk_id))
        return None

    if act.startswith("upgr_"):
        kind = act.split("_", 1)[1]
        _send_view(session, upgrades_text(chat_id, kind), upgrades_kb(chat_id, kind, sid, vk_id))
        return None

    if act.startswith("upgrade_buy_"):
        snack = _do_upgrade(session, act.split("_", 2)[2])
        return {"snack": snack}

    if act.startswith("withdraw_"):
        snack = _do_withdraw(session, act.split("_", 1)[1])
        return {"snack": snack}

    if act.startswith("pay_go_"):
        snack = _do_pay_fee(session, act.split("_", 2)[2])
        return {"snack": snack}

    if act.startswith("pay_"):
        kind = act.split("_", 1)[1]
        fee = monthly_fee(int((db.biz_get(chat_id, kind) or {}).get("upgrades") or 0), kind)
        _send_view(
            session,
            (
                "⚠️ Подтверждение оплаты\n\n"
                "Оплатить бизнес %s за %s элитов 💎?"
                % (BIZ[kind]["emoji"] + " «%s»" % BIZ[kind]["name"], format_amount(fee))
            ),
            _kb(sid, [[("✅ Оплатить", "pay_go_" + kind, "positive"), ("❌ Отмена", "manage_" + kind)]]),
        )
        return None

    if act.startswith("sell_pick_"):
        try:
            _, _, kind, buyer_raw, amount_raw = act.split("_", 4)
            buyer_id = int(buyer_raw)
            amount = int(amount_raw)
        except ValueError:
            return None
        _send_view(
            session,
            _sell_text(chat_id, kind, buyer_id, amount),
            _sell_kb(kind, buyer_id, amount, sid),
        )
        return None

    if act.startswith("sell_go_"):
        try:
            _, _, kind, buyer_raw, amount_raw = act.split("_", 4)
            buyer_id = int(buyer_raw)
            amount = int(amount_raw)
        except ValueError:
            return None
        if _is_bot(buyer_id):
            return _sell_to_bot(session, kind)
        biz = db.biz_get(chat_id, kind) or {}
        if int(biz.get("owner_vk") or 0) != int(vk_id):
            return {"snack": "Это уже не ваш бизнес!"}
        _close_session(session)
        return {"snack": start_deal(session, kind, buyer_id, amount)}

    return None


def handle_message_event(data):
    obj = data.get("object") or {}
    payload_raw = obj.get("payload")
    if not payload_raw:
        return False
    if isinstance(payload_raw, str):
        try:
            payload = json.loads(payload_raw)
        except Exception:
            return False
    else:
        payload = payload_raw
    if not isinstance(payload, dict) or payload.get("type") != "biz":
        return False

    user_id = int(data.get("user_id") or obj.get("user_id") or 0)
    peer_id = int(data.get("peer_id") or obj.get("peer_id") or 0)
    event_id = obj.get("event_id") or data.get("event_id")

    sid = payload.get("sid")
    with _SESSIONS_LOCK:
        session0 = SESSIONS.get(sid)
    if not session0 or session0.get("status") != "active":
        pass
    clicked_cmid = obj.get("conversation_message_id")
    if session0 and clicked_cmid and session0.get("cmid") != clicked_cmid:
        session0["cmid"] = clicked_cmid

    try:
        answer = _handle_payload(payload, user_id, peer_id, event_id, obj)
    except Exception:
        answer = {"snack": "⚠️ Ошибка, попробуйте ещё раз"}

    snack_text = ""
    if isinstance(answer, dict):
        if answer.get("snack"):
            snack_text = str(answer["snack"])
        elif answer.get("type") == "show_snackbar":
            snack_text = str(answer.get("text") or "")
    if not snack_text:
        return None
    return _snack(event_id, user_id, peer_id, snack_text)


def _handle_payload(payload, user_id, peer_id, event_id, obj):
    act = payload.get("act") or ""

    if act == "pay" and peer_id < CHAT_PEER_ID_MIN:
        chat_id = int(payload.get("chat") or 0)
        kind = payload.get("kind")
        if not chat_id or kind not in BIZ:
            return None
        session = {
            "sid": uuid.uuid4().hex[:10], "peer": chat_id, "vk": user_id,
            "status": "active", "cmid": None, "lock": threading.Lock(),
        }
        with _SESSIONS_LOCK:
            SESSIONS[session["sid"]] = session
        _arm_timer(session)
        result = _route(session, "pay_go_" + kind)
        if isinstance(result, dict) and result.get("snack"):
            try:
                from utils.vk import send_plain

                send_plain(user_id, "%s %s" % (BIZ[kind]["emoji"], result["snack"]))
            except Exception:
                pass
        return None

    if act == "deal":
        deal_id = payload.get("id")
        confirm_deal(deal_id, user_id, peer_id, event_id, user_id)
        return {"snack": "🤝 Сделка обрабатывается..."}

    if act == "deal_cancel":
        return {"snack": cancel_deal(payload.get("id"), user_id, peer_id)}

    sid = payload.get("sid")
    with _SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if not session or session.get("status") != "active":
        return {
            "type": "show_snackbar",
            "text": "⏳ Меню устарело. Отправьте «бизнес» или «бизнес чек» заново",
        }
    if int(session.get("vk") or 0) != user_id:
        return {"snack": random.choice(_NOT_YOUR_PHRASES)}

    with session["lock"]:
        return _route(session, act)


@command("бизнес")
def cmd_biz(user, args, message):
    peer = message.get("peer_id") or 0
    vk_id = user["vk_id"]
    rest = (args or "").strip()

    if peer < CHAT_PEER_ID_MIN:
        from utils.vk import send_plain

        send_plain(peer, "💼 Бизнесы доступны только в беседах!")
        return None

    maybe_maintenance(peer, throttled=True)

    if rest.lower() in ("продать", "sell") or rest.lower().startswith("продать "):
        target_id, amount, hint = parse_sell_args(user, rest[7:].strip(), message)
        if hint:
            from utils.vk import send_plain

            send_plain(peer, hint)
            return None
        kinds = owned_kinds(peer, vk_id)
        if not kinds:
            from utils.vk import send_plain

            send_plain(peer, "У вас нет бизнесов для продажи 😢")
            return None
        if not _is_bot(target_id) and target_id not in (db.get_chat_member_ids(peer) or []):
            from utils.vk import send_plain

            send_plain(
                peer,
                "%s не участник этой беседы — продать бизнес можно только участнику 💼"
                % display_name_by_vk_id(target_id),
            )
            return None
        session = _new_session(user, message)
        if len(kinds) == 1:
            kind = kinds[0]
            _send_view(
                session,
                _sell_text(peer, kind, target_id, amount),
                _sell_kb(kind, target_id, amount, session["sid"]),
            )
            return None
        rows = []
        row = []
        for kind in kinds:
            row.append((BIZ[kind]["name"], "sell_pick_%s_%s_%s" % (kind, target_id, amount), "primary"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([("❌ Отмена", "close")])
        _send_view(
            session,
            "🏷 Какой бизнес продаём %s?" % display_name_by_vk_id(target_id),
            _kb(session["sid"], rows),
        )
        return None

    low = rest.lower()
    if low in ("чек", "check"):
        session = _new_session(user, message)
        _route(session, "check")
    elif not rest:
        kinds = owned_kinds(peer, vk_id)
        if not kinds:
            from utils.vk import send_plain

            send_plain(
                peer,
                "%s, у тебя нет бизнеса.\nДля приобретения — «бизнес чек», "
                "или соверши сделку с другими владельцами 💼" % display_name_by_vk_id(vk_id),
            )
            return None
        session = _new_session(user, message)
        _route(session, "mine")
    return None
