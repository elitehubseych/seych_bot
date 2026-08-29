
import json
import random
import re
import threading
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db

CHAT_PEER_ID_MIN = 2000000000
from config import config
from handlers import loans
from handlers.coin import _send_event_answer, _temp_chat_message
from handlers.registry import DEAD_SESSION, command
from utils.parse import extract_target_id, format_amount, parse_amount
from utils.vk import display_name_by_vk_id, mention, vk


def _safe_name(vk_id):
    try:
        return display_name_by_vk_id(vk_id)
    except Exception:
        try:
            return mention(vk_id, "игрок")
        except Exception:
            return "игрок"

MSK = ZoneInfo("Europe/Moscow")

LOAN_TIERS = [
    {"amount": 100_000, "days": 5, "parts": 5, "rate": 10, "min_rating": 40},
    {"amount": 350_000, "days": 7, "parts": 7, "rate": 15, "min_rating": 50},
    {"amount": 500_000, "days": 14, "parts": 2, "rate": 20, "min_rating": 70},
    {"amount": 1_000_000, "days": 21, "parts": 3, "rate": 30, "min_rating": 90},
]
LOAN_LOG_LIMIT = 8

_EVENT_CTX = threading.local()

COMMISSION_RATE = 0.15
DEV_ID = int(config.DEV_ID) if str(config.DEV_ID).strip() else None
SESSION_TTL_SECONDS = 600

SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()

NOT_YOUR_PHRASES = [
    "Не лезь сюда 🤨",
    "Это не твой банк, пройди мимо 🚶",
    "Руки прочь от чужого кошелька! ✋",
    "Своё открывай, а это не твоё 😏",
    "Ты кто такой? Давай, до свидания! 👋",
    "Чужие счета не трогаем 🏦",
    "Полицейский уже выехал за тобой 🚓",
    "Не твоя кнопочка, солнышко ☀️",
    "Взлом банка карается элитами 🔒",
    "Отойди от банкомата, гражданин 🚧",
    "Эта кнопка биометрию не прошла ❌",
    "Служба безопасности банка наблюдает 👀",
    "Мимо. Совсем мимо 😐",
    "Тут даже подсказки нет тебе 🙅",
    "Своя карточка есть? Вот и иди с ней 💳",
]


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
            line.append({
                "action": {
                    "type": "callback",
                    "label": label,
                    "payload": json.dumps({"type": "bank", "sid": sid, "act": act}),
                },
                "color": color,
            })
        buttons.append(line)
    return {"inline": True, "buttons": buttons}


def _fmt_src(source):
    return "Банк" if source == "bank" else "наличные"


def _menu_text(vk_id, info):
    name = display_name_by_vk_id(vk_id)
    return (
        f"🏦 {name}, вы попали в банк ELITE BANK!\n\n"
        f"Ваш счет в банке: {info['account_number']}\n"
        f"На счету: {format_amount(info['bank_balance'])} элитов 💎\n"
        f"Лимит хранилища в банке: {format_amount(db.BANK_CAP)} элитов\n\n"
        "💎 Привилегия хранителей: +7% за каждую полную неделю хранения"
    )


def _account_text(vk_id, note=None):
    info = db.get_bank_info(vk_id) or {"balance": 0, "bank_balance": 0}
    text = (
        "🔐 Вы попали в свое личное хранилище\n\n"
        f"На вашем счету: {format_amount(info['bank_balance'])} элитов 💎\n"
        f"Наличные: {format_amount(info['balance'])} элитов 💵\n"
        "📈 Процент: +7% за полную неделю"
    )
    if note:
        text += "\n\n" + note
    return text


def _transactions_text(vk_id):
    rows = db.get_bank_transactions(vk_id)
    if not rows:
        return (
            "📜 Ваша история переводов, пополнений, снятия:\n\n"
            "Пока пусто. Банк ждёт вашей первой операции 💤"
        )
    lines = ["📜 Ваша история переводов, пополнений, снятия:", ""]
    for row in rows:
        amount = format_amount(row["amount"])
        kind = row["kind"]
        who = row.get("counterparty_id")
        name = display_name_by_vk_id(who) if who else ""
        if kind == "deposit":
            lines.append(f"Вы внесли {amount} в банк")
        elif kind == "withdraw":
            lines.append(f"Вы сняли {amount} с банка")
        elif kind == "transfer_out":
            lines.append(f"Вы перевели {amount} {name} ({_fmt_src(row['source'])})")
        elif kind == "transfer_in":
            lines.append(f"{name} перевёл Вам {amount} ({_fmt_src(row['source'])})")
    return "\n".join(lines)


