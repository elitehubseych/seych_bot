
import json
import logging
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import db
from config import config
from handlers.coin import (
    _delete_orphan_button_message,
    _extract_message_id,
    _mark_event_processed,
    _purge_processed_events,
    _reject,
)
from handlers.registry import DEAD_SESSION, command
from utils.certificates import (
    build_divorce_certificate,
    build_marriage_certificate,
    send_certificate_to_chat,
)
from utils.humantime import format_duration
from utils.parse import extract_target_id
from utils.vk import display_name_by_vk_id, gform, vk

logger = logging.getLogger(__name__)

BOT_VK_ID = -abs(int(config.ID_GROUP))
DEV_ID = int(config.DEV_ID) if str(config.DEV_ID).strip() else None

PROPOSALS = {}
DIVORCES = {}
REGISTRY_LOCK = threading.Lock()

MARRIAGE_ACTION_LOCK = threading.Lock()

BUTTON_TTL_SECONDS = 600

_MEMBER_CACHE = {}
_MEMBER_CACHE_TTL_SECONDS = 90
_MEMBER_LOCK = threading.Lock()


def _chat_member_ids(peer_id):
    now = time.time()
    with _MEMBER_LOCK:
        cached = _MEMBER_CACHE.get(peer_id)
        if cached and now - cached[0] < _MEMBER_CACHE_TTL_SECONDS:
            return cached[1]

    member_ids = None
    try:
        result = vk.messages.getConversationMembers(peer_id=peer_id)
        ids = {
            int(item["member_id"])
            for item in (result.get("items") or [])
            if item.get("member_id")
        }
        ids |= {
            int(profile["id"])
            for profile in (result.get("profiles") or [])
            if profile.get("id")
        }
        if ids:
            member_ids = ids
    except Exception as error:
        pass

    if member_ids is None:
        fallback_ids = db.get_chat_member_ids(peer_id) or []
        if fallback_ids:
            member_ids = set(fallback_ids)

    if member_ids is not None:
        with _MEMBER_LOCK:
            _MEMBER_CACHE[peer_id] = (now, member_ids)
    return member_ids

_MENTION_MARKUP_RE = re.compile(r"\[id\d+\|([^\]]*)\]")

_WRONG_CLICK_PHRASES = [
    "Руки! Эта кнопка не ваша 😤",
    "Свидетели кнопки не жмут 🙅",
    "Не ваша свадьба — проходите мимо 🚶",
    "Ой, вас сюда вроде не звали 🙊",
    "Здесь решают судьбу двоих, вы третий лишний 🎭",
    "Кнопка зарегистрирована на другое лицо 📋",
    "Не трогайте чужое счастье 💍",
    "Ваша очередь придёт на вашем собственном браке 💒",
    "Эта кнопка кусается. Она только для адресата 🦷",
    "Тссс! Романтический момент, не мешайте 🕯️",
    "А вас тут кто-то спрашивал? 🤨",
    "Любопытство сгубило кошку 🐱",
    "Держите дистанцию, это церемония 👑",
    "Не ваша свадьба — не ваши кнопки. Закон прост ⚖️",
    "Кнопка любопытная, но чужая 👀",
    "Мимо! Частная собственность пары 🔒",
    "Зависть морщинки делает, а кнопку всё равно жать нельзя 😌",
    "Скриншот вашего тыка уже отправлен парочке 📸",
    "Кнопка обиделась и жалуется молодожёнам 😤",
    "Приходите со своим предложением — вот тогда и нажмёте 💐",
]

_SINGLE_TEMPLATES = [
    "💔 {name} сейчас {free} — сердце никому не принадлежит!",
    "🕊 {name} пока {free}, но всё впереди 😉",
    "💍 В этой беседе у {name} пары нет. Страница сердца пуста!",
    "✨ {name} {free}! Место для второй половинки свободно",
]


