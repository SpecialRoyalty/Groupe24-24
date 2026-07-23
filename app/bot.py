from __future__ import annotations
import asyncio
import logging
from html import escape
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, func, text
from .config import get_settings
from .db import SessionLocal
from .keyboards import access_methods, admin_home, kb, payment_details_keyboard, payment_keyboard, rules_keyboard
from .models import AccessMethod, AccessRequest, AccessStatus, ActivityMedia, Invite, MediaSubmission, Membership, PaymentProof, Referral, TelegramChat, User, ForbiddenWord, LinkWhitelistDomain, LinkWhitelistUser, MediaHash, ModerationStat, MembershipRecovery
from .services import active_request, activity_count, create_personal_invite, create_request, get_or_create_user, get_setting, pub_chat, set_group_open, set_setting, validated_referrals, vip_chat
from .moderation import apply_sanction, forbidden_word_hit, links_blocked, process_repost, safe_delete, stat_inc

settings = get_settings()
bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
r = Router(); dp.include_router(r)

LAST_MAINTENANCE_AT: datetime | None = None
LAST_MAINTENANCE_ERROR: str | None = None
LAST_HEALTH_SIGNATURE: str | None = None
ADMIN_INPUT_MODE: dict[int, str] = {}
logger = logging.getLogger("telegram-vip-bot")

DEFAULT_WELCOME_TEXT = "Bienvenue sur le service d’accès au groupe privé ouvert 24 h/24.\n\nVeuillez d’abord consulter les règles."
DEFAULT_PUB_AD_TEXT = "Découvrez notre groupe privé. Utilisez le bouton ci-dessous pour commencer votre demande d’accès."

RULES = """<b>Règles du groupe VIP</b>\n\n• Premier média dans les 24 heures.\n• Ensuite, au moins 5 photos ou vidéos valides toutes les 72 heures.\n• Les liens externes sont interdits.\n• Les transferts et redistributions sont interdits.\n• Les infractions entraînent 1 jour, puis 3 jours de restriction, puis un bannissement.\n• Les contenus peuvent être archivés pour restaurer un groupe de remplacement.\n\nEn cliquant sur « J’adhère », vous acceptez ces règles."""

ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}

async def edit_or_send(message: Message, text_value: str, **kwargs):
    """Édite texte/légende quand possible, sinon envoie un nouveau message sans planter."""
    try:
        if message.photo or message.video or message.animation or message.document or message.caption is not None:
            return await message.edit_caption(caption=text_value, **kwargs)
        if message.text is not None:
            return await message.edit_text(text_value, **kwargs)
        return await message.answer(text_value, **kwargs)
    except TelegramBadRequest as exc:
        error = str(exc).lower()
        handled = (
            "message can't be edited",
            "message is not modified",
            "there is no text in the message",
            "message to edit not found",
        )
        logger.warning("Erreur Telegram édition: %s", exc)
        if "message is not modified" in error:
            return message
        if any(item in error for item in handled):
            return await message.answer(text_value, **kwargs)
        try:
            return await message.answer(text_value, **kwargs)
        except Exception:
            logger.exception("Erreur Telegram lors du fallback answer")
            return message
    except Exception:
        logger.exception("Erreur Telegram edit_or_send")
        try:
            return await message.answer(text_value, **kwargs)
        except Exception:
            logger.exception("Erreur Telegram answer finale")
            return message

edit_message = edit_or_send

async def paid_access_state(session, user_id: int):
    """Retourne la dernière demande payée validée et indique si son lien a déjà été utilisé."""
    req = await session.scalar(
        select(AccessRequest)
        .where(
            AccessRequest.user_id == user_id,
            AccessRequest.method == AccessMethod.payment.value,
            AccessRequest.status.in_([AccessStatus.approved.value, AccessStatus.member.value]),
        )
        .order_by(AccessRequest.id.desc())
    )
    if not req:
        return None, False
    used_invite = await session.scalar(
        select(Invite).where(Invite.request_id == req.id, Invite.used_at.is_not(None)).order_by(Invite.id.desc())
    )
    return req, bool(used_invite or req.status == AccessStatus.member.value)

async def recovery_for_user(session, user_id: int) -> MembershipRecovery | None:
    return await session.scalar(
        select(MembershipRecovery)
        .join(Membership, MembershipRecovery.membership_id == Membership.id)
        .where(
            Membership.user_id == user_id,
            Membership.active.is_(False),
            MembershipRecovery.removed_at.is_not(None),
            MembershipRecovery.rejoined_at.is_(None),
        )
        .order_by(MembershipRecovery.removed_at.desc())
    )


async def show_existing_paid_access(message: Message, req: AccessRequest, link_used: bool, recovery: MembershipRecovery | None = None):
    if recovery:
        await message.answer(
            "✅ <b>Votre paiement reste valide.</b>\n\n"
            "Vous avez été retiré du groupe car le premier média n’a pas été publié dans le délai prévu. "
            "Vous pouvez revenir sans repayer. Après votre retour, un nouveau délai commencera et les rappels seront envoyés automatiquement.",
            reply_markup=kb([("🔁 Revenir dans le groupe", f"recovery:invite:{recovery.id}"), ("🏠 Menu", "menu")]),
        )
    elif link_used:
        await message.answer(
            "✅ <b>Vous êtes déjà membre VIP.</b>",
            reply_markup=kb([("🏠 Menu", "menu")]),
        )
    else:
        await message.answer(
            "✅ <b>Votre paiement a déjà été validé.</b>\n\n"
            "Vous ne pouvez pas effectuer une nouvelle souscription. Utilisez votre accès déjà approuvé.",
            reply_markup=kb([("🔗 Générer mon lien 24 h", f"invite:create:{req.id}"), ("🏠 Menu", "menu")]),
        )

async def admin_ids_for_chat(chat_id: int) -> set[int]:
    try:
        admins = await bot.get_chat_administrators(chat_id)
        return {member.user.id for member in admins if not member.user.is_bot}
    except Exception:
        return set()

async def detected_admin_ids() -> set[int]:
    """Administrateurs Telegram détectés dans les groupes actifs + IDs bootstrap facultatifs."""
    ids = set(settings.admin_id_set)
    async with SessionLocal() as s:
        chats = list((await s.scalars(select(TelegramChat).where(TelegramChat.active.is_(True)))).all())
    for chat in chats:
        ids.update(await admin_ids_for_chat(chat.telegram_chat_id))
    return ids

async def is_admin(user_id: int, chat_id: int | None = None) -> bool:
    if user_id in settings.admin_id_set:
        return True
    if chat_id is not None:
        return user_id in await admin_ids_for_chat(chat_id)
    return user_id in await detected_admin_ids()

async def notify_admins(method: str, *args, **kwargs):
    """Envoie aux admins détectés ayant déjà démarré le bot; ignore les DM impossibles."""
    for admin_id in await detected_admin_ids():
        try:
            await getattr(bot, method)(admin_id, *args, **kwargs)
        except Exception:
            pass


async def build_health_report() -> tuple[str, list[str], str]:
    """Vérifie la base, Telegram, le webhook et les groupes obligatoires."""
    checks: list[str] = []
    alerts: list[str] = []

    # Base de données
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
            chats = list((await s.scalars(select(TelegramChat).where(TelegramChat.active.is_(True)))).all())
        checks.append("✅ Base PostgreSQL accessible")
    except Exception as exc:
        chats = []
        checks.append("❌ Base PostgreSQL inaccessible")
        alerts.append(f"Base de données : {type(exc).__name__}")

    # Identité du bot et webhook
    bot_id: int | None = None
    try:
        me = await bot.get_me()
        bot_id = me.id
        checks.append(f"✅ Bot Telegram connecté : @{me.username or me.id}")
    except Exception as exc:
        checks.append("❌ Connexion Telegram impossible")
        alerts.append(f"Telegram : {type(exc).__name__}")

    try:
        webhook = await bot.get_webhook_info()
        if webhook.url == settings.webhook_url and not webhook.last_error_message:
            checks.append("✅ Webhook actif et sans erreur connue")
        else:
            checks.append("⚠️ Webhook incorrect ou en erreur")
            if webhook.url != settings.webhook_url:
                alerts.append("URL du webhook différente de PUBLIC_BASE_URL")
            if webhook.last_error_message:
                alerts.append(f"Dernière erreur webhook : {webhook.last_error_message}")
        if webhook.pending_update_count:
            checks.append(f"⚠️ {webhook.pending_update_count} mise(s) à jour Telegram en attente")
    except Exception as exc:
        checks.append("❌ Impossible de lire l’état du webhook")
        alerts.append(f"Webhook : {type(exc).__name__}")

    vip = [c for c in chats if c.role == "vip"]
    pubs = [c for c in chats if c.role == "pub"]
    if vip:
        checks.append(f"✅ Groupe VIP configuré : {vip[0].title or vip[0].telegram_chat_id}")
    else:
        checks.append("❌ Aucun groupe VIP actif")
        alerts.append("Aucun groupe VIP actif")
    if pubs:
        checks.append(f"✅ Groupe(s) PUB actif(s) : {len(pubs)}")
    else:
        checks.append("❌ Aucun groupe PUB actif")
        alerts.append("Aucun groupe PUB actif")

    # Présence et permissions du bot dans chaque groupe essentiel
    if bot_id:
        for chat in vip + pubs:
            label = f"{chat.role.upper()} — {chat.title or chat.telegram_chat_id}"
            try:
                member = await bot.get_chat_member(chat.telegram_chat_id, bot_id)
                if member.status not in ADMIN_STATUSES:
                    checks.append(f"❌ {label} : bot non administrateur")
                    alerts.append(f"{label} : droits administrateur manquants")
                    continue
                missing: list[str] = []
                if chat.role == "vip":
                    for attr, title in (("can_delete_messages", "supprimer"), ("can_restrict_members", "restreindre/bannir"), ("can_invite_users", "inviter")):
                        if not getattr(member, attr, False):
                            missing.append(title)
                else:
                    if not getattr(member, "can_invite_users", False):
                        missing.append("inviter")
                if missing:
                    checks.append(f"⚠️ {label} : droits manquants ({', '.join(missing)})")
                    alerts.append(f"{label} : droits manquants ({', '.join(missing)})")
                else:
                    checks.append(f"✅ {label} : bot administrateur et droits essentiels OK")
            except Exception as exc:
                checks.append(f"❌ {label} : groupe inaccessible")
                alerts.append(f"{label} inaccessible : {type(exc).__name__}")

    if LAST_MAINTENANCE_AT:
        age = datetime.now(timezone.utc) - LAST_MAINTENANCE_AT
        if age <= timedelta(minutes=3):
            checks.append("✅ Tâche automatique active")
        else:
            checks.append("❌ Tâche automatique en retard")
            alerts.append("La boucle de maintenance ne répond plus normalement")
    else:
        checks.append("⚠️ Tâche automatique pas encore confirmée")
    if LAST_MAINTENANCE_ERROR:
        checks.append("⚠️ Une erreur récente de maintenance est enregistrée")
        alerts.append(f"Maintenance : {LAST_MAINTENANCE_ERROR[:180]}")

    try:
        async with SessionLocal() as s:
            impacted = int(await s.scalar(select(func.count(MembershipRecovery.id)).where(MembershipRecovery.removed_at.is_not(None), MembershipRecovery.rejoined_at.is_(None))) or 0)
            pending_contact = int(await s.scalar(select(func.count(MembershipRecovery.id)).where(MembershipRecovery.removed_at.is_not(None), MembershipRecovery.contacted_at.is_(None))) or 0)
            contacted = int(await s.scalar(select(func.count(MembershipRecovery.id)).where(MembershipRecovery.removed_at.is_not(None), MembershipRecovery.contacted_at.is_not(None))) or 0)
            future_reminders = int(await s.scalar(select(func.count(Membership.id)).where(Membership.active.is_(True), Membership.first_media_at.is_(None))) or 0)
        checks.append(f"👥 Réintégrations concernées : {impacted}")
        checks.append(f"✅ Personnes impactées contactées : {contacted}")
        checks.append(f"📨 Personnes restant à contacter : {pending_contact}")
        checks.append(f"⏰ Membres suivis pour rappels : {future_reminders}")
        if pending_contact:
            alerts.append(f"{pending_contact} personne(s) impactée(s) restent à contacter")
    except Exception as exc:
        checks.append("⚠️ Impossible de compter les personnes impactées")
        alerts.append(f"Comptage réintégration : {type(exc).__name__}")

    status = "OK" if not alerts else ("CRITIQUE" if any(a.startswith("Aucun groupe VIP") or "Base de données" in a or "Telegram" in a for a in alerts) else "ATTENTION")
    text_report = f"<b>🩺 Santé du système — {status}</b>\n\n" + "\n".join(checks)
    if alerts:
        text_report += "\n\n<b>Alertes</b>\n• " + "\n• ".join(alerts[:12])
    text_report += f"\n\nDernière vérification : {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M:%S')} UTC"
    signature = "|".join(sorted(alerts))
    return text_report, alerts, signature

