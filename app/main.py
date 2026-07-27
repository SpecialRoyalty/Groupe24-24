import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import text

from .bot import bot, dp, maintenance_loop, startup_membership_audit
from .config import get_settings
from .db import engine, SessionLocal
from .models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram-vip-bot")
settings = get_settings()

UPDATE_QUEUE: asyncio.Queue[Update] = asyncio.Queue(maxsize=1000)
UPDATE_WORKERS: list[asyncio.Task] = []
UPDATE_TIMEOUT_SECONDS = 60

STARTUP_STATE = {
    "database": "starting",
    "webhook": "starting",
    "last_error": None,
    "last_attempt_at": None,
}


async def initialise_dependencies() -> None:
    """Initialise PostgreSQL et Telegram sans bloquer le serveur HTTP.

    Railway peut ainsi joindre /health même pendant une panne temporaire de la
    base ou de Telegram. Les tentatives continuent automatiquement.
    """
    delay = 2
    while True:
        STARTUP_STATE["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        errors: list[str] = []

        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # Migrations légères et idempotentes pour les bases Railway existantes.
                await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_lifetime_reentry BOOLEAN NOT NULL DEFAULT FALSE"))
            STARTUP_STATE["database"] = "ok"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            STARTUP_STATE["database"] = "error"
            errors.append(f"database:{type(exc).__name__}:{exc}")
            logger.exception("Initialisation PostgreSQL impossible")

        if not settings.webhook_url:
            STARTUP_STATE["webhook"] = "error"
            errors.append("webhook:PUBLIC_BASE_URL ou RAILWAY_PUBLIC_DOMAIN manquant")
        else:
            try:
                await bot.set_webhook(
                    settings.webhook_url,
                    secret_token=settings.resolved_webhook_secret,
                    allowed_updates=dp.resolve_used_update_types(),
                )
                STARTUP_STATE["webhook"] = "ok"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                STARTUP_STATE["webhook"] = "error"
                errors.append(f"webhook:{type(exc).__name__}:{exc}")
                logger.exception("Enregistrement du webhook Telegram impossible")

        STARTUP_STATE["last_error"] = " | ".join(errors) if errors else None
        if not errors:
            logger.info("PostgreSQL et webhook Telegram initialisés")
            try:
                await startup_membership_audit()
            except Exception:
                logger.exception("Audit de réparation au démarrage impossible")
            return

        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def process_telegram_updates(worker_id: int) -> None:
    """Traite les mises à jour hors de la requête HTTP du webhook.

    Telegram reçoit immédiatement un HTTP 200, même si un handler doit appeler
    plusieurs fois l API Telegram ou PostgreSQL. Un handler bloqué est annulé
    après UPDATE_TIMEOUT_SECONDS sans empêcher les mises à jour suivantes.
    """
    while True:
        update = await UPDATE_QUEUE.get()
        try:
            await asyncio.wait_for(
                dp.feed_update(bot, update),
                timeout=UPDATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Timeout pendant le traitement de la mise à jour Telegram %s (worker %s)",
                update.update_id, worker_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Erreur pendant le traitement de la mise à jour Telegram %s (worker %s)",
                update.update_id, worker_id,
            )
        finally:
            UPDATE_QUEUE.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_task = asyncio.create_task(initialise_dependencies(), name="initialise-dependencies")
    maintenance_task = asyncio.create_task(maintenance_loop(), name="maintenance-loop")
    workers = [
        asyncio.create_task(process_telegram_updates(i + 1), name=f"telegram-worker-{i + 1}")
        for i in range(4)
    ]
    UPDATE_WORKERS[:] = workers
    yield
    for task in (init_task, maintenance_task, *workers):
        task.cancel()
    for task in (init_task, maintenance_task, *workers):
        with suppress(asyncio.CancelledError):
            await task
    with suppress(Exception):
        await bot.delete_webhook()
    await bot.session.close()
    await engine.dispose()


app = FastAPI(title="Telegram VIP Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    """Sonde de vie Railway : confirme que le serveur HTTP répond."""
    return {
        "status": "ok",
        "service": "telegram-vip-bot",
        "database": STARTUP_STATE["database"],
        "webhook": STARTUP_STATE["webhook"],
        "update_queue": UPDATE_QUEUE.qsize(),
        "update_workers": sum(1 for task in UPDATE_WORKERS if not task.done()),
    }


@app.get("/ready")
async def ready():
    """Sonde détaillée : 503 tant que PostgreSQL ou le webhook ne sont pas prêts."""
    db_status = "error"
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:
        STARTUP_STATE["last_error"] = f"database:{type(exc).__name__}:{exc}"

    payload = {
        "status": "ok" if db_status == "ok" and STARTUP_STATE["webhook"] == "ok" else "degraded",
        "database": db_status,
        "webhook": STARTUP_STATE["webhook"],
        "last_error": STARTUP_STATE["last_error"],
        "last_attempt_at": STARTUP_STATE["last_attempt_at"],
    }
    if payload["status"] != "ok":
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if x_telegram_bot_api_secret_token != settings.resolved_webhook_secret:
        raise HTTPException(status_code=403, detail="invalid secret")
    update = Update.model_validate(await request.json(), context={"bot": bot})
    try:
        UPDATE_QUEUE.put_nowait(update)
    except asyncio.QueueFull:
        # Ne jamais répondre 500 à Telegram. Le cas est journalisé et visible via /health.
        logger.error("File Telegram saturée; mise à jour %s ignorée", update.update_id)
        return {"ok": False, "queued": False, "reason": "queue_full"}
    return {"ok": True, "queued": True}
