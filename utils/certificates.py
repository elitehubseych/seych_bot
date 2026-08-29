
import io
import secrets
from datetime import timezone

from PIL import Image, ImageDraw

from config import config

from utils.humantime import format_duration
from utils.receipt import (
    CARD_MARGIN,
    COLOR_ACCENT,
    COLOR_CARD,
    COLOR_DIVIDER,
    COLOR_MUTED,
    COLOR_TEXT,
    FONT_BODY,
    FONT_LABEL,
    FONT_LOGO,
    FONT_SUBTITLE,
    HEIGHT,
    WIDTH,
    _BASE_BACKGROUND,
    _draw_participant,
    _draw_row,
    _fetch_circle_avatar,
    _get_chat_info,
    _get_profiles,
    _get_user_label,
    _load_font,
    _upload_photo_for_dm,
)
from utils.vk import group_info, user_sex, vk

CERT_WIDTH = WIDTH
CERT_HEIGHT = HEIGHT

_STAMP_NAME_FONT = _load_font("Jura-Medium.ttf", 15)
_STAMP_SUB_FONT = _load_font("Jura-Light.ttf", 11)

_GROUP_NAME_CACHE = []


def _group_name():
    if _GROUP_NAME_CACHE:
        return _GROUP_NAME_CACHE[0]
    name = group_info(config.ID_GROUP).get("name") or ""
    if not name:
        name = "Сейч"
    _GROUP_NAME_CACHE.append(name)
    return name


def certificate_number():
    return f"W-{secrets.token_hex(4).upper()}"


def _wrap_stamp_name(name, max_chars=16):
    words = name.split()
    if not words:
        return ["Elite Bot"]
    line = words[0]
    lines = []
    for word in words[1:]:
        if len(line) + 1 + len(word) <= max_chars:
            line += " " + word
        else:
            lines.append(line)
            line = word
        if len(lines) == 1 and len(line) > max_chars:
            break
    lines.append(line)
    return lines[:2]