def _plural_payments(n):
    if n % 10 == 1 and n % 100 != 11:
        return "платёж"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "платежа"
    return "платежей"


def _credit_text(vk_id):
    user = db.get_user(vk_id) or {}
    loan = loans._loan_from(user)
    rating = loans.sync_rating(vk_id)

    lines = [
        "🏦 %s, вы попали в кредитный отдел" % _safe_name(vk_id),
        "",
        "📊 Твой рейтинг: %d/100 · %s" % (rating, loans.rating_status(rating)),
        "",
    ]

    if loan:
        left = loans.total_left(loan)
        gross = loan.get("gross") or sum(i["amt"] for i in loan["inst"])
        nxt = loans.next_unpaid(loan)
        lines.append("📌 ВАШ КРЕДИТ")
        lines.append(
            "Взяли: %s · Вернуть: %s"
            % (format_amount(loan.get("total") or 0), format_amount(gross))
        )
        lines.append("⚠️ Остаток долга: %s элитов 💎" % format_amount(left))
        if nxt:
            due = loans.parse_dt(nxt["due"])
            rest = loans.owed_of(nxt)
            lines.append(
                "🚨 Просрочен платёж: %s (был до %s МСК)"
                % (format_amount(rest), _fmt_deadline(due))
                if due < loans.now_msk()
                else "👉 Платёж: %s до %s МСК" % (format_amount(rest), _fmt_deadline(due))
            )
        if loans.has_overdue(loan):
            lines.append("⚠️ С выигрышей долг списывается автоматически")
        lines.append("")
        lines.append("❗ Новый кредит — после погашения текущего")

    lines.append("💳 ДОСТУПНЫЕ КРЕДИТЫ")
    for idx, tier in enumerate(LOAN_TIERS, start=1):
        gross = loans.loan_total(tier)
        base = gross // tier["parts"]
        days_word = "день" if tier["days"] == 1 else ("дня" if tier["days"] % 10 in (2, 3, 4) and tier["days"] % 100 not in (12, 13, 14) else "дней")
        lock = "" if rating >= tier.get("min_rating", 0) else " 🔒 с рейтинга %d" % tier["min_rating"]
        lines.append(
            "%s %s под %d%%%s" % (_NUM_EMOJI[idx], format_amount(tier["amount"]), tier["rate"], lock)
        )
        lines.append(
            "　└ %d %s · %d %s по %s"
            % (
                tier["days"], days_word,
                tier["parts"], _plural_payments(tier["parts"]),
                format_amount(base),
            )
        )

    if not loan and rating < loans.RATING_MIN_LOAN:
        lines.append("")
        lines.append("🚫 Кредиты недоступны: плохая кредитная история")
        lines.append("💡 Рейтинг восстановится: +2 за день без просрочек")
    return "\n".join(lines)


_NUM_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣"}


def _history_text(vk_id):
    user = db.get_user(vk_id) or {}
    history = loans.make_log(user)

    lines = ["📜 %s, ваша кредитная история" % _safe_name(vk_id), ""]
    if history:
        for entry in reversed(history[-loans.LOG_LIMIT:]):
            lines.append("• %s — %s" % (entry["t"], entry["txt"]))
    else:
        lines.append("• Пока пусто — кредитов не было")
    return "\n".join(lines)


def _fmt_deadline(dt):
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


_CREDIT_KB = lambda sid: _kb(sid, [
    [("100к", "credit_take_0", "positive"), ("350к", "credit_take_1", "positive")],
    [("500к", "credit_take_2", "primary"), ("1 млн", "credit_take_3", "primary")],
    [("💳 Погасить", "credit_repay", "negative"), ("📜 История", "credit_history")],
    [("⬅ Назад", "menu")],
])

