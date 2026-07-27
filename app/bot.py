from __future__ import annotations
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, func, text
from .config import get_settings
from .db import SessionLocal
from .keyboards import access_methods, admin_home, kb, payment_keyboard, rules_keyboard
from .models import AccessMethod, AccessRequest, AccessStatus, ActivityMedia, Invite, MediaSubmission, Membership, PaymentProof, Referral, Setting, TelegramChat, User
from .services import active_request, activity_count, create_personal_invite, create_request, get_or_create_user, get_setting, pub_chat, set_group_open, set_setting, validated_referrals, vip_chat

settings = get_settings()
bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
r = Router(); dp.include_router(r)

LAST_MAINTENANCE_AT: datetime | None = None
LAST_MAINTENANCE_ERROR: str | None = None
LAST_HEALTH_SIGNATURE: str | None = None
ADMIN_INPUT_MODE: dict[int, str] = {}

DEFAULT_WELCOME_TEXT = "Bienvenue sur le service d’accès au groupe privé ouvert 24 h/24.\n\nVeuillez d’abord consulter les règles."
DEFAULT_PUB_AD_TEXT = "Découvrez notre groupe privé. Utilisez le bouton ci-dessous pour commencer votre demande d’accès."

RULES = """<b>Règles du groupe VIP</b>\n\n• Premier média dans les 24 heures.\n• Ensuite, au moins 5 photos ou vidéos valides toutes les 72 heures.\n• Les liens externes sont interdits.\n• Les transferts et redistributions sont interdits.\n• Les infractions entraînent 1 jour, puis 3 jours de restriction, puis un bannissement.\n• Les contenus peuvent être archivés pour restaurer un groupe de remplacement.\n\nEn cliquant sur « J’adhère », vous acceptez ces règles."""

ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