async def automatic_health_alerts() -> None:
    global LAST_HEALTH_SIGNATURE
    report, alerts, signature = await build_health_report()
    if alerts and signature != LAST_HEALTH_SIGNATURE:
        await notify_admins("send_message", "<b>🚨 Nouvelle alerte système</b>\n\n" + report)
    elif not alerts and LAST_HEALTH_SIGNATURE:
        await notify_admins("send_message", "<b>✅ Santé rétablie</b>\n\nTous les contrôles essentiels sont revenus à la normale.")
    LAST_HEALTH_SIGNATURE = signature

@r.message(CommandStart())
async def start(message: Message):
    if message.chat.type != "private": return
    async with SessionLocal() as s:
        user = await get_or_create_user(s, message.from_user)
        logger.info("Nouvelle connexion user=%s username=%s", message.from_user.id, message.from_user.username)
        paid_req, link_used = await paid_access_state(s, user.id)
        recovery = await recovery_for_user(s, user.id)
    if paid_req:
        await show_existing_paid_access(message, paid_req, link_used, recovery)
        return
    rows = [("📜 Consulter les règles", "rules:show")]
    if await is_admin(message.from_user.id):
        rows.append(("⚙️ Panneau administrateur", "admin:home"))
    async with SessionLocal() as s:
        welcome_text = await get_setting(s, "welcome_text", DEFAULT_WELCOME_TEXT)
        welcome_photo = await get_setting(s, "welcome_photo_file_id", "")
    markup = kb(rows)
    if welcome_photo:
        try:
            await message.answer_photo(welcome_photo, caption=welcome_text, reply_markup=markup)
        except Exception:
            await message.answer(welcome_text, reply_markup=markup)
    else:
        await message.answer(welcome_text, reply_markup=markup)

@r.callback_query(F.data == "rules:show")
async def show_rules(c: CallbackQuery):
    await edit_message(c.message, RULES, reply_markup=rules_keyboard()); await c.answer()

@r.callback_query(F.data == "rules:accept")
async def accept_rules(c: CallbackQuery):
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        paid_req, link_used = await paid_access_state(s, user.id)
        enabled = (await get_setting(s, "alternative_access_enabled", "1")) == "1"
    if paid_req:
        await show_existing_paid_access(c.message, paid_req, link_used)
        await c.answer("Votre paiement est déjà validé.", show_alert=True)
        return
    text = "Choisissez votre méthode d’accès :" if enabled else "L’accès est actuellement disponible uniquement par paiement."
    await edit_message(c.message, text, reply_markup=access_methods(enabled)); await c.answer()

@r.callback_query(F.data.startswith("access:"))
async def choose_access(c: CallbackQuery):
    method = c.data.split(":",1)[1]
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        paid_req, link_used = await paid_access_state(s, user.id)
        enabled = (await get_setting(s, "alternative_access_enabled", "1")) == "1"
        if paid_req:
            await show_existing_paid_access(c.message, paid_req, link_used)
            await c.answer("Vous avez déjà payé : nouvelle souscription bloquée.", show_alert=True)
            return
        if method != "payment" and not enabled:
            await c.answer("Cette option est désactivée.", show_alert=True); return
        req = await create_request(s, user.id, method)
    if method == "payment":
        await edit_message(c.message, f"Le prix de l’accès est de <b>{settings.entry_price_eur} €</b>.\nRéférence : <code>{req.reference}</code>\n\nChoisissez le moyen de paiement.", reply_markup=payment_keyboard())
    elif method == "media":
        await edit_message(c.message, "Envoyez entre 5 et 10 photos ou vidéos représentant la même personne, visage visible. Vous pouvez envoyer un album complet. Après validation, le dossier sera publié dans le groupe et comptera comme première participation.\n\nProgression : <b>0/5</b>", reply_markup=kb([("❌ Annuler", "menu")]))
    else:
        async with SessionLocal() as s:
            pub = await pub_chat(s)
        if not pub:
            await edit_message(c.message, "Le groupe PUB n’est pas encore configuré. Contactez un administrateur.", reply_markup=kb([("⬅️ Retour", "menu")])) ; return
        link = await bot.create_chat_invite_link(pub.telegram_chat_id, name=f"REF-{req.id}", expire_date=req.expires_at, member_limit=99999)
        await edit_message(c.message, f"Votre lien personnel de parrainage :\n{link.invite_link}\n\nObjectif : <b>{settings.referral_target}</b> invitations validées en 48 heures.\nProgression : <b>0/{settings.referral_target}</b>", reply_markup=kb([("📊 Voir ma progression", f"ref:progress:{req.id}"), ("⬅️ Retour", "menu")]))
    await c.answer()

@r.callback_query(F.data == "paymethods")
async def payment_methods_back(c: CallbackQuery):
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        req = await active_request(s, user.id)
    if not req or req.method != AccessMethod.payment.value:
        await c.answer("Aucune demande de paiement active.", show_alert=True)
        return
    await edit_message(
        c.message,
        f"Le prix de l’accès est de <b>{settings.entry_price_eur} €</b>.\n"
        f"Référence : <code>{escape(req.reference)}</code>\n\n"
        "Choisissez le moyen de paiement.",
        reply_markup=payment_keyboard(),
    )
    await c.answer()


@r.callback_query(F.data.startswith("payment:"))
async def payment_choice(c: CallbackQuery):
    method = c.data.split(":",1)[1]
    if method not in {"paypal","revolut"}: return
    details = settings.paypal_details if method == "paypal" else settings.revolut_details
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user); req = await active_request(s, user.id)
    extra = ""
    if method == "paypal":
        extra = (
            "\n\n<b>Important PayPal :</b> utilisez le type de paiement conforme proposé par PayPal pour cette transaction. "
            "Ne classez pas volontairement un achat d’accès comme un envoi personnel afin de contourner les frais ou protections. "
            "Un paiement non conforme pourra être refusé et transmis aux administrateurs pour examen."
        )
    safe_details = escape(details or "Non configuré")
    raw_details = (details or "").strip()
    if raw_details.lower().startswith(("https://", "http://")):
        destination = f'<a href="{safe_details}">{safe_details}</a>'
    elif "@" in raw_details and "." in raw_details.split("@")[-1]:
        destination = f'<a href="mailto:{safe_details}">{safe_details}</a>'
    elif raw_details.startswith("@"):
        username = escape(raw_details[1:])
        destination = f'<a href="https://t.me/{username}">{safe_details}</a>'
    else:
        destination = f"<code>{safe_details}</code>"

    await edit_message(
        c.message,
        f"Envoyez exactement <b>{settings.entry_price_eur} €</b>.\n"
        f"Moyen : <b>{method.title()}</b>\n"
        f"Destinataire : {destination}\n"
        f"Référence obligatoire : <code>{escape(req.reference)}</code>{extra}\n\n"
        "Envoyez ensuite la capture d’écran ici.",
        reply_markup=payment_details_keyboard(),
        disable_web_page_preview=True,
    )
    await c.answer()