_CREDIT_HISTORY_KB = lambda sid: _kb(sid, [
    [("⬅ Назад", "credit_menu")],
    [("❌ Закрыть", "close", "negative")],
])


def _take_credit(session, tier_arg):
    vk_id = session["vk"]
    try:
        from handlers import business

        if business.is_bank_owner(session["peer"], vk_id):
            return _snack(business.self_loan_phrase())
    except Exception:
        pass
    user = db.get_user(vk_id)
    if loans._loan_from(user):
        _send_view(session, _credit_text(vk_id), _CREDIT_KB(session["sid"]))
        return _snack("❌ Сначала погасите текущий кредит")

    rating = loans.sync_rating(vk_id)
    if rating < loans.RATING_MIN_LOAN:
        _send_view(session, _credit_text(vk_id), _CREDIT_KB(session["sid"]))
        return _snack(
            "❌ Кредит недоступен: рейтинг %d/100 (%s).\n"
            "Гаси долги вовремя — рейтинг растёт +2 в день"
            % (rating, loans.rating_status(rating).lower())
        )

    try:
        idx = int(tier_arg)
        tier = LOAN_TIERS[idx]
    except (ValueError, IndexError):
        return None

    if rating < tier.get("min_rating", 0):
        _send_view(session, _credit_text(vk_id), _CREDIT_KB(session["sid"]))
        return _snack(
            "🔒 Кредит %s доступен с рейтинга %d. Твой рейтинг: %d"
            % (format_amount(tier["amount"]), tier["min_rating"], rating)
        )

    now = loans.now_msk()
    gross = loans.loan_total(tier)
    loan = {
        "tid": idx,
        "total": tier["amount"],
        "gross": gross,
        "issued": now.isoformat(),
        "inst": loans.build_installments(tier, now),
    }
    db.update_balance(vk_id, tier["amount"])
    log = loans.append_log(
        loans.make_log(user),
        "взял кредит %s под %d%% (вернуть %s)"
        % (format_amount(tier["amount"]), tier["rate"], format_amount(gross)),
    )
    db.set_loan(
        vk_id,
        json.dumps(loan, ensure_ascii=False),
        json.dumps(log, ensure_ascii=False),
    )
    _send_view(session, _credit_text(vk_id), _CREDIT_KB(session["sid"]))
    return _snack(
        "✅ Кредит %s элитов выдан! Вернуть: %s (%d платежа)"
        % (format_amount(tier["amount"]), format_amount(gross), tier["parts"])
    )


def _repay_credit(session):
    vk_id = session["vk"]
    result = loans.repay_payment(vk_id, session["peer"])

    if result["status"] == "no_loan":
        return _snack("💡 У Вас нет активного кредита")
    if result["status"] == "defaulted":
        return _snack(
            "🚨 Долг признан безнадёжным: счета арестованы (изъято %s),\n"
            "кредит закрыт как НЕ ВОЗВРАЩЁННЫЙ"
            % format_amount(result.get("seized") or 0)
        )
    if result["status"] == "insufficient":
        return _snack(
            "❌ Не хватает средств: нужно %s (наличные %s 💵 + счёт %s 💎)"
            % (
                format_amount(result["need"]),
                format_amount(result["cash"]),
                format_amount((db.get_bank_info(vk_id) or {}).get("bank_balance") or 0),
            )
        )

    amount = result["amount"]
    from_bank = result.get("from_bank") or 0
    _send_view(session, _credit_text(vk_id), _CREDIT_KB(session["sid"]))
    if result["status"] == "closed":
        return _snack("🎉 Кредит полностью закрыт!")
    note = ", из них %s со счёта" % format_amount(from_bank) if from_bank else ""
    return _snack(
        "✅ Платёж %s внесён%s. Осталось: %s 💎"
        % (format_amount(amount), note, format_amount(result["left"]))
    )


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


ASYNC_SEND = True


