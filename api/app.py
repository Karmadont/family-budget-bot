"""
api/app.py — веб-сервер мини-приложения.

Отдаёт две вещи: JSON по /api/* и статику приложения по /. Живёт в том же
процессе, что и бот, и пользуется тем же соединением с SQLite — см. run.py.

Наружу этот сервер не смотрит: по умолчанию слушает 127.0.0.1, а HTTPS и
сертификат обеспечивает Caddy перед ним (deploy/Caddyfile).
"""
from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from aiogram import Bot
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from api.auth import AuthError
from api.routes import router

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


def build_app(bot: Bot) -> FastAPI:
    app = FastAPI(
        title="Family budget Mini App",
        docs_url=None,      # схему API наружу не отдаём: эндпоинты не публичные
        redoc_url=None,
        openapi_url=None,
    )
    # Бот нужен эндпоинтам, чтобы спрашивать у Telegram про членство в чате.
    app.state.bot = bot

    @app.exception_handler(AuthError)
    async def _auth_failed(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True}

    app.include_router(router)

    # Статику монтируем последней: иначе она перехватила бы /api/*.
    if WEBAPP_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
    else:
        log.warning("Папки %s нет — мини-приложение отдавать нечего.", WEBAPP_DIR)

    return app


def create_server(bot: Bot) -> uvicorn.Server:
    """
    Собрать веб-сервер, не запуская его.

    Останавливать сервер будем не отменой задачи, а через `should_exit`: так
    uvicorn успевает доиграть открытые запросы. Свои обработчики сигналов он при
    этом не ставит — Ctrl+C ловит bot.py и гасит бота и сервер разом.
    """
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(bot),
            host=config.WEBAPP_HOST,
            port=config.WEBAPP_PORT,
            log_level=config.LOG_LEVEL.lower(),
            access_log=False,       # свои строчки пишем сами, чужие только шумят
            # Перед приложением всегда стоит обратный прокси: он держит HTTPS, а
            # настоящий адрес клиента передаёт в X-Forwarded-For. Без этого в
            # логах был бы один сплошной адрес прокси. Кому именно верить,
            # зависит от площадки — см. WEBAPP_TRUSTED_PROXY в config.py.
            proxy_headers=True,
            forwarded_allow_ips=config.WEBAPP_TRUSTED_PROXY,
        )
    )
    server.install_signal_handlers = lambda: None
    return server
