import json
import logging
import time
from typing import AsyncIterator, Set, Tuple, Optional, Dict, Any, List
import re

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
    """For Gemini generateContent requests, ensure generationConfig has responseModalities."""
    if 'generateContent' not in path and 'streamGenerateContent' not in path:
        return body
    try:
        data = json.loads(body)
        gc = data.get('generationConfig', None)
        # 如果没有 generationConfig 或 responseModalities 为空，注入 IMAGE+TEXT
        if gc is None or (isinstance(gc, dict) and not gc.get('responseModalities')):
            if 'generationConfig' not in data:
                data['generationConfig'] = {}
            data['generationConfig']['responseModalities'] = ['IMAGE', 'TEXT']
        return json.dumps(data).encode()
    except Exception:
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


def _extract_draw_overrides(prompt: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Extract aspectRatio / imageSize hints from prompt text.
    Returns (clean_prompt, aspectRatio, imageSize)
    """
    if not prompt:
        return "", None, None
    text = prompt
    aspect = None
    size = None
    # image size hints
    if re.search(r"\b4k\b", text, re.IGNORECASE) or "输出4K" in text:
        size = "4K"
    elif re.search(r"\b2k\b", text, re.IGNORECASE) or "输出2K" in text:
        size = "2K"
    elif re.search(r"\b1k\b", text, re.IGNORECASE) or "输出1K" in text:
        size = "1K"
    # aspect ratio hints like 16:9, 9:16, 4:3, 3:4, 3:2, 2:3, 5:4, 4:5, 21:9, 1:1, auto
    m = re.search(r"\b(\d{1,2}:\d{1,2}|auto)\b", text)
    if m:
        aspect = m.group(1)
    # remove hints from prompt
    text = re.sub(r"\b(1k|2k|4k|auto|\d{1,2}:\d{1,2})\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b输出\s*(1k|2k|4k)\b", "", text, flags=re.IGNORECASE)
    # cleanup spaces/lines
    text = "\n".join([ln.strip() for ln in text.splitlines() if ln.strip()])
    return text, aspect, size


_ALLOWED_MODELS = {
    "nano-banana-pro",
    "nano-banana-2",
    "nano-banana-pro-vt",
    "nano-banana-fast",
    "nano-banana",
}

_ALLOWED_ASPECTS = {
    "auto",
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
    "5:4",
    "4:5",
    "21:9",
}

_ALLOWED_SIZES = {"1K", "2K", "4K"}


def _validate_model(model: Optional[str]) -> Optional[str]:
    if not model:
        return "model is required"
    if model not in _ALLOWED_MODELS:
        return f"unsupported model: {model}"
    return None


def _validate_draw_payload(payload: Dict[str, Any]) -> Optional[str]:
    model = payload.get("model")
    err = _validate_model(model)
    if err:
        return err
    prompt = payload.get("prompt")
    if not prompt or not str(prompt).strip():
        return "prompt is required"

    aspect = payload.get("aspectRatio")
    if aspect and aspect not in _ALLOWED_ASPECTS:
        return f"unsupported aspectRatio: {aspect}"

    size = payload.get("imageSize")
    if size:
        if size not in _ALLOWED_SIZES:
            return f"unsupported imageSize: {size}"
        # model-specific size support
        if model == "nano-banana-pro-vip" and size not in ("1K", "2K"):
            return "nano-banana-pro-vip only supports 1K/2K"
        if model == "nano-banana-pro-4k-vip" and size != "4K":
            return "nano-banana-pro-4k-vip only supports 4K"
        if model not in {
            "nano-banana-2",
            "nano-banana-pro",
            "nano-banana-pro-vt",
            "nano-banana-pro-cl",
            "nano-banana-pro-vip",
            "nano-banana-pro-4k-vip",
        }:
            return f"imageSize not supported for model: {model}"

    # urls type check (optional)
    urls = payload.get("urls")
    if urls is not None and not isinstance(urls, list):
        return "urls must be an array"

    return None


def _map_openai_image_request(path: str, body: bytes) -> Tuple[str, bytes, bool]:
    """
    Map OpenAI-compatible image endpoints to grsai draw endpoint.
    Returns (new_path, new_body, is_openai_image).
    """
    if path not in ("v1/images/generations", "v1/images/edits"):
        return path, body, False
    try:
        data = json.loads(body or b"{}")
    except Exception:
        return path, body, True
    model = data.get("model") or "nano-banana"
    # Map size/aspectRatio (best-effort)
    size = data.get("size") or ""
    image_size = ""
    if isinstance(size, str):
        if size.startswith("1024"):
            image_size = "1K"
        elif size.startswith("2048"):
            image_size = "2K"
        elif size.startswith("4096"):
            image_size = "4K"
    prompt = data.get("prompt", "")
    prompt, aspect, size = _extract_draw_overrides(prompt)
    payload = {
        "model": model,
        "prompt": prompt,
    }
    if aspect:
        payload["aspectRatio"] = aspect
    if size:
        payload["imageSize"] = size
    if image_size:
        payload["imageSize"] = image_size
    # NOTE: OpenAI image edits use multipart; not handled here.
    # Grsai draw endpoint is fixed: /v1/draw/nano-banana
    new_path = "v1/draw/nano-banana"
    return new_path, json.dumps(payload, ensure_ascii=False).encode(), True


def _convert_draw_to_openai(content: bytes, content_type: str) -> bytes:
    """Convert grsai draw response to OpenAI image response (best-effort)."""
    ct = content_type or ""
    # If SSE, extract last JSON payload
    data = None
    if "text/event-stream" in ct:
        last = None
        for line in content.decode("utf-8", errors="ignore").splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    last = json.loads(payload)
                except Exception:
                    pass
        data = last
    else:
        try:
            data = json.loads(content)
        except Exception:
            return content

    if not isinstance(data, dict):
        return content

    # Success format
    if data.get("status") == "succeeded" and isinstance(data.get("results"), list):
        items = []
        for r in data["results"]:
            if not isinstance(r, dict):
                continue
            if r.get("url"):
                items.append({"url": r["url"]})
            elif r.get("b64_json"):
                items.append({"b64_json": r["b64_json"]})
        if items:
            return json.dumps(
                {"created": int(time.time()), "data": items},
                ensure_ascii=False
            ).encode()

    # Fallback: pass through error in OpenAI-like shape
    if data.get("status") == "failed" or data.get("error"):
        msg = data.get("error") or data.get("message") or "image generation failed"
        return json.dumps(
            {"error": {"message": msg, "type": "invalid_request_error"}},
            ensure_ascii=False
        ).encode()

    return content


def _map_gemini_official_request(path: str, body: bytes) -> Tuple[str, bytes, bool, bool, str]:
    """
    Map Gemini official API (v1beta models:*GenerateContent) to grsai draw endpoint.
    Returns (new_path, new_body, is_gemini_official, is_stream, model).
    """
    import re
    m = re.match(r"^v1beta/models/([^/:]+):(generateContent|streamGenerateContent)$", path)
    if not m:
        return path, body, False, False, ""
    model = m.group(1)
    is_stream = m.group(2) == "streamGenerateContent"
    try:
        data = json.loads(body or b"{}")
    except Exception:
        return path, body, True, is_stream, model

    # Extract prompt text and inline images
    prompt_parts = []
    urls = []
    md_img_re = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    contents = data.get("contents") or []
    # Only use the last user message (or last item) for prompt & refs
    content = None
    for c in reversed(contents):
        if isinstance(c, dict) and c.get("role") == "user":
            content = c
            break
    if content is None and contents:
        content = contents[-1] if isinstance(contents[-1], dict) else None

    if content:
        parts = content.get("parts") or []
        for p in parts:
            if isinstance(p, dict) and "text" in p and p["text"]:
                # Only text goes into prompt; extract markdown images to urls
                t = str(p["text"])
                # extract markdown image urls
                for m in md_img_re.findall(t):
                    if m:
                        urls.append(m)
                # strip markdown images from prompt
                t = md_img_re.sub("", t)
                # drop empty lines after stripping
                t = "\n".join([ln for ln in t.splitlines() if ln.strip()])
                if t:
                    prompt_parts.append(t)
            if isinstance(p, dict):
                inline = p.get("inlineData")
                if isinstance(inline, dict) and inline.get("data"):
                    # Reference image (base64)
                    urls.append(inline["data"])
                file_data = p.get("fileData")
                if isinstance(file_data, dict) and file_data.get("fileUri"):
                    # Reference image (URL)
                    urls.append(file_data["fileUri"])

    merged_prompt = "\n".join(prompt_parts).strip()
    merged_prompt, p_aspect, p_size = _extract_draw_overrides(merged_prompt)
    payload = {
        "model": model,
        "prompt": merged_prompt,
    }
    # Map generationConfig -> draw params (best-effort)
    gc = data.get("generationConfig") or {}
    aspect = gc.get("aspectRatio") or data.get("aspectRatio") or p_aspect
    size = gc.get("imageSize") or data.get("imageSize") or p_size
    if aspect:
        payload["aspectRatio"] = aspect
    if size:
        payload["imageSize"] = size
    if urls:
        payload["urls"] = urls

    # Grsai draw endpoint is fixed: /v1/draw/nano-banana
    new_path = "v1/draw/nano-banana"
    return new_path, json.dumps(payload, ensure_ascii=False).encode(), True, is_stream, model


def _parse_draw_response(content: bytes, content_type: str) -> Optional[Dict[str, Any]]:
    ct = content_type or ""
    if "text/event-stream" in ct:
        last = None
        for line in content.decode("utf-8", errors="ignore").splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    last = json.loads(payload)
                except Exception:
                    pass
        return last if isinstance(last, dict) else None
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _convert_draw_to_gemini_async(content: bytes, content_type: str, is_stream: bool) -> bytes:
    """Convert grsai draw response to Gemini official response (best-effort)."""
    data = _parse_draw_response(content, content_type)
    if not isinstance(data, dict):
        return content

    parts: List[Dict[str, Any]] = []
    if data.get("status") == "succeeded" and isinstance(data.get("results"), list):
        # Prefer inlineData; if only URL present, try to fetch and embed
        async with httpx.AsyncClient(timeout=30) as client:
            for r in data["results"]:
                if not isinstance(r, dict):
                    continue
                if r.get("b64_json"):
                    parts.append({
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": r["b64_json"],
                        }
                    })
                    continue
                url = r.get("url")
                if url:
                    try:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        mime = resp.headers.get("content-type") or "image/png"
                        b64 = base64.b64encode(resp.content).decode()
                        parts.append({
                            "inlineData": {
                                "mimeType": mime.split(";")[0],
                                "data": b64,
                            }
                        })
                    except Exception:
                        # fallback to markdown image so UI can render
                        parts.append({"text": f"![image]({url})"})

    if not parts:
        if data.get("status") == "failed" or data.get("error"):
            msg = data.get("error") or data.get("message") or "image generation failed"
            gem = {
                "candidates": [{
                    "content": {"role": "model", "parts": [{"text": msg}]},
                    "finishReason": "STOP",
                    "index": 0
                }]
            }
        else:
            return content
    else:
        gem = {
            "candidates": [{
                "content": {"role": "model", "parts": parts},
                "finishReason": "STOP",
                "index": 0
            }]
        }

    if is_stream:
        return (f"data: {json.dumps(gem, ensure_ascii=False)}\n\n"
                f"data: [DONE]\n\n").encode("utf-8")
    return json.dumps(gem, ensure_ascii=False).encode("utf-8")


async def proxy_request(request: Request, path: str) -> Response:
    body = await request.body()
    # Map OpenAI image endpoints to draw endpoint (best-effort)
    path, body, is_openai_image = _map_openai_image_request(path, body)
    # Map Gemini official endpoint to draw endpoint (best-effort)
    path, body, is_gemini_official, gemini_stream, gemini_model = _map_gemini_official_request(path, body)
    body = _patch_gemini_request(body, path)
    # For native draw requests, parse prompt hints if aspectRatio/imageSize not provided
    if path.startswith("v1/draw/"):
        try:
            _d = json.loads(body or b"{}")
            prompt = _d.get("prompt", "")
            clean_prompt, aspect, size = _extract_draw_overrides(prompt)
            if clean_prompt != prompt:
                _d["prompt"] = clean_prompt
            if aspect and not _d.get("aspectRatio"):
                _d["aspectRatio"] = aspect
            if size and not _d.get("imageSize"):
                _d["imageSize"] = size
            err = _validate_draw_payload(_d)
            if err:
                return Response(
                    content=json.dumps({"error": err}, ensure_ascii=False).encode(),
                    status_code=400,
                    media_type="application/json",
                )
            body = json.dumps(_d, ensure_ascii=False).encode()
        except Exception:
            pass
    model = _get_model_from_request(body)
    # 对 Gemini 接口，从 path 中提取模型名
    if not model and ('generateContent' in path or 'streamGenerateContent' in path):
        import re
        m = re.search(r'/models/([^/:]+)', path)
        if m:
            model = m.group(1)
    if not model and is_gemini_official and gemini_model:
        model = gemini_model
    # no extra model logging
    cost = get_model_cost(model)

    selected_key = key_manager.get_next_key(cost=cost)
    if selected_key is None:
        # Refresh credits once and retry selection
        try:
            logger.info("No available keys, refreshing credits before retry...")
            await key_manager.refresh_all_credits()
        except Exception as exc:
            logger.warning("Credits refresh failed: %s", exc)
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
    # Log draw request params (model/prompt/size/ratio)
    # no extra draw-params logging

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
    # Do not use raw Gemini streaming branch when we mapped official Gemini to draw
    if is_gemini_official:
        is_gemini_sse = False
    # Support Gemini official API key header format as well
    if is_gemini_sse:
        forward_headers["x-goog-api-key"] = selected_key
    response_headers = {}

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
                        # 完全透传，不做任何修改
                        nonlocal response_headers
                        response_headers = {
                            k: v for k, v in resp.headers.items()
                            if k.lower() not in _HOP_BY_HOP
                        }
                        async for chunk in resp.aiter_bytes():
                            yield chunk
            except Exception as exc:
                logger.error("Gemini stream error: %s", repr(exc))
                yield b'data: {"error": "Stream failed"}\n\n'
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

    # Convert draw response to OpenAI image format if needed
    if is_openai_image:
        content = _convert_draw_to_openai(content, upstream_resp.headers.get("content-type", ""))
        response_headers["content-type"] = "application/json"
    # Convert draw response to Gemini official response if needed
    if is_gemini_official:
        content = await _convert_draw_to_gemini_async(
            content,
            upstream_resp.headers.get("content-type", ""),
            gemini_stream
        )
        response_headers["content-type"] = "text/event-stream" if gemini_stream else "application/json"

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