def _send_view(session, text, keyboard=None, reply_to=None):
    def _work():
        cmid = session.get("cmid")
        if cmid:
            try:
                edit_kwargs = {
                    "peer_id": session["peer"],
                    "conversation_message_id": cmid,
                    "message": text,
                }
                if keyboard is not None:
                    edit_kwargs["keyboard"] = json.dumps(keyboard)
                if vk.messages.edit(**edit_kwargs):
                    return
            except Exception:
                pass
        _delete_message(session)
        try:
            kwargs = {
                "peer_ids": [session["peer"]],
                "message": text,
                "random_id": random.randrange(2**31),
            }
            if keyboard:
                kwargs["keyboard"] = json.dumps(keyboard)
            sent = vk.messages.send(**kwargs)
            sent = sent[0] if isinstance(sent, list) else sent
            if isinstance(sent, dict) and sent.get("conversation_message_id"):
                session["cmid"] = sent["conversation_message_id"]
        except Exception:
            pass

    if ASYNC_SEND:
        threading.Thread(target=_work, daemon=True).start()
    else:
        _work()


def _arm_timer(session):
    def _expire():
        with _SESSIONS_LOCK:
            if SESSIONS.get(session["sid"]) is session:
                SESSIONS.pop(session["sid"], None)
        if session.get("status") == "active":
            session["status"] = "expired"
            _delete_message(session)

    timer = threading.Timer(SESSION_TTL_SECONDS, _expire)
    timer.daemon = True
    timer.start()
    session["timer"] = timer


def _close_session(session):
    session["status"] = "done"
    timer = session.pop("timer", None)
    if timer:
        timer.cancel()
    with _SESSIONS_LOCK:
        if SESSIONS.get(session["sid"]) is session:
            SESSIONS.pop(session["sid"], None)
    _delete_message(session)


@command("банк")
def cmd_bank(user, args, message):
    peer = message.get("peer_id") or 0
    if peer < CHAT_PEER_ID_MIN:
        return "🏦 Банк работает только в беседах!"
    tokens = (args or "").split(maxsplit=1)
    head = tokens[0].lower() if tokens else ""
    rest = tokens[1] if len(tokens) > 1 else ""

    if head in ("внести", "пополнить", "вложить"):
        return _immediate(user, rest, deposit=True)
    if head == "снять":
        return _immediate(user, rest, deposit=False)
    if head == "погасить":
        return _immediate_repay(user, rest, peer)
    if args.strip():
        return None

    peer = message.get("peer_id")
    info = db.ensure_bank_account(user["vk_id"])
    if info is None:
        return "Банк временно закрыт на техобслуживание 🛠"

    sid = uuid.uuid4().hex[:10]
    session = {
        "sid": sid, "peer": peer, "vk": user["vk_id"],
        "status": "active", "cmid": None, "lock": threading.Lock(),
    }
    with _SESSIONS_LOCK:
        SESSIONS[sid] = session

    _send_view(session, _menu_text(user["vk_id"], info), _MENU_KB(sid))
    _arm_timer(session)
    return None


def _resolve_amount(raw, available):
    if raw.lower() in ("все", "всё"):
        return available
    return parse_amount(raw, default=None)


def _loan_summary_line(vk_id):
    loan = loans.load_loan(vk_id)
    if not loan:
        return ""
    gross = loan.get("gross") or sum(i["amt"] for i in loan["inst"])
    left = loans.total_left(loan)
    paid_count = sum(1 for i in loan["inst"] if not i.get("paid"))
    return (
        "📌 Взяли: %s · Вернуть: %s · Осталось: %s (%d %s)"
        % (
            format_amount(loan.get("total") or 0),
            format_amount(gross),
            format_amount(left),
            paid_count,
            _plural_payments(paid_count),
        )
    )