@r.message(F.chat.type == "private", F.photo)
async def private_photo(message: Message):
    mode = ADMIN_INPUT_MODE.get(message.from_user.id)
    if mode in {"welcome_photo", "pub_photo"} and await is_admin(message.from_user.id):
        key = "welcome_photo_file_id" if mode == "welcome_photo" else "pub_ad_photo_file_id"
        async with SessionLocal() as s: await set_setting(s, key, message.photo[-1].file_id)
        ADMIN_INPUT_MODE.pop(message.from_user.id, None)
        await message.answer("✅ Image enregistrée.", reply_markup=kb([("⚙️ Panneau administrateur", "admin:home")]))
        return
    async with SessionLocal() as s:
        user = await get_or_create_user(s, message.from_user); req = await active_request(s, user.id)
        if not req: return
        if req.method == AccessMethod.payment.value:
            logger.info("Paiement reçu user=%s request=%s", message.from_user.id, req.id)
            proof = PaymentProof(request_id=req.id, file_id=message.photo[-1].file_id, payment_method="manual")
            req.status = AccessStatus.pending_review.value; s.add(proof); await s.commit()
            cap = f"Paiement à vérifier\nUtilisateur : {message.from_user.full_name} (@{message.from_user.username or '-'})\nID : <code>{message.from_user.id}</code>\nRéférence : <code>{req.reference}</code>"
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Valider", callback_data=f"review:pay:ok:{req.id}"),InlineKeyboardButton(text="❌ Refuser", callback_data=f"review:pay:no:{req.id}")]])
            await notify_admins("send_photo", proof.file_id, caption=cap, reply_markup=markup)
            await message.answer("Votre justificatif a été reçu et envoyé aux administrateurs.", reply_markup=kb([("🏠 Menu principal", "menu")]))
        elif req.method == AccessMethod.media.value:
            count = int(await s.scalar(select(func.count(MediaSubmission.id)).where(MediaSubmission.request_id == req.id)) or 0)
            if count >= 10: await message.answer("Maximum de 10 médias atteint."); return
            s.add(MediaSubmission(request_id=req.id, file_id=message.photo[-1].file_id, media_type="photo", media_group_id=message.media_group_id)); await s.commit()
            count += 1
            txt = f"Média reçu. Progression : <b>{count}/5</b>"
            if count >= 5: txt += "\nVotre dossier est complet."
            await message.answer(txt, reply_markup=kb([("📤 Envoyer en vérification", f"media:submit:{req.id}")]) if count >= 5 else None)

@r.message(F.chat.type == "private", F.video)
async def private_video(message: Message):
    async with SessionLocal() as s:
        user = await get_or_create_user(s, message.from_user); req = await active_request(s, user.id)
        if not req or req.method != AccessMethod.media.value: return
        count = int(await s.scalar(select(func.count(MediaSubmission.id)).where(MediaSubmission.request_id == req.id)) or 0)
        if count >= 10: await message.answer("Maximum de 10 médias atteint."); return
        s.add(MediaSubmission(request_id=req.id, file_id=message.video.file_id, media_type="video", media_group_id=message.media_group_id)); await s.commit(); count += 1
    await message.answer(f"Média reçu. Progression : <b>{count}/5</b>", reply_markup=kb([("📤 Envoyer en vérification", f"media:submit:{req.id}")]) if count >= 5 else None)

@r.callback_query(F.data.startswith("media:submit:"))
async def submit_media(c: CallbackQuery):
    req_id = int(c.data.rsplit(":",1)[1])
    async with SessionLocal() as s:
        req = await s.get(AccessRequest, req_id)
        files = list((await s.scalars(select(MediaSubmission).where(MediaSubmission.request_id == req_id))).all())
        if not req or req.user_id != (await get_or_create_user(s,c.from_user)).id or len(files)<5: await c.answer("Dossier incomplet",show_alert=True); return
        req.status=AccessStatus.pending_review.value; await s.commit()
    for aid in await detected_admin_ids():
        try:
            await bot.send_message(aid, f"Dossier média #{req_id} — {len(files)} médias", reply_markup=kb([("✅ Accepter", f"review:media:ok:{req_id}"),("❌ Refuser", f"review:media:no:{req_id}")]))
            for f in files:
                if f.media_type=="photo": await bot.send_photo(aid,f.file_id)
                else: await bot.send_video(aid,f.file_id)
        except Exception:
            pass
    await edit_message(c.message, "Votre dossier a été transmis aux modérateurs.", reply_markup=kb([("🏠 Menu principal", "menu")])) ; await c.answer()

@r.callback_query(F.data.startswith("review:"))
async def review(c: CallbackQuery):
    if not await is_admin(c.from_user.id): await c.answer("Accès refusé",show_alert=True); return
    _,kind,decision,reqid = c.data.split(":"); req_id=int(reqid)
    async with SessionLocal() as s:
        req=await s.get(AccessRequest,req_id)
        if not req: return
        req.status=AccessStatus.approved.value if decision=="ok" else AccessStatus.rejected.value
        user=await s.get(User,req.user_id); await s.commit()
        if decision=="ok":
            await bot.send_message(user.telegram_id,"Votre demande a été validée.",reply_markup=kb([("🔗 Générer mon lien 24 h",f"invite:create:{req.id}")]))
        else: await bot.send_message(user.telegram_id,"Votre demande a été refusée. Le paiement reste disponible depuis /start.")
    await c.message.edit_reply_markup(reply_markup=None); await c.answer("Décision enregistrée")

@r.callback_query(F.data.startswith("recovery:invite:"))
async def recovery_invite(c: CallbackQuery):
    recovery_id = int(c.data.rsplit(":", 1)[1])
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        recovery = await s.get(MembershipRecovery, recovery_id)
        membership = await s.get(Membership, recovery.membership_id) if recovery else None
        if not recovery or not membership or membership.user_id != user.id or recovery.rejoined_at is not None:
            await c.answer("Réintégration indisponible.", show_alert=True)
            return
        req, _ = await paid_access_state(s, user.id)
        if not req:
            await c.answer("Paiement validé introuvable. Contactez un administrateur.", show_alert=True)
            return
        old = await s.scalar(select(Invite).where(Invite.user_id == user.id, Invite.revoked.is_(False), Invite.used_at.is_(None), Invite.expires_at > datetime.now(timezone.utc)))
        inv = old or await create_personal_invite(bot, s, user, req)
    await edit_or_send(c.message, f"🔁 <b>Votre retour est autorisé sans nouveau paiement.</b>\n\nLien valable 24 heures :\n{inv.invite_link}\n\nAprès votre retour, vous disposerez d’un nouveau délai pour publier votre premier média.", reply_markup=kb([("🏠 Menu", "menu")]))
    await c.answer()


@r.callback_query(F.data.startswith("invite:create:"))
async def invite_create(c: CallbackQuery):
    req_id=int(c.data.rsplit(":",1)[1])
    async with SessionLocal() as s:
        user=await get_or_create_user(s,c.from_user); req=await s.get(AccessRequest,req_id)
        if not req or req.user_id!=user.id or req.status!=AccessStatus.approved.value: await c.answer("Accès non autorisé",show_alert=True); return
        old=await s.scalar(select(Invite).where(Invite.user_id==user.id,Invite.revoked.is_(False),Invite.used_at.is_(None),Invite.expires_at>datetime.now(timezone.utc)))
        inv=old or await create_personal_invite(bot,s,user,req)
    await edit_message(c.message, f"Votre lien personnel est valable 24 heures :\n{inv.invite_link}\n\nNe le partagez pas.", reply_markup=kb([("🏠 Menu principal", "menu")])) ; await c.answer()

@r.chat_join_request()
async def join_request(j: ChatJoinRequest):
    async with SessionLocal() as s:
        user=await s.scalar(select(User).where(User.telegram_id==j.from_user.id)); chat=await s.scalar(select(TelegramChat).where(TelegramChat.telegram_chat_id==j.chat.id))
        if not user or not chat or chat.role!="vip": await bot.decline_chat_join_request(j.chat.id,j.from_user.id); return
        inv=await s.scalar(select(Invite).where(Invite.user_id==user.id,Invite.revoked.is_(False),Invite.used_at.is_(None),Invite.expires_at>datetime.now(timezone.utc)))
        if not inv: await bot.decline_chat_join_request(j.chat.id,j.from_user.id); return
        await bot.approve_chat_join_request(j.chat.id,j.from_user.id); inv.used_at=datetime.now(timezone.utc); inv.revoked=True
        req=await s.get(AccessRequest,inv.request_id); req.status=AccessStatus.member.value
        membership=await s.scalar(select(Membership).where(Membership.user_id==user.id,Membership.chat_id==chat.id))
        if not membership:
            membership=Membership(user_id=user.id,chat_id=chat.id)
            s.add(membership)
            await s.flush()
        else:
            membership.active=True
            membership.joined_at=datetime.now(timezone.utc)
            membership.first_media_at=None
            membership.warned_first_day=False
            membership.warned_activity=False
            recovery = await s.scalar(select(MembershipRecovery).where(MembershipRecovery.membership_id == membership.id))
            if recovery:
                recovery.rejoin_count += 1
                recovery.rejoined_at = datetime.now(timezone.utc)
                recovery.reminder_24h_sent_at = None
                recovery.reminder_1h_sent_at = None
                recovery.removed_at = None
                recovery.contacted_at = None
                recovery.last_contact_error = None
                # Le cycle repart immédiatement; rejoined_at est seulement informatif.
        # Un dossier accepté est publié à l'entrée et compte comme première participation.
        if req.method == AccessMethod.media.value:
            files=list((await s.scalars(select(MediaSubmission).where(MediaSubmission.request_id==req.id))).all())
            for media in files:
                try:
                    sent = await (bot.send_photo(j.chat.id, media.file_id) if media.media_type=="photo" else bot.send_video(j.chat.id, media.file_id))
                    s.add(ActivityMedia(membership_id=membership.id,message_id=sent.message_id,media_type=media.media_type))
                except Exception:
                    pass
            membership.first_media_at=datetime.now(timezone.utc)
        await s.commit()
    await bot.send_message(user.telegram_id,"Bienvenue dans le groupe VIP. Consultez /statut pour suivre votre activité.")


