import json
import logging
import random
import threading
import uuid

import db
from handlers import loans
from handlers.registry import DEAD_SESSION, command
from handlers.coin import (
    BOT_VK_ID,
    _extract_message_id,
    _poor_clicker_phrase,
    _reject,
    _self_click_message,
)
from utils.parse import format_amount, parse_amount
from utils.vk import display_name_by_vk_id, vk

logger = logging.getLogger(__name__)

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
GAME_TIMEOUT_SECONDS = 120
MIN_STAKE = 100
DEFAULT_STAKE = 1000

SUITS = ("♠", "♥", "♦", "♣")
RANKS = (
    ("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6), ("7", 7),
    ("8", 8), ("9", 9), ("10", 10), ("В", 10), ("Д", 10), ("К", 10), ("А", 11),
)
RANK_VALUE = dict(RANKS)


def _new_deck():
    deck = [(rank, suit) for rank, _ in RANKS for suit in SUITS]
    random.shuffle(deck)
    return deck


def _card_str(card):
    rank, suit = card
    return f"{rank}{suit}"


def _hand_value(cards):
    """Сумма руки: туз = 11, но при переборе падает до 1 (А+А = 12)."""
    total = sum(RANK_VALUE[r] for r, _ in cards)
    aces = sum(1 for r, _ in cards if RANK_VALUE[r] == 11)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _is_blackjack(cards):
    return len(cards) == 2 and _hand_value(cards) == 21


def _hand_str(cards):
    return " ".join(f"[{_card_str(c)}]" for c in cards)


def _score_str(cards):
    total = _hand_value(cards)
    if total > 21:
        return f"{total} (перебор)"
    return str(total)


def _bot_name():
    return display_name_by_vk_id(BOT_VK_ID)


def _session_lock(session):
    lock = session.get("lock")
    if lock is None:
        lock = threading.Lock()
        session["lock"] = lock
    return lock


def _delete_view(session):
    peer_id = session.get("peer_id")
    cmid = session.get("cmid")
    if cmid and peer_id is not None:
        try:
            vk.messages.delete(
                peer_id=peer_id,
                conversation_message_ids=[cmid],
                delete_for_all=1,
            )
        except Exception:
            pass
    session["cmid"] = None


ASYNC_SEND = True


def _send_view(session, text, keyboard=None, reply_to=None):
    """Редактируем сообщение сессии; без cmid — отправляем новое.

    Отправка идёт в фоне: VK ждёт ответ на нажатие кнопки всего 3 секунды,
    а запрос к API из облака может занять 1-2 с — иначе кнопка «грузит».
    reply_to игнорируется: реплаи часто падают и нагружают чат.
    """

    def _work():
        # Сначала пробуем отредактировать существующее сообщение — меньше спама.
        cmid = session.get("cmid")
        if cmid:
            try:
                params = {
                    "peer_id": session["peer_id"],
                    "conversation_message_id": cmid,
                    "message": text,
                }
                if keyboard is not None:
                    params["keyboard"] = json.dumps(keyboard)
                if vk.messages.edit(**params):
                    return
            except Exception:
                pass
        # Edit не удался — отправляем новое, НЕ удаляя старое.
        try:
            params = {
                "peer_ids": [session["peer_id"]],
                "message": text,
                "random_id": random.randrange(2 ** 31),
            }
            if keyboard is not None:
                params["keyboard"] = json.dumps(keyboard)
            sent = vk.messages.send(**params)
            session["cmid"] = _extract_message_id(sent) or session.get("cmid")
        except Exception:
            logger.exception("Не удалось отправить экран блэкджека sid=%s", session.get("sid"))

    if ASYNC_SEND:
        threading.Thread(target=_work, daemon=True).start()
    else:
        _work()


def _kb(sid, rows):
    return {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "callback",
                        "label": label,
                        "payload": json.dumps({"type": "bj", "sid": sid, "act": act}),
                    },
                    "color": color,
                }
                for label, act, color in row
            ]
            for row in rows
        ],
    }