async def safe_edit(message: Message, text: str, reply_markup=None) -> Message | None:
    """Modifie un message texte ou sa légende, sinon envoie un nouveau message.

    Telegram refuse edit_text() sur un message photo/vidéo sans texte. Cette fonction
    centralise le choix et évite qu'un bouton casse le traitement de la mise à jour.
    """
    try:
        if message.text is not None:
            return await message.edit_text(text, reply_markup=reply_markup)
        if message.caption is not None or message.photo or message.video or message.document or message.animation:
            return await message.edit_caption(caption=text, reply_markup=reply_markup)
        return await message.answer(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        error = str(exc).lower()
        if "message is not modified" in error:
            return None
        if "there is no text in the message to edit" in error or "message can't be edited" in error or "message caption is not modified" in error:
            return await message.answer(text, reply_markup=reply_markup)
        raise

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

async def trusted_admin_ids() -> set[int]:
    """IDs explicitement autorisés à recevoir et traiter les données sensibles."""
    return set(settings.admin_id_set)

async def is_trusted_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_set

async def is_admin(user_id: int, chat_id: int | None = None) -> bool:
    if user_id in settings.admin_id_set:
        return True
    if chat_id is not None:
        return user_id in await admin_ids_for_chat(chat_id)
    return user_id in await detected_admin_ids()

async def notify_admins(method: str, *args, **kwargs):
    """Envoie aux admins détectés ayant déjà démarré le bot; ignore les DM impossibles."""
    for admin_id in await trusted_admin_ids():
        try:
            await getattr(bot, method)(admin_id, *args, **kwargs)
        except Exception:
            pass


async def build_health_report() -> tuple[str, list[str], str]:
    """Vérifie la base, Telegram, le webhook et les groupes obligatoires.

    Les groupes PUB définitivement inaccessibles sont désactivés automatiquement.
    Une panne d'un seul groupe PUB ne doit jamais rendre le bot entier CRITIQUE.
    """
    checks: list[str] = []
    alerts: list[str] = []
    critical_alerts: list[str] = []

    # Base de données
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
            chats = list((await s.scalars(select(TelegramChat).where(TelegramChat.active.is_(True)))).all())
        checks.append("✅ Base PostgreSQL accessible")
    except Exception as exc:
        chats = []
        message = f"Base de données : {type(exc).__name__}"
        checks.append("❌ Base PostgreSQL inaccessible")
        alerts.append(message)
        critical_alerts.append(message)

    # Identité du bot et webhook
    bot_id: int | None = None
    try:
        me = await bot.get_me()
        bot_id = me.id
        checks.append(f"✅ Bot Telegram connecté : @{me.username or me.id}")
    except Exception as exc:
        message = f"Connexion Telegram : {type(exc).__name__}"
        checks.append("❌ Connexion Telegram impossible")
        alerts.append(message)
        critical_alerts.append(message)

    try:
        webhook = await bot.get_webhook_info()
        if webhook.url == settings.webhook_url:
            checks.append("✅ Webhook actif et correctement configuré")
            if webhook.last_error_message:
                checks.append("ℹ️ Telegram conserve une ancienne erreur de livraison (information seulement)")
        else:
            message = "URL du webhook différente de PUBLIC_BASE_URL"
            checks.append("❌ URL du webhook incorrecte")
            alerts.append(message)
            critical_alerts.append(message)
        if webhook.pending_update_count:
            checks.append(f"⚠️ {webhook.pending_update_count} mise(s) à jour Telegram en attente")
    except Exception as exc:
        message = f"Webhook : {type(exc).__name__}"
        checks.append("❌ Impossible de lire l’état du webhook")
        alerts.append(message)
        critical_alerts.append(message)

    vip = [c for c in chats if c.role == "vip"]
    pubs = [c for c in chats if c.role == "pub"]
    if vip:
        checks.append(f"✅ Groupe VIP configuré : {vip[0].title or vip[0].telegram_chat_id}")
    else:
        message = "Aucun groupe VIP actif"
        checks.append("❌ Aucun groupe VIP actif")
        alerts.append(message)
        critical_alerts.append(message)

    if pubs:
        checks.append(f"✅ Groupe(s) PUB actif(s) : {len(pubs)}")
    else:
        checks.append("❌ Aucun groupe PUB actif")
        alerts.append("Aucun groupe PUB actif")

    # Présence et permissions du bot dans chaque groupe essentiel.
    # Un ancien groupe PUB supprimé/retiré est désactivé au lieu de bloquer le bot.
    stale_pub_ids: list[int] = []
    if bot_id:
        for chat in vip + pubs:
            label = f"{chat.role.upper()} — {chat.title or chat.telegram_chat_id}"
            try:
                member = await bot.get_chat_member(chat.telegram_chat_id, bot_id)
                if member.status not in ADMIN_STATUSES:
                    checks.append(f"❌ {label} : bot non administrateur")
                    message = f"{label} : droits administrateur manquants"
                    alerts.append(message)
                    if chat.role == "vip":
                        critical_alerts.append(message)
                    continue
                missing: list[str] = []
                if chat.role == "vip":
                    for attr, title in (("can_delete_messages", "supprimer"), ("can_restrict_members", "restreindre/bannir"), ("can_invite_users", "inviter")):
                        if not getattr(member, attr, False):
                            missing.append(title)
                elif not getattr(member, "can_invite_users", False):
                    missing.append("inviter")
                if missing:
                    checks.append(f"⚠️ {label} : droits manquants ({', '.join(missing)})")
                    message = f"{label} : droits manquants ({', '.join(missing)})"
                    alerts.append(message)
                    if chat.role == "vip":
                        critical_alerts.append(message)
                else:
                    checks.append(f"✅ {label} : bot administrateur et droits essentiels OK")
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                if chat.role == "pub":
                    stale_pub_ids.append(chat.id)
                    checks.append(f"ℹ️ {label} : ancien groupe inaccessible, désactivé automatiquement")
                else:
                    message = f"{label} inaccessible : {type(exc).__name__}"
                    checks.append(f"❌ {label} : groupe inaccessible")
                    alerts.append(message)
                    critical_alerts.append(message)
            except Exception as exc:
                message = f"{label} inaccessible : {type(exc).__name__}"
                checks.append(f"⚠️ {label} : vérification impossible")
                alerts.append(message)
                if chat.role == "vip":
                    critical_alerts.append(message)

    if stale_pub_ids:
        try:
            async with SessionLocal() as s:
                stale_rows = list((await s.scalars(select(TelegramChat).where(TelegramChat.id.in_(stale_pub_ids)))).all())
                for row in stale_rows:
                    row.active = False
                await s.commit()
        except Exception as exc:
            alerts.append(f"Nettoyage des groupes PUB obsolètes : {type(exc).__name__}")

    if LAST_MAINTENANCE_AT:
        age = datetime.now(timezone.utc) - LAST_MAINTENANCE_AT
        if age <= timedelta(minutes=3):
            checks.append("✅ Tâche automatique active")
        else:
            message = "La boucle de maintenance ne répond plus normalement"
            checks.append("❌ Tâche automatique en retard")
            alerts.append(message)
            critical_alerts.append(message)
    else:
        checks.append("⚠️ Tâche automatique pas encore confirmée")
    if LAST_MAINTENANCE_ERROR:
        checks.append("⚠️ Une erreur récente de maintenance est enregistrée")
        alerts.append(f"Maintenance : {LAST_MAINTENANCE_ERROR[:180]}")

    status = "CRITIQUE" if critical_alerts else ("ATTENTION" if alerts else "OK")
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

def reentry_rows(user: User) -> list[tuple[str, str]]:
    if user.has_lifetime_reentry:
        return [("♾ Réactiver mon accès Lifetime", "reentry:lifetime_free")]
    return [
        (f"🔄 Retour — {settings.reentry_price_eur} €", "reentry:standard"),
        (f"♾ Lifetime — {settings.lifetime_reentry_price_eur} €", "reentry:lifetime"),
    ]


async def startup_membership_audit() -> None:
    """Répare les statuts incohérents et envoie un bilan Lifetime détaillé aux admins.

    La clé de réparation est versionnée afin de retenter les comptes qui auraient
    été ignorés par une ancienne version du correctif.
    """
    checked = corrected = eligible = contacted = failed = already = 0
    lifetime_checked = lifetime_absent = lifetime_repaired = lifetime_contacted = 0
    details: list[str] = []

    async with SessionLocal() as s:
        vip = await vip_chat(s)
        if not vip:
            for admin_id in settings.admin_id_set:
                try:
                    await bot.send_message(
                        admin_id,
                        "⚠️ <b>Bilan Lifetime impossible</b>\n\nAucun groupe VIP actif n'est configuré.",
                    )
                except Exception:
                    pass
            return

        memberships = list((await s.scalars(
            select(Membership).where(Membership.chat_id == vip.id)
        )).all())

        for membership in memberships:
            checked += 1
            user = await s.get(User, membership.user_id)
            if not user or (user.is_banned and not user.has_lifetime_reentry):
                continue

            telegram_status = "inconnu"
            telegram_active = False
            try:
                actual = await bot.get_chat_member(vip.telegram_chat_id, user.telegram_id)
                telegram_status = str(actual.status)
                telegram_active = actual.status in {
                    ChatMemberStatus.MEMBER,
                    ChatMemberStatus.RESTRICTED,
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.CREATOR,
                }
            except Exception as exc:
                telegram_status = f"erreur:{type(exc).__name__}"

            if membership.active and not telegram_active and not telegram_status.startswith("erreur:"):
                membership.active = False
                corrected += 1

            if user.has_lifetime_reentry:
                lifetime_checked += 1
                display_name = " ".join(x for x in (user.first_name, user.last_name) if x).strip() or "Sans nom"
                username = f"@{user.username}" if user.username else "sans username"

                if telegram_active:
                    details.append(
                        f"✅ {display_name} — {username} — <code>{user.telegram_id}</code> : déjà présent ({telegram_status})"
                    )
                    continue

                lifetime_absent += 1
                eligible += 1
                repair_key = f"lifetime_ban_repair_v2:{user.id}:{vip.id}"
                was_done = await get_setting(s, repair_key, "0") == "1"
                if was_done:
                    already += 1
                    details.append(
                        f"ℹ️ {display_name} — {username} — <code>{user.telegram_id}</code> : correctif déjà exécuté, statut Telegram {telegram_status}"
                    )
                    continue

                unban_result = "non nécessaire"
                invite_result = "non généré"
                message_result = "non envoyé"
                try:
                    try:
                        await bot.unban_chat_member(
                            vip.telegram_chat_id,
                            user.telegram_id,
                            only_if_banned=True,
                        )
                        unban_result = "réussi"
                    except Exception as exc:
                        # Un compte simplement sorti n'est pas forcément banni.
                        unban_result = f"{type(exc).__name__}"

                    user.is_banned = False
                    membership.active = False
                    await s.commit()

                    req = AccessRequest(
                        user_id=user.id,
                        method=AccessMethod.payment.value,
                        status=AccessStatus.approved.value,
                        reference=f"LFR2-{user.telegram_id}-{int(datetime.now(timezone.utc).timestamp())}",
                    )
                    s.add(req)
                    await s.commit()
                    await s.refresh(req)
                    inv = await create_personal_invite(bot, s, user, req)
                    invite_result = "généré"
                    lifetime_repaired += 1

                    await bot.send_message(
                        user.telegram_id,
                        "♾ <b>Accès Lifetime réactivé</b>\n\n"
                        "Votre accès Lifetime avait été retiré automatiquement par erreur. "
                        "Le correctif est maintenant appliqué : vous recevrez toujours les rappels, "
                        "mais vous ne serez plus exclu automatiquement pour inactivité.\n\n"
                        f"Votre nouveau lien personnel est valable {settings.invite_ttl_hours} heures :\n{inv.invite_link}",
                    )
                    message_result = "envoyé"
                    lifetime_contacted += 1
                    contacted += 1
                    await set_setting(s, repair_key, "1")

                    details.append(
                        f"✅ {display_name} — {username} — <code>{user.telegram_id}</code> : "
                        f"statut {telegram_status}, déban {unban_result}, lien {invite_result}, message {message_result}"
                    )
                except Exception as exc:
                    failed += 1
                    details.append(
                        f"❌ {display_name} — {username} — <code>{user.telegram_id}</code> : "
                        f"statut {telegram_status}, déban {unban_result}, lien {invite_result}, "
                        f"message {message_result}, erreur {type(exc).__name__}: {str(exc)[:180]}"
                    )
                    print("startup lifetime repair error", user.telegram_id, repr(exc))
                continue

            if not membership.active:
                eligible += 1
                contact_key = f"reentry_contacted:{membership.id}"
                if await get_setting(s, contact_key, "0") == "1":
                    already += 1
                    continue
                try:
                    text_msg = (
                        "🔄 <b>Votre retour est disponible</b>\n\n"
                        "Votre statut a été vérifié après la mise à jour du bot. "
                        f"Vous pouvez revenir pour <b>{settings.reentry_price_eur} €</b> "
                        f"ou choisir l’accès <b>Lifetime à {settings.lifetime_reentry_price_eur} €</b>."
                    )
                    await bot.send_message(
                        user.telegram_id,
                        text_msg,
                        reply_markup=kb(reentry_rows(user) + [("📜 Consulter les règles", "rules:show")]),
                    )
                    await set_setting(s, contact_key, "1")
                    contacted += 1
                except Exception as exc:
                    print("startup reentry contact error", user.telegram_id, repr(exc))
                    failed += 1

        await s.commit()

    summary = (
        "🧰 <b>Bilan de réparation au démarrage</b>\n\n"
        f"Membres vérifiés : <b>{checked}</b>\n"
        f"Statuts locaux corrigés : <b>{corrected}</b>\n"
        f"Comptes Lifetime vérifiés : <b>{lifetime_checked}</b>\n"
        f"Lifetime absents/bannis détectés : <b>{lifetime_absent}</b>\n"
        f"Lifetime réparés avec lien : <b>{lifetime_repaired}</b>\n"
        f"Messages Lifetime envoyés : <b>{lifetime_contacted}</b>\n"
        f"Déjà traités par ce correctif : <b>{already}</b>\n"
        f"Échecs : <b>{failed}</b>"
    )

    # Telegram limite un message à 4096 caractères. Le résumé est envoyé en
    # premier, puis la liste détaillée en plusieurs blocs.
    detail_chunks: list[str] = []
    if details:
        current = "👥 <b>Détail des comptes Lifetime</b>\n\n"
        for line in details:
            addition = line + "\n"
            if len(current) + len(addition) > 3900:
                detail_chunks.append(current.rstrip())
                current = "👥 <b>Suite du bilan Lifetime</b>\n\n" + addition
            else:
                current += addition
        if current.strip():
            detail_chunks.append(current.rstrip())

    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, summary)
            for chunk in detail_chunks:
                await bot.send_message(admin_id, chunk)
        except Exception as exc:
            print("admin lifetime report error", admin_id, repr(exc))