def _plain(vk_id):
    raw = display_name_by_vk_id(vk_id)
    match = _MENTION_MARKUP_RE.fullmatch(raw.strip())
    return match.group(1) if match else raw


def _partner_id(marriage, vk_id):
    return marriage["user2_id"] if marriage["user1_id"] == vk_id else marriage["user1_id"]


def _fmt_date(value):
    try:
        return value.strftime("%d.%m.%Y в %H:%M")
    except AttributeError:
        return ""


def _now_utc():
    return datetime.now(timezone.utc)


def _as_aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _duration_since(started_at):
    started_at = _as_aware(started_at)
    try:
        seconds = (_now_utc() - started_at).total_seconds()
    except TypeError:
        return "меньше минуты"
    return format_duration(max(0, seconds))


def _single_reply(vk_id):
    name = display_name_by_vk_id(vk_id)
    free = gform(vk_id, "свободен", "свободна")
    return random.choice(_SINGLE_TEMPLATES).format(name=name, free=free)


def _marriage_reply(peer_id, who_id):
    marriage = db.get_active_marriage_for(peer_id, who_id)
    if not marriage:
        return _single_reply(who_id)
    partner_id = _partner_id(marriage, who_id)
    verb = gform(who_id, "женат на", "замужем за")
    return (
        f"💍 {display_name_by_vk_id(who_id)} {verb} "
        f"{display_name_by_vk_id(partner_id)}!\n\n"
        f"⏳ Брак длится: {_duration_since(marriage['married_at'])}\n"
        f"📅 Дата регистрации: {_fmt_date(marriage['married_at'])}"
    )


def _already_married_reply(peer_id, who_id):
    marriage = db.get_active_marriage_for(peer_id, who_id)
    verb = gform(who_id, "уже женат на", "уже замужем за")
    name = display_name_by_vk_id(who_id)
    if not marriage:
        return f"💔 {name} {verb} ком-то неизвестном..."
    partner_id = _partner_id(marriage, who_id)
    return (
        f"💔 {name} {verb} {display_name_by_vk_id(partner_id)}.\n\n"
        f"⏳ Брак длится: {_duration_since(marriage['married_at'])}\n"
        f"📅 Дата регистрации: {_fmt_date(marriage['married_at'])}\n\n"
        "🚫 Не стоит портить чужие семьи!"
    )


def _proposal_keyboard(pid):
    return {
        "inline": True,
        "buttons": [[
            {"action": {"type": "callback", "label": "❤ Согласиться", "payload": json.dumps({"type": "marriage", "action": "accept", "pid": pid})}, "color": "positive"},
            {"action": {"type": "callback", "label": "💔 Отказать", "payload": json.dumps({"type": "marriage", "action": "decline", "pid": pid})}, "color": "negative"},
        ]],
    }


def _divorce_keyboard(did):
    return {
        "inline": True,
        "buttons": [
            [
                {"action": {"type": "callback", "label": "Подтвердить развод", "payload": json.dumps({"type": "divorce", "action": "confirm", "did": did})}, "color": "negative"},
                {"action": {"type": "callback", "label": "Отмена", "payload": json.dumps({"type": "divorce", "action": "cancel", "did": did})}, "color": "secondary"},
            ],
        ],
    }


def _delete_entry_message(entry):
    peer_id = entry.get("peer_id")
    cmid = entry.get("cmid")
    if peer_id is not None and cmid:
        try:
            vk.messages.delete(
                peer_id=peer_id,
                conversation_message_ids=[cmid],
                delete_for_all=1,
            )
            entry["message_id"] = None
            entry["cmid"] = None
            return
        except Exception as exc:
            pass

    message_id = entry.get("message_id")
    if message_id:
        try:
            vk.messages.delete(
                message_ids=[message_id],
                peer_id=peer_id,
                delete_for_all=1,
            )
        except Exception as exc:
            pass
    entry["message_id"] = None
    entry["cmid"] = None


