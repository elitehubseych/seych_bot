
import json
import logging
import random
import threading
import time
import unicodedata

import db
from vk_api import VkApi
from vk_api.exceptions import ApiError

from config import config

logger = logging.getLogger(__name__)

vk_session = VkApi(token=config.TOKEN_GROUP, api_version="5.199")
vk = vk_session.get_api()

_SEX_CACHE = {}
_SEX_LOCK = threading.Lock()

_FULLNAME_CACHE = {}
_FULLNAME_LOCK = threading.Lock()

_DISPLAY_CACHE = {}
_DISPLAY_LOCK = threading.Lock()
_DISPLAY_TTL = 60


def user_sex(vk_id):
    with _SEX_LOCK:
        cached = _SEX_CACHE.get(vk_id)
    if cached in (1, 2):
        return cached
    if vk_id < 0:
        _SEX_CACHE[vk_id] = 2
        return 2
    try:
        info = vk.users.get(user_ids=vk_id, fields="sex")
        sex = int(info[0].get("sex") or 0)
    except Exception as error:
        logger.warning("Не удалось определить пол %s: %s", vk_id, error)
        sex = 0
    if sex not in (1, 2):
        sex = 2
    with _SEX_LOCK:
        _SEX_CACHE[vk_id] = sex
    return sex


def gform(vk_id, male_form, female_form):
    return female_form if user_sex(vk_id) == 1 else male_form


def send_reply(peer_id, conversation_message_id, text):
    forward = {
        "peer_id": peer_id,
        "conversation_message_ids": [conversation_message_id],
        "is_reply": 1,
    }
    return _send(peer_id, text, forward=forward)


def send_plain(peer_id, text):
    """Обычное сообщение без reply — быстрее и не нагружает чат."""
    return _send(peer_id, text, forward=None)


def _send(peer_id, text, forward):
    kwargs = {
        "peer_id": peer_id,
        "message": text,
        "random_id": random.randrange(2**31),
    }
    if forward:
        kwargs["forward"] = json.dumps(forward)
    try:
        vk.messages.send(**kwargs)
        return True
    except ApiError as error:
        if error.code == 901:
            logger.warning(
                "Юзер %s не знаком с ботом (ошибка 901), сообщение не отправлено",
                peer_id,
            )
        else:
            logger.error("Ошибка VK API peer_id=%s: %s", peer_id, error)
        return False
    except Exception as error:
        logger.error("Ошибка отправки сообщения peer_id=%s: %s", peer_id, error)
        return False


def _safe_text(text):
    """Убирает только невидимые управляющие символы, текст не трогаем."""
    cleaned = "".join(
        ch for ch in str(text or "")
        if unicodedata.category(ch) not in ("Cf", "Cc")
    )
    return " ".join(cleaned.split()) or str(text or "").strip()


def mention(vk_id, text):
    text = _safe_text(text)
    if vk_id < 0:
        return f"[club{abs(vk_id)}|{text}]"
    return f"[id{vk_id}|{text}]"


_GROUP_INFO_CACHE = {}
_GROUP_INFO_LOCK = threading.Lock()


def group_info(group_id):
    key = int(group_id)
    with _GROUP_INFO_LOCK:
        cached = _GROUP_INFO_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        raw = vk.groups.getById(group_id=str(key))
        items = raw.get("groups") if isinstance(raw, dict) else raw
        info = (items or [{}])[0] or {}
    except Exception as error:
        logger.warning("Не удалось получить данные сообщества %s: %s", key, error)
        info = {}
    with _GROUP_INFO_LOCK:
        _GROUP_INFO_CACHE[key] = info
    return info


def prefetch_full_names(vk_ids):
    """Загружает имена пачками (до 100 за один запрос) в кэш."""
    ids = []
    for vk_id in vk_ids:
        try:
            vk_id = int(vk_id)
        except Exception:
            continue
        if vk_id > 0 and not _FULLNAME_CACHE.get(vk_id):
            ids.append(vk_id)
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        try:
            info = vk.users.get(user_ids=",".join(str(i) for i in chunk))
        except Exception as error:
            logger.warning("Не удалось пакетно получить имена: %s", error)
            break
        for item in info or []:
            vk_id = int(item.get("id") or 0)
            name = _safe_text(f"{item.get('first_name', '')} {item.get('last_name', '')}").strip()
            if vk_id and name:
                with _FULLNAME_LOCK:
                    _FULLNAME_CACHE[vk_id] = name


def get_full_name(vk_id):
    with _FULLNAME_LOCK:
        cached = _FULLNAME_CACHE.get(vk_id)
    if cached:
        return cached
    name = None
    try:
        if vk_id < 0:
            name = group_info(abs(vk_id)).get("name") or "Группа"
        else:
            info = vk.users.get(user_ids=vk_id)
            if info:
                name = f"{info[0]['first_name']} {info[0]['last_name']}"
    except Exception as error:
        logger.error("Не удалось получить имя VK для %s: %s", vk_id, error)
    if not name:
        return "Группа" if vk_id < 0 else "Пользователь"
    name = _safe_text(name)
    with _FULLNAME_LOCK:
        _FULLNAME_CACHE[vk_id] = name
    return name


def display_name(user):
    nickname = user.get("nickname") if user else None
    vk_id = user["vk_id"] if user else None
    if not vk_id:
        return "Пользователь"
    if nickname:
        return mention(vk_id, nickname)
    return mention(vk_id, get_full_name(vk_id))


def short_name(vk_id):
    """Имя/ник без [id|...] — для снекберов."""
    user = db.get_user_readonly(vk_id)
    if user and user.get("nickname"):
        return user["nickname"]
    full = get_full_name(vk_id)
    return full if full else str(vk_id)


def display_name_by_vk_id(vk_id):
    # кеш на 60 сек — убирает N запросов в титулах/топах
    now = time.monotonic()
    with _DISPLAY_LOCK:
        ent = _DISPLAY_CACHE.get(vk_id)
        if ent and now - ent[0] < _DISPLAY_TTL:
            return ent[1]
    user = db.get_user_readonly(vk_id)
    res = display_name(user) if user else display_name({"vk_id": vk_id})
    with _DISPLAY_LOCK:
        _DISPLAY_CACHE[vk_id] = (now, res)
    return res


def bot_mention():
    """Кликабельное упоминание бота: [clubID|Имя]."""
    try:
        gid = int(str(config.ID_GROUP).strip())
        info = group_info(gid)
        name = (info.get("name") or info.get("screen_name") or "Бот")
        return mention(-abs(gid), name)
    except Exception:
        return mention(-1, "Бот")


def notify_developer(text):
    try:
        vk.messages.send(
            user_id=int(config.DEV_ID),
            message=text,
            random_id=random.randrange(2**31),
        )
        return True
    except Exception as error:
        logger.error("Не удалось уведомить разработчика: %s", error)
        return False
