# Correctif anti-blocage Telegram

- Le webhook répond immédiatement HTTP 200 après avoir placé la mise à jour dans une file interne.
- Quatre workers traitent les mises à jour en parallèle.
- Une action bloquée est interrompue après 60 secondes et ne bloque pas les suivantes.
- `/health` expose `update_queue` et `update_workers`.
- La base de données n'est pas modifiée par ce correctif.
