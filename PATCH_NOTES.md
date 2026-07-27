# Correctif retour / Lifetime / rappels

Cette version utilise exactement les variables Railway suivantes :

- `REENTRY_PRICE_EUR=5`
- `LIFETIME_REENTRY_PRICE_EUR=10`
- `FIRST_MEDIA_REMINDER_HOURS=12`
- `FIRST_MEDIA_FINAL_REMINDER_MINUTES=60`

Au démarrage, deux colonnes manquantes sont ajoutées de façon idempotente à PostgreSQL, puis les anciens statuts sont comparés au statut réel Telegram. Les personnes sorties du VIP sont contactées une seule fois et un bilan est envoyé aux `ADMIN_IDS`.


## Correctif compatibilité base existante
- Suppression de la dépendance à la colonne `memberships.warned_first_final`.
- Le dernier rappel est désormais mémorisé via la table `settings` déjà existante.
- Aucune modification de la table `memberships` n’est effectuée au démarrage.
