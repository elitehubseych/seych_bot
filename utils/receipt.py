
import io
import json
import os
import secrets
import time
import uuid

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont
from vk_api.exceptions import ApiError

import db
from utils.parse import format_amount
from utils.vk import display_name, group_info, vk

WIDTH, HEIGHT = 900, 850
CARD_MARGIN = 60

COLOR_BG_TOP = (22, 24, 36)
COLOR_BG_BOTTOM = (10, 11, 18)
COLOR_CARD = (28, 30, 44)
COLOR_ACCENT = (255, 201, 92)
COLOR_TEXT = (240, 240, 245)
COLOR_MUTED = (145, 147, 163)
COLOR_DIVIDER = (52, 54, 72)

FONTS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")
FONT_BOLD_FILE = "Tektur-Medium.ttf"
FONT_REGULAR_FILE = "Jura-Medium.ttf"
FONT_LIGHT_FILE = "Jura-Light.ttf"


def _load_font(filename, size):
    path = os.path.join(FONTS_DIR, filename)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


FONT_LOGO = _load_font(FONT_BOLD_FILE, 44)
FONT_SUBTITLE = _load_font(FONT_LIGHT_FILE, 20)
FONT_LABEL = _load_font(FONT_LIGHT_FILE, 20)
FONT_BODY = _load_font(FONT_REGULAR_FILE, 26)
FONT_AMOUNT = _load_font(FONT_BOLD_FILE, 30)


def _build_background():
    top = np.array(COLOR_BG_TOP, dtype=float)
    bottom = np.array(COLOR_BG_BOTTOM, dtype=float)
    ramp = np.linspace(0, 1, HEIGHT)[:, None]
    gradient = (top * (1 - ramp) + bottom * ramp).astype("uint8")
    gradient = np.repeat(gradient[:, None, :], WIDTH, axis=1)
    return Image.fromarray(gradient, "RGB")


_BASE_BACKGROUND = _build_background()


def generate_transaction_id():
    return f"EB-{uuid.uuid4().hex[:10].upper()}"


def _fetch_circle_avatar(url, size=72):
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception:
        img = Image.new("RGB", (size, size), COLOR_CARD)

    img = img.resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    circle = Image.new("RGBA", (size, size))
    circle.paste(img, (0, 0), mask)
    return circle


def _get_profiles(vk_ids):
    positive_ids = [v for v in vk_ids if v and int(v) > 0]
    if not positive_ids:
        return {}
    try:
        info = vk.users.get(user_ids=positive_ids, fields="photo_100")
        return {int(item["id"]): item for item in info}
    except Exception:
        return {}


def _get_user_label(vk_id):
    user = db.get_user_readonly(vk_id)
    if user and user.get("nickname"):
        return user["nickname"]

    if vk_id < 0:
        return group_info(abs(vk_id)).get("name") or "Группа"

    info = _get_profiles([vk_id]).get(vk_id, {})
    if info:
        return f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or "Пользователь"
    return "Пользователь"


def _get_chat_info(peer_id):
    if peer_id < 2_000_000_000:
        return None

    def _photo_url_from(source):
        if not isinstance(source, dict):
            return None
        candidates = []
        nested = source.get("photo")
        if isinstance(nested, dict):
            candidates += [nested.get("photo_200"), nested.get("photo_100"), nested.get("photo_50")]
        elif isinstance(nested, str):
            candidates.append(nested)
        candidates += [source.get(key) for key in ("photo_200", "photo_100", "photo_50")]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.startswith("http"):
                return candidate
        return None

    try:
        result = vk.messages.getConversationsById(peer_ids=[peer_id])
        items = result.get("items") or []
        if items:
            item = items[0]
            conversation = item.get("conversation") or {}
            chat_settings = (
                item.get("chat_settings")
                or conversation.get("chat_settings")
                or {}
            )
            title = (
                chat_settings.get("title")
                or conversation.get("title")
                or item.get("title")
            )
            photo = _photo_url_from(chat_settings) or _photo_url_from(conversation)
            if title:
                return {"title": title, "photo": photo}
    except ApiError as error:
        pass
    except Exception as error:
        pass

    try:
        result = vk.messages.getChatPreview(peer_id=peer_id)
        preview = result.get("preview") or {}
        title = preview.get("title") or "Беседа"
        photo = preview.get("photo") or preview.get("photo_100")
        if title:
            return {"title": title, "photo": photo}
    except ApiError as error:
        if error.code not in (27, 100, 901, 113):
            pass
        else:
            pass
    except Exception:
        pass

    try:
        chat_id = peer_id - 2_000_000_000
        result = vk.messages.getChat(chat_id=chat_id)
        return {"title": result.get("title", "Беседа"), "photo": result.get("photo_100")}
    except ApiError as error:
        if error.code in (27, 100, 901, 113):
            return None
        raise
    except Exception:
        return None


