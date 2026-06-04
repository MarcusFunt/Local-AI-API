from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..normalize import allowed_model_ids

router = APIRouter()


@router.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "local-ai-api",
                }
                for model_id in allowed_model_ids()
            ],
        }
    )
