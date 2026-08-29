
import json
import random
import re
import threading
import time
import uuid

import requests

import db

from config import config
from handlers import loans
from handlers.registry import DEAD_SESSION, command
from utils.parse import extract_target_id, format_amount, parse_amount
from utils.vk import display_name, display_name_by_vk_id, mention, vk, vk_session

GAMES = {}
GAMES_LOCK = threading.Lock()
PROCESSED_EVENT_IDS = {}
DEDUP_LOCK = threading.Lock()
GAME_TIMEOUT_SECONDS = 120
MUTE_SECONDS = 120
MIN_STAKE = 100
DEFAULT_STAKE = 1000
ROULETTE_SIDES = ("влево", "прямо", "вправо")
FEEDBACK_LIFETIME_SECONDS = 10

try:
    BOT_VK_ID = -abs(int(str(config.ID_GROUP).strip()))
except Exception:
    BOT_VK_ID = 0

COIN_SIDES = ("орел", "решка")


def _purge_processed_events():
    now = time.time()
    expired = [event_id for event_id, ts in PROCESSED_EVENT_IDS.items() if now - ts > 300]
    for event_id in expired:
        PROCESSED_EVENT_IDS.pop(event_id, None)


def _mark_event_processed(event_id):
    with DEDUP_LOCK:
        if event_id in PROCESSED_EVENT_IDS:
            return False
        PROCESSED_EVENT_IDS[event_id] = time.time()
        return True


def _game_lock(game):
    lock = game.get("lock")
    if lock is None:
        lock = threading.Lock()
        game["lock"] = lock
    return lock


def _user_balance(vk_id):
    user = db.get_user(vk_id)
    return user["balance"] if user else 0


def _can_join(vk_id, amount):
    if vk_id == BOT_VK_ID:
        return True
    return _user_balance(vk_id) >= amount


def _delete_orphan_button_message(obj, peer_id):
    conversation_message_id = obj.get("conversation_message_id")
    if not conversation_message_id or peer_id is None:
        return
    try:
        vk.messages.delete(
            peer_id=peer_id,
            conversation_message_ids=[conversation_message_id],
            delete_for_all=1,
        )
    except Exception as exc:
        pass


