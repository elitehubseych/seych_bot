import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db
from utils.parse import format_amount

MSK = ZoneInfo("Europe/Moscow")
LOG_LIMIT = 8

RATING_START = 50
RATING_MAX = 100
RATING_MIN_LOAN = 40
DAILY_RECOVER = 2
RATING_DEFAULT = 10
PAYMENT_BONUS = 3
CLOSE_BONUS = 7

OVERDUE_GRACE_SECONDS = 5 * 24 * 60 * 60


def now_msk():
    return datetime.now(MSK)


def rating_status(rating):
    if rating >= 60:
        return "Хороший"
    if rating >= 30:
        return "Средний"
    return "Плохой"


def _as_msk(dt):
    if dt is None:
        return now_msk()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=MSK)
    return dt


def parse_dt(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt


def build_installments(tier, now):
    gross = round(tier["amount"] * (100 + tier["rate"]) / 100)
    base = gross // tier["parts"]
    amounts = [base] * tier["parts"]
    amounts[-1] += gross - base * tier["parts"]
    result = []
    for i, amt in enumerate(amounts, start=1):
        due = now + timedelta(days=round(tier["days"] * i / tier["parts"]))
        result.append({"due": due.isoformat(), "amt": amt, "paid": False})
    return result


def loan_total(tier):
    return round(tier["amount"] * (100 + tier["rate"]) / 100)


def make_log(user_row):
    try:
        log = json.loads((user_row or {}).get("loan_log") or "[]")
        return log if isinstance(log, list) else []
    except Exception:
        return []


def append_log(log, text):
    log.append({"t": now_msk().strftime("%d.%m"), "txt": text})
    return log[-LOG_LIMIT:]


def load_loan(vk_id):
    return _loan_from(db.get_user(vk_id))


def _loan_from(user_row):
    raw = (user_row or {}).get("loan")
    if not raw:
        return None
    try:
        loan = json.loads(raw)
    except Exception:
        return None
    if isinstance(loan, dict) and isinstance(loan.get("inst"), list) and loan["inst"]:
        return loan
    return None


def owed_of(inst):
    if inst.get("paid"):
        return 0
    return inst["amt"] - inst.get("part", 0)


def total_left(loan):
    return sum(owed_of(item) for item in loan["inst"])


def next_unpaid(loan):
    for item in loan["inst"]:
        if not item.get("paid"):
            return item
    return None


def has_overdue(loan):
    now = now_msk()
    for item in loan["inst"]:
        if not item.get("paid") and parse_dt(item["due"]) < now:
            return True
    return False


def oldest_overdue(loan):
    now = now_msk()
    dates = [
        parse_dt(item["due"])
        for item in loan["inst"]
        if not item.get("paid") and parse_dt(item["due"]) < now
    ]
    return min(dates) if dates else None


def sync_rating(vk_id):
    user = db.get_user(vk_id) or {}
    loan = _loan_from(user)
    rating = int(user.get("credit_rating") or RATING_START)
    at = _as_msk(user.get("credit_rating_at"))
    now = now_msk()

    if loan is not None and has_overdue(loan):
        if at < now:
            db.set_credit_rating(vk_id, rating, now)
        return rating

    days = int((now - at).total_seconds() // 86400)
    if days > 0 and rating < RATING_MAX:
        new_rating = min(RATING_MAX, rating + DAILY_RECOVER * days)
        db.set_credit_rating(vk_id, new_rating, at + timedelta(days=days))
        return new_rating
    return rating


def adjust_rating(vk_id, delta):
    user = db.get_user(vk_id) or {}
    rating = int(user.get("credit_rating") or RATING_START)
    rating = max(0, min(RATING_MAX, rating + delta))
    db.set_credit_rating(vk_id, rating, now_msk())
    return rating


def enforce_default(vk_id, peer_id=None):
    user = db.get_user(vk_id)
    loan = _loan_from(user)
    if not loan:
        return None
    od = oldest_overdue(loan)
    if od is None:
        return None
    if (now_msk() - od).total_seconds() < OVERDUE_GRACE_SECONDS:
        return None

    info = db.get_bank_info(vk_id) or {}
    on_bank = info.get("bank_balance") or 0
    seized = (info.get("balance") or 0) + on_bank
    if on_bank > 0:
        db.bank_withdraw(vk_id, on_bank)
    fresh_cash = (db.get_bank_info(vk_id) or {}).get("balance") or 0
    if fresh_cash > 0:
        db.update_balance(vk_id, -fresh_cash)

    _save(
        vk_id, loan, user,
        "❌ КРЕДИТ НЕ ВОЗВРАЩЁН — арест счетов (%s)" % format_amount(seized),
        clear=True,
    )
    db.set_credit_rating(vk_id, RATING_DEFAULT, now_msk())
    if peer_id and seized > 0:
        try:
            from handlers import business

            business.charge(peer_id, "bank", seized, [vk_id])
        except Exception:
            pass
    return {"seized": seized}


def _save(vk_id, loan, user_row, log_text=None, clear=False):
    log = make_log(user_row)
    if log_text:
        log = append_log(log, log_text)
    db.set_loan(
        vk_id,
        None if clear else json.dumps(loan, ensure_ascii=False),
        json.dumps(log, ensure_ascii=False),
    )


def collect_from_win(vk_id, win_amount, peer_id=None):
    win_amount = int(win_amount or 0)
    if win_amount <= 0:
        return 0
    user = db.get_user(vk_id)
    loan = _loan_from(user)
    if not loan:
        return 0

    od = oldest_overdue(loan)
    if od is not None and (now_msk() - od).total_seconds() >= OVERDUE_GRACE_SECONDS:
        seized = enforce_default(vk_id, peer_id)
        return seized["seized"] if seized else 0

    now = now_msk()
    collected = 0
    rest = win_amount
    newly_paid = 0
    for inst in loan["inst"]:
        if rest <= 0:
            break
        if inst.get("paid"):
            continue
        if parse_dt(inst["due"]) >= now:
            continue
        need = owed_of(inst)
        use = min(need, rest)
        inst["part"] = inst.get("part", 0) + use
        if inst["part"] >= inst["amt"]:
            inst["paid"] = True
            newly_paid += 1
        collected += use
        rest -= use

    if not collected:
        return 0
    _save(vk_id, loan, user, "автосписание %s с выигрыша" % format_amount(collected))
    for _ in range(newly_paid):
        adjust_rating(vk_id, PAYMENT_BONUS)
    return collected


def repay_payment(vk_id, peer_id=None):
    defaulted = enforce_default(vk_id, peer_id)
    if defaulted:
        return {"status": "defaulted", "seized": defaulted["seized"]}

    user = db.get_user(vk_id)
    loan = _loan_from(user)
    if not loan:
        return {"status": "no_loan"}

    inst = next_unpaid(loan)
    need = owed_of(inst)
    info = db.get_bank_info(vk_id) or {}
    cash = info.get("balance") or 0
    on_bank = info.get("bank_balance") or 0
    if cash + on_bank < need:
        return {"status": "insufficient", "need": need, "cash": cash}

    from_cash = min(cash, need)
    from_bank = need - from_cash
    if from_bank > 0:
        res = db.bank_withdraw(vk_id, from_bank)
        if not res or not res.get("ok"):
            return {"status": "insufficient", "need": need, "cash": cash}

    db.update_balance(vk_id, -need)
    inst["part"] = inst["amt"]
    inst["paid"] = True
    closed = all(item.get("paid") for item in loan["inst"])

    if closed:
        _save(vk_id, loan, user, "кредит закрыт ✅", clear=True)
        adjust_rating(vk_id, CLOSE_BONUS)
    else:
        suffix = " (частично со счёта)" if from_bank > 0 else ""
        _save(vk_id, loan, user, "внёс платёж %s%s" % (format_amount(need), suffix))
    adjust_rating(vk_id, PAYMENT_BONUS)

    if peer_id and need > 0:
        try:
            from handlers import business

            business.charge(peer_id, "bank", need, [vk_id])
        except Exception:
            pass

    return {
        "status": "closed" if closed else "ok",
        "amount": need,
        "from_bank": from_bank,
        "left": 0 if closed else total_left(loan),
    }


def repay_all(vk_id, peer_id=None):
    defaulted = enforce_default(vk_id, peer_id)
    if defaulted:
        return {"status": "defaulted", "seized": defaulted["seized"]}

    user = db.get_user(vk_id)
    loan = _loan_from(user)
    if not loan:
        return {"status": "no_loan"}

    need = total_left(loan)
    info = db.get_bank_info(vk_id) or {}
    cash = info.get("balance") or 0
    on_bank = info.get("bank_balance") or 0
    if cash + on_bank < need:
        return {"status": "insufficient", "need": need, "cash": cash}

    from_cash = min(cash, need)
    from_bank = need - from_cash
    if from_bank > 0:
        res = db.bank_withdraw(vk_id, from_bank)
        if not res or not res.get("ok"):
            return {"status": "insufficient", "need": need, "cash": cash}

    db.update_balance(vk_id, -need)
    for inst in loan["inst"]:
        inst["part"] = inst["amt"]
        inst["paid"] = True

    _save(vk_id, loan, user, "погасил весь кредит досрочно ✅", clear=True)
    adjust_rating(vk_id, PAYMENT_BONUS)
    adjust_rating(vk_id, CLOSE_BONUS)

    if peer_id and need > 0:
        try:
            from handlers import business

            business.charge(peer_id, "bank", need, [vk_id])
        except Exception:
            pass

    return {
        "status": "closed",
        "amount": need,
        "from_bank": from_bank,
        "left": 0,
    }
