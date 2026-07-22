# Telegram VIP Bot — Railway

Projet Python prêt à déployer sur Railway pour gérer l’accès à un groupe Telegram VIP.

## Fonctions

- Détection automatique des groupes et de leurs administrateurs Telegram.
- Attribution VIP/PUB par boutons, réservée aux administrateurs réels.
- Interface administrateur en boutons : options d’accès, ouverture du groupe, paiements, dossiers, broadcast, statistiques, groupes et santé.
- Accès par paiement manuel PayPal/Revolut, dossier de 5 à 10 médias ou parrainage de 20 membres.
- Validation interne des filleuls après cinq minutes, sans afficher cette règle aux utilisateurs.
- Invitation VIP personnelle avec demande d’adhésion et expiration après 24 heures.
- Publication d’un dossier accepté dans le VIP et comptabilisation comme première participation.
- Premier média exigé sous 24 heures, puis cinq médias dans une fenêtre glissante de 72 heures.
- Exclusion automatique en cas d’inactivité et bannissement immédiat des liens détectés.
- Diagnostic PostgreSQL, Telegram, webhook, groupes VIP/PUB, permissions et tâche de maintenance.
- Alertes privées aux administrateurs en cas de panne ou de rétablissement.
- URL PostgreSQL Railway normale acceptée directement (`postgresql://`, `postgres://` ou `postgresql+asyncpg://`).

## Déploiement Railway

1. Créez le bot avec BotFather.
2. Désactivez le mode confidentialité avec `/setprivacy` si le bot doit contrôler les messages du VIP.
3. Placez le contenu de ce dossier dans un dépôt GitHub.
4. Créez un projet Railway et ajoutez PostgreSQL.
5. Ajoutez le dépôt GitHub comme service.
6. Générez un domaine public Railway.
7. Ajoutez les variables décrites dans `.env.example`.
8. Utilisez `DATABASE_URL=${{Postgres.DATABASE_URL}}` sans modifier son préfixe.
9. Renseignez `PUBLIC_BASE_URL` avec le domaine HTTPS, sans slash final.
10. Ajoutez le bot comme administrateur des groupes VIP et PUB.
11. Démarrez le bot en privé, puis ajoutez-le aux groupes et attribuez les rôles avec les boutons.

## Droits Telegram requis

Dans le VIP : supprimer des messages, bannir/restreindre, inviter des utilisateurs et modifier les permissions. Dans le PUB : inviter des utilisateurs et recevoir les événements de membres.

Les administrateurs doivent avoir démarré le bot en privé au moins une fois afin de recevoir les demandes de validation et les alertes.

## Paiement

Les paiements sont contrôlés manuellement à partir d’une capture. Le bot ne vérifie pas l’API PayPal ou Revolut et ne peut donc pas certifier automatiquement qu’un paiement est réel. Il demande d’utiliser le type de transaction conforme proposé par le prestataire.

## Limites Telegram

Telegram ne fournit pas les accusés de lecture des messages de groupe aux bots. Un bot ne peut pas garantir qu’un groupe ne sera jamais supprimé ni recréer automatiquement un supergroupe supprimé. Les albums envoyés au bot sont enregistrés média par média.

## Vérification locale

```bash
python -m compileall -q app
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

L’endpoint `GET /health` vérifie la connexion à la base. Le diagnostic complet est accessible avec le bouton **🩺 Santé du système**.

## Diagnostic Railway : healthcheck indisponible

La route `/health` est une sonde de vie et renvoie HTTP 200 dès que le serveur FastAPI écoute.
La route `/ready` vérifie séparément PostgreSQL et le webhook Telegram ; elle peut renvoyer 503 avec le détail de l'erreur sans provoquer l'arrêt du déploiement.

Variables minimales :

```env
BOT_TOKEN=123456:telegram-token
DATABASE_URL=${{Postgres.DATABASE_URL}}
PUBLIC_BASE_URL=https://votre-domaine.up.railway.app
WEBHOOK_SECRET=une_valeur_aleatoire_stable
```

`PUBLIC_BASE_URL` peut être omise si Railway injecte `RAILWAY_PUBLIC_DOMAIN`. Le bot ajoute alors automatiquement `https://`.
Après le déploiement, ouvrez `/ready` dans le navigateur pour voir si la base ou le webhook restent en erreur.

## Gestion des erreurs Railway / Telegram

Le webhook répond toujours en HTTP 200 après réception valide d'une mise à jour Telegram, même lorsqu'une action métier échoue. Cela évite les répétitions de mise à jour et les boucles `500 Internal Server Error`.

Si un administrateur tente d'ouvrir ou fermer le groupe avant d'avoir défini un groupe VIP, le bot affiche désormais une explication et un bouton vers **Groupes détectés**. Les erreurs inattendues affichent un message dans Telegram avec un accès direct à **Santé du système**.

## Contenus configurables

Le panneau administrateur permet maintenant de configurer :

- le texte et l'image du message d'accueil privé ;
- le texte et l'image de la publicité destinée aux groupes PUB ;
- la prévisualisation de chaque contenu ;
- l'envoi manuel de la publicité à tous les groupes PUB actifs.

Lorsqu'il est ajouté à un groupe, le bot n'envoie aucun message dans ce groupe. Il enregistre le groupe et adresse la demande de configuration uniquement en message privé aux identifiants présents dans `ADMIN_IDS`. Chaque administrateur doit avoir démarré le bot en privé au moins une fois pour recevoir ce message.