async def reconcile_vip_membership(session, user: User) -> Membership | None:
    """Réconcilie l’état local avec le statut réel Telegram.

    Compatibilité avec les anciennes versions : certains membres expulsés sont
    restés marqués active=true dans PostgreSQL. Telegram est la source de vérité.
    """
    vip = await vip_chat(session)
    if not vip:
        return None
    membership = await session.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.chat_id == vip.id,
        )
    )
    if not membership:
        return None
    try:
        actual = await bot.get_chat_member(vip.telegram_chat_id, user.telegram_id)
        telegram_active = actual.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.RESTRICTED,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
    except Exception:
        # Ne pas modifier la base si Telegram est temporairement inaccessible.
        return membership
    if membership.active and not telegram_active:
        membership.active = False
        await session.commit()
    return membership


async def user_access_state(session, user: User) -> tuple[str, Membership | None]:
    """Retourne new, active ou reentry après vérification du statut Telegram réel.

    Cette vérification est appelée à chaque callback sensible afin que les anciens
    boutons Telegram ne puissent jamais contourner le tarif de réintégration.
    """
    membership = await reconcile_vip_membership(session, user)
    if membership is None:
        return "new", None
    if membership.active:
        return "active", membership
    return "reentry", membership


async def show_reentry_required(c: CallbackQuery, user: User) -> None:
    await safe_edit(c.message, 
        "<b>🔄 Retour au groupe VIP</b>\n\n"
        "Votre ancien accès a été retiré. Le tarif d’entrée initial ne peut plus être utilisé, "
        "même depuis un ancien bouton Telegram.\n\n"
        f"• Retour simple : <b>{settings.reentry_price_eur} €</b>\n"
        f"• Lifetime : <b>{settings.lifetime_reentry_price_eur} €</b>",
        reply_markup=kb(reentry_rows(user) + [("📜 Consulter les règles", "rules:show")]),
    )


