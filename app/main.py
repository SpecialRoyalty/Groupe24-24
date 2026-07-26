import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import text

from .bot import bot, dp, maintenance_loop
from .config import get_settings
from .db import engine, SessionLocal
from .models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram-vip-bot")
settings = get_settings()

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
            return

        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_task = asyncio.create_task(initialise_dependencies(), name="initialise-dependencies")
    maintenance_task = asyncio.create_task(maintenance_loop(), name="maintenance-loop")
    yield
    for task in (init_task, maintenance_task):
        task.cancel()
    for task in (init_task, maintenance_task):
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
        await dp.feed_update(bot, update)
    except Exception as exc:
        # Telegram réessaie les mises à jour reçues avec un code 500, ce qui peut
        # provoquer une boucle. L'erreur est journalisée, mais le webhook reste sain.
        logger.exception(
            "Erreur inattendue pendant le traitement de la mise à jour Telegram %s",
            update.update_id,
        )
        return {
            "ok": False,
            "handled": True,
            "update_id": update.update_id,
            "error": type(exc).__name__,
        }
    return {"ok": True}
