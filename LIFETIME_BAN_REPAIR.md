# Correctif des titulaires Lifetime déjà expulsés

Au démarrage, le bot vérifie les adhésions VIP enregistrées.

Pour chaque utilisateur avec `has_lifetime_reentry=true` absent ou banni du groupe VIP :

- le bannissement Telegram est levé ;
- l'ancien indicateur `users.is_banned` est remis à `false` ;
- un nouveau droit d'accès approuvé est créé ;
- un nouveau lien personnel valable 24 heures est généré ;
- le lien est envoyé automatiquement à l'utilisateur en privé ;
- une clé spécifique empêche les doublons aux démarrages suivants ;
- un bilan est envoyé aux IDs configurés dans `ADMIN_IDS`.

Une ancienne notification de retour à 5 € ne peut plus empêcher cette réparation Lifetime.