@r.message(CommandStart())
async def start(message: Message):
    if message.chat.type != "private": return
    async with SessionLocal() as s:
        user = await get_or_create_user(s, message.from_user)
        await reconcile_vip_membership(s, user)
        previous_membership = await s.scalar(
            select(Membership)
            .join(TelegramChat, Membership.chat_id == TelegramChat.id)
            .where(
                Membership.user_id == user.id,
                TelegramChat.role == "vip",
                Membership.active.is_(False),
            )
            .order_by(Membership.id.desc())
        )
    if previous_membership:
        rows = reentry_rows(user) + [("📜 Consulter les règles", "rules:show")]
    else:
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

@r.callback_query(F.data == "reentry:start")
async def reentry_start(c: CallbackQuery):
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
    await safe_edit(c.message, 
        "<b>Choisissez votre formule de retour</b>\n\n"
        f"• Retour simple : <b>{settings.reentry_price_eur} €</b>\n"
        f"• Lifetime : <b>{settings.lifetime_reentry_price_eur} €</b>",
        reply_markup=kb(reentry_rows(user) + [("⬅️ Retour", "menu")]),
    )
    await c.answer()


async def create_reentry_payment(c: CallbackQuery, lifetime: bool) -> None:
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        previous = await s.scalar(
            select(Membership).join(TelegramChat, Membership.chat_id == TelegramChat.id).where(
                Membership.user_id == user.id, TelegramChat.role == "vip", Membership.active.is_(False)
            ).order_by(Membership.id.desc())
        )
        if not previous:
            await c.answer("Aucune exclusion donnant droit à un retour n’a été trouvée.", show_alert=True)
            return
        req = await create_request(s, user.id, AccessMethod.payment.value)
        prefix = "LFT" if lifetime else "RET"
        req.reference = f"{prefix}-{req.reference.removeprefix('VIP-')}"
        await s.commit()
    price = settings.lifetime_reentry_price_eur if lifetime else settings.reentry_price_eur
    label = "Lifetime" if lifetime else "retour simple"
    await safe_edit(c.message, 
        f"<b>Demande de {label}</b>\n\nMontant : <b>{price} €</b>\n"
        f"Référence : <code>{req.reference}</code>\n\nChoisissez votre moyen de paiement.",
        reply_markup=payment_keyboard(),
    )
    await c.answer()


@r.callback_query(F.data == "reentry:standard")
async def reentry_standard(c: CallbackQuery):
    await create_reentry_payment(c, False)


@r.callback_query(F.data == "reentry:lifetime")
async def reentry_lifetime(c: CallbackQuery):
    await create_reentry_payment(c, True)


@r.callback_query(F.data == "reentry:lifetime_free")
async def reentry_lifetime_free(c: CallbackQuery):
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        if not user.has_lifetime_reentry:
            await c.answer("Aucun droit Lifetime actif.", show_alert=True); return
        previous = await s.scalar(select(Membership).join(TelegramChat, Membership.chat_id == TelegramChat.id).where(Membership.user_id == user.id, TelegramChat.role == "vip", Membership.active.is_(False)))
        if not previous:
            await c.answer("Vous n’êtes pas éligible à une réactivation.", show_alert=True); return
        req = await create_request(s, user.id, AccessMethod.payment.value)
        req.reference = f"LFR-{req.reference.removeprefix('VIP-')}"
        req.status = AccessStatus.approved.value
        await s.commit()
    await safe_edit(c.message, "♾ Votre droit Lifetime est reconnu.", reply_markup=kb([("🔗 Générer mon lien 24 h", f"invite:create:{req.id}")]))
    await c.answer()


@r.callback_query(F.data == "rules:show")
async def show_rules(c: CallbackQuery):
    await safe_edit(c.message, RULES, reply_markup=rules_keyboard()); await c.answer()

@r.callback_query(F.data == "rules:accept")
async def accept_rules(c: CallbackQuery):
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        state, _ = await user_access_state(s, user)
        enabled = (await get_setting(s, "alternative_access_enabled", "1")) == "1"
    if state == "active":
        await c.answer("Vous êtes déjà membre actif du groupe VIP.", show_alert=True)
        return
    if state == "reentry":
        await show_reentry_required(c, user)
        await c.answer()
        return
    text = "Choisissez votre méthode d’accès :" if enabled else "L’accès est actuellement disponible uniquement par paiement."
    await safe_edit(c.message, text, reply_markup=access_methods(enabled)); await c.answer()

@r.callback_query(F.data.startswith("access:"))
async def choose_access(c: CallbackQuery):
    method = c.data.split(":",1)[1]
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        state, _ = await user_access_state(s, user)
        enabled = (await get_setting(s, "alternative_access_enabled", "1")) == "1"
        if state == "active":
            await c.answer("Vous êtes déjà membre actif du groupe VIP.", show_alert=True)
            return
        if state == "reentry":
            await show_reentry_required(c, user)
            await c.answer("L’ancien tarif de 2 € n’est plus disponible après une exclusion.", show_alert=True)
            return
        if method != "payment" and not enabled:
            await c.answer("Cette option est désactivée.", show_alert=True); return
        req = await create_request(s, user.id, method)
    if method == "payment":
        await safe_edit(c.message, f"Le prix de l’accès est de <b>{settings.entry_price_eur} €</b>.\nRéférence : <code>{req.reference}</code>\n\nChoisissez le moyen de paiement.", reply_markup=payment_keyboard())
    elif method == "media":
        await safe_edit(c.message, "Envoyez entre 5 et 10 photos ou vidéos représentant la même personne, visage visible. Vous pouvez envoyer un album complet. Après validation, le dossier sera publié dans le groupe et comptera comme première participation.\n\nProgression : <b>0/5</b>", reply_markup=kb([("❌ Annuler", "menu")]))
    else:
        async with SessionLocal() as s:
            pub = await pub_chat(s)
        if not pub:
            await safe_edit(c.message, "Le groupe PUB n’est pas encore configuré. Contactez un administrateur."); return
        link = await bot.create_chat_invite_link(pub.telegram_chat_id, name=f"REF-{req.id}", expire_date=req.expires_at, member_limit=99999)
        await safe_edit(c.message, f"Votre lien personnel de parrainage :\n{link.invite_link}\n\nObjectif : <b>{settings.referral_target}</b> invitations validées en 48 heures.\nProgression : <b>0/{settings.referral_target}</b>", reply_markup=kb([("📊 Voir ma progression", f"ref:progress:{req.id}")]))
    await c.answer()