def _build_stamp_layer():
    layer = Image.new("RGBA", (CERT_WIDTH, CERT_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    cx, cy = CERT_WIDTH - 185, CERT_HEIGHT - 175
    radius_outer, radius_inner = 96, 78
    ring_alpha = 34
    ring_color = COLOR_ACCENT + (ring_alpha,)

    draw.ellipse(
        (cx - radius_outer, cy - radius_outer, cx + radius_outer, cy + radius_outer),
        outline=ring_color, width=3,
    )
    draw.ellipse(
        (cx - radius_inner, cy - radius_inner, cx + radius_inner, cy + radius_inner),
        outline=ring_color, width=2,
    )
    for dx in (-radius_outer + 9, radius_outer - 9):
        draw.ellipse((cx + dx - 2, cy - 2, cx + dx + 2, cy + 2), fill=ring_color)

    name_lines = _wrap_stamp_name(_group_name())
    text_alpha = 46
    line_height = 18
    total_h = line_height * len(name_lines)
    y = cy - total_h // 2 - 4
    for line in name_lines:
        width = draw.textlength(line, font=_STAMP_NAME_FONT)
        draw.text((cx - width / 2, y), line, font=_STAMP_NAME_FONT,
                  fill=(240, 240, 245, text_alpha))
        y += line_height

    sub = "ОФИЦИАЛЬНЫЙ ДОКУМЕНТ"
    sub_width = draw.textlength(sub, font=_STAMP_SUB_FONT)
    draw.text((cx - sub_width / 2, cy + total_h // 2 + 6), sub,
              font=_STAMP_SUB_FONT, fill=(240, 240, 245, int(text_alpha * 0.75)))

    return layer.rotate(-12, resample=Image.BICUBIC, center=(cx, cy))


def _build_certificate(peer_id, people_ids, doc_subtitle, roles, rows):
    img = _BASE_BACKGROUND.copy().convert("RGBA")
    draw = ImageDraw.Draw(img)

    draw.text((CERT_WIDTH // 2, 60), "Свидетельство", font=FONT_LOGO, fill=COLOR_ACCENT, anchor="mm")
    draw.text((CERT_WIDTH // 2, 102), doc_subtitle, font=FONT_SUBTITLE, fill=COLOR_MUTED, anchor="mm")

    card_box = (CARD_MARGIN, 130, CERT_WIDTH - CARD_MARGIN, CERT_HEIGHT - CARD_MARGIN)
    draw.rounded_rectangle(card_box, radius=28, fill=COLOR_CARD)

    profiles = _get_profiles(list(people_ids))
    chat_info = _get_chat_info(peer_id)

    x_left = CARD_MARGIN + 34
    x_right = CERT_WIDTH - CARD_MARGIN - 34
    y = 168

    if chat_info:
        draw.text((x_left, y), "Место регистрации", font=FONT_LABEL, fill=COLOR_MUTED)
        y += 26
        if chat_info.get("photo"):
            avatar = _fetch_circle_avatar(chat_info["photo"], size=44)
            img.paste(avatar, (x_left, y), avatar)
            draw.text(
                (x_left + 44 + 14, y + 22),
                chat_info["title"],
                font=FONT_BODY,
                fill=COLOR_TEXT,
                anchor="lm",
            )
        else:
            draw.text((x_left, y), chat_info["title"], font=FONT_BODY, fill=COLOR_TEXT)
        y += 60

    for vk_id in people_ids:
        label = roles[1] if user_sex(vk_id) == 1 else roles[0]
        name = _get_user_label(vk_id)
        photo = profiles.get(vk_id, {}).get("photo_100")
        if not photo and vk_id < 0:
            photo = group_info(abs(vk_id)).get("photo_200")
        y = _draw_participant(img, draw, x_left, y, label, name, photo)

    y += 6
    draw.line((x_left, y, x_right, y), fill=COLOR_DIVIDER, width=2)
    y += 30

    for label, value, accent in rows:
        y = _draw_row(draw, x_left, x_right, y, label, value, accent=accent)

    stamp = _build_stamp_layer()
    img = Image.alpha_composite(img, stamp)

    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="JPEG", quality=90)
    buffer.seek(0)
    return buffer


def build_marriage_certificate(peer_id, user1_id, user2_id, married_at):
    date_text = married_at.strftime("%d.%m.%Y") if married_at else ""
    return _build_certificate(
        peer_id=peer_id,
        people_ids=[user1_id, user2_id],
        doc_subtitle="о заключении брака",
        roles=("Супруг", "Супруга"),
        rows=[
            ("Дата регистрации", date_text, False),
            ("Номер записи", certificate_number(), True),
        ],
    )


def _as_aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def build_divorce_certificate(peer_id, user1_id, user2_id, married_at, divorced_at):
    reg_text = married_at.strftime("%d.%m.%Y") if married_at else ""
    div_text = divorced_at.strftime("%d.%m.%Y") if divorced_at else ""
    try:
        seconds = (_as_aware(divorced_at) - _as_aware(married_at)).total_seconds()
        duration = format_duration(max(0, seconds)) if married_at and divorced_at else ""
    except TypeError:
        duration = ""
    return _build_certificate(
        peer_id=peer_id,
        people_ids=[user1_id, user2_id],
        doc_subtitle="о расторжении брака",
        roles=("Бывший супруг", "Бывшая супруга"),
        rows=[
            ("Дата регистрации", reg_text, False),
            ("Брак длился", duration, True),
            ("Дата расторжения", div_text, False),
        ],
    )


def send_certificate_to_chat(peer_id, caption, image_bytes):
    attachment = _upload_photo_for_dm(peer_id, image_bytes)
    vk.messages.send(
        peer_id=peer_id,
        message=caption,
        attachment=attachment,
        random_id=secrets.randbelow(2**31),
    )
    return True
