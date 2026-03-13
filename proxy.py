import json
import logging
from typing import AsyncIterator, Set

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from config import UPSTREAM_BASE_URL
from key_manager import key_manager
from model_credits import get_model_cost

logger = logging.getLogger(__name__)

# Headers that must not be forwarded (hop-by-hop)
_HOP_BY_HOP: Set[str] = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


def _get_model_from_request(body: bytes) -> str:
    """Extract model field from JSON request body."""
    try:
        return json.loads(body).get("model", "") or ""
    except Exception:
        return ""


def _check_draw_succeeded(content: bytes, content_type: str) -> bool:
    """Parse draw API response to check if generation succeeded."""
    try:
        ct = content_type or ""
        if "application/json" in ct:
            data = json.loads(content)
            # webhook mode: {"code":0, "data":{"id":"..."}} — charge on submission
            if data.get("code") == 0 and isinstance(data.get("data"), dict):
                return True
            return False
        # SSE stream: find last data line with status
        status = None
        failure_reason = None
        for line in content.decode("utf-8", errors="ignore").splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                    if "status" in obj:
                        status = obj["status"]
                        failure_reason = obj.get("failure_reason", "")
                except Exception:
                    pass
        # succeeded = charge, failed with moderation/error = refund (don't charge)
        return status == "succeeded"
    except Exception:
        return False


def _patch_gemini_request(body: bytes, path: str) -> bytes:
    """Pass through as-is - let upstream handle generationConfig."""
    return body


def _clean_gemini_sse(content: bytes) -> bytes:
    """Remove sdkHttpResponse field from Gemini SSE response lines."""
    lines = content.decode('utf-8', errors='replace').splitlines(keepends=True)
    cleaned = []
    for line in lines:
        if line.startswith('data:') and 'sdkHttpResponse' in line:
            try:
                prefix = 'data: '
                json_str = line[len(prefix):].strip()
                if json_str and json_str != '[DONE]':
                    obj = json.loads(json_str)
                    obj.pop('sdkHttpResponse', None)
                    cleaned.append(prefix + json.dumps(obj, ensure_ascii=False) + '\n')
                    continue
            except Exception:
                pass
        cleaned.append(line)
    return ''.join(cleaned).encode('utf-8')


async def proxy_request(request: Request, path: str) -> Response:
    body = await request.body()
    body = _patch_gemini_request(body, path)
    model = _get_model_from_request(body)
    # 对 Gemini 接口，从 path 中提取模型名
    if not model and ('generateContent' in path or 'streamGenerateContent' in path):
        import re
        m = re.search(r'/models/([^/:]+)', path)
        if m:
            model = m.group(1)
    cost = get_model_cost(model)

    selected_key = key_manager.get_next_key(cost=cost)
    if selected_key is None:
        logger.error("No available API keys for model '%s' (cost=%d)", model, cost)
        return Response(
            content=b'{"error": "No available API keys"}',
            status_code=503,
            media_type="application/json",
        )

    # Build target URL
    query = request.url.query
    target_url = f"{UPSTREAM_BASE_URL}/{path}"
    if query:
        target_url = f"{target_url}?{query}"

    # Filter and rebuild headers
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    forward_headers["authorization"] = f"Bearer {selected_key}"
    # 移除 Accept-Encoding，让上游返回未压缩数据，避免代理转发压缩内容时丢失 Content-Encoding
    forward_headers.pop("accept-encoding", None)
    # 更新 Content-Length（body 可能被 _patch_gemini_request 修改过）
    if "content-length" in forward_headers:
        forward_headers["content-length"] = str(len(body))

    is_gemini_sse = ('generateContent' in path or 'streamGenerateContent' in path)

    # Gemini SSE requests: stream response directly
    if is_gemini_sse:
        logger.info("Gemini request headers: %s", dict(forward_headers))
        logger.info("Gemini request body: %s", body[:200])
        async def stream_gemini():
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    async with client.stream(
                        method=request.method,
                        url=target_url,
                        headers=forward_headers,
                        content=body,
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if line.startswith('data:') and 'sdkHttpResponse' in line:
                                try:
                                    import json as _json
                                    prefix = 'data: '
                                    json_str = line[len(prefix):].strip()
                                    if json_str and json_str != '[DONE]':
                                        obj = _json.loads(json_str)
                                        obj.pop('sdkHttpResponse', None)
                                        line = prefix + _json.dumps(obj, ensure_ascii=False)
                                except Exception:
                                    pass
                            yield line + '\n\n'
            except Exception as exc:
                logger.error("Gemini stream error: %s", repr(exc))
                yield 'data: {"error": "Stream failed"}\n\n'
        logger.info("%s /%s model=%s (gemini stream, key ...%s)",
                    request.method, path, model or "-", selected_key[-6:])
        return StreamingResponse(stream_gemini(), media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            upstream_resp = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=body,
            )
    except httpx.RequestError as exc:
        logger.error("Upstream request error: %s", repr(exc))
        return Response(
            content=b'{"error": "Upstream request failed"}',
            status_code=502,
            media_type="application/json",
        )

    # 请求成功且有固定积分消耗时，检查是否真正成功再扣除
    if upstream_resp.status_code == 200 and cost > 0:
        should_charge = True
        if "/v1/draw/" in path or path.startswith("v1/draw/"):
            should_charge = _check_draw_succeeded(
                upstream_resp.content,
                upstream_resp.headers.get("content-type", "")
            )
        if should_charge:
            key_manager.deduct_credits(selected_key, cost)
        else:
            logger.info(
                "Key ...%s NOT charged (draw failed/moderated), cost=%d returned",
                selected_key[-6:], cost
            )

    # Strip hop-by-hop headers from upstream response
    response_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    content = upstream_resp.content
    # 清理 Gemini 响应中的 sdkHttpResponse 字段
    if 'generateContent' in path or 'streamGenerateContent' in path:
        content = _clean_gemini_sse(content)

    logger.info(
        "%s /%s model=%s cost=%d -> %d (key ...%s)",
        request.method,
        path,
        model or "-",
        cost,
        upstream_resp.status_code,
        selected_key[-6:],
    )

    return Response(
        content=content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
