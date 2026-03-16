import json
import logging
import time
import asyncio
import base64
from typing import AsyncIterator, Set, Tuple, Optional, Dict, Any, List
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

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

def _gemini_keepalive_chunk() -> str:
    # Minimal non-final chunk to keep SSE clients alive
    return 'data: {"candidates":[{"content":{"role":"model","parts":[{"text":""}]},"index":0}]}\n\n'


def _gemini_error_chunk(message: str) -> str:
    gem = {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": message}]},
            "finishReason": "STOP",
            "index": 0,
        }]
    }
    return f"data: {json.dumps(gem, ensure_ascii=False)}\n\n" + "data: [DONE]\n\n"


def _drop_alt_sse(url: str) -> str:
    try:
        parts = urlsplit(url)
        qs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "alt"]
        new_query = urlencode(qs)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    except Exception:
        return url


def _is_credit_error(obj: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(obj, dict):
        return False
    code = obj.get("code")
    msg = str(obj.get("msg") or obj.get("error") or "")
    low = msg.lower()
    if code in (-1, 402):
        return True
    if "credits not enough" in low or "credit not enough" in low:
        return True
    if "credits" in low and "not" in low:
        return True
    if "积分" in msg and ("不足" in msg or "不够" in msg):
        return True
    return False


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

# Map Gemini official model names to grsai draw models
_GEMINI_MODEL_ALIAS = {
    "gemini-2.5-flash-image": "nano-banana-fast",
}


def _map_gemini_model_name(model: str) -> str:
    if not model:
        return model
    return _GEMINI_MODEL_ALIAS.get(model, model)

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


def _map_openai_chat_request(path: str, body: bytes) -> Tuple[str, bytes, bool, bool, str]:
    """
    Map OpenAI-compatible chat completions to grsai draw endpoint.
    Returns (new_path, new_body, is_openai_chat, is_stream, model).
    """
    if path not in ("v1/chat/completions", "v1/chat/completions/"):
        return path, body, False, False, ""
    try:
        data = json.loads(body or b"{}")
    except Exception:
        return path, body, True, False, ""

    is_stream = bool(data.get("stream"))
    model = data.get("model") or ""

    def _strip_data_uri(data_str: str) -> str:
        if not isinstance(data_str, str):
            return data_str
        if data_str.startswith("data:") and "base64," in data_str:
            return data_str.split("base64,", 1)[1]
        return data_str

    prompt_parts: List[str] = []
    urls: List[str] = []
    messages = data.get("messages") or []
    msg = None
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            msg = m
            break
    if msg is None and messages:
        msg = messages[-1] if isinstance(messages[-1], dict) else None

    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        prompt_parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text" and part.get("text"):
                    prompt_parts.append(str(part.get("text")))
                elif ptype in ("image_url", "image"):
                    img = part.get("image_url") or part.get("image") or {}
                    if isinstance(img, dict) and img.get("url"):
                        urls.append(str(img.get("url")))
                    elif isinstance(img, str):
                        urls.append(_strip_data_uri(img))
                elif ptype == "file":
                    if part.get("data"):
                        urls.append(_strip_data_uri(str(part.get("data"))))
                elif part.get("image_url"):
                    img = part.get("image_url")
                    if isinstance(img, dict) and img.get("url"):
                        urls.append(str(img.get("url")))
            elif isinstance(part, str):
                prompt_parts.append(part)

    merged_prompt = "\n".join([p for p in prompt_parts if p]).strip()
    merged_prompt, p_aspect, p_size = _extract_draw_overrides(merged_prompt)
    if not merged_prompt:
        merged_prompt = "image"

    payload = {
        "model": model or "nano-banana",
        "prompt": merged_prompt,
    }
    if p_aspect:
        payload["aspectRatio"] = p_aspect
    if p_size:
        payload["imageSize"] = p_size
    if urls:
        payload["urls"] = urls

    new_path = "v1/draw/nano-banana"
    return new_path, json.dumps(payload, ensure_ascii=False).encode(), True, is_stream, payload["model"]


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


def _convert_draw_to_openai_chat(content: bytes, content_type: str, is_stream: bool, model: str) -> bytes:
    data = _parse_draw_response(content, content_type)
    if not isinstance(data, dict):
        return content

    d = data.get("data") if isinstance(data.get("data"), dict) else data
    if isinstance(d, dict) and ("results" in d or "status" in d or "error" in d):
        data = d

    if data.get("code") not in (None, 0):
        msg = data.get("msg") or data.get("error") or "request failed"
        err = {"error": {"message": msg, "type": "invalid_request_error"}}
        if is_stream:
            return (f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                    f"data: [DONE]\n\n").encode("utf-8")
        return json.dumps(err, ensure_ascii=False).encode("utf-8")

    text = ""
    results = data.get("results") if isinstance(data.get("results"), list) else []
    if results:
        parts = []
        for r in results:
            if not isinstance(r, dict):
                continue
            if r.get("url"):
                parts.append(f"![image]({r['url']})")
            elif r.get("b64_json"):
                parts.append(f"data:image/png;base64,{r['b64_json']}")
        text = "\n".join(parts)
    if not text:
        if data.get("status") == "failed" or data.get("error") or data.get("failure_reason"):
            msg = data.get("error") or data.get("failure_reason") or "image generation failed"
        else:
            msg = "no output"
        err = {"error": {"message": msg, "type": "invalid_request_error"}}
        if is_stream:
            return (f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                    f"data: [DONE]\n\n").encode("utf-8")
        return json.dumps(err, ensure_ascii=False).encode("utf-8")

    created = int(time.time())
    if is_stream:
        chunk = {
            "id": f"chatcmpl-{created}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model or "nano-banana",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": None
            }]
        }
        final = {
            "id": f"chatcmpl-{created}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model or "nano-banana",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }]
        }
        return (f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
                f"data: [DONE]\n\n").encode("utf-8")

    resp = {
        "id": f"chatcmpl-{created}",
        "object": "chat.completion",
        "created": created,
        "model": model or "nano-banana",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }]
    }
    return json.dumps(resp, ensure_ascii=False).encode("utf-8")


