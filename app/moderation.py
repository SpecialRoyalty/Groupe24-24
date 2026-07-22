from __future__ import annotations

import hashlib
import io
import logging
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import imagehash
from PIL import Image
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .models import ForbiddenWord, LinkWhitelistDomain, LinkWhitelistUser, MediaHash, ModerationStat
from .services import get_setting

logger = logging.getLogger("telegram-vip-bot.moderation")
URL_RE = re.compile(r"(?i)\b((?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>]+)")

async def stat_inc(session, key: str, amount: int = 1) -> None:
    row = await session.get(ModerationStat, key)
    if row:
        row.value += amount
    else:
        session.add(ModerationStat(key=key, value=amount))

async def safe_delete(message: Message) -> bool:
    try:
        await message.delete()
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("Erreur Telegram suppression: %s", exc)
    except Exception:
        logger.exception("Erreur Telegram suppression inattendue")
    return False

async def apply_sanction(bot: Bot, message: Message, sanction: str, reason: str) -> None:
    if not message.from_user:
        return
    uid, cid = message.from_user.id, message.chat.id
    try:
        if sanction == "warning":
            await bot.send_message(cid, f"⚠️ {message.from_user.mention_html()} — {reason}")
        elif sanction == "mute":
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await bot.restrict_chat_member(cid, uid, ChatPermissions(can_send_messages=False), until_date=until)
        elif sanction == "kick":
            await bot.ban_chat_member(cid, uid)
            await bot.unban_chat_member(cid, uid, only_if_banned=True)
        elif sanction == "ban":
            await bot.ban_chat_member(cid, uid)
    except Exception:
        logger.exception("Erreur Telegram sanction=%s chat=%s user=%s", sanction, cid, uid)

async def forbidden_word_hit(session, text: str) -> str | None:
    if (await get_setting(session, "forbidden_words_enabled", "0")) != "1":
        return None
    words = list((await session.scalars(select(ForbiddenWord).where(ForbiddenWord.active.is_(True)))).all())
    lowered = text.casefold()
    for row in words:
        pattern = r"(?<!\w)" + re.escape(row.word.casefold()) + r"(?!\w)"
        if re.search(pattern, lowered):
            return row.word
    return None

def extract_urls(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    urls = URL_RE.findall(text)
    for entity in (message.entities or []) + (message.caption_entities or []):
        if entity.type == "text_link" and entity.url:
            urls.append(entity.url)
        elif entity.type == "url":
            with suppress(Exception):
                urls.append(entity.extract_from(text))
    return urls

async def links_blocked(session, message: Message, sender_is_admin: bool) -> bool:
    if (await get_setting(session, "anti_links_enabled", "0")) != "1":
        return False
    if sender_is_admin and (await get_setting(session, "anti_links_allow_admins", "1")) == "1":
        return False
    if message.from_user:
        allowed_user = await session.scalar(select(LinkWhitelistUser).where(LinkWhitelistUser.telegram_id == message.from_user.id))
        if allowed_user:
            return False
    urls = extract_urls(message)
    if not urls:
        return False
    allowed_domains = {x.domain.casefold().lstrip(".") for x in (await session.scalars(select(LinkWhitelistDomain))).all()}
    for raw in urls:
        candidate = raw if "://" in raw else "https://" + raw
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").casefold().lstrip("www.")
        scheme = parsed.scheme.casefold()
        if any(host == d or host.endswith("." + d) for d in allowed_domains):
            continue
        is_tg = host in {"t.me", "telegram.me", "telegram.org"}
        if is_tg and (await get_setting(session, "anti_links_allow_telegram", "0")) == "1":
            continue
        if host == "t.me" and (await get_setting(session, "anti_links_allow_tme", "0")) == "1":
            continue
        if scheme == "http" and (await get_setting(session, "anti_links_allow_http", "0")) == "1":
            continue
        if scheme == "https" and (await get_setting(session, "anti_links_allow_https", "0")) == "1":
            continue
        return True
    return False

async def media_fingerprints(bot: Bot, message: Message) -> tuple[str, str, str | None] | None:
    file_id = None
    kind = None
    if message.photo:
        file_id, kind = message.photo[-1].file_id, "photo"
    elif message.video:
        file_id, kind = message.video.file_id, "video"
    if not file_id:
        return None
    buffer = io.BytesIO()
    await bot.download(file_id, destination=buffer)
    data = buffer.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    phash = None
    if kind == "photo":
        with Image.open(io.BytesIO(data)) as image:
            phash = str(imagehash.phash(image.convert("RGB")))
    return kind, sha, phash

async def process_repost(bot: Bot, session, message: Message) -> bool:
    if (await get_setting(session, "anti_repost_enabled", "0")) != "1":
        return False
    fp = await media_fingerprints(bot, message)
    if not fp or not message.from_user:
        return False
    kind, sha, phash = fp
    duplicate = await session.scalar(select(MediaHash).where(MediaHash.chat_telegram_id == message.chat.id, MediaHash.sha256 == sha))
    if not duplicate and kind == "photo" and phash:
        candidates = list((await session.scalars(select(MediaHash).where(MediaHash.chat_telegram_id == message.chat.id, MediaHash.media_type == "photo", MediaHash.perceptual_hash.is_not(None)))).all())
        threshold = int(await get_setting(session, "anti_repost_phash_distance", "5"))
        for row in candidates:
            with suppress(Exception):
                if imagehash.hex_to_hash(row.perceptual_hash) - imagehash.hex_to_hash(phash) <= threshold:
                    duplicate = row
                    break
    if duplicate:
        await stat_inc(session, "reposts_detected")
        await session.commit()
        if (await get_setting(session, "anti_repost_auto_delete", "1")) == "1":
            await safe_delete(message)
        template = await get_setting(session, "anti_repost_message", "{user}\n\n♻️ Les doublons sont interdits pour le bien du groupe.\n\nMerci de poster du neuf.")
        mention = "@" + message.from_user.username if message.from_user.username else message.from_user.mention_html()
        with suppress(Exception):
            await bot.send_message(message.chat.id, template.replace("{user}", mention))
        logger.info("Repost détecté chat=%s user=%s", message.chat.id, message.from_user.id)
        return True
    session.add(MediaHash(chat_telegram_id=message.chat.id, user_telegram_id=message.from_user.id, message_id=message.message_id, media_type=kind, sha256=sha, perceptual_hash=phash))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return True
    return False