def _immediate_repay(user, raw, peer=None):
    vk_id = user["vk_id"]
    rest = (raw or "").strip().lower()
    if rest and rest.split()[0] not in ("все", "всё"):
        return None

    if rest:
        result = loans.repay_all(vk_id, peer)
    else:
        result = loans.repay_payment(vk_id, peer)

    if result["status"] == "no_loan":
        return "💡 У вас нет активного кредита"
    if result["status"] == "defaulted":
        return (
            "🚨 Долг признан безнадёжным: счета арестованы (изъято %s),\n"
            "кредит закрыт как НЕ ВОЗВРАЩЁННЫЙ" % format_amount(result.get("seized") or 0)
        )
    if result["status"] == "insufficient":
        on_bank = (db.get_bank_info(vk_id) or {}).get("bank_balance") or 0
        return (
            "❌ Не хватает средств: нужно %s\n"
            "Наличные: %s 💵 | Счёт: %s 💎"
            % (
                format_amount(result["need"]),
                format_amount(result["cash"]),
                format_amount(on_bank),
            )
        )

    amount = result["amount"]
    from_bank = result.get("from_bank") or 0
    note = ", из них %s со счёта" % format_amount(from_bank) if from_bank else ""

    if rest:
        return (
            "✅ Кредит погашен полностью!\n"
            "💸 Списано: %s%s\n🎉 Кредит закрыт, рейтинг вырос 📈" % (format_amount(amount), note)
        )

    if result.get("status") == "closed":
        return "✅ Платёж %s внесён%s\n🎉 Кредит полностью закрыт! Рейтинг вырос 📈" % (
            format_amount(amount), note
        )
    summary = _loan_summary_line(vk_id)
    return (
        "✅ Платёж %s внесён%s\n%s" % (format_amount(amount), note, summary)
    )


def _immediate(user, raw, deposit):
    raw = (raw or "").strip()
    parts = raw.split()
    if len(parts) != 1:
        return None
    info = db.get_bank_info(user["vk_id"])
    if info is None:
        return "Банк временно закрыт на техобслуживание 🛠"

    if deposit:
        available = info["balance"]
        cap_left = max(db.BANK_CAP - info["bank_balance"], 0)
    else:
        available = info["bank_balance"]
        cap_left = None

    amount = _resolve_amount(raw, available)
    if amount is None:
        return None
    if deposit:
        amount = min(amount, cap_left)
    if amount <= 0:
        if deposit and cap_left <= 0:
            return "🏦 Хранилище переполнено! Лимит: %s элитов" % format_amount(db.BANK_CAP)
        return "Нечего %s 🤷" % ("вносить" if deposit else "снимать")

    result = (db.bank_deposit(user["vk_id"], amount) if deposit
              else db.bank_withdraw(user["vk_id"], amount))
    if result.get("ok"):
        verb = "внесли" if deposit else "сняли"
        prep = "в ELITE BANK" if deposit else "с ELITE BANK"
        return (
            f"✅ Вы {verb} {format_amount(amount)} элитов {prep}\n"
            f"Наличные: {format_amount(result['cash'])} 💵 | "
            f"Счет: {format_amount(result['bank'])} 💎"
        )
    if result.get("reason") == "funds":
        where = "на наличных" if deposit else "на счету"
        return f"Недостаточно элитов {where}: {format_amount(available)} 💰"
    if result.get("reason") == "cap":
        left = result.get("cap_left", 0)
        return (
            f"🏦 Лимит хранилища {format_amount(db.BANK_CAP)}. "
            f"Свободно: {format_amount(left)} элитов"
        )
    return "Операция не удалась, попробуй позже"


def handle_bank_event(data):
    obj = data.get("object") or {}
    payload_raw = obj.get("payload")
    if not payload_raw:
        return None
    try:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
    except Exception:
        return None
    if payload.get("type") != "bank":
        return None

    sid = payload.get("sid")
    act = payload.get("act")
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
            return _route(session, act)
    finally:
        _EVENT_CTX.event = None


_ACCOUNT_KB = lambda sid: _kb(sid, [
    [("📥 Внести", "dep_menu", "positive"), ("📤 Снять", "wd_menu", "negative")],
    [("⬅ Назад", "menu")],
])

_MENU_KB = lambda sid: _kb(sid, [
    [("💼 Личный счет", "account", "primary")],
    [("🏦 Кредит", "credit_menu")],
    [("📊 Меню транзакций", "tx")],
    [("❌ Закрыть банк", "close", "negative")],
])

_DEP_KB = lambda sid: _kb(sid, [
    [("10.000", "dep_10000", "positive"), ("50.000", "dep_50000", "positive"), ("100.000", "dep_100000", "positive")],
    [("✍ Другая сумма", "dep_other")],
    [("💎 Все элиты", "dep_all", "primary")],
    [("⬅ Назад", "account")],
])