def _arm_timer(store, key, seconds, on_expire):
    def fire():
        on_expire(key)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    with REGISTRY_LOCK:
        entry = store.get(key)
        if entry is None:
            return None
        old_timer = entry.get("timer")
        entry["timer"] = timer
    if old_timer:
        old_timer.cancel()
    timer.start()
    return timer


def _expire_proposal(pid):
    with REGISTRY_LOCK:
        proposal = PROPOSALS.get(pid)
    if proposal is None:
        return
    with proposal["lock"]:
        if proposal.get("status") != "active":
            return
        proposal["status"] = "expired"
    _delete_entry_message(proposal)
    target_id = proposal["target_id"]
    hesitated = gform(target_id, "так и не решился", "так и не решилась")
    try:
        vk.messages.send(
            peer_id=proposal["peer_id"],
            message=(
                f"⌛ {display_name_by_vk_id(target_id)} {hesitated} дать ответ — "
                "предложение руки и сердца сгорело 🔥"
            ),
            random_id=random.randrange(2**31),
        )
    except Exception:
        logger.exception("Не удалось отправить сообщение о сгоревшем предложении pid=%s", pid)
    with REGISTRY_LOCK:
        if PROPOSALS.get(pid) is proposal:
            PROPOSALS.pop(pid, None)


def _expire_divorce(did):
    with REGISTRY_LOCK:
        record = DIVORCES.get(did)
    if record is None:
        return
    with record["lock"]:
        if record.get("status") != "active":
            return
        record["status"] = "expired"
    _delete_entry_message(record)
    initiator_id = record["initiator_id"]
    changed_mind = gform(initiator_id, "передумал", "передумала")
    try:
        vk.messages.send(
            peer_id=record["peer_id"],
            message=(
                f"⌛ {display_name_by_vk_id(initiator_id)} {changed_mind} подтверждать развод — "
                "заявление аннулировано 📄✨"
            ),
            random_id=random.randrange(2**31),
        )
    except Exception:
        logger.exception("Не удалось отправить сообщение о сгоревшем разводе did=%s", did)
    with REGISTRY_LOCK:
        if DIVORCES.get(did) is record:
            DIVORCES.pop(did, None)


@command("брак")
def cmd_marriage(user, args, message):
    peer_id = message.get("peer_id")
    if peer_id is None or peer_id < 2000000000:
        return "💍 Браки заключаются только в беседах!"

    tokens = (args or "").split(maxsplit=1)
    head = tokens[0].lower() if tokens else ""
    rest = tokens[1] if len(tokens) > 1 else ""

    if head in ("запрос", "предложение"):
        return _propose_marriage(user, rest, message, peer_id)
    if head == "развод":
        if rest.strip():
            return None
        return _start_divorce(user, peer_id)

    target_id, remaining = extract_target_id(args, message.get("reply_message"))
    if remaining.strip():
        return None
    who_id = target_id if target_id and target_id > 0 else user["vk_id"]
    return _marriage_reply(peer_id, who_id)


@command("браки")
def cmd_marriages_list(user, args, message):
    if args.strip():
        return None

    peer_id = message.get("peer_id")
    if peer_id is None or peer_id < 2000000000:
        return "💍 Браки существуют только в беседах!"

    marriages = db.get_active_marriages(peer_id)
    if not marriages:
        return "💔 В этой беседе ещё нет ни одного брака! Станьте первой парой 💞"

    lines = ["💞 Браки беседы:", ""]
    for index, marriage in enumerate(marriages, start=1):
        duration = format_duration(max(0, (_now_utc() - _as_aware(marriage["married_at"])).total_seconds()))
        lines.append(
            f"{index}. {display_name_by_vk_id(marriage['user1_id'])} ❤️ "
            f"{display_name_by_vk_id(marriage['user2_id'])} ({duration})"
        )
    lines.append("")
    lines.append(f"Всего пар: {len(marriages)} 💒")
    return "\n".join(lines)


