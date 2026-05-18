import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import client as ollama_client
from ..config import settings
from ..models import ChatCompletionRequest
from ..normalize import resolve_model

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    req = ChatCompletionRequest.model_validate(body)

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
    }

    if req.stream:
        return StreamingResponse(
            ollama_client.proxy_streaming(resolved, request_dict, settings),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    completion = await ollama_client.proxy_non_streaming(resolved, request_dict, settings)
    return JSONResponse(completion.model_dump())