_WD_KB = lambda sid: _kb(sid, [
    [("10.000", "wd_10000", "positive"), ("50.000", "wd_50000", "positive"), ("100.000", "wd_100000", "positive")],
    [("✍ Другая сумма", "wd_other")],
    [("💎 Все элиты", "wd_all", "primary")],
    [("⬅ Назад", "account")],
])


def _route(session, act):
    peer = session["peer"]
    vk_id = session["vk"]

    if act == "close":
        _close_session(session)
        return None

    if act in ("from_cash", "from_bank"):
        return _execute_paybank(session, "cash" if act == "from_cash" else "bank")

    if act == "menu":
        info = db.ensure_bank_account(vk_id)
        _send_view(session, _menu_text(vk_id, info), _MENU_KB(session["sid"]))
        return None

    if act == "account":
        _send_view(session, _account_text(vk_id), _ACCOUNT_KB(session["sid"]))
        return None

    if act == "tx":
        _send_view(session, _transactions_text(vk_id),
                   _kb(session["sid"], [[("⬅ Назад", "menu")]]))
        return None

    if act == "credit_menu":
        try:
            from handlers import business

            if business.is_bank_owner(peer, vk_id):
                return _snack(business.self_loan_phrase())
        except Exception:
            pass
        _send_view(session, _credit_text(vk_id), _CREDIT_KB(session["sid"]))
        return None

    if act == "credit_history":
        _send_view(session, _history_text(vk_id), _CREDIT_HISTORY_KB(session["sid"]))
        return None

    if act.startswith("credit_take_"):
        return _take_credit(session, act.rsplit("_", 1)[1])

    if act == "credit_repay":
        return _repay_credit(session)

    if act == "dep_menu":
        _send_view(session, "Какую сумму Вы желаете внести в банк?", _DEP_KB(session["sid"]))
        return None

    if act == "wd_menu":
        _send_view(session, "Какую сумму Вы желаете снять с банка?", _WD_KB(session["sid"]))
        return None

    if act in ("dep_other", "wd_other"):
        cmd = "банк вложить X" if act.startswith("dep") else "банк снять X"
        return _snack("💡 Другая сумма — командой: %s" % cmd)

    if act.startswith(("dep_", "wd_")):
        return _do_deposit_withdraw(session, act)

    return None


def _do_deposit_withdraw(session, act):
    deposit = act.startswith("dep_")
    tail = act.split("_", 1)[1]
    vk_id = session["vk"]

    info = db.get_bank_info(vk_id) or {"balance": 0, "bank_balance": 0}
    if tail == "all":
        available = info["balance"] if deposit else info["bank_balance"]
        if deposit:
            available = min(available, max(db.BANK_CAP - info["bank_balance"], 0))
    else:
        available = {"10000": 10_000, "50000": 50_000, "100000": 100_000}.get(tail)

    if not available or available <= 0:
        return _snack("Нет подходящей суммы для операции 🤷")

    result = (db.bank_deposit(vk_id, available) if deposit
              else db.bank_withdraw(vk_id, available))
    if not result.get("ok"):
        if result.get("reason") == "cap":
            return _snack(
                "Лимит хранилища! Свободно: "
                + format_amount(result.get("cap_left", 0))
            )
        return _snack("Недостаточно средств 😕")

    verb = "внесли" if deposit else "сняли"
    prep = "в банк" if deposit else "с банка"
    note = f"✅ Вы {verb} {format_amount(available)} элитов {prep}."
    _send_view(session, _account_text(vk_id, note=note), _ACCOUNT_KB(session["sid"]))
    return None


