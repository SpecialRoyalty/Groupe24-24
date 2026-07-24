from datetime import datetime

# Dernière requête Telegram ayant atteint la vraie route webhook avec le secret valide.
# Le simple fait d'atteindre cette route prouve qu'un ancien 404 n'est plus actif.
LAST_WEBHOOK_RECEIVED_AT: datetime | None = None