def _map_gemini_official_request(path: str, body: bytes) -> Tuple[str, bytes, bool, bool, str]:
    """
    Map Gemini official API (v1beta models:*GenerateContent) to grsai draw endpoint.
    Returns (new_path, new_body, is_gemini_official, is_stream, model).
    """
    import re
    # handle URL-encoded colon in path (e.g. ...%3AstreamGenerateContent)
    norm_path = path.replace("%3A", ":").replace("%3a", ":")
    m = re.match(r"^v1beta/models/([^/:]+):(generateContent|streamGenerateContent)$", norm_path)
    if not m:
        return path, body, False, False, ""
    model = _map_gemini_model_name(m.group(1))
    is_stream = m.group(2) == "streamGenerateContent"
    try:
        data = json.loads(body or b"{}")
    except Exception:
        return path, body, True, is_stream, model

    def _strip_data_uri(data_str: str) -> str:
        if not isinstance(data_str, str):
            return data_str
        if data_str.startswith("data:") and "base64," in data_str:
            return data_str.split("base64,", 1)[1]
        return data_str

    def _extract_from_parts(parts: Any) -> Tuple[List[str], List[str]]:
        prompt_parts: List[str] = []
        urls: List[str] = []
        if not isinstance(parts, list):
            return prompt_parts, urls
        md_img_re = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
        for p in parts:
            text = None
            if isinstance(p, str):
                text = p
            elif isinstance(p, dict):
                if "text" in p and p["text"]:
                    text = str(p["text"])
                inline = p.get("inlineData")
                if isinstance(inline, dict) and inline.get("data"):
                    urls.append(_strip_data_uri(str(inline["data"])))
                file_data = p.get("fileData")
                if isinstance(file_data, dict) and file_data.get("fileUri"):
                    urls.append(str(file_data["fileUri"]))
                image_url = p.get("image_url") or p.get("imageUrl")
                if isinstance(image_url, dict) and image_url.get("url"):
                    urls.append(str(image_url["url"]))
            if text:
                for u in md_img_re.findall(text):
                    if u:
                        urls.append(u)
                text = md_img_re.sub("", text)
                text = "\n".join([ln for ln in text.splitlines() if ln.strip()])
                if text:
                    prompt_parts.append(text)
        return prompt_parts, urls

    contents = data.get("contents")
    if isinstance(contents, dict):
        contents = [contents]
    if not isinstance(contents, list):
        contents = []

    # Only use the last user message (or last item) for prompt & refs
    content = None
    for c in reversed(contents):
        if isinstance(c, dict) and c.get("role") == "user":
            content = c
            break
    if content is None and contents:
        content = contents[-1] if isinstance(contents[-1], dict) else None

    prompt_parts: List[str] = []
    urls: List[str] = []

    system = data.get("systemInstruction")
    if isinstance(system, dict):
        sys_parts = system.get("parts")
        sys_text, _ = _extract_from_parts(sys_parts)
        if sys_text:
            prompt_parts.append("\n".join(sys_text))

    if content:
        parts = content.get("parts") or []
        text_parts, ref_urls = _extract_from_parts(parts)
        prompt_parts.extend(text_parts)
        urls.extend(ref_urls)

    merged_prompt = "\n".join([p for p in prompt_parts if p]).strip()
    merged_prompt, p_aspect, p_size = _extract_draw_overrides(merged_prompt)
    if not merged_prompt:
        merged_prompt = "image"
    payload = {
        "model": model,
        "prompt": merged_prompt,
    }
    # Map generationConfig -> draw params (best-effort)
    gc = data.get("generationConfig") or {}
    if not isinstance(gc, dict):
        gc = {}
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

    if data.get("code") not in (None, 0):
        msg = data.get("msg") or data.get("error") or "request failed"
        gem = {
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": msg}]},
                "finishReason": "STOP",
                "index": 0
            }]
        }
        if is_stream:
            return (f"data: {json.dumps(gem, ensure_ascii=False)}\n\n"
                    f"data: [DONE]\n\n").encode("utf-8")
        return json.dumps(gem, ensure_ascii=False).encode("utf-8")

    parts: List[Dict[str, Any]] = []
    has_results = isinstance(data.get("results"), list) and len(data.get("results")) > 0
    if has_results:
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
        if data.get("status") == "failed" or data.get("error") or data.get("failure_reason"):
            msg = data.get("error") or data.get("failure_reason") or data.get("message") or "image generation failed"
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
    # Map OpenAI chat completions to draw endpoint (best-effort)
    path, body, is_openai_chat, openai_chat_stream, openai_chat_model = _map_openai_chat_request(path, body)
    # Map Gemini official endpoint to draw endpoint (best-effort)
    path, body, is_gemini_official, gemini_stream, gemini_model = _map_gemini_official_request(path, body)
    body = _patch_gemini_request(body, path)
    # For Gemini official stream, prefer upstream SSE if available; do not force webhook
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
            # If model doesn't support imageSize, drop it instead of failing
            if _d.get("imageSize"):
                _model = _d.get("model")
                if _model not in {
                    "nano-banana-2",
                    "nano-banana-pro",
                    "nano-banana-pro-vt",
                    "nano-banana-pro-cl",
                    "nano-banana-pro-vip",
                    "nano-banana-pro-4k-vip",
                }:
                    _d.pop("imageSize", None)
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
    if not model and is_openai_chat and openai_chat_model:
        model = openai_chat_model
    # no extra model logging
    cost = get_model_cost(model)

    if cost > 0:
        selected_key = await key_manager.reserve_key(cost=cost)
    else:
        selected_key = key_manager.get_next_key(cost=cost)
    if selected_key is None:
        # Refresh credits once and retry selection
        try:
            logger.info("No available keys, refreshing credits before retry...")
            await key_manager.refresh_all_credits()
        except Exception as exc:
            logger.warning("Credits refresh failed: %s", exc)
        if cost > 0:
            selected_key = await key_manager.reserve_key(cost=cost)
        else:
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
    if path.startswith("v1/draw/"):
        try:
            _d = json.loads(body or b"{}")
            urls = _d.get("urls") or []
            def _brief(u):
                if not isinstance(u, str):
                    return "<non-string>"
                return (u[:120] + "...") if len(u) > 120 else u
            _log = {
                "model": _d.get("model"),
                "aspectRatio": _d.get("aspectRatio"),
                "imageSize": _d.get("imageSize"),
                "urls_count": len(urls),
                "urls_sample": [_brief(u) for u in urls[:2]],
                "prompt": (_d.get("prompt") or "")[:200],
            }
            logger.info("Draw 请求参数: %s", _log)
        except Exception:
            logger.info("Draw 请求参数: <unreadable>")

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

    # Gemini official streaming (mapped to draw) - stream & convert to Gemini SSE
    if is_gemini_official and gemini_stream:
        target_url_nostream = _drop_alt_sse(target_url)

        async def poll_draw_result_once(client: httpx.AsyncClient, draw_id: str) -> Optional[dict]:
            url = f"{UPSTREAM_BASE_URL}/v1/draw/result"
            try:
                poll_headers = dict(forward_headers)
                poll_headers.pop("content-length", None)
                poll_headers["content-type"] = "application/json"
                resp = await client.post(url, headers=poll_headers, json={"id": draw_id})
                return resp.json()
            except Exception:
                return None

        async def stream_gemini_from_draw():
            refunded_keys: set[str] = set()
            current_key = selected_key

            async def _refund_key_once(key: Optional[str]):
                if not key or cost <= 0:
                    return
                if key in refunded_keys:
                    return
                await key_manager.refund_credits(key, cost)
                refunded_keys.add(key)
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    attempts = 0
                    while attempts < 2:
                        attempts += 1
                        forward_headers["authorization"] = f"Bearer {current_key}"

                        async def poll_and_send(draw_id: str, status_box: dict):
                            end = time.time() + 300
                            poll_count = 0
                            last_progress = None
                            stale_count = 0
                            stalled = False
                            last_emit = 0.0
                            status_box["done"] = False
                            status_box["stalled"] = False
                            status_box["sent"] = False
                            # initial keep-alive to open stream immediately
                            yield _gemini_keepalive_chunk()
                            last_emit = time.time()
                            while time.time() < end:
                                now = time.time()
                                if now - last_emit >= 2:
                                    yield _gemini_keepalive_chunk()
                                    last_emit = now
                                data = await poll_draw_result_once(client, draw_id)
                                poll_count += 1
                                if isinstance(data, dict):
                                    d = data.get("data") if isinstance(data.get("data"), dict) else data
                                    if not isinstance(d, dict):
                                        d = {}
                                    status = d.get("status")
                                    progress = d.get("progress")
                                    results = d.get("results") if isinstance(d.get("results"), list) else []
                                    if poll_count == 1 or poll_count % 5 == 0:
                                        logger.info(
                                            "Draw poll #%d status=%s progress=%s has_results=%s code=%s msg=%s",
                                            poll_count,
                                            status,
                                            progress,
                                            bool(results),
                                            data.get("code"),
                                            str(data.get("msg") or d.get("error") or d.get("failure_reason") or ""),
                                        )
                                    if isinstance(progress, int):
                                        if last_progress is None or progress != last_progress:
                                            last_progress = progress
                                            stale_count = 0
                                        else:
                                            stale_count += 1
                                        if stale_count >= 20:
                                            stalled = True
                                            break
                                    if status in ("succeeded", "failed") or results or d.get("error"):
                                        first_url = ""
                                        if results:
                                            r0 = results[0] if isinstance(results[0], dict) else {}
                                            first_url = r0.get("url") or ""
                                        err_msg = str(d.get("error") or d.get("failure_reason") or "")
                                        logger.info(
                                            "Draw poll result status=%s has_results=%s results_count=%s first_url=%s error=%s",
                                            status,
                                            bool(results),
                                            len(results),
                                            first_url[:200],
                                            err_msg[:200],
                                        )
                                        if status == "failed" or d.get("error") or d.get("failure_reason"):
                                            await _refund_key_once(current_key)
                                        final_bytes = await _convert_draw_to_gemini_async(
                                            json.dumps(d, ensure_ascii=False).encode(),
                                            "application/json",
                                            True
                                        )
                                        yield final_bytes.decode("utf-8", errors="ignore")
                                        status_box["sent"] = True
                                        status_box["done"] = True
                                        return
                                await asyncio.sleep(2)
                            if stalled:
                                logger.info("Draw poll stalled, retrying with another key")
                                await _refund_key_once(current_key)
                                status_box["stalled"] = True
                                return
                            yield _gemini_error_chunk("draw result timeout")
                            status_box["sent"] = True
                            status_box["done"] = True
                            return
                        async with client.stream(
                            method=request.method,
                            url=target_url,
                            headers=forward_headers,
                            content=body,
                        ) as resp:
                            ct = resp.headers.get("content-type", "")
                            logger.info("Upstream content-type (stream): %s", ct or "<missing>")
                            # If upstream is not SSE, read full body and convert once
                            if "text/event-stream" not in ct:
                                content = await resp.aread()
                                logger.info("Upstream JSON len=%d sample=%s", len(content or b""), (content or b"")[:200])
                                # Try parse id response and poll for result
                                try:
                                    obj = json.loads(content)
                                except Exception:
                                    obj = None
                                if _is_credit_error(obj):
                                    logger.info("Upstream credits not enough for key ...%s", current_key[-6:])
                                    try:
                                        await key_manager.refresh_credits(current_key)
                                    except Exception:
                                        pass
                                    # refund current reserved key before retry
                                    await _refund_key_once(current_key)
                                    # retry with another key once
                                    if attempts < 2:
                                        new_key = await key_manager.reserve_key(cost=cost) if cost > 0 else key_manager.get_next_key(cost=cost)
                                        if new_key:
                                            logger.info("Retrying with another key ...%s", new_key[-6:])
                                            current_key = new_key
                                            continue
                                    yield _gemini_error_chunk("apikey credits not enough")
                                    return
                                draw_id = None
                                if isinstance(obj, dict):
                                    if isinstance(obj.get("data"), dict):
                                        draw_id = obj["data"].get("id")
                                    draw_id = draw_id or obj.get("id")
                                logger.info("Upstream draw_id: %s", draw_id or "<missing>")
                                if draw_id:
                                    status_box = {}
                                    async for chunk in poll_and_send(draw_id, status_box):
                                        yield chunk
                                    if status_box.get("done"):
                                        return
                                    # stalled -> try another key
                                    if status_box.get("stalled") and attempts < 2:
                                        new_key = await key_manager.reserve_key(cost=cost) if cost > 0 else key_manager.get_next_key(cost=cost)
                                        if new_key:
                                            logger.info("Retrying with another key ...%s", new_key[-6:])
                                            current_key = new_key
                                            continue
                                    if status_box.get("stalled"):
                                        yield _gemini_error_chunk("draw result stalled")
                                        return
                                # fallback: convert whatever we got
                                final_bytes = await _convert_draw_to_gemini_async(
                                    content, ct, True
                                )
                                yield final_bytes.decode("utf-8", errors="ignore")
                                return
                            aiter = resp.aiter_lines()
                            last_emit = 0.0
                            sent_content = False
                            # initial keep-alive to open stream immediately
                            yield _gemini_keepalive_chunk()
                            last_emit = time.time()
                            while True:
                                try:
                                    line = await asyncio.wait_for(aiter.__anext__(), timeout=1)
                                except asyncio.TimeoutError:
                                    # keep-alive ping
                                    now = time.time()
                                    if now - last_emit >= 2:
                                        yield _gemini_keepalive_chunk()
                                        last_emit = now
                                    continue
                                except StopAsyncIteration:
                                    break
                                if not line.startswith("data:"):
                                    # Some upstreams send JSON without data: prefix
                                    try:
                                        raw = line.strip()
                                        if raw.startswith('{'):
                                            obj = json.loads(raw)
                                            draw_id = None
                                            if isinstance(obj, dict):
                                                if isinstance(obj.get("data"), dict):
                                                    draw_id = obj["data"].get("id")
                                                draw_id = draw_id or obj.get("id")
                                            if draw_id:
                                                status_box = {}
                                                async for chunk in poll_and_send(draw_id, status_box):
                                                    yield chunk
                                                if status_box.get("done"):
                                                    return
                                                # stalled -> try another key
                                                if status_box.get("stalled") and attempts < 2:
                                                    new_key = await key_manager.reserve_key(cost=cost) if cost > 0 else key_manager.get_next_key(cost=cost)
                                                    if new_key:
                                                        logger.info("Retrying with another key ...%s", new_key[-6:])
                                                        current_key = new_key
                                                        continue
                                                if status_box.get("stalled"):
                                                    yield _gemini_error_chunk("draw result stalled")
                                                    return
                                            # If this is already a result payload, convert it
                                            if isinstance(obj, dict) and (obj.get('status') or obj.get('results') or obj.get('error')):
                                                final_bytes = await _convert_draw_to_gemini_async(
                                                    json.dumps(obj, ensure_ascii=False).encode(),
                                                    'application/json',
                                                    True
                                                )
                                                yield final_bytes.decode('utf-8', errors='ignore')
                                                return
                                    except Exception:
                                        pass
                                    continue
                                payload = line[5:].strip()
                                if not payload or payload == "[DONE]":
                                    continue
                                try:
                                    evt = json.loads(payload)
                                except Exception:
                                    continue
                                # detect draw id (some streams only return id)
                                draw_id = None
                                if isinstance(evt, dict):
                                    if isinstance(evt.get("data"), dict) and evt["data"].get("id"):
                                        draw_id = evt["data"].get("id")
                                # some streams wrap payload in {"code":0,"data":{...}}
                                if isinstance(evt, dict) and isinstance(evt.get("data"), dict):
                                    evt = evt["data"]
                                if isinstance(evt, dict) and evt.get("id"):
                                    draw_id = draw_id or evt.get("id")
                                if draw_id and not (isinstance(evt, dict) and (evt.get("status") or evt.get("results") or evt.get("error"))):
                                    status_box = {}
                                    async for chunk in poll_and_send(draw_id, status_box):
                                        yield chunk
                                    if status_box.get("done"):
                                        return
                                    # stalled -> try another key
                                    if status_box.get("stalled") and attempts < 2:
                                        new_key = await key_manager.reserve_key(cost=cost) if cost > 0 else key_manager.get_next_key(cost=cost)
                                        if new_key:
                                            logger.info("Retrying with another key ...%s", new_key[-6:])
                                            current_key = new_key
                                            continue
                                    if status_box.get("stalled"):
                                        yield _gemini_error_chunk("draw result stalled")
                                        sent_content = True
                                        return
                                # progress updates
                                progress = evt.get("progress")
                                status = evt.get("status")
                                has_results = isinstance(evt.get("results"), list) and len(evt.get("results")) > 0
                                has_error = bool(evt.get("error"))
                                if isinstance(progress, int):
                                    # no progress output; keep-alive handled by timeout
                                    now = time.time()
                                    if now - last_emit >= 2:
                                        yield _gemini_keepalive_chunk()
                                        last_emit = now
                                # final success/fail
                                if status in ("succeeded", "failed") or has_results or has_error:
                                    try:
                                        results = evt.get("results") or []
                                        first_url = ""
                                        if isinstance(results, list) and results:
                                            r0 = results[0] if isinstance(results[0], dict) else {}
                                            first_url = r0.get("url") or ""
                                        logger.info(
                                            "Draw stream final status=%s has_results=%s results_count=%s first_url=%s error=%s",
                                            status,
                                            bool(results),
                                            len(results) if isinstance(results, list) else 0,
                                            first_url[:200],
                                            str(evt.get("error") or evt.get("failure_reason") or ""),
                                        )
                                    except Exception:
                                        pass
                                    if status == "failed" or has_error:
                                        await _refund_key_once(current_key)
                                    final_bytes = await _convert_draw_to_gemini_async(
                                        json.dumps(evt, ensure_ascii=False).encode(),
                                        "application/json",
                                        True
                                    )
                                    yield final_bytes.decode("utf-8", errors="ignore")
                                    sent_content = True
                                    break
                            if not sent_content:
                                # Fallback: retry without alt=sse to force JSON id then poll
                                try:
                                    fb_try = 0
                                    while fb_try < 2:
                                        fb_try += 1
                                        fallback_headers = dict(forward_headers)
                                        fallback_headers.pop("content-length", None)
                                        fallback_headers["authorization"] = f"Bearer {current_key}"
                                        resp2 = await client.post(target_url_nostream, headers=fallback_headers, content=body)
                                        ct2 = resp2.headers.get("content-type", "")
                                        content2 = await resp2.aread()
                                        try:
                                            obj2 = json.loads(content2)
                                        except Exception:
                                            obj2 = None
                                        if _is_credit_error(obj2):
                                            logger.info("Upstream credits not enough for key ...%s", current_key[-6:])
                                            try:
                                                await key_manager.refresh_credits(current_key)
                                            except Exception:
                                                pass
                                            await _refund_key_once(current_key)
                                            if fb_try < 2:
                                                new_key = await key_manager.reserve_key(cost=cost) if cost > 0 else key_manager.get_next_key(cost=cost)
                                                if new_key:
                                                    logger.info("Retrying with another key ...%s", new_key[-6:])
                                                    current_key = new_key
                                                    continue
                                            yield _gemini_error_chunk("apikey credits not enough")
                                            return
                                        draw_id2 = None
                                        if isinstance(obj2, dict):
                                            if isinstance(obj2.get("data"), dict):
                                                draw_id2 = obj2["data"].get("id")
                                            draw_id2 = draw_id2 or obj2.get("id")
                                        if draw_id2:
                                            status_box = {}
                                            async for chunk in poll_and_send(draw_id2, status_box):
                                                yield chunk
                                            if status_box.get("done"):
                                                return
                                        final_bytes = await _convert_draw_to_gemini_async(
                                            content2, ct2, True
                                        )
                                        yield final_bytes.decode("utf-8", errors="ignore")
                                        return
                                except Exception:
                                    pass
                                yield _gemini_error_chunk("empty stream")
                            return
            except Exception as exc:
                logger.error("Gemini mapped stream error: %s", repr(exc))
                await _refund_key_once(current_key)
                yield _gemini_error_chunk("stream failed")

        return StreamingResponse(stream_gemini_from_draw(), media_type="text/event-stream")
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
        if cost > 0 and selected_key:
            await key_manager.refund_credits(selected_key, cost)
        return Response(
            content=b'{"error": "Upstream request failed"}',
            status_code=502,
            media_type="application/json",
        )
    logger.info("Upstream content-type (normal): %s", upstream_resp.headers.get("content-type", "") or "<missing>")
    # Log draw result summary for non-stream responses
    if "/v1/draw/" in path or path.startswith("v1/draw/"):
        try:
            ct = upstream_resp.headers.get("content-type", "")
            if "application/json" in ct:
                obj = json.loads(upstream_resp.content)
                d = obj.get("data") if isinstance(obj.get("data"), dict) else obj
                results = d.get("results") if isinstance(d, dict) else []
                first_url = ""
                if isinstance(results, list) and results:
                    r0 = results[0] if isinstance(results[0], dict) else {}
                    first_url = r0.get("url") or ""
                logger.info(
                    "Draw result status=%s has_results=%s results_count=%s first_url=%s error=%s",
                    d.get("status") if isinstance(d, dict) else None,
                    bool(results),
                    len(results) if isinstance(results, list) else 0,
                    first_url[:200],
                    str(d.get("error") or d.get("failure_reason") or obj.get("msg") or ""),
                )
        except Exception:
            pass

    # Pre-deducted credits: keep on success, refund on failure
    if cost > 0 and selected_key:
        should_keep = upstream_resp.status_code == 200
        credit_err = False
        if "/v1/draw/" in path or path.startswith("v1/draw/"):
            should_keep = should_keep and _check_draw_succeeded(
                upstream_resp.content,
                upstream_resp.headers.get("content-type", "")
            )
            ct = upstream_resp.headers.get("content-type", "")
            if "application/json" in ct:
                try:
                    credit_err = _is_credit_error(json.loads(upstream_resp.content))
                except Exception:
                    credit_err = False
        if credit_err:
            try:
                await key_manager.refresh_credits(selected_key)
            except Exception:
                pass
            logger.info("Key ...%s not refunded due to credit error", selected_key[-6:])
        elif not should_keep:
            await key_manager.refund_credits(selected_key, cost)

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
    # Convert draw response to OpenAI chat completion format if needed
    if is_openai_chat:
        content = _convert_draw_to_openai_chat(
            content,
            upstream_resp.headers.get("content-type", ""),
            openai_chat_stream,
            model,
        )
        response_headers["content-type"] = "text/event-stream" if openai_chat_stream else "application/json"
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
