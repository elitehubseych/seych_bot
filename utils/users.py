
import re

import db

MENTION_PATTERN = re.compile(r"\[id(\d+)(?:\|[^\]]*)?\]")


def extract_mentioned_ids(text):
    return [int(match) for match in MENTION_PATTERN.findall(text or "")]


def ensure_users(vk_ids):
    registered = []
    for vk_id in vk_ids:
        user = db.get_user(vk_id)
        if user is not None:
            registered.append(user)
    return registered
