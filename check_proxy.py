# -*- coding: utf-8 -*-
import re

content = open('C:/Users/Administrator/GrsaiProxyManager/proxy.py', encoding='utf-8').read()

# Find and replace the proxy_request function's httpx client section
# Add streaming support for Gemini SSE

old = '''    try:
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
    # 清리 Gemini 응答중의 sdkHttpResponse 字段
    if \'generateContent\' in path or \'streamGenerateContent\' in path:
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
    )'''

print('length:', len(content))
print('found old:', old[:50] in content)
