
import re

MENTION_RE = re.compile(r"\[id(\d+)\|[^\]]*\]")
CLUB_RE = re.compile(r"\[(?:club|public|event)(\d+)\|[^\]]*\]")
ID_RE = re.compile(r"(?:^|\s)@?id(\d+)")
LINK_RE = re.compile(r"vk\.com/id(\d+)")


def format_amount(n):
    return f"{n:,}".replace(",", ".")


def parse_amount(text, default=1000):
    text = (text or "").strip().lower().replace(",", ".").replace("ё", "е")
    if not text:
        return default

    match = re.fullmatch(r"(\d{1,3}(?:\.\d{3})+|\d+(?:\.\d+)?)\s*(кк|kk|к|k)?", text)
    if not match:
        return None

    number_str = match.group(1)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", number_str):
        number_str = number_str.replace(".", "")
    number = float(number_str)
    if match.group(2) in ("к", "k"):
        number *= 1000
    elif match.group(2):
        number *= 1_000_000
    return int(number)


def extract_target_id(args, reply_message=None):
    remaining = args or ""

    club_match = CLUB_RE.search(remaining)
    if club_match:
        return -int(club_match.group(1)), CLUB_RE.sub("", remaining, count=1).strip()

    for pattern in (MENTION_RE, ID_RE, LINK_RE):
        match = pattern.search(remaining)
        if match:
            return int(match.group(1)), pattern.sub("", remaining, count=1).strip()

    if reply_message:
        from_id = reply_message.get("from_id")
        if from_id:
            return from_id, remaining

    return None, remaining
