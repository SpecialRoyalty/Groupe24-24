import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request
from aiogram.types import Update
from .bot import bot, dp, maintenance_loop
from .config import get_settings
from .db import engine, SessionLocal
from .models import Base
from sqlalchemy import text

settings=get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret, allowed_updates=dp.resolve_used_update_types())
    task=asyncio.create_task(maintenance_loop())
    yield
    task.cancel()
    await bot.delete_webhook()
    await bot.session.close()

app=FastAPI(title="Telegram VIP Bot",lifespan=lifespan)

@app.get("/health")
async def health():
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"status": "error", "database": type(exc).__name__})

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str|None=Header(default=None)):
    if x_telegram_bot_api_secret_token!=settings.webhook_secret: raise HTTPException(status_code=403,detail="invalid secret")
    update=Update.model_validate(await request.json(),context={"bot":bot})
    await dp.feed_update(bot,update)
    return {"ok":True}
