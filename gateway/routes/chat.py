import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from .. import client as ollama_client
from ..config import settings
from ..models import ChatCompletionRequest, ChatMessage
from ..normalize import resolve_model

logger = logging.getLogger(__name__)

router = APIRouter()

_NEWLINE = "\n"
_DOUBLE_NEWLINE = "\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest) -> Any:
    model_alias = req.model if req.model else settings.default_model_profile
    resolved = resolve_model(model_alias, settings)

    logger.info(
        "Chat request: alias=%r resolved=%r stream=%s messages=%d use_rag=%s",
        model_alias,
        resolved,
        req.stream,
        len(req.messages),
        req.use_rag,
    )

    # RAG context injection - lazy imports keep startup fast when RAG is disabled.
    if req.use_rag:
        from ..rag import config as rag_config
        if rag_config.RAG_ENABLED:
            from ..rag.store import search as rag_search
            # Find the last user message to use as the retrieval query.
            last_user = next(
                (m.content for m in reversed(req.messages) if m.role == "user"),
                None,
            )
            if last_user and isinstance(last_user, str):
                try:
                    chunks = await rag_search(str(last_user), top_k=rag_config.TOP_K)
                    if chunks:
                        context_parts = [
                            "[Source: " + c["filename"] + "]" + _NEWLINE + c["text"]
                            for c in chunks
                        ]
                        context = _DOUBLE_NEWLINE.join(context_parts)
                        system_msg = (
                            "Use the following context to answer the question:"
                            + _DOUBLE_NEWLINE
                            + context
                        )
                        req.messages.insert(
                            0, ChatMessage(role="system", content=system_msg)
                        )
                        logger.info("RAG injected %d chunks into context", len(chunks))
                except Exception as exc:
                    # Non-fatal: log and continue without RAG context.
                    logger.warning(
                        "RAG retrieval failed, continuing without context: %s", exc
                    )

    max_tokens = req.max_tokens if req.max_tokens is not None else req.max_completion_tokens
    request_dict: dict[str, Any] = {
        "messages": [m.model_dump() for m in req.messages],
        "temperature": req.temperature,
        "top_p": req.top_p,
        "max_tokens": max_tokens,
        "stop": req.stop,
        "seed": req.seed,
        "tools": req.tools,
        "tool_choice": req.tool_choice,
        "response_format": req.response_format,
        "stream_options": req.stream_options,
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
    return JSONResponse(completion.model_dump(exclude_none=True))
