# Correctif des anciens boutons d’entrée

Chaque callback d’accès et de paiement revérifie maintenant l’état réel du membre dans Telegram et PostgreSQL.

- Un ancien membre exclu ne peut plus utiliser un ancien bouton « Payer 2 € ».
- Il est redirigé vers le retour à `REENTRY_PRICE_EUR` ou le Lifetime à `LIFETIME_REENTRY_PRICE_EUR`.
- Une ancienne capture liée à une demande d’entrée initiale est refusée côté serveur.
- Un membre encore actif ne peut pas créer une nouvelle demande de paiement.
- Le contrôle est effectué au clic, et pas uniquement lors de l’affichage du clavier.
