import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from .. import client as ollama_client
from ..config import settings
from ..models import ChatCompletionRequest
from ..normalize import resolve_model

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest) -> Any:
    model_alias = req.model if req.model else settings.default_model_profile
    resolved = resolve_model(model_alias, settings)

    logger.info(
        "Chat request: alias=%r resolved=%r stream=%s messages=%d",
        model_alias,
        resolved,
        req.stream,
        len(req.messages),
    )

    request_dict: dict[str, Any] = {
        "messages": [m.model_dump() for m in req.messages],
        "temperature": req.temperature,
        "top_p": req.top_p,
        "max_tokens": req.max_tokens,
        "stop": req.stop,
        "seed": req.seed,
    }

    if req.stream:
        stream = await ollama_client.proxy_streaming(resolved, request_dict, settings)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    completion = await ollama_client.proxy_non_streaming(resolved, request_dict, settings)
    return JSONResponse(completion.model_dump())
