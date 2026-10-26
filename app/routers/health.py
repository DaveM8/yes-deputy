from __future__ import annotations

from fastapi import APIRouter, Request
from app.config import settings
from app.llm.registry import MODEL_REGISTRY

router = APIRouter()


@router.get("/api/health")
async def health(request: Request) -> dict:
    es = request.app.state.es
    index = settings.es_index

    try:
        count_resp = await es.count(index=index)
        doc_count = count_resp["count"]
        es_status = "ok"
    except Exception as exc:
        doc_count = None
        es_status = str(exc)

    models = [
        {
            "key": key,
            "display_name": info.display_name,
            "tier": info.tier,
            "provider": info.provider,
        }
        for key, info in MODEL_REGISTRY.items()
    ]

    return {
        "status": "ok",
        "es_status": es_status,
        "doc_count": doc_count,
        "index": index,
        "models": models,
    }
