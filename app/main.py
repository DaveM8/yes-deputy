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
from app.routers.sources import router as sources_router
from app.retrieval import MemberIndex


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialise SQLite schema
    init_db(settings.db_path, settings.query_log_table)

    # Initialise shared async clients
    es_kwargs: dict = {"hosts": [settings.es_host]}
    if settings.es_user and settings.es_pass:
        es_kwargs["basic_auth"] = (settings.es_user, settings.es_pass)

    app.state.es = AsyncElasticsearch(**es_kwargs)
    app.state.openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    app.state.settings = settings

    # Person names for spotting "Micheál Martin" etc. in questions. A failure here
    # (e.g. ES briefly down) must not stop the app; ensure_fresh retries per request.
    app.state.members = MemberIndex()
    try:
        await app.state.members.refresh(app.state.es, settings.es_index)
    except Exception:
        pass

    yield

    await app.state.es.close()
    await app.state.openai_client.close()


app = FastAPI(title="Yes Deputy — Oireachtas Q&A", lifespan=lifespan)

app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(health_router)
app.include_router(sources_router)

# "no-cache" means "revalidate before use", not "don't store": browsers keep
# the file but check its ETag first (a cheap 304). Without it, browsers guess a
# lifetime from Last-Modified, and iOS kept running an old app.js against a
# new index.html after a deploy.
_NO_CACHE = {"Cache-Control": "no-cache"}


class _RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(_NO_CACHE)
        return response


app.mount("/static", _RevalidatedStaticFiles(directory="static"), name="static")


def _page(path: str) -> FileResponse:
    return FileResponse(path, headers=_NO_CACHE)


@app.get("/")
async def index():
    return _page("static/index.html")


@app.get("/login")
async def login_page():
    return _page("static/auth.html")


@app.get("/admin")
async def admin_page():
    return _page("static/admin.html")


@app.get("/source/{chunk_id}")
async def source_page(chunk_id: str):
    # The page reads the chunk ID from its own URL and fetches /api/sources/{id}
    return _page("static/source.html")