def _table_text(session):
    """Общий верх таблицы: ставки и карты."""
    player = session["player"]
    dealer_shown = session["dealer"][:1]

    lines = ["🎰 ELITE CASINO — BLACKJACK", ""]
    lines.append(f"Твоя ставка: {format_amount(session['bet'])} 💎")
    if session.get("insurance_paid"):
        lines.append(f"Страховка: {format_amount(session['insurance_paid'])} 💎 🛡")
    lines.append("")
    lines.append(f"{display_name_by_vk_id(session['vk'])}: {_hand_str(player)} = {_score_str(player)}")
    if session["phase"] == "done" or len(session["dealer"]) == 1:
        lines.append(
            f"{_bot_name()}: {_hand_str(session['dealer'])} = {_score_str(session['dealer'])}"
        )
    else:
        lines.append(f"{_bot_name()}: [{_card_str(dealer_shown[0])}] = {_score_str(dealer_shown)}")
    lines.append("")
    return "\n".join(lines)


def _game_keyboard(session):
    sid = session["sid"]
    if session["phase"] == "insurance":
        cost = session["bet"] // 2
        return _kb(sid, [
            [("🛡 Страховка (" + format_amount(cost) + ")", "ins", "primary"),
             ("🚫 Не страховать", "noins", "secondary")],
        ])
    if session["phase"] == "player":
        can_double = len(session["player"]) == 2 and not session.get("doubled")
        rows = [[
            ("🃏 Ещё", "hit", "positive"),
            ("✋ Хватит", "stand", "secondary"),
        ]]
        if can_double:
            rows.append([("💰 Дабл ×2", "dbl", "primary")])
        return _kb(sid, rows)
    return _kb(sid, [
        [("🔁 Ещё раз", "again", "positive"), ("❌ Закрыть", "close", "secondary")],
    ])


def _refresh(session):
    _send_view(session, _table_text(session), _game_keyboard(session))


def _start_timeout(session):
    timer = None
    old_timer = None
    gen = None
    with SESSIONS_LOCK:
        live = SESSIONS.get(session["sid"])
        if live is not session:
            return
        session["timeout_gen"] = gen = session.get("timeout_gen", 0) + 1
        old_timer = session.get("timer")
        timer = threading.Timer(GAME_TIMEOUT_SECONDS, _timeout_session, args=(session["sid"], gen))
        timer.daemon = True
        session["timer"] = timer
    if old_timer:
        old_timer.cancel()
    timer.start()


def _refund_bet(session, reason_text):
    """Возвращает ставку игроку (страховка к этому моменту уже решена)."""
    db.update_balance(session["vk"], session["bet"])
    _delete_view(session)
    SESSIONS.pop(session["sid"], None)
    try:
        vk.messages.send(
            peer_id=session["peer_id"],
            message=reason_text,
            random_id=random.randrange(2 ** 31),
        )
    except Exception:
        logger.exception("Не удалось отправить сообщение о возврате sid=%s", session.get("sid"))


def _timeout_session(sid, gen):
    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if not session:
        return
    with _session_lock(session):
        if session.get("timeout_gen") != gen or session.get("status") != "active":
            return
        if session.get("phase") == "done":
            return
        acted = bool(session.get("acted"))
        if not acted:
            session["status"] = "expired"
        else:
            session["status"] = "settling"

    name = display_name_by_vk_id(session["vk"])
    if not acted:
        _refund_bet(
            session,
            f"⏳ {name}, ты думал(а) слишком долго.\nСтавка {format_amount(session['bet'])} 💎 возвращена на баланс.",
        )
        return
    try:
        _dealer_reveal_and_settle(session, "\n⏳ Время вышло — остаёшься на своих картах.")
    except Exception:
        logger.exception("Таймаут-доигрыш не удался sid=%s", sid)
        session["status"] = "expired"
        _refund_bet(
            session,
            f"⚠️ {name}, раздача сломалась по таймауту.\nСтавка {format_amount(session['bet'])} 💎 возвращена.",
        )


def _cleanup_peer_sessions(peer_id):
    """Новая игра в беседе гасит старую с полным возвратом ставки."""
    with SESSIONS_LOCK:
        stale = [
            s for s in SESSIONS.values()
            if s.get("peer_id") == peer_id and s.get("status") == "active"
        ]
        for s in stale:
            s["status"] = "expired"
    for s in stale:
        with _session_lock(s):
            _refund_bet(
                s,
                f"♻️ Открыта новая раздача — прошлая отменена.\n{display_name_by_vk_id(s['vk'])}, ставка {format_amount(s['bet'])} 💎 возвращена.",
            )