@r.callback_query(F.data.startswith("payment:"))
async def payment_choice(c: CallbackQuery):
    method = c.data.split(":",1)[1]
    if method not in {"paypal","revolut"}: return
    details = settings.paypal_details if method == "paypal" else settings.revolut_details
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        state, _ = await user_access_state(s, user)
        req = await active_request(s, user.id)
    if not req:
        await c.answer("Aucune demande de paiement active.", show_alert=True); return
    is_reentry_request = (req.reference or "").startswith(("RET-", "LFT-", "LFR-"))
    if state == "active":
        await c.answer("Vous êtes déjà membre actif du groupe VIP.", show_alert=True)
        return
    if state == "reentry" and not is_reentry_request:
        await show_reentry_required(c, user)
        await c.answer("Cet ancien paiement à 2 € n’est plus valable.", show_alert=True)
        return
    price = settings.lifetime_reentry_price_eur if (req.reference or "").startswith("LFT-") else (settings.reentry_price_eur if (req.reference or "").startswith("RET-") else settings.entry_price_eur)
    extra = ""
    if method == "paypal":
        extra = (
            "\n\n<b>Important PayPal :</b> utilisez le type de paiement conforme proposé par PayPal pour cette transaction. "
            "Ne classez pas volontairement un achat d’accès comme un envoi personnel afin de contourner les frais ou protections. "
            "Un paiement non conforme pourra être refusé et transmis aux administrateurs pour examen."
        )
    await safe_edit(c.message, f"Envoyez exactement <b>{price} €</b>.\nMoyen : <b>{method.title()}</b>\nDestinataire : <code>{details}</code>\nRéférence obligatoire : <code>{req.reference}</code>{extra}\n\nEnvoyez ensuite la capture d’écran ici.")
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
        user = await get_or_create_user(s, message.from_user)
        state, _ = await user_access_state(s, user)
        req = await active_request(s, user.id)
        if not req:
            return
        is_reentry_request = (req.reference or "").startswith(("RET-", "LFT-", "LFR-"))
        if state == "active":
            await message.answer("✅ Vous êtes déjà membre actif du groupe VIP. Aucun nouveau paiement n’est nécessaire.")
            return
        if state == "reentry" and not is_reentry_request:
            if req.status in {AccessStatus.in_progress.value, AccessStatus.pending_review.value}:
                req.status = AccessStatus.rejected.value
                await s.commit()
            await message.answer(
                "⚠️ Cet ancien paiement d’entrée à 2 € n’est plus valable après une exclusion.\n\n"
                f"Choisissez un retour à {settings.reentry_price_eur} € ou Lifetime à {settings.lifetime_reentry_price_eur} €.",
                reply_markup=kb(reentry_rows(user)),
            )
            return
        if req.method == AccessMethod.payment.value:
            proof = PaymentProof(request_id=req.id, file_id=message.photo[-1].file_id, payment_method="manual")
            req.status = AccessStatus.pending_review.value; s.add(proof); await s.commit()
            expected_price = settings.lifetime_reentry_price_eur if (req.reference or "").startswith("LFT-") else (settings.reentry_price_eur if (req.reference or "").startswith("RET-") else settings.entry_price_eur)
            cap = f"Paiement à vérifier\nUtilisateur : {message.from_user.full_name} (@{message.from_user.username or '-'})\nID : <code>{message.from_user.id}</code>\nRéférence : <code>{req.reference}</code>\nMontant attendu : <b>{expected_price} €</b>"
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Valider", callback_data=f"review:pay:ok:{req.id}"),InlineKeyboardButton(text="❌ Refuser", callback_data=f"review:pay:no:{req.id}")]])
            await notify_admins("send_photo", proof.file_id, caption=cap, reply_markup=markup)
            await message.answer("Votre justificatif a été reçu et envoyé aux administrateurs.")
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
    for aid in await trusted_admin_ids():
        try:
            await bot.send_message(aid, f"Dossier média #{req_id} — {len(files)} médias", reply_markup=kb([("✅ Accepter", f"review:media:ok:{req_id}"),("❌ Refuser", f"review:media:no:{req_id}")]))
            for f in files:
                if f.media_type=="photo": await bot.send_photo(aid,f.file_id)
                else: await bot.send_video(aid,f.file_id)
        except Exception:
            pass
    await safe_edit(c.message, "Votre dossier a été transmis aux modérateurs."); await c.answer()

@r.callback_query(F.data.startswith("review:"))
async def review(c: CallbackQuery):
    if not await is_trusted_admin(c.from_user.id):
        await c.answer("Validation réservée aux administrateurs autorisés", show_alert=True)
        return
    _,kind,decision,reqid = c.data.split(":"); req_id=int(reqid)
    async with SessionLocal() as s:
        req=await s.get(AccessRequest,req_id)
        if not req: return
        req.status=AccessStatus.approved.value if decision=="ok" else AccessStatus.rejected.value
        user=await s.get(User,req.user_id)
        if decision == "ok" and (req.reference or "").startswith("LFT-"):
            user.has_lifetime_reentry = True
        await s.commit()
        if decision=="ok":
            await bot.send_message(user.telegram_id,"Votre demande a été validée.",reply_markup=kb([("🔗 Générer mon lien 24 h",f"invite:create:{req.id}")]))
        else: await bot.send_message(user.telegram_id,"Votre demande a été refusée. Le paiement reste disponible depuis /start.")
    await c.message.edit_reply_markup(reply_markup=None); await c.answer("Décision enregistrée")