def _propose_marriage(user, args, message, peer_id):
    proposer_id = user["vk_id"]

    target_id, remaining = extract_target_id(args, message.get("reply_message"))
    if remaining.strip():
        return None
    if not target_id:
        return "💌 Укажите, кому предлагаете руку и сердце: брак запрос @user"
    if target_id == proposer_id:
        return "🪞 На себе жениться — оригинально, но нельзя!"
    if target_id == BOT_VK_ID and proposer_id != DEV_ID:
        return "💍 На боте женится только его разработчик 😏"
    if target_id < 0 and target_id != BOT_VK_ID:
        return "🤖 С другими сообществами браки не регистрируем. Только живые люди!"

    if target_id != BOT_VK_ID:
        member_ids = _chat_member_ids(peer_id)
        if member_ids is not None and target_id not in member_ids:
            return (
                f"🚫 {display_name_by_vk_id(target_id)} не состоит в этой беседе!\n\n"
                "Под венец зовём только тех, кто здесь присутствует 💍"
            )

    own_marriage = db.get_active_marriage_for(peer_id, proposer_id)
    if own_marriage:
        return (
            "🚩 Вы сами уже в браке!\n\n" + _already_married_reply(peer_id, proposer_id)
        )

    target_marriage = db.get_active_marriage_for(peer_id, target_id)
    if target_marriage:
        return _already_married_reply(peer_id, target_id)

    with REGISTRY_LOCK:
        for proposal in PROPOSALS.values():
            if proposal.get("peer_id") != peer_id or proposal.get("status") != "active":
                continue
            if proposal.get("proposer_id") == proposer_id:
                return "✈️ Ваше предыдущее предложение ещё летит. Дождитесь ответа!"
            if proposal.get("target_id") == target_id:
                return f"🤝 У {display_name_by_vk_id(target_id)} уже ждёт ответа одно предложение. Очередь!"

    pid = uuid.uuid4().hex[:10]
    proposal = {
        "pid": pid,
        "peer_id": peer_id,
        "proposer_id": proposer_id,
        "target_id": target_id,
        "status": "active",
        "lock": threading.Lock(),
    }
    with REGISTRY_LOCK:
        PROPOSALS[pid] = proposal

    decided = gform(proposer_id, "решил", "решила")
    is_bot_target = target_id == BOT_VK_ID
    if is_bot_target:
        tail = f"🤖 {display_name_by_vk_id(target_id)} обдумывает решение...\n\n⏳ Ответ будет совсем скоро!"
    else:
        tail = (
            f"{display_name_by_vk_id(target_id)}, Вы согласны вступить в брак?\n\n"
            "⏳ На раздумья 10 минут!"
        )
    text = (
        "💍 Минуточку внимания!\n\n"
        f"{display_name_by_vk_id(proposer_id)} {decided} взять руки и сердца у "
        f"{display_name_by_vk_id(target_id)}.\n\n{tail}"
    )
    try:
        send_kwargs = {
            "peer_ids": [peer_id],
            "message": text,
            "random_id": random.randrange(2**31),
        }
        if not is_bot_target:
            send_kwargs["keyboard"] = json.dumps(_proposal_keyboard(pid))
        sent = vk.messages.send(**send_kwargs)
        sent = sent[0] if isinstance(sent, list) else sent
    except Exception:
        logger.exception("Не удалось отправить предложение pid=%s", pid)
        with REGISTRY_LOCK:
            PROPOSALS.pop(pid, None)
        return "😢 Не удалось отправить предложение. Попробуйте ещё раз!"

    proposal["message_id"] = _extract_message_id(sent)
    if isinstance(sent, dict) and sent.get("conversation_message_id"):
        proposal["cmid"] = sent["conversation_message_id"]
    _arm_timer(PROPOSALS, pid, BUTTON_TTL_SECONDS, _expire_proposal)
    if target_id == BOT_VK_ID:
        _schedule_bot_accept(pid)
    return None