def _finish(session, outcome, extra=""):
    """Завершает раздачу: расчёт по исходу + финальный экран с кнопками."""
    timer = session.get("timer")
    if timer:
        timer.cancel()
        session["timer"] = None
    session["phase"] = "done"
    session["status"] = "finished"
    bet = session["bet"]
    vk_id = session["vk"]

    payouts = {
        "win": bet * 2,
        "bj_win": int(bet * 2.5),
        "push": bet,
        "ins_win": int((bet // 2) * 3),
        "lose": 0,
    }
    titles = {
        "win": "✅ Ты выиграл!",
        "bj_win": "🃏 BLACKJACK! Выплата 3:2",
        "push": "🤝 Ничья — ставка возвращена",
        "ins_win": "🛡 Страховка сыграла! Тебе всё вернули",
        "lose": "❌ Ты проиграл",
    }
    payout = payouts[outcome]
    if payout:
        db.update_balance(vk_id, payout)
    collected = 0
    if payout and outcome in ("win", "bj_win"):
        collected = loans.collect_from_win(vk_id, payout, session.get("peer_id"))

    try:
        from handlers import business

        business.charge(session.get("peer_id"), "blackjack", bet, [vk_id])
    except Exception:
        pass

    text = _table_text(session)
    text += f"{titles[outcome]}\n"
    if payout:
        text += f"Выплата: +{format_amount(payout)} 💎\n"
    if collected:
        text += f"⚠️ Автосписание: {format_amount(collected)} ушло на погашение просрочки 💳\n"
    if extra:
        text += f"\n{extra}"
    _send_view(session, text, _game_keyboard(session))


def _dealer_reveal_and_settle(session, extra=""):
    """Вскрывает крупье, добирает до 17 (макс. 5 карт) и считает результат."""
    dealer = session["dealer"]
    player = session["player"]
    pv = _hand_value(player)

    if pv > 21:
        # Классика: перебор игрока — мгновенный проигрыш, крупье не играет
        return _finish(session, "lose", f"Перебор ({pv}) — ставка сгорела.")

    if _is_blackjack(dealer):
        if _is_blackjack(player):
            return _finish(session, "push", "У обоих блэкджек — ничья.")
        return _finish(session, "lose", f"У {_bot_name()} блэкджек.")

    while _hand_value(dealer) < 17 and len(dealer) < 5 and session["deck"]:
        dealer.append(session["deck"].pop())

    dv = _hand_value(dealer)

    if dv > 21:
        return _finish(session, "win", f"У {_bot_name()} перебор ({dv}).{extra}")
    if pv > dv:
        return _finish(session, "win", extra)
    if pv == dv:
        return _finish(session, "push", extra)
    return _finish(session, "lose", extra)


def _resolve_insurance(session, accepted):
    bet = session["bet"]
    vk_id = session["vk"]

    if accepted:
        cost = bet // 2
        db.update_balance(vk_id, -cost)
        session["insurance_paid"] = cost
        if _is_blackjack(session["dealer"]):
            return _finish(
                session, "ins_win",
                f"У {_bot_name()} БЛЭКДЖЕК!\nСтавка ({format_amount(bet)}) потеряна, "
                f"но страховка выплатила {format_amount(int(cost * 3))} 💎 — итог: при своих.",
            )
        note = f"🛡 Страховка куплена за {format_amount(cost)} 💎 и сгорела — у {_bot_name()} нет блэкджека."
    else:
        if _is_blackjack(session["dealer"]):
            if _is_blackjack(session["player"]):
                return _finish(session, "push", "У обоих блэкджек — ничья.")
            return _finish(session, "lose", f"Отказался от страховки — а у {_bot_name()} был блэкджек.")
        note = "🚫 Без страховки. Игра продолжается."

    if _is_blackjack(session["player"]) and not _is_blackjack(session["dealer"]):
        return _finish(session, "bj_win")

    session["phase"] = "player"
    _send_view(session, _table_text(session) + f"{note}\n", _game_keyboard(session))
    return None


def _act_ins(session, accepted, event_id, user_id, peer_id):
    if accepted and _user_balance(session["vk"]) < session["bet"] // 2:
        return _reject(event_id, user_id, peer_id, _poor_clicker_phrase())
    _resolve_insurance(session, accepted)
    return None


def _act_double(session, event_id, user_id, peer_id):
    bet = session["bet"]
    if len(session["player"]) != 2 or session.get("doubled"):
        return _reject(event_id, user_id, peer_id, "Дабл только на первых двух картах!")
    if _user_balance(session["vk"]) < bet:
        return _reject(event_id, user_id, peer_id, _poor_clicker_phrase())

    session["doubled"] = True
    db.update_balance(session["vk"], -bet)
    session["bet"] = bet * 2
    if session["deck"]:
        session["player"].append(session["deck"].pop())

    if _hand_value(session["player"]) > 21:
        return _finish(session, "lose", "Дабл не спас — перебор.")

    # После дабла ход завершается сам: крупье играет
    return _dealer_reveal_and_settle(session) or None


def _delete_orphan_view(obj, peer_id):
    cmid = obj.get("conversation_message_id")
    if not cmid or peer_id is None:
        return
    try:
        vk.messages.delete(
            peer_id=peer_id,
            conversation_message_ids=[cmid],
            delete_for_all=1,
        )
    except Exception:
        pass


def _user_balance(vk_id):
    u = db.get_user(vk_id)
    return u["balance"] if u else 0


def _process_act(session, user_id, act, event_id=None, peer_id=None, cmid=None):
    """Вызывается под локом сессии. Возвращает payload снекбара или None."""
    if user_id != session["vk"]:
        return _reject(event_id, user_id, peer_id, _self_click_message())

    if act == "close":
        _delete_view(session)
        SESSIONS.pop(session["sid"], None)
        return None

    if act == "abort":
        if cmid:
            try:
                vk.messages.delete(
                    peer_id=session["peer_id"],
                    conversation_message_ids=[cmid],
                    delete_for_all=1,
                )
            except Exception:
                pass
        _delete_view(session)
        SESSIONS.pop(session["sid"], None)
        return None

    if session["phase"] == "done":
        if act == "again":
            vk_id = session["vk"]
            bet = session["bet"] // (2 if session.get("doubled") else 1)
            if _user_balance(vk_id) < bet:
                return _reject(event_id, user_id, peer_id, _poor_clicker_phrase())
            old_cmid = session.get("cmid")
            peer_id = session["peer_id"]
            session["status"] = "finished"
            SESSIONS.pop(session["sid"], None)
            new_session = _new_round(peer_id, vk_id, bet, reuse_cmid=old_cmid)
            if new_session is None:
                return _reject(event_id, user_id, peer_id, "Банк временно закрыт 🛠")
            return None
        return _reject(event_id, user_id, peer_id, "Раздача окончена")

    if session["phase"] == "insurance":
        if act == "ins":
            session["acted"] = True
            return _act_ins(session, True, event_id, user_id, peer_id)
        if act == "noins":
            session["acted"] = True
            return _act_ins(session, False, event_id, user_id, peer_id)
        return _reject(event_id, user_id, peer_id, "Сначала реши со страховкой!")

    if act == "hit":
        session["acted"] = True
        if not session["deck"]:
            return _dealer_reveal_and_settle(session, "\n🃏 Колода закончилась — шоудаун!") or None
        session["player"].append(session["deck"].pop())
        if len(session["player"]) == 5 and _hand_value(session["player"]) <= 21:
            return _dealer_reveal_and_settle(session, "\n🃏 Пять карт — автоматический шоудаун!") or None
        if _hand_value(session["player"]) > 21:
            return _dealer_reveal_and_settle(session) or None
        _refresh(session)
        _start_timeout(session)
        return None

    if act == "stand":
        session["acted"] = True
        return _dealer_reveal_and_settle(session) or None

    if act == "dbl":
        session["acted"] = True
        result = _act_double(session, event_id, user_id, peer_id)
        return result

    return _reject(event_id, user_id, peer_id, "Не понял кнопку 👀")


def _new_round(peer_id, vk_id, bet, reply_to=None, reuse_cmid=None):
    """Создаёт сессию, списывает ставку и сдаёт первые карты."""
    db.update_balance(vk_id, -bet)
    deck = _new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]

    sid = uuid.uuid4().hex[:10]
    session = {
        "sid": sid, "peer_id": peer_id, "vk": vk_id,
        "bet": bet, "insurance_paid": 0, "doubled": False,
        "deck": deck, "player": player, "dealer": dealer,
        "phase": "insurance", "status": "active",
        "cmid": reuse_cmid, "timer": None, "timeout_gen": 0,
        "acted": False, "reply_to": reply_to,
        "lock": threading.Lock(),
    }
    with SESSIONS_LOCK:
        SESSIONS[sid] = session

    upcard = dealer[0]
    player_bj = _is_blackjack(player)
    if dict(RANKS)[upcard[0]] == 11:
        session["phase"] = "insurance"  # туз крупье — выбор страховки
        note = f"🛡 У {_bot_name()} туз! Можно застраховаться за полставки."
    elif player_bj:
        session["phase"] = "done"
        _finish(session, "bj_win")
        return session
    else:
        session["phase"] = "player"
        note = ""

    text = _table_text(session)
    if note:
        text += f"{note}\n"
    _send_view(session, text, _game_keyboard(session))
    _start_timeout(session)
    return session


def cmd_blackjack(user, args, message):
    peer_id = message.get("peer_id")
    if peer_id is None or peer_id < 2000000000:
        return "Казино работает только в беседе."

    raw = (args or "").strip()
    if raw.lower() in ("все", "всё"):
        bet = _user_balance(user["vk_id"])
    else:
        bet = parse_amount(raw, default=None)
        if bet is None and raw:
            return None  # мусор после команды — молчим
        bet = DEFAULT_STAKE if bet is None else bet

    if bet < MIN_STAKE:
        return f"Минимальная ставка {MIN_STAKE}. Пример: блэкджек 5к"
    if _user_balance(user["vk_id"]) < bet:
        return (
            f"{display_name_by_vk_id(user['vk_id'])}, у тебя недостаточно элитов. "
            f"На балансе: {format_amount(_user_balance(user['vk_id']))} 💎"
        )

    with SESSIONS_LOCK:
        unfinished = [
            s for s in SESSIONS.values()
            if s.get("peer_id") == peer_id
            and s.get("vk") == user["vk_id"]
            and s.get("status") == "active"
            and s.get("phase") != "done"
        ]
    if unfinished:
        warn_kb = _kb(unfinished[0]["sid"], [
            [("❌ Закрыть раздачу", "abort", "negative")],
        ])
        try:
            vk.messages.send(
                peer_id=peer_id,
                message="⚠️ У тебя идёт раздача — доиграй её или закрой кнопкой.",
                keyboard=json.dumps(warn_kb),
                random_id=random.randrange(2 ** 31),
            )
        except Exception:
            logger.exception("Не удалось отправить предупреждение о раздаче")
        return None

    _cleanup_peer_sessions(peer_id)
    _new_round(
        peer_id, user["vk_id"], bet,
        reply_to=message.get("id") or message.get("conversation_message_id"),
    )
    return None


def handle_bj_event(data):
    obj = data.get("object") or {}
    payload_raw = obj.get("payload")
    if not payload_raw:
        return

    if isinstance(payload_raw, dict):
        payload = payload_raw
    else:
        try:
            payload = json.loads(payload_raw)
        except Exception:
            return

    if payload.get("type") != "bj":
        return
    sid = payload.get("sid")
    if not sid:
        return

    event_id = obj.get("event_id") or data.get("event_id")
    user_id = data.get("user_id") or obj.get("user_id")
    peer_id = data.get("peer_id") or obj.get("peer_id")

    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if not session or session.get("status") not in ("active", "finished"):
        if session is None:
            # Сессии нет (рестарт бота) — убираем мёртвые кнопки из чата
            _delete_orphan_view(obj, peer_id)
        return DEAD_SESSION

    clicked_cmid = obj.get("conversation_message_id")
    if clicked_cmid and payload.get("act") != "abort":
        session["cmid"] = clicked_cmid

    with _session_lock(session):
        if session.get("status") not in ("active", "finished"):
            return None
        return _process_act(session, user_id, payload.get("act"), event_id, peer_id, cmid=clicked_cmid)


command("блэкджек", "блекджек")(cmd_blackjack)