def _send_event_answer(event_id, user_id, peer_id, text):
    params = {
        "event_id": event_id,
        "user_id": user_id,
        "peer_id": peer_id,
        "event_data": json.dumps({"type": "show_snackbar", "text": text}, ensure_ascii=False),
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
            body = resp.text or ""
        except Exception as exc:
            time.sleep(0.35)
            continue
        if '"response":1' in body:
            return True
        time.sleep(0.35)
    return False


def _temp_chat_message(peer_id, text):
    try:
        sent = vk.messages.send(peer_id=peer_id, message=text, random_id=random.randrange(2**31))
        mid = _extract_message_id(sent)

        def _cleanup():
            try:
                vk.messages.delete(message_ids=mid, delete_for_all=1)
            except Exception:
                pass

        timer = threading.Timer(FEEDBACK_LIFETIME_SECONDS, _cleanup)
        timer.daemon = True
        timer.start()
    except Exception:
        pass


def _reject(event_id, user_id, peer_id, text):
    delivered = False
    if event_id and user_id and peer_id:
        delivered = _send_event_answer(event_id, user_id, peer_id, text)
    if not delivered:
        _temp_chat_message(peer_id, text)
    return {"type": "show_snackbar", "text": text}


def _self_click_message():
    return random.choice([
        "КУДЫ ТЫ ЖМАЛ\nПодумай головой\nРазве это твои кнопки?",
        "Твои кнопки не для тебя\nНе жми на чужое!",
        "Потише, это не твой ход\nПодумай головой!",
        "Своё не трогай\nЭто не твоя кнопка!",
        "Руки убрал\nЭто не твоё!",
        "Ай-яй-яй\nЧужие кнопки жмём?",
        "Э, стоп!\nКуда лапы тянешь?",
        "Мимо касса\nИди своей дорогой",
        "Не твоя улица, пацан\nПройди мимо",
        "Ты кто такой?\nДавай, до свидания!",
        "Кнопка занята\nПриходи в следующий раз",
        "Не твоё дело\nНе суйся!",
        "Отойди от кнопки\nОна тебя боится",
        "Слышь, сосед\nКнопка чужая, понял?",
        "Тут приватная территория\nУбирайся!",
        "Жми свои кнопки\nЕсли найдёшь их",
        "Опять ты?\nДай другим нажать",
        "Палец долой\nКнопка не резиновая",
        "Не лазь!\nБатя в здании",
        "Ты кнопки путаешь\nИди проспись",
        "Эта кнопка на гарантии\nТы её сломаешь",
        "Кнопке больно\nХватит её тыкать",
        "Всё, кнопка обиделась\nТеперь она с тобой не разговаривает",
        "Кнопка вызвала охрану\nБеги!",
        "Ты серьёзно?\nЕщё раз жмёшь?",
        "Ну ты и наглый\nЧужое жмёшь и не краснеешь",
        "Смелый шаг\nНо неправильный",
        "Ошибка 404\nТвои права на кнопку не найдены",
        "Доступ запрещён\nОбратись к админу кнопки",
        "Кнопка под паролем\nА ты его не знаешь",
        "Здесь была кнопка\nА теперь разочарование",
        "Поздравляю!\nТы нашёл чужую кнопку",
        "Нажато успешно\nЖертва: твоё самолюбие",
        "Кнопка приняла твой клик\nИ посмеялась над ним",
        "Твой палец устал?\nКнопка — нет",
        "Так, стоп\nЯ вызываю модератора",
        "Скриншот отправлен\nТвоей маме",
        "Кнопка записала тебя в чёрный список",
        "Не сегодня\nМожет завтра повезёт",
        "Шанс был один\nИ ты его профукал",
        "Почти получилось\nНет, не получилось",
        "Попробуй ещё раз\nЧерез годок",
        "Кнопка думает\nЧто ты странный",
        "Мы всё видим\nДа-да, именно ты",
        "Записали\nВ списке позора ты первый",
        "Тихо-тихо\nНикто ничего не видел... кроме всех",
        "Серьёзно?\nПрям вот так вот?",
        "Ладно\nОдна попытка была у Вселенной",
        "Кнопка выше твоего уровня\nПрокачайся и возвращайся",
        "Не заслужил\nРаботай над собой",
        "Сначала заработай право\nПотом жми",
        "Тут даже пробовать не стоит\nНо ты попробовал. Молодец. Нет.",
        "Кнопка закрыта на переучёт\nПриходите позже",
        "Техническое обслуживание\nТвоего мозга требуется",
        "Инструкция по кнопкам\nГлава 1: читай чужие метки",
        "Ты жмёшь как в последний раз\nКак будто других кнопок нет",
        "Гениальный ход\nЖаль, что не тот",
        "Красиво нажал\nАж плакать хочется. От жалости.",
    ])


def _plain_name(vk_id):
    return re.sub(r"\[(?:id|club)\d+\|([^\]]+)\]", r"\1", display_name_by_vk_id(vk_id))


_ROULETTE_TURN_PHRASES = [
    "Сейчас он ходит\nРуки убрал!",
    "Не твой ход\nЖди своей очереди",
    "Руки убрал\nСтреляет другой",
    "Куда лезешь?\nОчередь не твоя",
    "Отойди от кнопки\nСейчас стреляет не ты",
    "Ходит другой игрок\nПодожди, герой",
    "Пистолет не твой\nНе хватай его",
    "Сиди жди\nДойдут и до тебя",
    "Спокойно\nНе твоя секунда",
    "Стреляет сосед\nНаберись терпения",
    "Э, шустрый!\nЧья очередь? Не твоя",
    "Подожди\nДо тебя дойдёт, если повезёт",
    "Не суетись\nПуля подождёт",
    "Твоя мама знает,\nчто ты чужие кнопки жмёшь?",
    "Мама говорила:\nне лезь, пока не спросили",
    "Даже мамка знает,\nчто сейчас не твой ход",
    "Очередь занята\nПриходи позже",
    "Ага, конечно\nСтреляй... шучу, сиди",
    "Не твоё\nВот прям совсем не твоё",
    "Так-так-так\nКто тут лишний?",
    "Опять ты?\nДай другому поиграть",
    "Терпение, падаван\nСейчас ход сильнейших",
    "Сначала вырасти\nПотом стреляй",
    "Твоя очередь будет\nЛет через пять",
    "Не наглей\nОчередь одна на всех",
    "Жди\nКак все нормальные люди",
    "Ходит он\nА ты пока зритель",
    "Займись чем-нибудь\nНапример подожди",
    "Тихо\nИдёт процесс",
    "Не жми\nОна взрывается",
    "Последнее предупреждение\nЕщё раз — и маме расскажу",
    "Маме уже пожаловался?\nИли сначала дожми кнопку?",
    "Скрин твоего клика\nуже летит твоей маме",
    "Правило тут одно:\nкто не в очереди — тот молчит",
    "Ты вообще правила читал?\nПервое правило: жди",
    "Иди чай завари\nПока другие стреляют",
    "Секунду выдержать не можешь?\nА пуля выдержит",
    "Шаг влево, шаг вправо\nВсё равно ход не твой",
    "Запомни этот клик\nТочный выстрел — мимо очереди",
    "Учи матчасть\nПункт первый: не твой ход",
    "Не сегодня\nТвой ход был в прошлой жизни",
    "Кнопка думает, что ты странный\nЯ с ней согласен",
    "Мы всё видим\nДа-да, именно ты",
    "Записали\nВ списке нетерпеливых ты первый",
    "Красиво жмёшь\nТолько мимо своей очереди",
    "Гениальный план\nЖаль, что ход не твой",
    "Смелый клик\nНо неправильный",
    "Доступ запрещён\nОбратись к владельцу очереди",
    "Ошибка 404\nТвоя очередь не найдена",
    "Техническое обслуживание\nТвоего терпения требуется",
]


def _roulette_turn_phrase():
    return random.choice(_ROULETTE_TURN_PHRASES)


_POOR_CLICKER_PHRASES = [
    "У тебя нет элитов\nИди отсюда",
    "Бомжи тут не нужны\nЭлитов нет — иди работай",
    "С таким балансом можно\nтолько смотреть",
    "Элитов нет?\nКнопки для богатых, извини",
    "Тыщу раз говорили:\nсначала элиты, потом кнопки",
    "Пустой кошелёк —\nпустые клики",
    "Иди заработай\nПотом приходи играть",
    "Баланс грустный\nКак твои шансы на победу",
    "Нет элитов — нет игры\nТакие правила",
    "Бедность не порок,\nно играть мешает",
    "Кошелёк пустой\nПрям как обещания твоей мамы купить тебе элиты",
    "Мама элитов не дала?\nРабота есть на заводе",
    "Даже твоя мама в курсе,\nчто ты без элитов",
    "Пожалуйся маме\nМожет она тебе элитов накинет",
    "Ты кто такой?\nДавай, до свидания! Элитов нет — игнор",
    "Ноль на счету?\nНу хоть что-то ты не просадил",
    "Банк отказал?\nМы тоже отказываем",
    "Кредит не выдадим\nЭлитов нет — приходи позже",
    "Тут играют на элиты,\nа не на честном слове",
    "Скидок нет\nРассрочки тоже нет\nЭлитов нет — иди отсюда",
    "Не расстраивайся\nВсё равно бы проиграл",
    "Сначала копи\nПотом проигрывай как все",
    "Твоя ставка отклонена\nПричина: ты бомж",
    "Ошибка 402\nPayment Required. Точнее — элиты required",
    "Элитов ноль?\nКрасиво живёшь, но не играешь",
    "Донать\nИли терпи",
    "Работай больше\nИграй больше\nПока только первое",
    "Такие дела\nБез элитов даже пуля мимо полетит",
    "Тебе сюда нельзя\nДресс-код: наличие элитов",
    "Вход платный\nУ тебя ни копейки",
    "Аукцион закрыт для тебя\nБаланс не тот",
    "С таким балансом\nдаже монетка не бросится",
    "Элитов нет\nШансов тоже нет\nНо клики бесплатные, жми",
    "Проверил кошелёк?\nПроверь ещё раз\nВсё равно пусто",
    "Не грусти\nБогатыми не рождаются\nТы вот точно не рождался",
    "Твой баланс плачет\nУспокой его работой",
    "Здесь элиты решают\nУ тебя решает ничего",
    "Извини, нищий\nСегодня не твой день\nКак и вчера",
    "Бомж-режим активирован\nКнопки заблокированы",
    "Ты бы ещё камнем расплатился",
    "Долги раздал?\nЗаходи, когда отдашь",
    "Экономика страдает\nКогда такие как ты жмут кнопки",
    "Скопи на ставку\nНачни с мелочи\nНапример с работы",
    "Твой баланс — это шутка?\nСмешно, да",
    "Нечего ставить —\nнечего и нажимать",
    "Вернись, когда будут элиты\nМы подождём\nВечно",
    "Ставку не примем\nБлаготворительностью не займёмся",
    "У нас всё честно\nПоэтому ты и не играешь",
    "Элиты кончились?\nИди покушай чая",
    "Следующий!\nА, это ты... мимо",
]


def _poor_clicker_phrase():
    return random.choice(_POOR_CLICKER_PHRASES)


_BROKE_TARGET_TEMPLATES = [
    "{name}, сегодня на бедном,\nу него не хватает средств",
    "{name} без элитов\nПришёл к нам нищим\nУходит нищим",
    "У {name} элитов нет\nИгра отменяется по бедности",
    "{name}, у тебя пусто\nИди заработай, потом играй",
    "{name} хочет играть,\nно кошелёк против",
    "Не судьба: у {name}\nэлитов меньше, чем совести",
    "{name}, ты бомж сегодня\nВозвращайся с элитами",
    "Вызов отклонён:\n{name} банкрот",
    "{name}, где элиты?\nВот там и игра",
    "У {name} баланс как у моей мамы\nТо есть ноль",
    "{name} бедный сегодня\nДайте ему монетку... то есть элитов",
    "{name}, даже пуля не полетит\n— денег нет",
    "Игра не состоится:\n{name} финансово несостоятелен",
    "{name} хотел рискнуть,\nно рисковать нечем",
    "Элитов у {name} нет,\nзато смелости вагон",
    "{name}, сначала работа,\nпотом рулетка",
    "Отказано: {name}\nПричина — пустой карман",
    "{name}, иди копи\nМы подождём. Вечно.",
    "Баланс {name} плачет",
    "{name}, ты сегодня зритель\nБилет стоит элиты",
    "У {name} не хватает средств\nЗато самооценка зашкаливает",
    "{name} и деньги —\nистория несбывшейся любви",
    "{name}, вернись с элитами\nКнопки подождут",
    "Никакой игры:\n{name} сегодня в минусе ещё до начала",
    "{name}, мама сказала:\nсначала элиты, потом дуэли",
    "{name}, твой кошелёк пуст\nКак обещания политиков",
    "Объявляем сбор для {name}\nЕму нужно немного элитов",
    "{name}, ты не прошёл проверку\nПроверка называется «баланс»",
    "У {name} элитов ноль\nНо уверенности — миллион",
    "{name}, бедность — не приговор\nНо сегодня — приговор",
    "Игра с {name} отменяется\nФорс-мажор: нет денег",
    "{name}, иди проси у мамы\nМожет даст на элиты",
    "{name} пытался зайти в игру\nНо дверь была платная",
    "{name}, твой баланс — тайна\nДаже для тебя самого\n(его нет)",
    "Не пускаем {name}\nКарантин по бедности",
    "{name}, элиты где?\nВопрос риторический",
    "Сегодня {name} не стреляет\nСтреляет его пустота баланса",
    "{name}, ну не сегодня\nСовсем не сегодня",
    "У {name} минус на душе\nИ почти минус на балансе",
    "{name}, копи дальше\nДо встречи через годик",
    "Отказ: {name}\nОснование — финансовая немощь",
    "{name}, ты бы ещё долгами играл",
    "{name} без элитов —\nкак пистолет без пули",
    "Элиты {name} закончились,\nне начавшись",
    "{name}, возвращайся,\nкогда будет что терять",
    "{name}, сегодня ты не игрок\nСегодня ты урок",
    "{name}, даже банк тебе отказал\nА он всем даёт",
    "{name}, твой вход закрыт\nТабличка: «Только для платёжеспособных»",
    "{name}, не грусти\nБогатые тоже плакали\nПравда, в другие игры",
    "{name}, сегодня на бедном\nПриходи, когда появятся элиты",
]


def _broke_target_reply(name):
    return random.choice(_BROKE_TARGET_TEMPLATES).format(name=name)


def _send_duel_prompt(peer_id, text, keyboard, reply_to=None):
    params = {
        "peer_ids": [peer_id],
        "message": text,
        "random_id": random.randrange(2 ** 31),
        "keyboard": json.dumps(keyboard),
    }
    return vk.messages.send(**params)


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


def _fetch_cmid(peer_id, message_id):
    return None


def _delete_game_message(game):
    peer_id = game.get("peer_id")
    if peer_id is None:
        return

    cmid = game.get("cmid")
    if cmid:
        try:
            vk.messages.delete(
                peer_id=peer_id,
                conversation_message_ids=[cmid],
                delete_for_all=1,
            )
            game["message_id"] = None
            game["cmid"] = None
            return
        except Exception as exc:
            pass

    raw_message_id = game.get("message_id")
    if raw_message_id is not None:
        items = raw_message_id if isinstance(raw_message_id, (list, tuple)) else [raw_message_id]
        ids = [
            item.get("message_id") or item.get("id") if isinstance(item, dict) else item
            for item in items
        ]
        ids = [item for item in ids if item is not None]
        if ids:
            try:
                vk.messages.delete(message_ids=ids, peer_id=peer_id, delete_for_all=1)
            except Exception as exc:
                pass

    game["message_id"] = None
    game["cmid"] = None


def _send_result(game, text, keyboard=None):
    params = {
        "peer_id": game["peer_id"],
        "message": text,
        "random_id": random.randrange(2 ** 31),
    }
    if keyboard is not None:
        params["keyboard"] = json.dumps(keyboard)
    return vk.messages.send(**params)


def _replace_game_message(game, text, keyboard):
    cmid = game.get("cmid")
    if cmid and game.get("peer_id") is not None:
        try:
            params = {
                "peer_id": game["peer_id"],
                "conversation_message_id": cmid,
                "message": text,
                "keyboard": json.dumps(keyboard),
            }
            if vk.messages.edit(**params):
                game["prompt"] = text
                return
        except Exception:
            pass
    _delete_game_message(game)
    try:
        params = {
            "peer_ids": [game["peer_id"]],
            "message": text,
            "random_id": random.randrange(2**31),
            "keyboard": json.dumps(keyboard),
        }
        sent = vk.messages.send(**params)
        game["cmid"] = _extract_message_id(sent) or game.get("cmid")
        game["prompt"] = text
    except Exception:
        pass


def _cleanup_peer_games(peer_id):
    with GAMES_LOCK:
        stale = [
            game for game in GAMES.values()
            if game.get("peer_id") == peer_id and game.get("status") == "active"
        ]
        for game in stale:
            game["status"] = "expired"
    for game in stale:
        with _game_lock(game):
            _delete_game_message(game)
    if stale:
        with GAMES_LOCK:
            for game in stale:
                if GAMES.get(game["game_id"]) is game:
                    GAMES.pop(game["game_id"], None)


def _timeout_game(game_id, gen):
    with GAMES_LOCK:
        game = GAMES.get(game_id)
    if not game:
        return
    with _game_lock(game):
        if game.get("status") != "active":
            return
        if game.get("timeout_gen") != gen:
            return
        game["status"] = "expired"
    try:
        _send_result(
            game,
            f"{display_name_by_vk_id(game['initiator_id'])} ждал 2 минуты без ответа после последнего хода.\nИгра была завершена ⏳",
        )
    except Exception:
        pass
    _delete_game_message(game)
    with GAMES_LOCK:
        if GAMES.get(game_id) is game:
            GAMES.pop(game_id, None)


def _start_timeout(game_id):
    timer = None
    old_timer = None
    gen = None
    with GAMES_LOCK:
        game = GAMES.get(game_id)
        if not game:
            return
        game["timeout_gen"] = gen = game.get("timeout_gen", 0) + 1
        old_timer = game.get("timer")
        timer = threading.Timer(GAME_TIMEOUT_SECONDS, _timeout_game, args=(game_id, gen))
        timer.daemon = True
        game["timer"] = timer
    if old_timer:
        old_timer.cancel()
    timer.start()


def _fire_bot_pick(game_id):
    with GAMES_LOCK:
        game = GAMES.get(game_id)
    if not game or game.get("status") != "active":
        return
    with _game_lock(game):
        if game.get("status") != "active":
            return
        if game.get("target_id") != BOT_VK_ID or game.get("accepted_id") is not None:
            return
        side = random.choice(COIN_SIDES)
        _process_coin_click(game, BOT_VK_ID, side)


def _schedule_bot_pick(game_id):
    timer = threading.Timer(1.5, _fire_bot_pick, args=(game_id,))
    timer.daemon = True
    timer.start()


def _coin_keyboard(game_id):
    return {
        "inline": True,
        "buttons": [
            [
                {"action": {"type": "callback", "label": "Орел", "payload": json.dumps({"type": "coin", "game_id": game_id, "side": "орел"})}, "color": "positive"},
                {"action": {"type": "callback", "label": "Решка", "payload": json.dumps({"type": "coin", "game_id": game_id, "side": "решка"})}, "color": "negative"},
            ],
            [
                {"action": {"type": "callback", "label": "Отмена", "payload": json.dumps({"type": "coin", "game_id": game_id, "side": "cancel"})}, "color": "secondary"},
            ],
        ],
    }


def _roulette_keyboard(game_id):
    return {
        "inline": True,
        "buttons": [[
            {"action": {"type": "callback", "label": "Влево", "payload": json.dumps({"type": "roulette", "game_id": game_id, "side": "влево"})}, "color": "primary"},
            {"action": {"type": "callback", "label": "Прямо", "payload": json.dumps({"type": "roulette", "game_id": game_id, "side": "прямо"})}, "color": "secondary"},
            {"action": {"type": "callback", "label": "Вправо", "payload": json.dumps({"type": "roulette", "game_id": game_id, "side": "вправо"})}, "color": "negative"},
        ]],
    }


def _settle(game, winner_id, loser_id):
    amount = game["amount"]
    collected = 0
    if bool(game.get("escrow")):
        if winner_id != BOT_VK_ID:
            db.update_balance(winner_id, amount * 2)
            collected = loans.collect_from_win(winner_id, amount * 2, game.get("peer_id"))
        return collected
    if winner_id != BOT_VK_ID and loser_id != BOT_VK_ID:
        db.update_balance(winner_id, amount * 2)
        collected = loans.collect_from_win(winner_id, amount * 2, game.get("peer_id"))
        db.update_balance(loser_id, -amount)
    elif winner_id != BOT_VK_ID:
        db.update_balance(winner_id, amount)
        collected = loans.collect_from_win(winner_id, amount, game.get("peer_id"))
    else:
        db.update_balance(loser_id, -amount)
    return collected


def _collect_note(collected):
    if not collected:
        return ""
    return "\n💳 %s ушло на погашение просрочки по кредиту" % format_amount(collected)


def _prize_amount(game, winner_id):
    amount = game["amount"]
    if bool(game.get("escrow")):
        return amount * 2
    if winner_id == BOT_VK_ID or game.get("target_id") == BOT_VK_ID:
        return amount
    return amount * 2


def _finish_coin(game, winner_id, loser_id, result):
    game["status"] = "finished"
    collected = _settle(game, winner_id, loser_id)

    try:
        from handlers import business

        clients = [v for v in (winner_id, loser_id) if v != BOT_VK_ID]
        turnover = game["amount"] * (2 if len(clients) == 2 else 1)
        business.charge(game.get("peer_id"), "coin", turnover, clients)
    except Exception:
        pass

    with GAMES_LOCK:
        if GAMES.get(game["game_id"]) is game:
            GAMES.pop(game["game_id"], None)

    coin_emoji = "🦅" if result == "орел" else "🪙"
    text = (
        f"{coin_emoji} Выпал {result}!\n"
        f"Победил {display_name_by_vk_id(winner_id)} и забирает {format_amount(_prize_amount(game, winner_id))} 💎\n"
        f"{display_name_by_vk_id(loser_id)}, не расстраивайся, возможно тебе тоже когда-то повезет."
    )
    text += _collect_note(collected)
    _delete_game_message(game)
    try:
        _send_result(game, text)
    except Exception:
        pass


def _mute_member(peer_id, vk_id, seconds):
    return vk_session.method(
        "messages.changeConversationMemberRestrictions",
        {
            "peer_id": peer_id,
            "member_ids": str(vk_id),
            "action": "ro",
            "for": seconds,
        },
    )


def _unmute_later(peer_id, vk_id, delay_sec):
    time.sleep(delay_sec)
    db.clear_chat_mute(peer_id, vk_id)
    try:
        vk_session.method(
            "messages.changeConversationMemberRestrictions",
            {
                "peer_id": peer_id,
                "member_ids": str(vk_id),
                "action": "rw",
            },
        )
    except Exception as exc:
        pass


def schedule_unmute(peer_id, vk_id, delay_sec):
    threading.Thread(target=_unmute_later, args=(peer_id, vk_id, delay_sec), daemon=True).start()


def _finish_roulette(game, winner_id, loser_id, side):
    game["status"] = "finished"
    collected = _settle(game, winner_id, loser_id)

    try:
        from handlers import business

        clients = [v for v in (winner_id, loser_id) if v != BOT_VK_ID]
        turnover = game["amount"] * (2 if len(clients) == 2 else 1)
        business.charge(game.get("peer_id"), "roulette", turnover, clients)
    except Exception:
        pass

    with GAMES_LOCK:
        if GAMES.get(game["game_id"]) is game:
            GAMES.pop(game["game_id"], None)

    text = (
        f"🔫 {display_name_by_vk_id(winner_id)} выстрелил «{side}» и попал в {display_name_by_vk_id(loser_id)}!\n"
        f"{display_name_by_vk_id(winner_id)} забирает {format_amount(_prize_amount(game, winner_id))} 💎\n"
        f"{display_name_by_vk_id(loser_id)}, сидишь без права слова 2 минуты 🔇"
    )
    text += _collect_note(collected)
    _delete_game_message(game)
    try:
        _send_result(game, text)
    except Exception:
        pass

    if loser_id == BOT_VK_ID:
        return
    try:
        _mute_member(game["peer_id"], loser_id, MUTE_SECONDS)
        db.set_chat_mute(game["peer_id"], loser_id, MUTE_SECONDS)
        schedule_unmute(game["peer_id"], loser_id, MUTE_SECONDS + 5)
    except Exception as exc:
        pass


def _process_coin_click(game, user_id, side, event_id=None, peer_id=None):
    initiator_id = game["initiator_id"]

    if side == "cancel":
        if user_id != initiator_id or game.get("accepted_id"):
            return _reject(event_id, user_id, peer_id, _self_click_message())
        game["status"] = "cancelled"
        with GAMES_LOCK:
            if GAMES.get(game["game_id"]) is game:
                GAMES.pop(game["game_id"], None)
        _delete_game_message(game)
        try:
            _send_result(
                game,
                f"❌ {display_name_by_vk_id(user_id)} отменил игру.",
            )
        except Exception:
            pass
        return None

    if user_id == initiator_id:
        return _reject(event_id, user_id, peer_id, _self_click_message())
    if game.get("target_id") is not None and user_id != game["target_id"]:
        return _reject(event_id, user_id, peer_id, _self_click_message())
    if game.get("accepted_id") is not None:
        return _reject(event_id, user_id, peer_id, _self_click_message())
    if not _can_join(user_id, game["amount"]):
        return _reject(event_id, user_id, peer_id, _poor_clicker_phrase())

    game["accepted_id"] = user_id
    game["escrow"] = True
    if game.get("target_id") == BOT_VK_ID:
        if not _can_join(initiator_id, game["amount"]):
            game["status"] = "cancelled"
            with GAMES_LOCK:
                if GAMES.get(game["game_id"]) is game:
                    GAMES.pop(game["game_id"], None)
            _delete_game_message(game)
            return _reject(
                event_id, user_id, peer_id,
                f"{_plain_name(initiator_id)}, элитов не хватило — игра отменена 🤔",
            )
        db.update_balance(initiator_id, -game["amount"])
    else:
        if not _can_join(initiator_id, game["amount"]) or not _can_join(user_id, game["amount"]):
            game["status"] = "cancelled"
            with GAMES_LOCK:
                if GAMES.get(game["game_id"]) is game:
                    GAMES.pop(game["game_id"], None)
            _delete_game_message(game)
            return _reject(event_id, user_id, peer_id, _poor_clicker_phrase())
        db.update_balance(initiator_id, -game["amount"])
        db.update_balance(user_id, -game["amount"])
    result = random.choice(["орел", "решка"])
    winner_id = initiator_id if side != result else user_id
    loser_id = user_id if winner_id == initiator_id else initiator_id
    _finish_coin(game, winner_id, loser_id, result)
    return None


def _process_roulette_click(game, user_id, side, event_id=None, peer_id=None):
    initiator_id = game["initiator_id"]

    if not game.get("target_id"):
        if user_id == initiator_id:
            return _reject(event_id, user_id, peer_id, _self_click_message())
        if not _can_join(user_id, game["amount"]):
            return _reject(event_id, user_id, peer_id, _poor_clicker_phrase())

        game["target_id"] = user_id
        game["current_turn"] = initiator_id
        game["escrow"] = True
        if (initiator_id != BOT_VK_ID and not _can_join(initiator_id, game["amount"])) \
                or (user_id != BOT_VK_ID and not _can_join(user_id, game["amount"])):
            game["status"] = "cancelled"
            with GAMES_LOCK:
                if GAMES.get(game["game_id"]) is game:
                    GAMES.pop(game["game_id"], None)
            _delete_game_message(game)
            return _reject(event_id, user_id, peer_id, _poor_clicker_phrase())
        if initiator_id != BOT_VK_ID:
            db.update_balance(initiator_id, -game["amount"])
        if user_id != BOT_VK_ID:
            db.update_balance(user_id, -game["amount"])
        prompt = (
            f"{display_name_by_vk_id(initiator_id)} вызывает {display_name_by_vk_id(user_id)} на дуэль!\n\n"
            f"🎯 Очередь {display_name_by_vk_id(initiator_id)}\n"
            "В какую сторону будешь стрелять?"
        )
        _replace_game_message(game, prompt, _roulette_keyboard(game["game_id"]))
        _start_timeout(game["game_id"])
        return None

    opponent_id = game["target_id"] if user_id == initiator_id else initiator_id
    if user_id not in {initiator_id, game["target_id"]}:
        return _reject(event_id, user_id, peer_id, _roulette_turn_phrase())

    if not _can_join(user_id, game["amount"]):
        return _reject(event_id, user_id, peer_id, _poor_clicker_phrase())

    if user_id != game.get("current_turn"):
        text = (
            f"{_roulette_turn_phrase()}\n\n"
            f"🎯 Очередь: {_plain_name(game['current_turn'])}"
        )
        return _reject(event_id, user_id, peer_id, text)

    victim_position = random.choice(ROULETTE_SIDES)
    shooter_name = display_name_by_vk_id(user_id)
    opponent_name = display_name_by_vk_id(opponent_id)

    if side == victim_position:
        _finish_roulette(game, user_id, opponent_id, side)
        return None

    game["current_turn"] = opponent_id
    text = (
        f"🤔 {shooter_name} выстрелил «{side}», но {opponent_name} стоял в другом месте!\n\n"
        f"🎯 Очередь {opponent_name}\n"
        "В какую сторону будешь стрелять?"
    )
    _replace_game_message(game, text, _roulette_keyboard(game["game_id"]))
    _start_timeout(game["game_id"])
    return None


def handle_message_event(data):
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

    game_id = payload.get("game_id")
    if not game_id:
        return

    event_id = obj.get("event_id") or data.get("event_id")
    user_id = data.get("user_id") or obj.get("user_id")
    peer_id = data.get("peer_id") or obj.get("peer_id")

    if event_id is not None:
        _purge_processed_events()
        if not _mark_event_processed(event_id):
            return

    with GAMES_LOCK:
        game = GAMES.get(game_id)
    if not game or game.get("status") != "active":
        if game is None:
            _delete_orphan_button_message(obj, peer_id)
        return DEAD_SESSION

    clicked_cmid = obj.get("conversation_message_id")
    if clicked_cmid:
        game["cmid"] = clicked_cmid

    side = payload.get("side")
    game_type = payload.get("type")
    if game_type not in ("coin", "roulette"):
        return

    with _game_lock(game):
        if game.get("status") != "active":
            return DEAD_SESSION
        if game_type == "coin":
            return _process_coin_click(game, user_id, side, event_id, peer_id)
        return _process_roulette_click(game, user_id, side, event_id, peer_id)


@command("монетка")
def cmd_coin(user, args, message):
    peer_id = message.get("peer_id")
    if peer_id is None or peer_id < 2000000000:
        return "Монетка работает только в беседе."

    target_id, remaining = extract_target_id(args, message.get("reply_message"))
    raw = remaining.strip()
    if raw.lower() in ("все", "всё"):
        amount = _user_balance(user["vk_id"])
    else:
        amount = parse_amount(raw, default=None)
        if amount is None and raw:
            return None
        amount = DEFAULT_STAKE if amount is None else amount
    if amount < MIN_STAKE:
        return f"Минимальная ставка {MIN_STAKE}. Пример: монетка @user 10к"
    if target_id is not None and target_id == user["vk_id"]:
        return "Себе нельзя."
    if target_id is not None and target_id < 0 and target_id != BOT_VK_ID:
        return "Играть можно только с людьми или с нашим ботом 😉"
    if not _can_join(user["vk_id"], amount):
        return (
            f"{display_name_by_vk_id(user['vk_id'])}, у тебя недостаточно элитов. "
            f"На балансе: {format_amount(_user_balance(user['vk_id']))} 💎"
        )
    if target_id is not None and not _can_join(target_id, amount):
        return _broke_target_reply(display_name_by_vk_id(target_id))

    game = {
        "game_id": uuid.uuid4().hex[:8],
        "type": "coin",
        "peer_id": peer_id,
        "initiator_id": user["vk_id"],
        "target_id": target_id,
        "amount": amount,
        "status": "active",
        "reply_to": message.get("id") or message.get("conversation_message_id"),
        "lock": threading.Lock(),
    }

    _cleanup_peer_games(peer_id)
    with GAMES_LOCK:
        GAMES[game["game_id"]] = game
    _start_timeout(game["game_id"])

    initiator_name = display_name_by_vk_id(user["vk_id"])
    if target_id is None:
        text = (
            f"Игрок {initiator_name} бросает вызов всему чату!\n"
            f"💵 Ставка игры: {format_amount(amount)} элитов\n"
            "🌗 Право выбора стороны предоставляется всему чату\n"
            "🍀 Да прибудет с Вами удача!"
        )
    else:
        target_name = display_name_by_vk_id(target_id)
        text = (
            f"Игрок {initiator_name} бросает вызов {target_name}!\n"
            f"💵 Ставка игры: {format_amount(amount)} элитов\n"
            f"🌗 Право выбора стороны предоставляется {target_name}\n"
            "🍀 Да прибудет с Вами удача!"
        )

    sent = _send_duel_prompt(
        peer_id, text, _coin_keyboard(game["game_id"]),
        reply_to=message.get("id") or message.get("conversation_message_id"),
    )
    game["cmid"] = _extract_message_id(sent) or game.get("cmid")
    game["prompt"] = text
    if target_id == BOT_VK_ID:
        _schedule_bot_pick(game["game_id"])
    return None


@command("рулетка")
def cmd_roulette(user, args, message):
    peer_id = message.get("peer_id")
    if peer_id is None or peer_id < 2000000000:
        return "Рулетка работает только в беседе."

    target_id, remaining = extract_target_id(args, message.get("reply_message"))
    raw = remaining.strip()
    if raw.lower() in ("все", "всё"):
        amount = _user_balance(user["vk_id"])
    else:
        amount = parse_amount(raw, default=None)
        if amount is None and raw:
            return None
        amount = DEFAULT_STAKE if amount is None else amount
    if amount < MIN_STAKE:
        return f"Минимальная ставка {MIN_STAKE}. Пример: рулетка @user 10к"
    if target_id is not None and target_id == user["vk_id"]:
        return "Себе нельзя."
    if target_id is not None and target_id < 0:
        return "В рулетку с сообществами не играем. Только с людьми!"
    if not _can_join(user["vk_id"], amount):
        return (
            f"{display_name_by_vk_id(user['vk_id'])}, у тебя недостаточно элитов. "
            f"На балансе: {format_amount(_user_balance(user['vk_id']))} 💎"
        )
    if target_id is not None and not _can_join(target_id, amount):
        return _broke_target_reply(display_name_by_vk_id(target_id))

    game = {
        "game_id": uuid.uuid4().hex[:8],
        "type": "roulette",
        "peer_id": peer_id,
        "initiator_id": user["vk_id"],
        "target_id": target_id,
        "amount": amount,
        "status": "active",
        "current_turn": target_id if target_id is not None else user["vk_id"],
        "reply_to": message.get("id") or message.get("conversation_message_id"),
        "lock": threading.Lock(),
    }

    _cleanup_peer_games(peer_id)
    with GAMES_LOCK:
        GAMES[game["game_id"]] = game
    _start_timeout(game["game_id"])

    initiator_name = display_name_by_vk_id(user["vk_id"])
    if target_id is None:
        text = (
            f"Игрок {initiator_name} бросает вызов всему чату!\n"
            f"💵 Ставка игры: {format_amount(amount)} элитов\n"
            "🔫 Как только кто-то вступит — дуэль начнётся\n"
            "🍀 Да прибудет с Вами удача!"
        )
    else:
        target_name = display_name_by_vk_id(target_id)
        text = (
            f"Игрок {initiator_name} бросает вызов {target_name}!\n"
            f"💵 Ставка игры: {format_amount(amount)} элитов\n"
            f"🎯 Право первого выстрела предоставляется {target_name}\n"
            "🍀 Да прибудет с Вами удача!"
        )

    sent = _send_duel_prompt(
        peer_id, text, _roulette_keyboard(game["game_id"]),
        reply_to=message.get("id") or message.get("conversation_message_id"),
    )
    game["cmid"] = _extract_message_id(sent) or game.get("cmid")
    game["prompt"] = text
    return None