@r.callback_query(F.data.startswith("invite:create:"))
async def invite_create(c: CallbackQuery):
    req_id=int(c.data.rsplit(":",1)[1])
    async with SessionLocal() as s:
        user=await get_or_create_user(s,c.from_user); req=await s.get(AccessRequest,req_id)
        if not req or req.user_id!=user.id or req.status!=AccessStatus.approved.value: await c.answer("Accès non autorisé",show_alert=True); return
        old=await s.scalar(select(Invite).where(Invite.user_id==user.id,Invite.revoked.is_(False),Invite.used_at.is_(None),Invite.expires_at>datetime.now(timezone.utc)))
        inv=old or await create_personal_invite(bot,s,user,req)
    await safe_edit(c.message, f"Votre lien personnel est valable 24 heures :\n{inv.invite_link}\n\nNe le partagez pas."); await c.answer()

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
            membership.active = True
            membership.joined_at = datetime.now(timezone.utc)
            membership.first_media_at = None
            membership.warned_first_day = False
            membership.warned_activity = False
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
            membership.warned_first_day = True
            membership.warned_activity = False
        await s.commit()
    await bot.send_message(user.telegram_id,"Bienvenue dans le groupe VIP. Consultez /statut pour suivre votre activité.")


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
    await safe_edit(c.message, f"Groupe configuré comme {role.upper()}."); await c.answer()

