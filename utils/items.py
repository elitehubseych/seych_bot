
"""Каталог предметов, кейсов и титулов ELITE CASE."""

import random

KEY_PRICE = 30_000

ITEMS = {
    "key": {"name": "🔑 Ключ", "usable": False},
    "gift_box": {"name": "🎁 Подарок", "usable": True},
    "magnet": {"name": "🧲 Магнит удачи", "usable": True},
    "vip_ticket": {"name": "🎫 VIP-билет", "usable": False},
    "medal_bronze": {"name": "🥉 Бронзовая медаль", "usable": False},
    "medal_silver": {"name": "🥈 Серебряная медаль", "usable": False},
    "medal_gold": {"name": "🥇 Золотая медаль", "usable": False},
    "crown": {"name": "👑 Корона чата", "usable": False},
}

TITLES = {
    "title_bomzh":  {"name": "🗑 Король Помойки",   "type": "Обычный",     "value": 100},
    "title_lucky":  {"name": "🍀 Балбес",            "type": "Мифический",  "value": 1000},
    "title_player": {"name": "🎮 Игрок",             "type": "Мифический",  "value": 1500},
    "title_thug":   {"name": "😈 Гопник Элиты",     "type": "Мифический",  "value": 2000},
    "title_magnat": {"name": "💼 Магнат",            "type": "Легендарный", "value": 15000},
    "title_vip":    {"name": "💎 VIP-Персона",       "type": "Легендарный", "value": 25000},
    "title_king":   {"name": "👑 Король Казино",     "type": "Элитный",     "value": 100000},
    "title_legend": {"name": "🏆 Легенда ELITE",     "type": "Элитный",     "value": 500000},
    "title_god":    {"name": "⚡ Бог Элитов",        "type": "Элитный",     "value": 300000},
}

TITLE_TYPE_ORDER = {"Обычный": 0, "Мифический": 1, "Легендарный": 2, "Элитный": 3}

CASES = {
    "case_bomzh": {"name": "📦 Кейс бомжа", "keys": 1},
    "case_normal": {"name": "📦 Кейс «Обычный»", "keys": 3},
    "case_legendary": {"name": "📦 Кейс «Легендарный»", "keys": 5},
    "case_elite": {"name": "📦 Кейс «Элитный»", "keys": 10},
}
CASE_ORDER = ["case_bomzh", "case_normal", "case_legendary", "case_elite"]

CASE_ALIASES = {
    "бомж": "case_bomzh", "бомжа": "case_bomzh",
    "обычный": "case_normal", "обычную": "case_normal",
    "легендарный": "case_legendary", "легенда": "case_legendary",
    "элитный": "case_elite", "элита": "case_elite",
}

# kind: elite -> value = сумма элитов; item -> value = ключ предмета;
# title -> value = ключ титула
_ROLLS = {
    "case_bomzh": [
        ("elite", 100, 280), ("elite", 200, 240), ("elite", 500, 200),
        ("elite", 1000, 140), ("item", "gift_box", 60),
        ("item", "medal_bronze", 40), ("item", "magnet", 20),
        ("title", "title_bomzh", 20),
    ],
    "case_normal": [
        ("elite", 500, 180), ("elite", 1000, 180), ("elite", 2500, 160),
        ("elite", 5000, 130), ("elite", 10000, 90),
        ("item", "key", 80), ("item", "gift_box", 50),
        ("item", "medal_silver", 40), ("item", "magnet", 40),
        ("title", "title_lucky", 30), ("title", "title_player", 20),
    ],
    "case_legendary": [
        ("elite", 5000, 150), ("elite", 10000, 180),
        ("elite", 15000, 150), ("elite", 25000, 120),
        ("elite", 50000, 90),
        ("item", "vip_ticket", 80), ("item", "magnet", 50),
        ("item", "medal_gold", 40), ("item", "crown", 30),
        ("title", "title_thug", 50), ("title", "title_magnat", 20),
        ("title", "title_vip", 10),
    ],
    "case_elite": [
        ("elite", 25000, 150), ("elite", 50000, 180),
        ("elite", 75000, 150), ("elite", 100000, 110),
        ("elite", 150000, 80),
        ("item", "vip_ticket", 100), ("item", "magnet", 50),
        ("item", "crown", 60), ("item", "medal_gold", 40),
        ("title", "title_king", 15), ("title", "title_god", 8),
        ("title", "title_legend", 1),
    ],
}


def display_name(item_key):
    entry = ITEMS.get(item_key) or TITLES.get(item_key) or CASES.get(item_key)
    return entry["name"] if entry else item_key


def roll_case(case_key):
    pool = _ROLLS[case_key]
    total = sum(w for _, _, w in pool)
    pick = random.randrange(total)
    for kind, value, weight in pool:
        pick -= weight
        if pick < 0:
            return kind, value
    return "elite", 100


def advent_reward(vk_id, cycle, day, owns_title):
    """Детерминированная награда за день цикла (1..10). Один предмет в день."""
    rng = random.Random("advent:%s:%s:%s" % (vk_id, cycle, day))

    def title_or_elite(candidates, fallback_amount):
        free = [t for t in candidates if not owns_title(t)]
        if free:
            return ("title", rng.choice(free))
        return ("elite", rng.choice(fallback_amount))

    grant = None
    if day == 1:
        grant = ("elite", rng.choice((300, 400, 500)))
    elif day == 2:
        grant = ("elite", rng.choice((500, 600, 700)))
    elif day == 3:
        grant = ("elite", rng.choice((800, 1000, 1200)))
    elif day == 4:
        r = rng.random()
        if r < 0.5:
            grant = ("item", "key")
        else:
            grant = ("elite", rng.choice((1000, 1200, 1500)))
    elif day == 5:
        r = rng.random()
        if r < 0.35:
            grant = ("item", "case_bomzh")
        elif r < 0.65:
            grant = ("item", "key")
        else:
            grant = ("elite", rng.choice((1500, 1800)))
    elif day == 6:
        grant = ("elite", rng.choice((1800, 2000, 2500)))
    elif day == 7:
        r = rng.random()
        if r < 0.3:
            grant = ("item", "magnet")
        elif r < 0.55:
            grant = ("item", "key")
        else:
            grant = ("elite", rng.choice((2200, 2500, 3000)))
    elif day == 8:
        r = rng.random()
        if r < 0.4:
            grant = title_or_elite(["title_lucky", "title_player", "title_thug"], (2500, 3000))
        else:
            grant = ("elite", rng.choice((2500, 3000, 3500)))
    elif day == 9:
        r = rng.random()
        if r < 0.25:
            grant = ("item", "vip_ticket")
        elif r < 0.45:
            grant = ("item", "case_normal")
        elif r < 0.60:
            grant = ("item", "case_legendary")
        else:
            grant = ("elite", rng.choice((3000, 4000, 5000)))
    else:
        r = rng.random()
        if r < 0.35:
            grant = ("item", "case_elite")
        elif r < 0.60:
            grant = title_or_elite(["title_king", "title_legend", "title_god"], (5000, 7500, 10000))
        else:
            grant = ("elite", rng.choice((7500, 10000)))

    return [grant]
