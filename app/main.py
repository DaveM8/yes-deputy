from __future__ import annotations

from contextlib import asynccontextmanager

from elasticsearch import AsyncElasticsearch
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI

from app.config import settings
from app.database import init_db
from app.routers.admin_router import router as admin_router
from app.routers.auth_router import router as auth_router
from app.routers.chat import router as chat_router
from app.routers.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialise SQLite schema
    init_db(settings.db_path)

    # Initialise shared async clients
    es_kwargs: dict = {"hosts": [settings.es_host]}
    if settings.es_user and settings.es_pass:
        es_kwargs["basic_auth"] = (settings.es_user, settings.es_pass)

    app.state.es = AsyncElasticsearch(**es_kwargs)
    app.state.openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    app.state.settings = settings

    yield

    await app.state.es.close()
    await app.state.openai_client.close()


app = FastAPI(title="Yes Deputy — Oireachtas Q&A", lifespan=lifespan)

app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(health_router)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/login")
async def login_page():
    return FileResponse("static/auth.html")


@app.get("/admin")
async def admin_page():
    return FileResponse("static/admin.html")