def _schedule_bot_accept(pid):
    timer = threading.Timer(2.0, _fire_bot_accept, args=(pid,))
    timer.daemon = True
    timer.start()


def _fire_bot_accept(pid):
    with REGISTRY_LOCK:
        proposal = PROPOSALS.get(pid)
    if proposal is None or proposal.get("status") != "active":
        return
    try:
        _accept_proposal(
            proposal,
            event_id="bot-%s" % pid,
            user_id=BOT_VK_ID,
            peer_id=proposal["peer_id"],
        )
        married = db.get_active_marriage_for(proposal["peer_id"], BOT_VK_ID)
        if married:
            pass
        else:
            logger.error(
                "Бот-согласие pid=%s: БРАК НЕ ЗАРЕГИСТРИРОВАН (занят или ошибка БД)", pid
            )
            _notify_dev_bot_marriage_failed(pid)
    except Exception:
        logger.exception("Бот не смог принять предложение pid=%s", pid)
        _notify_dev_bot_marriage_failed(pid)


def _notify_dev_bot_marriage_failed(pid):
    try:
        from utils.vk import notify_developer

        notify_developer(f"⚠️ Брак с ботом pid={pid} не зарегистрирован — глянь логи сервера.")
    except Exception:
        logger.exception("Не удалось уведомить разработчика о провале pid=%s", pid)


def _start_divorce(user, peer_id):
    initiator_id = user["vk_id"]

    marriage = db.get_active_marriage_for(peer_id, initiator_id)
    if not marriage:
        return f"🌬 {display_name_by_vk_id(initiator_id)}, вы и так свободны как ветер — разводить нечего!"

    mid = marriage["id"]
    with REGISTRY_LOCK:
        for record in DIVORCES.values():
            if record.get("mid") == mid and record.get("status") == "active":
                return "📄 Заявление на развод уже лежит выше. Подтвердите его!"

    did = uuid.uuid4().hex[:10]
    partner_id = _partner_id(marriage, initiator_id)
    record = {
        "did": did,
        "mid": mid,
        "peer_id": peer_id,
        "initiator_id": initiator_id,
        "partner_id": partner_id,
        "married_at": marriage["married_at"],
        "status": "active",
        "lock": threading.Lock(),
    }
    with REGISTRY_LOCK:
        DIVORCES[did] = record

    text = (
        "💔 Бракоразводный процесс...\n\n"
        f"{display_name_by_vk_id(initiator_id)}, вы уверены что хотите подать на развод?\n\n"
        f"⏳ Брак длится: {_duration_since(marriage['married_at'])}\n"
        f"📅 Дата регистрации: {_fmt_date(marriage['married_at'])}\n\n"
        "⏳ На подтверждение 10 минут!"
    )
    try:
        sent = vk.messages.send(
            peer_ids=[peer_id],
            message=text,
            random_id=random.randrange(2**31),
            keyboard=json.dumps(_divorce_keyboard(did)),
        )
        sent = sent[0] if isinstance(sent, list) else sent
    except Exception:
        logger.exception("Не удалось отправить подтверждение развода did=%s", did)
        with REGISTRY_LOCK:
            DIVORCES.pop(did, None)
        return "😢 Не удалось оформить заявление. Попробуйте ещё раз!"

    record["message_id"] = _extract_message_id(sent)
    if isinstance(sent, dict) and sent.get("conversation_message_id"):
        record["cmid"] = sent["conversation_message_id"]
    _arm_timer(DIVORCES, did, BUTTON_TTL_SECONDS, _expire_divorce)
    return None