@r.callback_query(F.data == "paid:already_used")
async def paid_already_used(c: CallbackQuery):
    await c.answer("Votre accès a déjà été utilisé.", show_alert=True)

async def system_message_deletion_enabled() -> bool:
    """Retourne le réglage sans laisser une panne PostgreSQL bloquer la modération.

    La valeur par défaut reste activée. Ainsi, les notifications d’entrée/sortie
    sont supprimées même pendant une reconnexion temporaire à la base.
    """
    try:
        async with SessionLocal() as s:
            return (await get_setting(s, "delete_system_messages", "1")) == "1"
    except Exception:
        logger.exception("Erreur base de données pendant la lecture du réglage messages système")
        return True


async def delete_system_message(message: Message, event_name: str) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        return
    if not await system_message_deletion_enabled():
        return
    deleted = await safe_delete(message)
    if deleted:
        logger.info("Message système supprimé type=%s chat=%s message=%s", event_name, message.chat.id, message.message_id)
    else:
        logger.warning(
            "Message système non supprimé type=%s chat=%s message=%s. "
            "Vérifiez que le bot est administrateur avec le droit Supprimer les messages.",
            event_name, message.chat.id, message.message_id,
        )


@r.message(F.new_chat_members)
async def delete_join_service_message(message: Message):
    """Supprime les notifications Telegram d’entrée et d’ajout de membres."""
    await delete_system_message(message, "new_chat_members")


@r.message(F.left_chat_member)
async def delete_leave_service_message(message: Message):
    """Supprime les notifications Telegram de sortie ou d’exclusion."""
    await delete_system_message(message, "left_chat_member")


@r.message(
    F.pinned_message
    | F.new_chat_title
    | F.new_chat_photo
    | F.delete_chat_photo
    | F.group_chat_created
    | F.supergroup_chat_created
    | F.channel_chat_created
    | F.message_auto_delete_timer_changed
    | F.migrate_to_chat_id
    | F.migrate_from_chat_id
)
async def delete_other_service_messages(message: Message):
    await delete_system_message(message, "other_service_message")

@r.chat_member()
async def member_update(event: ChatMemberUpdated):
    """Suit les arrivées/sorties du groupe PUB pour le parrainage."""
    if event.chat.type not in {"group","supergroup"}: return
    async with SessionLocal() as s:
        chat=await s.scalar(select(TelegramChat).where(TelegramChat.telegram_chat_id==event.chat.id))
        if not chat or chat.role!="pub": return
        old_status=event.old_chat_member.status
        new_status=event.new_chat_member.status
        joined=old_status in {ChatMemberStatus.LEFT,ChatMemberStatus.KICKED} and new_status in {ChatMemberStatus.MEMBER,ChatMemberStatus.RESTRICTED,ChatMemberStatus.ADMINISTRATOR}
        left=old_status in {ChatMemberStatus.MEMBER,ChatMemberStatus.RESTRICTED,ChatMemberStatus.ADMINISTRATOR} and new_status in {ChatMemberStatus.LEFT,ChatMemberStatus.KICKED}
        target_id=event.new_chat_member.user.id
        if joined and event.invite_link and event.invite_link.name and event.invite_link.name.startswith("REF-"):
            try: req_id=int(event.invite_link.name.split("-",1)[1])
            except ValueError: return
            req=await s.get(AccessRequest,req_id)
            if not req or req.status!=AccessStatus.in_progress.value or (req.expires_at and req.expires_at<datetime.now(timezone.utc)): return
            exists=await s.scalar(select(Referral).where(Referral.invited_telegram_id==target_id))
            if not exists and target_id != (await s.get(User,req.user_id)).telegram_id:
                s.add(Referral(request_id=req.id,inviter_user_id=req.user_id,invited_telegram_id=target_id))
                await s.commit()
        elif left:
            ref=await s.scalar(select(Referral).where(Referral.invited_telegram_id==target_id,Referral.validated_at.is_(None)))
            if ref:
                ref.rejected=True; await s.commit()

@r.my_chat_member()
async def bot_chat_update(event: ChatMemberUpdated):
    if event.chat.type not in {"group","supergroup"}: return
    async with SessionLocal() as s:
        chat=await s.scalar(select(TelegramChat).where(TelegramChat.telegram_chat_id==event.chat.id))
        if not chat: chat=TelegramChat(telegram_chat_id=event.chat.id,title=event.chat.title or "",role="unassigned"); s.add(chat)
        chat.active=event.new_chat_member.status not in {ChatMemberStatus.LEFT,ChatMemberStatus.KICKED}; await s.commit()
    if event.new_chat_member.status in {ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.MEMBER}:
        markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐ Définir VIP",callback_data=f"chatrole:vip:{event.chat.id}"),InlineKeyboardButton(text="📣 Définir PUB",callback_data=f"chatrole:pub:{event.chat.id}")]])
        text=(f"🤫 <b>Nouveau groupe détecté</b>\n\n"
              f"Nom : <b>{event.chat.title or 'Sans titre'}</b>\n"
              f"ID : <code>{event.chat.id}</code>\n\n"
              "Le bot restera silencieux dans ce groupe. Choisissez son rôle ci-dessous.")
        # La demande de configuration est envoyée uniquement aux ADMIN_IDS en privé.
        for aid in settings.admin_id_set:
            try: await bot.send_message(aid, text, reply_markup=markup)
            except Exception: pass

@r.callback_query(F.data.startswith("chatrole:"))
async def chat_role(c: CallbackQuery):
    _,role,cid=c.data.split(":"); chat_id=int(cid)
    if not await is_admin(c.from_user.id, chat_id):
        await c.answer("Seul un administrateur de ce groupe peut choisir son rôle.", show_alert=True)
        return
    async with SessionLocal() as s:
        if role=="vip":
            old=await s.scalar(select(TelegramChat).where(TelegramChat.role=="vip"));
            if old: old.role="unassigned"
        chat=await s.scalar(select(TelegramChat).where(TelegramChat.telegram_chat_id==chat_id)); chat.role=role; await s.commit()
    await edit_message(c.message, f"Groupe configuré comme {role.upper()}."); await c.answer()

async def render_admin_panel(target: Message, edit: bool = False):
    async with SessionLocal() as s:
        opt=(await get_setting(s,"alternative_access_enabled","1"))=="1"
        opened=(await get_setting(s,"group_open","1"))=="1"
    if edit:
        await edit_message(target, "<b>Panneau administrateur</b>\n\nTous les réglages sont accessibles avec les boutons ci-dessous.", reply_markup=admin_home(opt,opened))
    else:
        await target.answer("<b>Panneau administrateur</b>\n\nTous les réglages sont accessibles avec les boutons ci-dessous.", reply_markup=admin_home(opt,opened))

@r.message(Command("admin"))
async def admin(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("Vous n’êtes pas administrateur d’un groupe relié au bot.")
        return
    await render_admin_panel(message)

@r.callback_query(F.data=="admin:home")
async def admin_home_callback(c: CallbackQuery):
    if not await is_admin(c.from_user.id):
        await c.answer("Accès refusé", show_alert=True)
        return
    await render_admin_panel(c.message, edit=True)
    await c.answer()

@r.callback_query(F.data=="admin:toggle_options")
async def toggle_options(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return
    async with SessionLocal() as s:
        current=(await get_setting(s,"alternative_access_enabled","1"))=="1"; await set_setting(s,"alternative_access_enabled","0" if current else "1")
        opened=(await get_setting(s,"group_open","1"))=="1"
    await c.message.edit_reply_markup(reply_markup=admin_home(not current,opened)); await c.answer("Réglage modifié")

@r.callback_query(F.data=="admin:toggle_group")
async def toggle_group(c: CallbackQuery):
    if not await is_admin(c.from_user.id):
        await c.answer("Accès refusé", show_alert=True)
        return

    async with SessionLocal() as s:
        current = (await get_setting(s, "group_open", "1")) == "1"
        opt = (await get_setting(s, "alternative_access_enabled", "1")) == "1"
        try:
            await set_group_open(bot, s, not current)
        except RuntimeError as exc:
            await c.answer(str(exc), show_alert=True)
            await edit_message(c.message, 
                "⚠️ <b>Action impossible</b>\n\n"
                "Aucun groupe VIP n’est encore configuré.\n\n"
                "Ajoutez le bot à votre groupe, donnez-lui les droits administrateur, "
                "puis ouvrez <b>Groupes détectés</b> et définissez ce groupe comme VIP.",
                reply_markup=kb([("👥 Groupes détectés", "admin:groups"), ("⬅️ Retour", "admin:home")]),
            )
            return
        except Exception as exc:
            await c.answer("Impossible de modifier le groupe. Consultez Santé du système.", show_alert=True)
            await edit_message(c.message, 
                "❌ <b>Modification impossible</b>\n\n"
                f"Telegram a refusé la modification : <code>{type(exc).__name__}</code>.\n"
                "Vérifiez que le bot est administrateur du groupe VIP et possède le droit de modifier les permissions.",
                reply_markup=kb([("🩺 Santé du système", "admin:health"), ("⬅️ Retour", "admin:home")]),
            )
            return

    await c.message.edit_reply_markup(reply_markup=admin_home(opt, not current))
    await c.answer("Groupe ouvert" if not current else "Groupe fermé")

@r.callback_query(F.data=="admin:groups")
async def admin_groups(c: CallbackQuery):
    if not await is_admin(c.from_user.id):
        await c.answer("Accès refusé", show_alert=True); return
    async with SessionLocal() as s:
        chats=list((await s.scalars(select(TelegramChat).order_by(TelegramChat.id))).all())
    lines=["<b>Groupes détectés</b>"]
    rows=[]
    for chat in chats:
        lines.append(f"• {chat.title or chat.telegram_chat_id} — <b>{chat.role.upper()}</b> — {'actif' if chat.active else 'inactif'}")
        rows.append((f"⚙️ {chat.title[:28] or chat.telegram_chat_id}", f"admin:group:{chat.telegram_chat_id}"))
    rows.append(("⬅️ Retour", "admin:home"))
    await edit_message(c.message, "\n".join(lines) if chats else "Aucun groupe détecté.", reply_markup=kb(rows))
    await c.answer()

@r.callback_query(F.data.startswith("admin:group:"))
async def admin_group_detail(c: CallbackQuery):
    chat_id=int(c.data.rsplit(":",1)[1])
    if not await is_admin(c.from_user.id, chat_id):
        await c.answer("Vous devez administrer ce groupe.", show_alert=True); return
    async with SessionLocal() as s:
        chat=await s.scalar(select(TelegramChat).where(TelegramChat.telegram_chat_id==chat_id))
    if not chat:
        await c.answer("Groupe introuvable", show_alert=True); return
    markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Définir VIP",callback_data=f"chatrole:vip:{chat_id}"), InlineKeyboardButton(text="📣 Définir PUB",callback_data=f"chatrole:pub:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Retour",callback_data="admin:groups")],
    ])
    await edit_message(c.message, f"<b>{chat.title}</b>\nID : <code>{chat.telegram_chat_id}</code>\nRôle actuel : <b>{chat.role.upper()}</b>", reply_markup=markup)
    await c.answer()