@command("paybank")
def cmd_paybank(user, args, message):
    peer = message.get("peer_id")
    text_args = (args or "").strip()

    acct_match = re.match(r"^(?:сч[её]т\s+)?(\d{14})\b(.*)$", text_args, re.IGNORECASE)
    if acct_match:
        number = acct_match.group(1)
        target_id = db.find_user_by_account(number)
        remaining = acct_match.group(2)
        if target_id is None:
            return f"🏦 Счета {number} не существует"
    else:
        target_id, remaining = extract_target_id(args, message.get("reply_message"))

    if target_id is None:
        if not text_args:
            return "Укажи получателя: paybank @получатель сумма (или номер счёта)"
        return None
    if target_id == user["vk_id"]:
        return "Себе переводить нельзя 🙃"
    if target_id < 0:
        return "Переводить сообществам нельзя 😕"

    raw = remaining.strip()
    amount = None
    if raw.lower() in ("все", "всё"):
        amount_text = "ВСЁ"
    else:
        amount = parse_amount(raw, default=None)
        if amount is None or amount <= 0:
            return None
        amount_text = format_amount(amount)

    sender_info = db.get_bank_info(user["vk_id"])
    if sender_info is None:
        return "Банк временно закрыт на техобслуживание 🛠"
    if db.ensure_bank_account(target_id) is None:
        return "Не удалось открыть счет получателю, попробуй позже"

    sid = uuid.uuid4().hex[:10]
    session = {
        "sid": sid, "kind": "transfer",
        "peer": peer, "vk": user["vk_id"], "target": target_id,
        "amount": amount, "status": "active", "cmid": None,
        "lock": threading.Lock(),
    }
    with _SESSIONS_LOCK:
        SESSIONS[sid] = session

    text = (
        f"{display_name_by_vk_id(user['vk_id'])}, Вы собрались совершить перевод "
        f"{display_name_by_vk_id(target_id)} на сумму {amount_text} элитов.\n\n"
        "С какого счета, вы собираетесь перевести?"
    )
    _send_view(session, text, _kb(sid, [
        [("💵 Наличные", "from_cash", "positive"), ("🏦 С банка", "from_bank", "primary")],
        [("❌ Отмена", "close", "negative")],
      ]), reply_to=_reply_target(message))
    _arm_timer(session)
    return None


def _execute_paybank(session, source):
    from utils.receipt import generate_transaction_id, send_transfer_receipts

    vk_id, target_id = session["vk"], session["target"]
    sender_info = db.get_bank_info(vk_id) or {}
    column = "balance" if source == "cash" else "bank_balance"
    available = sender_info.get(column) or 0

    amount = session["amount"]
    if amount is None:
        amount = available
    if amount <= 0 or available < amount:
        _send_view(session, "😢 Недостаточно элитов на выбранном счете.", _kb(
            session["sid"],
            [
                [("💵 Наличные", "from_cash", "positive"), ("🏦 С банка", "from_bank", "primary")],
                [("❌ Отмена", "close", "negative")],
            ],
        ))
        return None

    result = db.transfer_mixed(
        vk_id, target_id, amount, COMMISSION_RATE, DEV_ID,
        sender_source=source, receiver_dest="bank",
    )
    if result is None:
        return _snack("Перевод не удался, попробуй ещё раз")

    _close_session(session)

    sender_name = display_name_by_vk_id(vk_id)
    receiver_name = display_name_by_vk_id(target_id)
    sender_account = (db.get_bank_info(vk_id) or {}).get("account_number")
    receiver_account = (db.get_bank_info(target_id) or {}).get("account_number")
    src_label = "Банк" if source == "bank" else "наличные"

    response_text = (
        f"{sender_name} пополнил счет в банке {receiver_name} "
        f"на {format_amount(amount)} элитов ({src_label}) 💸\n"
        f"Комиссия: {format_amount(result['commission'])} элитов 🏦\n"
        f"Итог: {format_amount(result['net'])} элитов ✅"
    )
    try:
        vk.messages.send(peer_id=session["peer"], message=response_text,
                         random_id=random.randrange(2**31))
    except Exception:
        pass

    threading.Thread(
        target=send_transfer_receipts,
        kwargs={
            "peer_id": session["peer"],
            "sender_id": vk_id,
            "receiver_id": target_id,
            "amount_text": format_amount(result["amount"]),
            "commission_text": format_amount(result["commission"]),
            "net_text": format_amount(result["net"]),
            "transaction_id": generate_transaction_id(),
            "message_text": response_text,
            "source_label": source,
            "dest_label": "bank",
            "sender_account": sender_account,
            "receiver_account": receiver_account,
        },
        daemon=True,
    ).start()
    return None

def _reply_target(message):
    return message.get("id") or message.get("conversation_message_id")