def _accept_proposal(proposal, event_id, user_id, peer_id):
    target_id = proposal["target_id"]
    proposer_id = proposal["proposer_id"]
    peer = proposal["peer_id"]

    if user_id != target_id:
        return _reject(event_id, user_id, peer, random.choice(_WRONG_CLICK_PHRASES))

    note = None
    created = None
    with MARRIAGE_ACTION_LOCK:
        fresh_proposer = db.get_active_marriage_for(peer, proposer_id)
        fresh_target = db.get_active_marriage_for(peer, target_id)
        if fresh_proposer or fresh_target:
            busy_id = proposer_id if fresh_proposer else target_id
            taken = gform(busy_id, "уже занят", "уже занята")
            note = f"😢 {display_name_by_vk_id(busy_id)} {taken} другим браком. Свадьба отменяется..."
            proposal["status"] = "done"
        else:
            created = db.create_chat_marriage(peer, proposer_id, target_id)
            if created:
                proposal["status"] = "done"
            else:
                logger.error("Не удалось записать брак pid=%s в БД", proposal["pid"])
                proposal["status"] = "failed"

    if proposal.get("status") != "done":
        return None

    with REGISTRY_LOCK:
        if PROPOSALS.get(proposal["pid"]) is proposal:
            PROPOSALS.pop(proposal["pid"], None)
    timer = proposal.pop("timer", None)
    if timer:
        timer.cancel()

    _delete_entry_message(proposal)

    if not created:
        if note:
            try:
                vk.messages.send(peer_id=peer, message=note, random_id=random.randrange(2**31))
            except Exception:
                logger.exception("Не удалось отправить заметку об отмене свадьбы pid=%s", proposal["pid"])
        return None

    accepted = gform(target_id, "принял", "приняла")
    caption = (
        "💒 Хорошие новости!\n\n"
        f"{display_name_by_vk_id(target_id)} {accepted} решение о вступлении в брак.\n\n"
        f"С этой минуты {display_name_by_vk_id(proposer_id)} и "
        f"{display_name_by_vk_id(target_id)} теперь супруги! 💕"
    )

    try:
        image = build_marriage_certificate(peer, proposer_id, target_id, created["married_at"])
        send_certificate_to_chat(peer, caption, image.getvalue())
    except Exception:
        logger.exception("Свидетельство о браке не отправилось pid=%s — шлём текстом", proposal["pid"])
        try:
            vk.messages.send(peer_id=peer, message=caption, random_id=random.randrange(2**31))
        except Exception:
            logger.exception("Не удалось отправить даже текст о браке pid=%s", proposal["pid"])
    return None


def _decline_proposal(proposal, event_id, user_id, peer_id):
    target_id = proposal["target_id"]
    proposer_id = proposal["proposer_id"]
    peer = proposal["peer_id"]

    if user_id != target_id:
        return _reject(event_id, user_id, peer, random.choice(_WRONG_CLICK_PHRASES))

    proposal["status"] = "done"
    with REGISTRY_LOCK:
        if PROPOSALS.get(proposal["pid"]) is proposal:
            PROPOSALS.pop(proposal["pid"], None)
    timer = proposal.pop("timer", None)
    if timer:
        timer.cancel()

    _delete_entry_message(proposal)

    declined = gform(target_id, "отказал", "отказала")
    caption = (
        "💔 Разбитое сердце...\n\n"
        f"{display_name_by_vk_id(proposer_id)}, к сожалению {display_name_by_vk_id(target_id)} "
        f"{declined} Ваше предложение о вступлении в брак.\n\n"
        "Может оно и к лучшему? 🌚"
    )
    try:
        vk.messages.send(peer_id=peer, message=caption, random_id=random.randrange(2**31))
    except Exception:
        logger.exception("Не удалось отправить сообщение об отказе pid=%s", proposal["pid"])
    return None