async def render_admin_panel(target: Message, edit: bool = False):
    async with SessionLocal() as s:
        opt=(await get_setting(s,"alternative_access_enabled","1"))=="1"
        opened=(await get_setting(s,"group_open","1"))=="1"
    if edit:
        await safe_edit(target, "<b>Panneau administrateur</b>\n\nTous les réglages sont accessibles avec les boutons ci-dessous.", reply_markup=admin_home(opt,opened))
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
            await safe_edit(c.message, 
                "⚠️ <b>Action impossible</b>\n\n"
                "Aucun groupe VIP n’est encore configuré.\n\n"
                "Ajoutez le bot à votre groupe, donnez-lui les droits administrateur, "
                "puis ouvrez <b>Groupes détectés</b> et définissez ce groupe comme VIP.",
                reply_markup=kb([("👥 Groupes détectés", "admin:groups"), ("⬅️ Retour", "admin:home")]),
            )
            return
        except Exception as exc:
            await c.answer("Impossible de modifier le groupe. Consultez Santé du système.", show_alert=True)
            await safe_edit(c.message, 
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
    await safe_edit(c.message, "\n".join(lines) if chats else "Aucun groupe détecté.", reply_markup=kb(rows))
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
    await safe_edit(c.message, f"<b>{chat.title}</b>\nID : <code>{chat.telegram_chat_id}</code>\nRôle actuel : <b>{chat.role.upper()}</b>", reply_markup=markup)
    await c.answer()

@r.callback_query(F.data == "admin:health")
async def admin_health(c: CallbackQuery):
    if not await is_admin(c.from_user.id):
        await c.answer("Accès refusé", show_alert=True)
        return
    await c.answer("Vérification en cours…")
    report, _, _ = await build_health_report()
    await safe_edit(c.message, report, reply_markup=kb([("🔄 Relancer le diagnostic", "admin:health"), ("⬅️ Retour", "admin:home")]))

@r.message(Command("statut"))
async def status(message: Message):
    async with SessionLocal() as s:
        user=await get_or_create_user(s,message.from_user); vip=await vip_chat(s)
        if not vip: await message.answer("Aucun groupe VIP configuré."); return
        m=await s.scalar(select(Membership).where(Membership.user_id==user.id,Membership.chat_id==vip.id,Membership.active.is_(True)))
        if not m: await message.answer("Vous n’êtes pas membre actif."); return
        count=await activity_count(s,m.id)
    await message.answer(f"Médias comptabilisés sur 72 h : <b>{count}/{settings.activity_media_target}</b>")

@r.callback_query(F.data == "member:status")
async def member_status_button(c: CallbackQuery):
    async with SessionLocal() as s:
        user = await get_or_create_user(s, c.from_user)
        vip = await vip_chat(s)
        membership = None if not vip else await s.scalar(select(Membership).where(Membership.user_id == user.id, Membership.chat_id == vip.id, Membership.active.is_(True)))
        if not membership:
            await c.answer("Vous n’êtes pas membre actif.", show_alert=True); return
        count = await activity_count(s, membership.id)
        first = "reçu" if membership.first_media_at else "en attente"
    await c.message.answer(f"<b>Votre activité</b>\n\nPremier média : <b>{first}</b>\nMédias sur 72 h : <b>{count}/{settings.activity_media_target}</b>")
    await c.answer()

@r.callback_query(F.data == "reentry:info")
async def reentry_info(c: CallbackQuery):
    await c.answer(f"Le retour à {settings.reentry_price_eur} € devient disponible seulement après une exclusion.", show_alert=True)

@r.message(F.chat.type.in_({"group","supergroup"}))
async def group_messages(message: Message):
    if not message.from_user or message.from_user.is_bot: return
    async with SessionLocal() as s:
        chat=await s.scalar(select(TelegramChat).where(TelegramChat.telegram_chat_id==message.chat.id))
        if not chat or chat.role!="vip": return
        user=await get_or_create_user(s,message.from_user)
        m=await s.scalar(select(Membership).where(Membership.user_id==user.id,Membership.chat_id==chat.id,Membership.active.is_(True)))
        if not m: return
        if message.photo or message.video:
            if not m.first_media_at: m.first_media_at=datetime.now(timezone.utc)
            s.add(ActivityMedia(membership_id=m.id,message_id=message.message_id,media_type="photo" if message.photo else "video")); await s.commit()
        entities=(message.entities or [])+(message.caption_entities or [])
        has_link=any(e.type in {"url","text_link"} for e in entities)
        if has_link and not await is_admin(message.from_user.id):
            try: await message.delete(); await bot.ban_chat_member(message.chat.id,message.from_user.id); await bot.send_message(message.from_user.id,"Vous avez été banni pour envoi d’un lien interdit.")
            except Exception: pass

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
                        first_deadline = m.joined_at + timedelta(hours=settings.first_media_hours)
                        first_reminder_at = first_deadline - timedelta(hours=settings.first_media_reminder_hours)
                        final_reminder_at = first_deadline - timedelta(minutes=settings.first_media_final_reminder_minutes)
                        if not m.first_media_at and not m.warned_first_day and now >= first_reminder_at and now < first_deadline:
                            remaining = max(1, int((first_deadline - now).total_seconds() // 3600) + 1)
                            try:
                                await bot.send_message(
                                    user.telegram_id,
                                    (f"⚠️ <b>Rappel de participation</b>\n\nVous n’avez pas encore publié votre premier média valide dans le groupe VIP. "
                                     f"Il vous reste environ <b>{remaining} heure(s)</b> avant l’échéance. "
                                     + ("Votre accès Lifetime ne sera pas retiré automatiquement, mais votre participation reste attendue." if user.has_lifetime_reentry else "Sans média, votre accès sera retiré automatiquement.")),
                                    reply_markup=kb([("📊 Voir mon statut", "member:status")]),
                                )
                                m.warned_first_day = True
                            except Exception as exc:
                                print("first media reminder error", user.telegram_id, repr(exc))
                        # Le dernier rappel est mémorisé dans la table générique `settings`.
                        # Cela évite d'exiger une nouvelle colonne dans `memberships` et reste
                        # compatible avec les bases créées par les anciennes versions du bot.
                        if not m.first_media_at and now >= final_reminder_at and now < first_deadline:
                            reminder_key = f"first_final:{m.id}:{int(m.joined_at.timestamp())}"
                            final_already_sent = await s.get(Setting, reminder_key) is not None
                            if not final_already_sent:
                                minutes = max(1, int((first_deadline - now).total_seconds() // 60) + 1)
                                try:
                                    await bot.send_message(
                                        user.telegram_id,
                                        (f"🚨 <b>Dernier rappel</b>\n\nVous n’avez toujours pas publié votre premier média. "
                                         f"Il vous reste environ <b>{minutes} minute(s)</b> avant l’échéance. "
                                         + ("Votre protection Lifetime empêche l’exclusion automatique, mais les règles d’activité restent applicables." if user.has_lifetime_reentry else "Sans média, votre accès sera retiré automatiquement.")),
                                        reply_markup=kb([("📊 Voir mon statut", "member:status")]),
                                    )
                                    s.add(Setting(key=reminder_key, value="1"))
                                except Exception as exc:
                                    print("final first media reminder error", user.telegram_id, repr(exc))
                        if not m.first_media_at and now >= first_deadline:
                            if user.has_lifetime_reentry:
                                protection_key = f"lifetime_first_protected:{m.id}:{int(m.joined_at.timestamp())}"
                                if await s.get(Setting, protection_key) is None:
                                    try:
                                        await bot.send_message(
                                            user.telegram_id,
                                            "♾ <b>Protection Lifetime active</b>\n\n"
                                            "L’échéance du premier média est dépassée. Votre accès n’a pas été retiré grâce à votre formule Lifetime. "
                                            "Merci de publier votre média dès que possible afin de respecter les règles du groupe.",
                                            reply_markup=kb([("📊 Voir mon statut", "member:status")]),
                                        )
                                        s.add(Setting(key=protection_key, value="1"))
                                    except Exception as exc:
                                        print("lifetime first protection notification error", user.telegram_id, repr(exc))
                            else:
                                try:
                                    await bot.ban_chat_member(chat.telegram_chat_id,user.telegram_id)
                                    await bot.unban_chat_member(chat.telegram_chat_id,user.telegram_id,only_if_banned=True)
                                    await bot.send_message(
                                        user.telegram_id,
                                        "❌ <b>Accès retiré</b>\n\nVotre accès a été retiré parce qu’aucun premier média valide n’a été publié dans le délai prévu. "
                                        f"Vous pouvez demander à revenir avec une participation de <b>{settings.reentry_price_eur} €</b>.",
                                        reply_markup=kb(reentry_rows(user) + [("📜 Consulter les règles", "rules:show")]),
                                    )
                                except Exception as exc:
                                    print("first media exclusion notification error", user.telegram_id, repr(exc))
                                m.active=False
                        elif m.first_media_at:
                            activity_deadline = m.joined_at + timedelta(hours=settings.activity_window_hours)
                            activity_reminder_at = activity_deadline - timedelta(hours=min(12, max(1, settings.activity_window_hours // 4)))
                            count = await activity_count(s,m.id)
                            if count < settings.activity_media_target and not m.warned_activity and now >= activity_reminder_at and now < activity_deadline:
                                remaining = settings.activity_media_target - count
                                try:
                                    await bot.send_message(
                                        user.telegram_id,
                                        f"⚠️ <b>Activité insuffisante</b>\n\nVous avez publié <b>{count}/{settings.activity_media_target}</b> médias valides. "
                                        f"Il vous en reste <b>{remaining}</b> à publier avant l’échéance.",
                                        reply_markup=kb([("📊 Voir mon statut", "member:status")]),
                                    )
                                    m.warned_activity = True
                                except Exception as exc:
                                    print("activity reminder error", user.telegram_id, repr(exc))
                            if now >= activity_deadline and count < settings.activity_media_target:
                                if user.has_lifetime_reentry:
                                    protection_key = f"lifetime_activity_protected:{m.id}:{int(m.joined_at.timestamp())}"
                                    if await s.get(Setting, protection_key) is None:
                                        try:
                                            await bot.send_message(
                                                user.telegram_id,
                                                f"♾ <b>Protection Lifetime active</b>\n\nSeulement <b>{count}/{settings.activity_media_target}</b> médias ont été comptabilisés. "
                                                "Votre accès n’a pas été retiré automatiquement grâce à votre formule Lifetime. "
                                                "Merci de reprendre votre participation dès que possible.",
                                                reply_markup=kb([("📊 Voir mon statut", "member:status")]),
                                            )
                                            s.add(Setting(key=protection_key, value="1"))
                                        except Exception as exc:
                                            print("lifetime activity protection notification error", user.telegram_id, repr(exc))
                                else:
                                    try:
                                        await bot.ban_chat_member(chat.telegram_chat_id,user.telegram_id)
                                        await bot.unban_chat_member(chat.telegram_chat_id,user.telegram_id,only_if_banned=True)
                                        await bot.send_message(
                                            user.telegram_id,
                                            f"❌ <b>Accès retiré pour inactivité</b>\n\nSeulement <b>{count}/{settings.activity_media_target}</b> médias ont été comptabilisés. "
                                            f"Vous pouvez demander à revenir avec une participation de <b>{settings.reentry_price_eur} €</b>.",
                                            reply_markup=kb(reentry_rows(user) + [("📜 Consulter les règles", "rules:show")]),
                                        )
                                    except Exception as exc:
                                        print("activity exclusion notification error", user.telegram_id, repr(exc))
                                    m.active=False
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
    await safe_edit(c.message, 
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
    await safe_edit(c.message, "Envoyez maintenant le nouveau texte d’accueil en message privé. HTML simple accepté.\n\nEnvoyez /annuler pour quitter.")
    await c.answer()

@r.callback_query(F.data == "admin:welcome_photo")
async def admin_welcome_photo(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    ADMIN_INPUT_MODE[c.from_user.id] = "welcome_photo"
    await safe_edit(c.message, "Envoyez maintenant l’image d’accueil en tant que photo.\n\nEnvoyez /annuler pour quitter.")
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
    await safe_edit(c.message, 
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
    await safe_edit(c.message, "Envoyez maintenant le texte de la publicité PUB.\n\nEnvoyez /annuler pour quitter.")
    await c.answer()

@r.callback_query(F.data == "admin:pub_photo")
async def admin_pub_photo(c: CallbackQuery):
    if not await is_admin(c.from_user.id): return await c.answer("Accès refusé", show_alert=True)
    ADMIN_INPUT_MODE[c.from_user.id] = "pub_photo"
    await safe_edit(c.message, "Envoyez maintenant l’image de la publicité PUB en tant que photo.\n\nEnvoyez /annuler pour quitter.")
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
        state, _ = await user_access_state(s, user)
        enabled = (await get_setting(s, "alternative_access_enabled", "1")) == "1"
    if state == "active":
        await safe_edit(c.message, "✅ <b>Vous êtes membre actif du groupe VIP.</b>", reply_markup=kb([("📊 Voir mon activité", "member:status")]))
    elif state == "reentry":
        await show_reentry_required(c, user)
    else:
        await safe_edit(c.message, "Choisissez votre méthode d’accès :" if enabled else "L’accès au groupe est actuellement disponible uniquement par paiement.", reply_markup=access_methods(enabled))
    await c.answer()

@r.callback_query(F.data == "rules:quit")
async def quit_rules(c: CallbackQuery):
    await safe_edit(c.message, "Vous n’avez pas accepté le règlement. Aucun accès ne peut être créé.\n\nVous pouvez revenir avec /start.")
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
    if not await is_trusted_admin(c.from_user.id): return await c.answer("Accès réservé", show_alert=True)
    text_, markup = await pending_requests_text(AccessMethod.payment.value)
    await safe_edit(c.message, text_, reply_markup=markup); await c.answer()

@r.callback_query(F.data == "admin:media_reviews")
async def admin_media_queue(c: CallbackQuery):
    if not await is_trusted_admin(c.from_user.id): return await c.answer("Accès réservé", show_alert=True)
    text_, markup = await pending_requests_text(AccessMethod.media.value)
    await safe_edit(c.message, text_, reply_markup=markup); await c.answer()

@r.callback_query(F.data.startswith("admin:pending_pay:"))
async def pending_pay_detail(c: CallbackQuery):
    if not await is_trusted_admin(c.from_user.id): return await c.answer("Accès réservé", show_alert=True)
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
    if not await is_trusted_admin(c.from_user.id): return await c.answer("Accès réservé", show_alert=True)
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
    await safe_edit(c.message, "<b>Broadcast</b>\n\nEnvoyez maintenant en message privé le texte à transmettre à tous les utilisateurs ayant démarré le bot.\n\nEnvoyez /annuler pour quitter.", reply_markup=kb([("⬅️ Annuler", "admin:broadcast_cancel")]))
    await c.answer()

@r.callback_query(F.data == "admin:broadcast_cancel")
async def broadcast_cancel_button(c: CallbackQuery):
    BROADCAST_WAITING.discard(c.from_user.id); await render_admin_panel(c.message, edit=True); await c.answer("Annulé")

@r.message(F.chat.type == "private", F.text)
async def broadcast_text(message: Message):
    mode = ADMIN_INPUT_MODE.get(message.from_user.id)
    if mode and await is_admin(message.from_user.id):
        if message.text.strip().lower() in {"/annuler", "annuler"}:
            ADMIN_INPUT_MODE.pop(message.from_user.id, None)
            await message.answer("Configuration annulée.", reply_markup=kb([("⚙️ Panneau administrateur", "admin:home")]))
            return
        if mode in {"welcome_text", "pub_text"}:
            key = "welcome_text" if mode == "welcome_text" else "pub_ad_text"
            async with SessionLocal() as s: await set_setting(s, key, message.text)
            ADMIN_INPUT_MODE.pop(message.from_user.id, None)
            await message.answer("✅ Texte enregistré.", reply_markup=kb([("⚙️ Panneau administrateur", "admin:home")]))
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
    await safe_edit(c.message, f"<b>📊 Statistiques</b>\n\nUtilisateurs enregistrés : <b>{users}</b>\nMembres VIP actifs : <b>{active}</b>\nAccès validés : <b>{approved}</b>\nPaiements à vérifier : <b>{pending_pay}</b>\nDossiers à vérifier : <b>{pending_media}</b>", reply_markup=kb([("🔄 Actualiser","admin:stats"),("⬅️ Retour","admin:home")]))
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
                await callback.answer(
                    "Une erreur est survenue. Ouvrez Santé du système ou réessayez.",
                    show_alert=True,
                )
            if callback.message:
                with suppress(Exception):
                    await callback.message.answer(
                        "⚠️ <b>Le bot a rencontré une erreur</b>\n\n"
                        "L’action n’a pas été appliquée. Vous pouvez relancer le diagnostic depuis le panneau administrateur.",
                        reply_markup=kb([("🩺 Santé du système", "admin:health"), ("🏠 Panneau admin", "admin:home")]),
                    )
        elif update.message:
            with suppress(Exception):
                await update.message.answer(
                    "⚠️ Une erreur temporaire est survenue. Votre demande n’a pas été perdue. Réessayez dans quelques instants."
                )
    finally:
        print(f"Unhandled bot error: {type(exc).__name__}: {exc}")
    return True