@r.callback_query(F.data == "admin:health")
async def admin_health(c: CallbackQuery):
    if not await is_admin(c.from_user.id):
        await c.answer("Accès refusé", show_alert=True)
        return
    await c.answer("Vérification en cours…")
    report, _, _ = await build_health_report()
    await edit_message(c.message, report, reply_markup=kb([("🔄 Relancer le diagnostic", "admin:health"), ("⬅️ Retour", "admin:home")]))

@r.message(Command("statut"))
async def status(message: Message):
    async with SessionLocal() as s:
        user=await get_or_create_user(s,message.from_user); vip=await vip_chat(s)
        if not vip: await message.answer("Aucun groupe VIP configuré."); return
        m=await s.scalar(select(Membership).where(Membership.user_id==user.id,Membership.chat_id==vip.id,Membership.active.is_(True)))
        if not m: await message.answer("Vous n’êtes pas membre actif."); return
        count=await activity_count(s,m.id)
    await message.answer(f"Médias comptabilisés sur 72 h : <b>{count}/{settings.activity_media_target}</b>")

@r.message(F.chat.type.in_({"group","supergroup"}))
async def group_messages(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    try:
        async with SessionLocal() as s:
            chat = await s.scalar(select(TelegramChat).where(TelegramChat.telegram_chat_id == message.chat.id))
            if not chat or chat.role != "vip":
                return
            sender_admin = await is_admin(message.from_user.id, message.chat.id)
            text_value = message.text or message.caption or ""

            hit = await forbidden_word_hit(s, text_value)
            if hit and not sender_admin:
                await safe_delete(message)
                await stat_inc(s, "forbidden_words_blocked")
                sanction = await get_setting(s, "forbidden_words_sanction", "warning")
                await s.commit()
                await apply_sanction(bot, message, sanction, "mot interdit")
                logger.info("Mot interdit chat=%s user=%s word=%s", message.chat.id, message.from_user.id, hit)
                return

            if await links_blocked(s, message, sender_admin):
                await safe_delete(message)
                await stat_inc(s, "links_blocked")
                sanction = await get_setting(s, "anti_links_sanction", "warning")
                await s.commit()
                await apply_sanction(bot, message, sanction, "lien interdit")
                logger.info("Lien bloqué chat=%s user=%s", message.chat.id, message.from_user.id)
                return

            if (message.photo or message.video) and await process_repost(bot, s, message):
                return

            user = await get_or_create_user(s, message.from_user)
            membership = await s.scalar(select(Membership).where(Membership.user_id == user.id, Membership.chat_id == chat.id, Membership.active.is_(True)))
            if membership and (message.photo or message.video):
                if not membership.first_media_at:
                    membership.first_media_at = datetime.now(timezone.utc)
                s.add(ActivityMedia(membership_id=membership.id, message_id=message.message_id, media_type="photo" if message.photo else "video"))
                await s.commit()
    except Exception:
        logger.exception("Erreur modération message chat=%s", message.chat.id)

async def maintenance_loop():
    global LAST_MAINTENANCE_AT, LAST_MAINTENANCE_ERROR
    health_tick = 0
    while True:
        try:
            LAST_MAINTENANCE_AT = datetime.now(timezone.utc)
            LAST_MAINTENANCE_ERROR = None
            now=datetime.now(timezone.utc)
            async with SessionLocal() as s:
                # Validate referrals after secret internal delay.
                refs=list((await s.scalars(select(Referral).where(Referral.validated_at.is_(None),Referral.rejected.is_(False),Referral.joined_at <= now-timedelta(minutes=settings.referral_validation_minutes)))).all())
                for ref in refs:
                    ref.validated_at=now
                    req=await s.get(AccessRequest,ref.request_id)
                    total=await validated_referrals(s,req.id)+1
                    user=await s.get(User,ref.inviter_user_id)
                    if total>=settings.referral_target:
                        req.status=AccessStatus.approved.value
                        try: await bot.send_message(user.telegram_id,"Objectif atteint. Votre accès est validé.",reply_markup=kb([("🔗 Générer mon lien 24 h",f"invite:create:{req.id}")]))
                        except Exception: pass
                await s.commit()
                # Activity enforcement only while group is open.
                if (await get_setting(s,"group_open","1"))=="1":
                    memberships=list((await s.scalars(select(Membership).where(Membership.active.is_(True)))).all())
                    for m in memberships:
                        user=await s.get(User,m.user_id); chat=await s.get(TelegramChat,m.chat_id)
                        age=now-m.joined_at
                        if not m.first_media_at:
                            recovery = await s.scalar(select(MembershipRecovery).where(MembershipRecovery.membership_id == m.id))
                            if not recovery:
                                recovery = MembershipRecovery(membership_id=m.id, reason="missing_first_media")
                                s.add(recovery)
                                await s.flush()
                            deadline = m.joined_at + timedelta(hours=settings.first_media_hours)
                            remaining = deadline - now
                            first_reminder_hours = max(2, min(12, settings.first_media_hours // 2))
                            if remaining <= timedelta(hours=first_reminder_hours) and remaining > timedelta(hours=1) and recovery.reminder_24h_sent_at is None:
                                try:
                                    await bot.send_message(user.telegram_id, f"⏳ <b>Rappel</b> : il vous reste environ {max(1, int(remaining.total_seconds() // 3600))} heure(s) pour publier votre premier média et conserver votre accès au groupe VIP.")
                                    recovery.reminder_24h_sent_at = now
                                except Exception as exc:
                                    recovery.last_contact_error = repr(exc)[:1000]
                            if remaining <= timedelta(hours=1) and remaining > timedelta(0) and recovery.reminder_1h_sent_at is None:
                                try:
                                    await bot.send_message(user.telegram_id, "⚠️ <b>Dernier rappel</b> : il vous reste moins d’une heure pour publier votre premier média.")
                                    recovery.reminder_1h_sent_at = now
                                except Exception as exc:
                                    recovery.last_contact_error = repr(exc)[:1000]
                            if now >= deadline:
                                try:
                                    await bot.ban_chat_member(chat.telegram_chat_id,user.telegram_id)
                                    await bot.unban_chat_member(chat.telegram_chat_id,user.telegram_id,only_if_banned=True)
                                except Exception:
                                    logger.exception("Erreur exclusion premier média chat=%s user=%s", chat.telegram_chat_id, user.telegram_id)
                                m.active=False
                                recovery.removed_at = recovery.removed_at or now
                                recovery.contact_attempts += 1
                                try:
                                    await bot.send_message(user.telegram_id, "Vous avez été retiré pour absence de premier média. Votre paiement reste valide et vous pouvez revenir sans repayer.", reply_markup=kb([("🔁 Revenir dans le groupe", f"recovery:invite:{recovery.id}")]))
                                    recovery.contacted_at = now
                                    recovery.last_contact_error = None
                                except Exception as exc:
                                    recovery.last_contact_error = repr(exc)[:1000]
                                    logger.exception("Impossible de contacter utilisateur impacté user=%s", user.telegram_id)
                        elif age>=timedelta(hours=settings.activity_window_hours):
                            count=await activity_count(s,m.id)
                            if count<settings.activity_media_target:
                                try: await bot.ban_chat_member(chat.telegram_chat_id,user.telegram_id); await bot.unban_chat_member(chat.telegram_chat_id,user.telegram_id,only_if_banned=True); await bot.send_message(user.telegram_id,"Vous avez été exclu pour activité insuffisante.")
                                except Exception: pass
                                m.active=False
                    # Rattrapage des personnes déjà exclues avant cette version.
                    impacted_memberships = list((await s.scalars(select(Membership).where(Membership.active.is_(False), Membership.first_media_at.is_(None)))).all())
                    for impacted_m in impacted_memberships:
                        recovery = await s.scalar(select(MembershipRecovery).where(MembershipRecovery.membership_id == impacted_m.id))
                        if not recovery:
                            recovery = MembershipRecovery(membership_id=impacted_m.id, reason="missing_first_media", removed_at=now)
                            s.add(recovery)
                            await s.flush()
                        if recovery.rejoined_at is None and recovery.contacted_at is None:
                            impacted_user = await s.get(User, impacted_m.user_id)
                            req, _ = await paid_access_state(s, impacted_user.id)
                            if req and impacted_user.started_bot:
                                recovery.contact_attempts += 1
                                try:
                                    await bot.send_message(impacted_user.telegram_id, "✅ Votre paiement reste valide. Vous aviez été retiré pour absence de premier média. Vous pouvez revenir sans repayer avec le bouton ci-dessous.", reply_markup=kb([("🔁 Revenir dans le groupe", f"recovery:invite:{recovery.id}")]))
                                    recovery.contacted_at = now
                                    recovery.last_contact_error = None
                                except Exception as exc:
                                    recovery.last_contact_error = repr(exc)[:1000]
                    await s.commit()
            health_tick += 1
            if health_tick >= 5:
                health_tick = 0
                await automatic_health_alerts()
        except Exception as exc:
            LAST_MAINTENANCE_ERROR = repr(exc)
            print("maintenance error", repr(exc))
        await asyncio.sleep(60)


# --- Configuration des contenus visibles ---
async def welcome_config_screen(c: CallbackQuery):
    async with SessionLocal() as s:
        text_value = await get_setting(s, "welcome_text", DEFAULT_WELCOME_TEXT)
        photo = await get_setting(s, "welcome_photo_file_id", "")
    preview = text_value[:700] + ("…" if len(text_value) > 700 else "")
    await edit_message(c.message, 
        "<b>🖼 Configuration de l’accueil</b>\n\n"
        f"Image : <b>{'configurée' if photo else 'aucune'}</b>\n\n"
        f"<b>Texte actuel :</b>\n{preview}",
        reply_markup=kb([
            ("✍️ Modifier le texte", "admin:welcome_text"),
            ("🖼 Modifier l’image", "admin:welcome_photo"),
            ("🗑 Retirer l’image", "admin:welcome_photo_remove"),
            ("👁 Prévisualiser", "admin:welcome_preview"),
            ("⬅️ Retour", "admin:home"),
        ]),
    )

@r.callback_query(F.data == "admin:welcome")
async def admin_welcome(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    await welcome_config_screen(c); await c.answer()

@r.callback_query(F.data == "admin:welcome_text")
async def admin_welcome_text(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    ADMIN_INPUT_MODE[c.from_user.id] = "welcome_text"
    await edit_message(c.message, "Envoyez maintenant le nouveau texte d’accueil en message privé. HTML simple accepté.\n\nEnvoyez /annuler pour quitter.")
    await c.answer()

@r.callback_query(F.data == "admin:welcome_photo")
async def admin_welcome_photo(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    ADMIN_INPUT_MODE[c.from_user.id] = "welcome_photo"
    await edit_message(c.message, "Envoyez maintenant l’image d’accueil en tant que photo.\n\nEnvoyez /annuler pour quitter.")
    await c.answer()

@r.callback_query(F.data == "admin:welcome_photo_remove")
async def admin_welcome_photo_remove(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    async with SessionLocal() as s: await set_setting(s, "welcome_photo_file_id", "")
    await c.answer("Image retirée"); await welcome_config_screen(c)

@r.callback_query(F.data == "admin:welcome_preview")
async def admin_welcome_preview(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    async with SessionLocal() as s:
        text_value=await get_setting(s,"welcome_text",DEFAULT_WELCOME_TEXT); photo=await get_setting(s,"welcome_photo_file_id","")
    markup=kb([("📜 Consulter les règles","rules:show")])
    if photo: await bot.send_photo(c.from_user.id, photo, caption=text_value, reply_markup=markup)
    else: await bot.send_message(c.from_user.id, text_value, reply_markup=markup)
    await c.answer("Prévisualisation envoyée")

async def pub_config_screen(c: CallbackQuery):
    async with SessionLocal() as s:
        text_value=await get_setting(s,"pub_ad_text",DEFAULT_PUB_AD_TEXT); photo=await get_setting(s,"pub_ad_photo_file_id","")
    preview=text_value[:700]+("…" if len(text_value)>700 else "")
    await edit_message(c.message, 
        "<b>📣 Publicité des groupes PUB</b>\n\n"
        f"Image : <b>{'configurée' if photo else 'aucune'}</b>\n\n<b>Texte actuel :</b>\n{preview}",
        reply_markup=kb([
            ("✍️ Modifier le texte", "admin:pub_text"),
            ("🖼 Modifier l’image", "admin:pub_photo"),
            ("🗑 Retirer l’image", "admin:pub_photo_remove"),
            ("👁 Prévisualiser", "admin:pub_preview"),
            ("🚀 Envoyer aux groupes PUB", "admin:pub_send"),
            ("⬅️ Retour", "admin:home"),
        ]),
    )

@r.callback_query(F.data == "admin:pub_ad")
async def admin_pub_ad(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    await pub_config_screen(c); await c.answer()

@r.callback_query(F.data == "admin:pub_text")
async def admin_pub_text(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    ADMIN_INPUT_MODE[c.from_user.id] = "pub_text"
    await edit_message(c.message, "Envoyez maintenant le texte de la publicité PUB.\n\nEnvoyez /annuler pour quitter.")
    await c.answer()

@r.callback_query(F.data == "admin:pub_photo")
async def admin_pub_photo(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    ADMIN_INPUT_MODE[c.from_user.id] = "pub_photo"
    await edit_message(c.message, "Envoyez maintenant l’image de la publicité PUB en tant que photo.\n\nEnvoyez /annuler pour quitter.")
    await c.answer()

@r.callback_query(F.data == "admin:pub_photo_remove")
async def admin_pub_photo_remove(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    async with SessionLocal() as s: await set_setting(s,"pub_ad_photo_file_id","")
    await c.answer("Image retirée"); await pub_config_screen(c)

@r.callback_query(F.data == "admin:pub_preview")
async def admin_pub_preview(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    async with SessionLocal() as s:
        text_value=await get_setting(s,"pub_ad_text",DEFAULT_PUB_AD_TEXT); photo=await get_setting(s,"pub_ad_photo_file_id","")
    markup=kb([("🚀 Demander mon accès","rules:show")])
    if photo: await bot.send_photo(c.from_user.id,photo,caption=text_value,reply_markup=markup)
    else: await bot.send_message(c.from_user.id,text_value,reply_markup=markup)
    await c.answer("Prévisualisation envoyée")

@r.callback_query(F.data == "admin:pub_send")
async def admin_pub_send(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    async with SessionLocal() as s:
        chats=list((await s.scalars(select(TelegramChat).where(TelegramChat.role=="pub",TelegramChat.active.is_(True)))).all())
        text_value=await get_setting(s,"pub_ad_text",DEFAULT_PUB_AD_TEXT); photo=await get_setting(s,"pub_ad_photo_file_id","")
    if not chats: return await c.answer("Aucun groupe PUB actif", show_alert=True)
    me=await bot.get_me(); markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Demander mon accès",url=f"https://t.me/{me.username}?start=pub")]])
    sent=failed=0
    for chat in chats:
        try:
            if photo: await bot.send_photo(chat.telegram_chat_id,photo,caption=text_value,reply_markup=markup)
            else: await bot.send_message(chat.telegram_chat_id,text_value,reply_markup=markup)
            sent+=1
        except Exception: failed+=1
    await c.answer(f"Envoyée : {sent} | Échecs : {failed}", show_alert=True)

# --- Extensions production : files d'attente, broadcast, statistiques et navigation ---
BROADCAST_WAITING: set[int] = set()

@r.callback_query(F.data == "menu")
async def back_to_menu(c: CallbackQuery):
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        paid_req, link_used = await paid_access_state(s, user.id)
        enabled = (await get_setting(s, "alternative_access_enabled", "1")) == "1"
    if paid_req:
        await show_existing_paid_access(c.message, paid_req, link_used)
        await c.answer()
        return
    await edit_message(c.message, "Choisissez votre méthode d’accès :" if enabled else "L’accès au groupe est actuellement disponible uniquement par paiement.", reply_markup=access_methods(enabled))
    await c.answer()

@r.callback_query(F.data == "rules:quit")
async def quit_rules(c: CallbackQuery):
    await edit_message(c.message, "Vous n’avez pas accepté le règlement. Aucun accès ne peut être créé.\n\nVous pouvez revenir avec /start.")
    await c.answer()

async def pending_requests_text(method: str) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as s:
        requests = list((await s.scalars(select(AccessRequest).where(AccessRequest.method == method, AccessRequest.status == AccessStatus.pending_review.value).order_by(AccessRequest.created_at))).all())
        rows=[]; lines=[]
        for req in requests[:30]:
            u=await s.get(User, req.user_id)
            label=f"#{req.id} — {(u.first_name or 'Utilisateur')[:18]}"
            cb="admin:pending_pay:" if method==AccessMethod.payment.value else "admin:pending_media:"
            rows.append((label, cb+str(req.id)))
            lines.append(f"• <b>#{req.id}</b> — {u.first_name} @{u.username or '-'} — {req.created_at.strftime('%d/%m %H:%M')}")
    rows.append(("⬅️ Retour", "admin:home"))
    title="Paiements en attente" if method==AccessMethod.payment.value else "Dossiers en attente"
    return f"<b>{title}</b>\n\n"+("\n".join(lines) if lines else "Aucune demande en attente."), kb(rows)

@r.callback_query(F.data == "admin:payments")
async def admin_payments(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    text_, markup = await pending_requests_text(AccessMethod.payment.value)
    await edit_message(c.message, text_, reply_markup=markup); await c.answer()

@r.callback_query(F.data == "admin:media_reviews")
async def admin_media_queue(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    text_, markup = await pending_requests_text(AccessMethod.media.value)
    await edit_message(c.message, text_, reply_markup=markup); await c.answer()

@r.callback_query(F.data.startswith("admin:pending_pay:"))
async def pending_pay_detail(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    req_id=int(c.data.rsplit(":",1)[1])
    async with SessionLocal() as s:
        req=await s.get(AccessRequest, req_id); user=await s.get(User, req.user_id) if req else None
        proof=await s.scalar(select(PaymentProof).where(PaymentProof.request_id==req_id).order_by(PaymentProof.id.desc()))
    if not req or not proof: return await c.answer("Demande introuvable", show_alert=True)
    caption=f"<b>Paiement #{req.id}</b>\nUtilisateur : {user.first_name} @{user.username or '-'}\nID : <code>{user.telegram_id}</code>\nRéférence : <code>{req.reference}</code>"
    await bot.send_photo(c.from_user.id, proof.file_id, caption=caption, reply_markup=kb([("✅ Valider",f"review:pay:ok:{req.id}"),("❌ Refuser",f"review:pay:no:{req.id}")]))
    await c.answer("Justificatif envoyé en privé")

@r.callback_query(F.data.startswith("admin:pending_media:"))
async def pending_media_detail(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    req_id=int(c.data.rsplit(":",1)[1])
    async with SessionLocal() as s:
        files=list((await s.scalars(select(MediaSubmission).where(MediaSubmission.request_id==req_id))).all())
    await bot.send_message(c.from_user.id, f"<b>Dossier #{req_id}</b> — {len(files)} média(s)", reply_markup=kb([("✅ Accepter",f"review:media:ok:{req_id}"),("❌ Refuser",f"review:media:no:{req_id}")]))
    for f in files:
        try:
            await (bot.send_photo(c.from_user.id,f.file_id) if f.media_type=="photo" else bot.send_video(c.from_user.id,f.file_id))
        except Exception: pass
    await c.answer("Dossier envoyé en privé")

@r.callback_query(F.data == "admin:broadcast")
async def broadcast_start(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    BROADCAST_WAITING.add(c.from_user.id)
    await edit_message(c.message, "<b>Broadcast</b>\n\nEnvoyez maintenant en message privé le texte à transmettre à tous les utilisateurs ayant démarré le bot.\n\nEnvoyez /annuler pour quitter.", reply_markup=kb([("⬅️ Annuler", "admin:broadcast_cancel")]))
    await c.answer()

@r.callback_query(F.data == "admin:broadcast_cancel")
async def broadcast_cancel_button(c: CallbackQuery):
    BROADCAST_WAITING.discard(c.from_user.id); await render_admin_panel(c.message, edit=True); await c.answer("Annulé")


async def moderation_home_screen(message: Message):
    async with SessionLocal() as s:
        fw = (await get_setting(s, "forbidden_words_enabled", "0")) == "1"
        al = (await get_setting(s, "anti_links_enabled", "0")) == "1"
        ar = (await get_setting(s, "anti_repost_enabled", "0")) == "1"
        sysmsg = (await get_setting(s, "delete_system_messages", "1")) == "1"
    await edit_or_send(message, "<b>🛡 Modération</b>\n\nToutes les protections sont stockées en PostgreSQL et configurables ici.", reply_markup=kb([
        (f"🚫 Mots interdits — {'ON' if fw else 'OFF'}", "mod:words"),
        (f"🔗 Anti-liens — {'ON' if al else 'OFF'}", "mod:links"),
        (f"♻️ Anti-repost — {'ON' if ar else 'OFF'}", "mod:repost"),
        (f"🧹 Messages système — {'ON' if sysmsg else 'OFF'}", "mod:system"),
        ("⚠️ Sanctions", "mod:sanctions"),
        ("📊 Statistiques", "mod:stats"),
        ("⬅ Retour", "admin:home"), ("🏠 Menu", "menu")
    ]))

@r.callback_query(F.data == "mod:home")
async def moderation_home(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    await moderation_home_screen(c.message); await c.answer()

@r.callback_query(F.data == "mod:words")
async def moderation_words(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    async with SessionLocal() as s:
        enabled=(await get_setting(s,"forbidden_words_enabled","0"))=="1"
        sanction=await get_setting(s,"forbidden_words_sanction","warning")
        words=list((await s.scalars(select(ForbiddenWord).order_by(ForbiddenWord.word))).all())
    listing="\n".join(f"• {escape(w.word)} {'✅' if w.active else '⏸'}" for w in words[:50]) or "Aucun mot enregistré."
    await edit_or_send(c.message, f"<b>🚫 Mots interdits</b>\n\nÉtat : <b>{'ON' if enabled else 'OFF'}</b>\nSanction : <b>{sanction}</b>\n\n{listing}", reply_markup=kb([
        ("Désactiver" if enabled else "Activer", "mod:words:toggle"), ("➕ Ajouter", "mod:words:add"), ("➖ Supprimer", "mod:words:remove"), ("⚠️ Changer sanction", "mod:words:sanction"), ("⬅ Retour", "mod:home"), ("🏠 Menu", "menu")]))
    await c.answer()

@r.callback_query(F.data == "mod:words:toggle")
async def moderation_words_toggle(c: CallbackQuery):
    async with SessionLocal() as s:
        cur=(await get_setting(s,"forbidden_words_enabled","0"))=="1"; await set_setting(s,"forbidden_words_enabled","0" if cur else "1")
    await moderation_words(c)

@r.callback_query(F.data.in_({"mod:words:add","mod:words:remove"}))
async def moderation_words_input(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    ADMIN_INPUT_MODE[c.from_user.id]="forbidden_add" if c.data.endswith("add") else "forbidden_remove"
    await edit_or_send(c.message,"Envoyez le mot ou l’expression. Envoyez /annuler pour quitter.",reply_markup=kb([("⬅ Retour","mod:words"),("🏠 Menu","menu")]))
    await c.answer()

@r.callback_query(F.data == "mod:words:sanction")
async def words_sanction(c: CallbackQuery):
    await edit_or_send(c.message,"Choisissez la sanction appliquée aux mots interdits.",reply_markup=kb([(x.title(),f"mod:set:forbidden_words_sanction:{x}") for x in ["delete","warning","mute","kick","ban"]]+[("⬅ Retour","mod:words")]))
    await c.answer()

@r.callback_query(F.data == "mod:links")
async def moderation_links(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    async with SessionLocal() as s:
        vals={k:(await get_setting(s,k,d))=="1" for k,d in [("anti_links_enabled","0"),("anti_links_allow_telegram","0"),("anti_links_allow_tme","0"),("anti_links_allow_http","0"),("anti_links_allow_https","0"),("anti_links_allow_admins","1")]}
        sanction=await get_setting(s,"anti_links_sanction","warning")
        domains=list((await s.scalars(select(LinkWhitelistDomain).order_by(LinkWhitelistDomain.domain))).all())
        users=list((await s.scalars(select(LinkWhitelistUser).order_by(LinkWhitelistUser.telegram_id))).all())
    info=f"<b>🔗 Anti-liens</b>\n\nSanction : <b>{sanction}</b>\nDomaines whitelist : <b>{len(domains)}</b>\nUtilisateurs whitelist : <b>{len(users)}</b>"
    rows=[(f"Protection {'ON' if vals['anti_links_enabled'] else 'OFF'}","mod:linktoggle:anti_links_enabled"),(f"Telegram {'✅' if vals['anti_links_allow_telegram'] else '❌'}","mod:linktoggle:anti_links_allow_telegram"),(f"t.me {'✅' if vals['anti_links_allow_tme'] else '❌'}","mod:linktoggle:anti_links_allow_tme"),(f"HTTP {'✅' if vals['anti_links_allow_http'] else '❌'}","mod:linktoggle:anti_links_allow_http"),(f"HTTPS {'✅' if vals['anti_links_allow_https'] else '❌'}","mod:linktoggle:anti_links_allow_https"),(f"Admins {'✅' if vals['anti_links_allow_admins'] else '❌'}","mod:linktoggle:anti_links_allow_admins"),("➕ Domaine whitelist","mod:domain:add"),("➖ Domaine whitelist","mod:domain:remove"),("➕ Utilisateur whitelist","mod:user:add"),("➖ Utilisateur whitelist","mod:user:remove"),("⚠️ Sanction","mod:links:sanction"),("⬅ Retour","mod:home"),("🏠 Menu","menu")]
    await edit_or_send(c.message,info,reply_markup=kb(rows)); await c.answer()

@r.callback_query(F.data.startswith("mod:linktoggle:"))
async def link_toggle(c: CallbackQuery):
    key=c.data.rsplit(":",1)[1]
    async with SessionLocal() as s:
        cur=(await get_setting(s,key,"0"))=="1"; await set_setting(s,key,"0" if cur else "1")
    await moderation_links(c)

@r.callback_query(F.data.in_({"mod:domain:add","mod:domain:remove","mod:user:add","mod:user:remove"}))
async def whitelist_input(c: CallbackQuery):
    ADMIN_INPUT_MODE[c.from_user.id]=c.data.replace("mod:","").replace(":","_")
    await edit_or_send(c.message,"Envoyez la valeur à ajouter ou supprimer. Pour un utilisateur, envoyez son ID Telegram numérique.",reply_markup=kb([("⬅ Retour","mod:links")]))
    await c.answer()

@r.callback_query(F.data == "mod:links:sanction")
async def links_sanction(c: CallbackQuery):
    await edit_or_send(c.message,"Choisissez la sanction anti-liens.",reply_markup=kb([(x.title(),f"mod:set:anti_links_sanction:{x}") for x in ["delete","warning","mute","kick","ban"]]+[("⬅ Retour","mod:links")]))
    await c.answer()

@r.callback_query(F.data.startswith("mod:set:"))
async def set_moderation_value(c: CallbackQuery):
    _,_,key,value=c.data.split(":",3)
    async with SessionLocal() as s: await set_setting(s,key,value)
    await (moderation_words(c) if key.startswith("forbidden") else moderation_links(c))

@r.callback_query(F.data == "mod:repost")
async def moderation_repost(c: CallbackQuery):
    async with SessionLocal() as s:
        enabled=(await get_setting(s,"anti_repost_enabled","0"))=="1"; auto=(await get_setting(s,"anti_repost_auto_delete","1"))=="1"
        count=int(await s.scalar(select(func.count(MediaHash.id))) or 0); dup=int((await s.get(ModerationStat,"reposts_detected")).value if await s.get(ModerationStat,"reposts_detected") else 0)
    await edit_or_send(c.message,f"<b>♻️ Anti-repost</b>\n\nÉtat : <b>{'ON' if enabled else 'OFF'}</b>\nSuppression automatique : <b>{'ON' if auto else 'OFF'}</b>\nHash enregistrés : <b>{count}</b>\nDoublons détectés : <b>{dup}</b>",reply_markup=kb([("Désactiver" if enabled else "Activer","mod:repost:toggle"),("Suppression auto ON/OFF","mod:repost:delete"),("✏️ Modifier le message","mod:repost:message"),("🗑 Vider les hash","mod:repost:clear"),("⬅ Retour","mod:home"),("🏠 Menu","menu")]))
    await c.answer()

@r.callback_query(F.data.in_({"mod:repost:toggle","mod:repost:delete"}))
async def repost_toggle(c: CallbackQuery):
    key="anti_repost_enabled" if c.data.endswith("toggle") else "anti_repost_auto_delete"
    async with SessionLocal() as s:
        cur=(await get_setting(s,key,"0"))=="1"; await set_setting(s,key,"0" if cur else "1")
    await moderation_repost(c)

@r.callback_query(F.data == "mod:repost:message")
async def repost_message_input(c: CallbackQuery):
    ADMIN_INPUT_MODE[c.from_user.id]="anti_repost_message"
    await edit_or_send(c.message,"Envoyez le nouveau message. Utilisez {user} pour la mention.",reply_markup=kb([("⬅ Retour","mod:repost")]))
    await c.answer()

@r.callback_query(F.data == "mod:repost:clear")
async def repost_clear(c: CallbackQuery):
    async with SessionLocal() as s:
        await s.execute(__import__('sqlalchemy').delete(MediaHash)); await s.commit()
    await moderation_repost(c)

@r.callback_query(F.data == "mod:system")
async def system_toggle(c: CallbackQuery):
    async with SessionLocal() as s:
        cur=(await get_setting(s,"delete_system_messages","1"))=="1"; await set_setting(s,"delete_system_messages","0" if cur else "1")
    await c.answer("Réglage modifié",show_alert=True); await moderation_home_screen(c.message)

@r.callback_query(F.data == "mod:sanctions")
async def sanctions_screen(c: CallbackQuery):
    async with SessionLocal() as s:
        fw=await get_setting(s,"forbidden_words_sanction","warning"); links=await get_setting(s,"anti_links_sanction","warning")
    await edit_or_send(c.message,f"<b>⚠️ Sanctions</b>\n\nMots interdits : <b>{fw}</b>\nAnti-liens : <b>{links}</b>\n\nMute : 1 heure.",reply_markup=kb([("🚫 Mots interdits","mod:words:sanction"),("🔗 Anti-liens","mod:links:sanction"),("⬅ Retour","mod:home")]))
    await c.answer()

@r.callback_query(F.data == "mod:stats")
async def moderation_stats(c: CallbackQuery):
    async with SessionLocal() as s:
        stats={x.key:x.value for x in (await s.scalars(select(ModerationStat))).all()}; hashes=int(await s.scalar(select(func.count(MediaHash.id))) or 0)
    await edit_or_send(c.message,f"<b>📊 Statistiques modération</b>\n\nMots bloqués : <b>{stats.get('forbidden_words_blocked',0)}</b>\nLiens bloqués : <b>{stats.get('links_blocked',0)}</b>\nDoublons détectés : <b>{stats.get('reposts_detected',0)}</b>\nHash médias : <b>{hashes}</b>",reply_markup=kb([("🔄 Actualiser","mod:stats"),("⬅ Retour","mod:home"),("🏠 Menu","menu")]))
    await c.answer()

@r.message(F.chat.type == "private", F.text)
async def broadcast_text(message: Message):
    mode = ADMIN_INPUT_MODE.get(message.from_user.id)
    if mode and await is_admin(message.from_user.id):
        if message.text.strip().lower() in {"/annuler", "annuler"}:
            ADMIN_INPUT_MODE.pop(message.from_user.id, None)
            await message.answer("Configuration annulée.", reply_markup=kb([("⚙️ Panneau administrateur", "admin:home")]))
            return
        if mode in {"welcome_text", "pub_text", "anti_repost_message"}:
            key = {"welcome_text": "welcome_text", "pub_text": "pub_ad_text", "anti_repost_message": "anti_repost_message"}[mode]
            async with SessionLocal() as s: await set_setting(s, key, message.text)
            ADMIN_INPUT_MODE.pop(message.from_user.id, None)
            await message.answer("✅ Texte enregistré.", reply_markup=kb([("⬅ Retour", "mod:repost" if mode == "anti_repost_message" else "admin:home"), ("🏠 Menu", "menu")]))
            return
        if mode in {"forbidden_add", "forbidden_remove", "domain_add", "domain_remove", "user_add", "user_remove"}:
            value = message.text.strip()
            async with SessionLocal() as s:
                if mode == "forbidden_add":
                    row = await s.scalar(select(ForbiddenWord).where(func.lower(ForbiddenWord.word) == value.lower()))
                    if row: row.active = True
                    else: s.add(ForbiddenWord(word=value, active=True))
                elif mode == "forbidden_remove":
                    row = await s.scalar(select(ForbiddenWord).where(func.lower(ForbiddenWord.word) == value.lower()))
                    if row: await s.delete(row)
                elif mode == "domain_add":
                    domain = value.lower().replace("https://", "").replace("http://", "").split("/")[0].lstrip("www.")
                    if not await s.scalar(select(LinkWhitelistDomain).where(LinkWhitelistDomain.domain == domain)): s.add(LinkWhitelistDomain(domain=domain))
                elif mode == "domain_remove":
                    domain = value.lower().replace("https://", "").replace("http://", "").split("/")[0].lstrip("www.")
                    row = await s.scalar(select(LinkWhitelistDomain).where(LinkWhitelistDomain.domain == domain))
                    if row: await s.delete(row)
                else:
                    try: uid = int(value)
                    except ValueError:
                        await message.answer("ID Telegram invalide."); return
                    row = await s.scalar(select(LinkWhitelistUser).where(LinkWhitelistUser.telegram_id == uid))
                    if mode == "user_add" and not row: s.add(LinkWhitelistUser(telegram_id=uid))
                    elif mode == "user_remove" and row: await s.delete(row)
                await s.commit()
            ADMIN_INPUT_MODE.pop(message.from_user.id, None)
            await message.answer("✅ Configuration enregistrée.", reply_markup=kb([("⬅ Retour", "mod:words" if mode.startswith("forbidden") else "mod:links"), ("🏠 Menu", "menu")]))
            return
    if message.from_user.id not in BROADCAST_WAITING: return
    if message.text.strip().lower() in {"/annuler","annuler"}:
        BROADCAST_WAITING.discard(message.from_user.id); return await message.answer("Broadcast annulé.")
    if not await is_admin(message.from_user.id):
        BROADCAST_WAITING.discard(message.from_user.id); return
    BROADCAST_WAITING.discard(message.from_user.id)
    async with SessionLocal() as s:
        ids=list((await s.scalars(select(User.telegram_id).where(User.started_bot.is_(True), User.is_banned.is_(False)))).all())
    sent=failed=0
    await message.answer(f"Envoi en cours vers {len(ids)} utilisateur(s)…")
    for uid in ids:
        try:
            await bot.send_message(uid, "<b>📢 Annonce des administrateurs</b>\n\n"+message.text)
            sent+=1
        except Exception: failed+=1
        await asyncio.sleep(0.04)
    await message.answer(f"Broadcast terminé.\n\n✅ Envoyés : {sent}\n❌ Échecs : {failed}", reply_markup=kb([("⚙️ Panneau admin","admin:home")]))

@r.callback_query(F.data == "admin:stats")
async def admin_stats(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    async with SessionLocal() as s:
        users=int(await s.scalar(select(func.count(User.id))) or 0)
        active=int(await s.scalar(select(func.count(Membership.id)).where(Membership.active.is_(True))) or 0)
        pending_pay=int(await s.scalar(select(func.count(AccessRequest.id)).where(AccessRequest.method==AccessMethod.payment.value,AccessRequest.status==AccessStatus.pending_review.value)) or 0)
        pending_media=int(await s.scalar(select(func.count(AccessRequest.id)).where(AccessRequest.method==AccessMethod.media.value,AccessRequest.status==AccessStatus.pending_review.value)) or 0)
        approved=int(await s.scalar(select(func.count(AccessRequest.id)).where(AccessRequest.status.in_([AccessStatus.approved.value,AccessStatus.member.value]))) or 0)
    await edit_message(c.message, f"<b>📊 Statistiques</b>\n\nUtilisateurs enregistrés : <b>{users}</b>\nMembres VIP actifs : <b>{active}</b>\nAccès validés : <b>{approved}</b>\nPaiements à vérifier : <b>{pending_pay}</b>\nDossiers à vérifier : <b>{pending_media}</b>", reply_markup=kb([("🔄 Actualiser","admin:stats"),("⬅️ Retour","admin:home")]))
    await c.answer()


@r.error()
async def global_error_handler(event: ErrorEvent):
    """Transforme les erreurs inattendues en réponse utilisateur au lieu d'un webhook 500."""
    exc = event.exception
    update = event.update
    try:
        if update.callback_query:
            callback = update.callback_query
            with suppress(Exception):
                admin_user = await is_admin(callback.from_user.id)
                await callback.answer(
                    "Une erreur est survenue. Ouvrez Santé du système ou réessayez."
                    if admin_user else
                    "Une erreur temporaire est survenue. Veuillez réessayer.",
                    show_alert=True,
                )
            if callback.message:
                with suppress(Exception):
                    if await is_admin(callback.from_user.id):
                        await callback.message.answer(
                            "⚠️ <b>Le bot a rencontré une erreur</b>\n\n"
                            "L’action n’a pas été appliquée. Vous pouvez relancer le diagnostic depuis le panneau administrateur.",
                            reply_markup=kb([("🩺 Santé du système", "admin:health"), ("🏠 Panneau admin", "admin:home")]),
                        )
                    else:
                        await callback.message.answer(
                            "⚠️ <b>Une erreur temporaire est survenue</b>\n\n"
                            "L’action n’a pas été appliquée. Veuillez réessayer dans quelques instants."
                        )
        elif update.message:
            with suppress(Exception):
                await update.message.answer(
                    "⚠️ Une erreur temporaire est survenue. Votre demande n’a pas été perdue. Réessayez dans quelques instants."
                )
    finally:
        logger.exception("Erreur callback/Telegram non gérée: %s", exc, exc_info=exc)
    return True
