from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)] for t, d in rows])


def access_methods(options_enabled: bool):
    rows = [("💳 Payer 2 €", "access:payment")]
    if options_enabled:
        rows += [("🗂 Envoyer un dossier média", "access:media"), ("👥 Inviter 20 personnes", "access:referral")]
    return kb(rows)


def rules_keyboard():
    return kb([("✅ J’adhère", "rules:accept"), ("❌ Quitter", "rules:quit")])


def payment_keyboard():
    return kb([("PayPal", "payment:paypal"), ("Revolut", "payment:revolut"), ("⬅️ Retour", "menu")])


def payment_details_keyboard():
    return kb([("⬅️ Retour aux moyens de paiement", "paymethods"), ("🏠 Menu principal", "menu")])


def admin_home(options_enabled: bool, group_open: bool):
    return kb([
        (f"🔀 Options d’accès : {'ON' if options_enabled else 'OFF'}", "admin:toggle_options"),
        (f"💬 Groupe ouvert : {'ON' if group_open else 'OFF'}", "admin:toggle_group"),
        ("💳 Paiements en attente", "admin:payments"),
        ("🗂 Dossiers en attente", "admin:media_reviews"),
        ("🖼 Configurer l’accueil", "admin:welcome"),
        ("📣 Configurer la publicité PUB", "admin:pub_ad"),
        ("🚀 Envoyer la publicité PUB", "admin:pub_send"),
        ("📢 Broadcast utilisateurs", "admin:broadcast"),
        ("🛡 Modération", "mod:home"),
        ("📊 Statistiques", "admin:stats"),
        ("👥 Groupes détectés", "admin:groups"),
        ("🩺 Santé du système", "admin:health"),
        ("🔄 Actualiser", "admin:home"),
    ])
