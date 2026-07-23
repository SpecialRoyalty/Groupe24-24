from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram import Bot
from aiogram.types import ChatPermissions
from .config import get_settings
from .models import AccessRequest, AccessStatus, ActivityMedia, Invite, Membership, Referral, Setting, TelegramChat, User

settings = get_settings()

async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    row = await session.scalar(select(User).where(User.telegram_id == tg_user.id))
    if row is None:
        row = User(telegram_id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name or "", last_name=tg_user.last_name or "", started_bot=True)
        session.add(row)
    else:
        row.username, row.first_name, row.last_name, row.started_bot = tg_user.username, tg_user.first_name or "", tg_user.last_name or "", True
    await session.commit(); await session.refresh(row)
    return row

async def get_setting(session: AsyncSession, key: str, default: str) -> str:
    obj = await session.get(Setting, key)
    return obj.value if obj else default

async def set_setting(session: AsyncSession, key: str, value: str):
    obj = await session.get(Setting, key)
    if obj: obj.value = value
    else: session.add(Setting(key=key, value=value))
    await session.commit()

async def active_request(session: AsyncSession, user_id: int):
    return await session.scalar(select(AccessRequest).where(AccessRequest.user_id == user_id, AccessRequest.status.in_([AccessStatus.in_progress.value, AccessStatus.pending_review.value, AccessStatus.approved.value])).order_by(AccessRequest.id.desc()))

async def create_request(session: AsyncSession, user_id: int, method: str, reference_prefix: str = "VIP") -> AccessRequest:
    old = await active_request(session, user_id)
    if old and old.status != AccessStatus.approved.value:
        old.status = AccessStatus.rejected.value
    req = AccessRequest(user_id=user_id, method=method, reference=f"{reference_prefix}-{secrets.token_hex(3).upper()}")
    if method == "referral": req.expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.referral_window_hours)
    session.add(req); await session.commit(); await session.refresh(req)
    return req

async def vip_chat(session: AsyncSession) -> TelegramChat | None:
    return await session.scalar(select(TelegramChat).where(TelegramChat.role == "vip", TelegramChat.active.is_(True)))

async def pub_chat(session: AsyncSession) -> TelegramChat | None:
    return await session.scalar(select(TelegramChat).where(TelegramChat.role == "pub", TelegramChat.active.is_(True)))

async def create_personal_invite(bot: Bot, session: AsyncSession, user: User, req: AccessRequest) -> Invite:
    chat = await vip_chat(session)
    if not chat: raise RuntimeError("Aucun groupe VIP configuré")
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.invite_ttl_hours)
    link = await bot.create_chat_invite_link(chat_id=chat.telegram_chat_id, name=f"VIP-{user.telegram_id}", expire_date=expires, creates_join_request=True)
    inv = Invite(request_id=req.id, user_id=user.id, invite_link=link.invite_link, expires_at=expires)
    session.add(inv); await session.commit(); await session.refresh(inv)
    return inv

async def set_group_open(bot: Bot, session: AsyncSession, is_open: bool):
    chat = await vip_chat(session)
    if not chat: raise RuntimeError("Aucun groupe VIP configuré")
    permissions = ChatPermissions(
        can_send_messages=is_open,
        can_send_audios=is_open,
        can_send_documents=is_open,
        can_send_photos=is_open,
        can_send_videos=is_open,
        can_send_video_notes=is_open,
        can_send_voice_notes=is_open,
        can_send_polls=is_open,
        can_send_other_messages=is_open,
        can_add_web_page_previews=is_open,
    )
    await bot.set_chat_permissions(chat.telegram_chat_id, permissions)
    await set_setting(session, "group_open", "1" if is_open else "0")

async def validated_referrals(session: AsyncSession, request_id: int) -> int:
    return int(await session.scalar(select(func.count(Referral.id)).where(Referral.request_id == request_id, Referral.validated_at.is_not(None), Referral.rejected.is_(False))) or 0)

async def activity_count(session: AsyncSession, membership_id: int) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=settings.activity_window_hours)
    return int(await session.scalar(select(func.count(ActivityMedia.id)).where(ActivityMedia.membership_id == membership_id, ActivityMedia.created_at >= since)) or 0)