def _draw_participant(img, draw, x, y, label, name, avatar_url):
    draw.text((x, y), label, font=FONT_LABEL, fill=COLOR_MUTED)
    avatar_size = 56
    avatar = _fetch_circle_avatar(avatar_url, size=avatar_size) if avatar_url else None
    text_y = y + 28
    if avatar is not None:
        img.paste(avatar, (x, text_y), avatar)
        text_x = x + avatar_size + 16
    else:
        text_x = x
    display_name_text = name or "Пользователь"
    draw.text((text_x, text_y + avatar_size // 2), display_name_text, font=FONT_BODY, fill=COLOR_TEXT, anchor="lm")
    return y + avatar_size + 44


def _draw_row(draw, x_left, x_right, y, label, value, accent=False):
    draw.text((x_left, y), label, font=FONT_BODY, fill=COLOR_MUTED)
    color = COLOR_ACCENT if accent else COLOR_TEXT
    font = FONT_AMOUNT if accent else FONT_BODY
    draw.text((x_right, y), value, font=font, fill=color, anchor="ra")
    return y + 48


def build_receipt_image(sender_id, receiver_id, amount_text, commission_text, net_text,
                         transaction_id, peer_id,
                         source_label=None, dest_label=None,
                         sender_account=None, receiver_account=None):
    profiles = _get_profiles([sender_id, receiver_id])
    sender_info = profiles.get(sender_id, {})
    receiver_info = profiles.get(receiver_id, {})
    chat_info = _get_chat_info(peer_id)

    sender_name = _get_user_label(sender_id)
    receiver_name = _get_user_label(receiver_id)

    if sender_id < 0:
        group = group_info(abs(sender_id))
        if group:
            sender_info = {"first_name": group.get("name", "Группа"), "photo_100": group.get("photo_100")}
    if receiver_id < 0:
        group = group_info(abs(receiver_id))
        if group:
            receiver_info = {"first_name": group.get("name", "Группа"), "photo_100": group.get("photo_100")}

    img = _BASE_BACKGROUND.copy()
    draw = ImageDraw.Draw(img)

    card_box = (CARD_MARGIN, 130, WIDTH - CARD_MARGIN, HEIGHT - CARD_MARGIN)
    draw.rounded_rectangle(card_box, radius=28, fill=COLOR_CARD)

    draw.text((WIDTH // 2, 62), "Elite Bank", font=FONT_LOGO, fill=COLOR_ACCENT, anchor="mm")
    draw.text((WIDTH // 2, 102), "чек перевода", font=FONT_SUBTITLE, fill=COLOR_MUTED, anchor="mm")

    x_left = CARD_MARGIN + 34
    x_right = WIDTH - CARD_MARGIN - 34
    y = 168

    if chat_info:
        draw.text((x_left, y), "Чат", font=FONT_LABEL, fill=COLOR_MUTED)
        y += 26
        if chat_info.get("photo"):
            avatar = _fetch_circle_avatar(chat_info["photo"], size=44)
            img.paste(avatar, (x_left, y), avatar)
            draw.text((x_left + 44 + 14, y + 22), chat_info["title"], font=FONT_BODY, fill=COLOR_TEXT, anchor="lm")
        else:
            draw.text((x_left, y), chat_info["title"], font=FONT_BODY, fill=COLOR_TEXT)
        y += 60

    y = _draw_participant(img, draw, x_left, y, "Отправитель", sender_name, sender_info.get("photo_100"))
    y = _draw_participant(img, draw, x_left, y, "Получатель", receiver_name, receiver_info.get("photo_100"))

    y += 6
    draw.line((x_left, y, x_right, y), fill=COLOR_DIVIDER, width=2)
    y += 30

    y = _draw_row(draw, x_left, x_right, y, "Перевод", f"{amount_text} элитов")
    y = _draw_row(draw, x_left, x_right, y, "Комиссия", f"{commission_text} элитов")
    y = _draw_row(draw, x_left, x_right, y, "Итог", f"{net_text} элитов", accent=True)

    if source_label or dest_label:
        y += 14
        draw.line((x_left, y, x_right, y), fill=COLOR_DIVIDER, width=2)
        y += 28
        src_text = "Наличные" if source_label == "cash" else "Банк"
        dst_text = "Наличные" if dest_label == "cash" else "Банк"
        if source_label == "bank" and sender_account:
            src_text = f"{src_text} · {sender_account}"
        if dest_label == "bank" and receiver_account:
            dst_text = f"{dst_text} · {receiver_account}"
        y = _draw_row(draw, x_left, x_right, y, "Счет списания", src_text)
        y = _draw_row(draw, x_left, x_right, y, "Счет пополнения", dst_text)

    y += 14
    draw.line((x_left, y, x_right, y), fill=COLOR_DIVIDER, width=2)
    y += 28
    draw.text((x_left, y), "ID транзакции", font=FONT_LABEL, fill=COLOR_MUTED)
    y += 24
    draw.text((x_left, y), transaction_id, font=FONT_BODY, fill=COLOR_TEXT)

    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="JPEG", quality=90)
    buffer.seek(0)
    return buffer


def _upload_photo_for_dm(peer_id, image_bytes):
    last_error = None
    for attempt in range(3):
        try:
            upload_server = vk.photos.getMessagesUploadServer(peer_id=peer_id)
            resp = requests.post(
                upload_server["upload_url"],
                files={"photo": ("receipt.jpg", image_bytes, "image/jpeg")},
                timeout=15,
            )
            resp.raise_for_status()
            upload_result = resp.json()
            if "photo" not in upload_result or "server" not in upload_result or "hash" not in upload_result:
                raise ValueError(f"Некорректный ответ загрузки фото: {upload_result}")

            saved = vk.photos.saveMessagesPhoto(
                photo=upload_result["photo"],
                server=upload_result["server"],
                hash=upload_result["hash"],
            )
            photo = saved[0]
            return f"photo{photo['owner_id']}_{photo['id']}"
        except (requests.exceptions.RequestException, ValueError, KeyError, ApiError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
    raise last_error


def send_transfer_receipts(peer_id, sender_id, receiver_id, amount_text, commission_text,
                            net_text, transaction_id, message_text=None,
                            source_label=None, dest_label=None,
                            sender_account=None, receiver_account=None):
    buffer = build_receipt_image(
        sender_id=sender_id,
        receiver_id=receiver_id,
        amount_text=amount_text,
        commission_text=commission_text,
        net_text=net_text,
        transaction_id=transaction_id,
        peer_id=peer_id,
        source_label=source_label,
        dest_label=dest_label,
        sender_account=sender_account,
        receiver_account=receiver_account,
    )
    image_bytes = buffer.getvalue()
    message_text = message_text or "Чек перевода."

    for recipient_id in (sender_id, receiver_id):
        try:
            attachment = _upload_photo_for_dm(recipient_id, image_bytes)
            vk.messages.send(
                user_id=recipient_id,
                message=message_text,
                attachment=attachment,
                random_id=secrets.randbelow(2**31),
            )
        except ApiError as error:
            if error.code == 901:
                continue
            raise
        except Exception:
            raise


def build_deal_image(seller_id, buyer_id, title_text, biz_name, price_text,
                     commission_text, net_text, transaction_id, peer_id,
                     stamp_text):
    profiles = _get_profiles([seller_id, buyer_id])
    seller_info = profiles.get(seller_id, {})
    buyer_info = profiles.get(buyer_id, {})
    if seller_id < 0:
        group = group_info(abs(seller_id))
        if group:
            seller_info = {"first_name": group.get("name", "Группа"), "photo_100": group.get("photo_100")}
    if buyer_id < 0:
        group = group_info(abs(buyer_id))
        if group:
            buyer_info = {"first_name": group.get("name", "Группа"), "photo_100": group.get("photo_100")}

    img = _BASE_BACKGROUND.copy()
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((CARD_MARGIN, 130, WIDTH - CARD_MARGIN, HEIGHT - CARD_MARGIN),
                           radius=28, fill=COLOR_CARD)

    draw.text((WIDTH // 2, 62), "Elite Business", font=FONT_LOGO, fill=COLOR_ACCENT, anchor="mm")
    draw.text((WIDTH // 2, 102), title_text, font=FONT_SUBTITLE, fill=COLOR_MUTED, anchor="mm")

    x_left = CARD_MARGIN + 34
    x_right = WIDTH - CARD_MARGIN - 34
    y = 168

    y = _draw_participant(img, draw, x_left, y, "Продавец", _get_user_label(seller_id), seller_info.get("photo_100"))
    y = _draw_participant(img, draw, x_left, y, "Покупатель", _get_user_label(buyer_id), buyer_info.get("photo_100"))

    y += 6
    draw.line((x_left, y, x_right, y), fill=COLOR_DIVIDER, width=2)
    y += 30
    y = _draw_row(draw, x_left, x_right, y, "Бизнес", biz_name)
    y = _draw_row(draw, x_left, x_right, y, "Цена", "%s элитов" % price_text)
    y = _draw_row(draw, x_left, x_right, y, "Комиссия", "%s элитов" % commission_text)
    y = _draw_row(draw, x_left, x_right, y, "Итог", "%s элитов" % net_text, accent=True)

    y += 14
    draw.line((x_left, y, x_right, y), fill=COLOR_DIVIDER, width=2)
    y += 28
    draw.text((x_left, y), "ID транзакции", font=FONT_LABEL, fill=COLOR_MUTED)
    draw.text((x_left, y + 24), transaction_id, font=FONT_BODY, fill=COLOR_TEXT)

    now_label = time.strftime("%d.%m.%Y %H:%M")
    draw.text((x_right, y + 24), now_label, font=FONT_LABEL, fill=COLOR_MUTED, anchor="ra")

    stamp_color = (90, 200, 120) if stamp_text == "Куплено" else (230, 110, 100)
    cx, cy, r = WIDTH - CARD_MARGIN - 130, HEIGHT - CARD_MARGIN - 150, 86
    stamp_layer = Image.new("RGBA", (r * 2 + 20, r * 2 + 20), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(stamp_layer)
    sdraw.ellipse((6, 6, r * 2 + 14, r * 2 + 14), outline=stamp_color + (220,), width=5)
    sdraw.ellipse((22, 22, r * 2 - 2, r * 2 - 2), outline=stamp_color + (160,), width=2)
    stamp_font = _load_font(FONT_BOLD_FILE, 30)
    bbox = sdraw.textbbox((0, 0), stamp_text, font=stamp_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    sdraw.text(((r * 2 + 20 - tw) // 2 - bbox[0], (r * 2 + 20 - th) // 2 - bbox[1]),
               stamp_text, font=stamp_font, fill=stamp_color + (230,))
    stamp_layer = stamp_layer.rotate(-18, expand=True, resample=Image.BICUBIC)
    img.paste(stamp_layer, (cx - r - 10, cy - r - 10), stamp_layer)

    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="JPEG", quality=90)
    buffer.seek(0)
    return buffer


def send_business_receipts(seller_id, buyer_id, biz_name, amount, commission, net):
    amount_text, commission_text, net_text = (
        format_amount(int(amount)), format_amount(int(commission)), format_amount(int(net)),
    )
    for recipient_id, title, stamp in (
        (seller_id, "продажа бизнеса", "Продано"),
        (buyer_id, "покупка бизнеса", "Куплено"),
    ):
        try:
            buffer = build_deal_image(
                seller_id=seller_id,
                buyer_id=buyer_id,
                title_text=title,
                biz_name=biz_name,
                price_text=amount_text,
                commission_text=commission_text,
                net_text=net_text,
                transaction_id=generate_transaction_id(),
                peer_id=None,
                stamp_text=stamp,
            )
            attachment = _upload_photo_for_dm(recipient_id, buffer.getvalue())
            vk.messages.send(
                user_id=recipient_id,
                message="🧾 Чек сделки с бизнесом.",
                attachment=attachment,
                random_id=secrets.randbelow(2 ** 31),
            )
        except ApiError as error:
            if error.code == 901:
                continue
            raise
        except Exception:
            raise