def _confirm_divorce(record, event_id, user_id, peer_id):
    initiator_id = record["initiator_id"]
    partner_id = record["partner_id"]
    peer = record["peer_id"]

    if user_id != initiator_id:
        return _reject(event_id, user_id, peer, random.choice(_WRONG_CLICK_PHRASES))

    ended = None
    with MARRIAGE_ACTION_LOCK:
        ended = db.end_chat_marriage(record["mid"])
        if ended:
            record["status"] = "done"
            record["ended"] = ended
    if not ended:
        record["status"] = "stale"

    if record["status"] != "done":
        with REGISTRY_LOCK:
            if DIVORCES.get(record["did"]) is record:
                DIVORCES.pop(record["did"], None)
        _delete_entry_message(record)
        return None

    with REGISTRY_LOCK:
        if DIVORCES.get(record["did"]) is record:
            DIVORCES.pop(record["did"], None)
    timer = record.pop("timer", None)
    if timer:
        timer.cancel()

    _delete_entry_message(record)

    caption = (
        "📜 Официально: брак расторгнут!\n\n"
        f"{display_name_by_vk_id(partner_id)}, к сожалению у вас был расторгнут брак — "
        f"{display_name_by_vk_id(initiator_id)} принял решение о расторжении.\n\n"
        "Вы теперь свободные люди, возможно оно и к лучшему? 🕊"
    )
    ended = record["ended"]
    try:
        image = build_divorce_certificate(
            peer,
            ended["user1_id"],
            ended["user2_id"],
            ended["married_at"],
            ended["divorced_at"],
        )
        send_certificate_to_chat(peer, caption, image.getvalue())
    except Exception:
        logger.exception("Свидетельство о разводе не отправилось did=%s — шлём текстом", record["did"])
        try:
            vk.messages.send(peer_id=peer, message=caption, random_id=random.randrange(2**31))
        except Exception:
            logger.exception("Не удалось отправить даже текст о разводе did=%s", record["did"])
    return None


def _cancel_divorce(record, event_id, user_id, peer_id):
    if user_id != record["initiator_id"]:
        return _reject(event_id, user_id, record["peer_id"], random.choice(_WRONG_CLICK_PHRASES))

    record["status"] = "cancelled"
    with REGISTRY_LOCK:
        if DIVORCES.get(record["did"]) is record:
            DIVORCES.pop(record["did"], None)
    timer = record.pop("timer", None)
    if timer:
        timer.cancel()

    _delete_entry_message(record)
    return _reject(
        event_id,
        user_id,
        record["peer_id"],
        f"{_plain(user_id)}, быстро же вы передумали, но оно и к лучшему! 😉",
    )


def handle_message_event(data):
    obj = data.get("object") or {}
    payload_raw = obj.get("payload")
    if not payload_raw:
        return None

    if isinstance(payload_raw, dict):
        payload = payload_raw
    else:
        try:
            payload = json.loads(payload_raw)
        except Exception:
            return None

    ptype = payload.get("type")
    action = payload.get("action")
    if ptype not in ("marriage", "divorce"):
        return None

    event_id = obj.get("event_id") or data.get("event_id")
    user_id = data.get("user_id") or obj.get("user_id")
    peer_id = data.get("peer_id") or obj.get("peer_id")

    if event_id is not None:
        _purge_processed_events()
        if not _mark_event_processed(event_id):
            return None

    store = PROPOSALS if ptype == "marriage" else DIVORCES
    key = payload.get("pid") if ptype == "marriage" else payload.get("did")

    with REGISTRY_LOCK:
        entry = store.get(key)
    if entry is None:
        _delete_orphan_button_message(obj, peer_id)
        return DEAD_SESSION

    clicked_cmid = obj.get("conversation_message_id")
    if clicked_cmid:
        entry["cmid"] = clicked_cmid

    with entry["lock"]:
        if entry.get("status") != "active":
            return None
        if ptype == "marriage":
            if action == "accept":
                return _accept_proposal(entry, event_id, user_id, peer_id)
            if action == "decline":
                return _decline_proposal(entry, event_id, user_id, peer_id)
        else:
            if action == "confirm":
                return _confirm_divorce(entry, event_id, user_id, peer_id)
            if action == "cancel":
                return _cancel_divorce(entry, event_id, user_id, peer_id)
    return None
