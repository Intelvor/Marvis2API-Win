"""ual_capture.py — mitmproxy 插件：只记录发往 Marvis 上游的请求头

用法：
    mitmdump -s tools/ual_capture.py -p 18080 --set confdir=tools/.mitm

观察目标：yybadaccess.3g.qq.com 的 /v1/chat/completions
输出：tools/ual-real-headers.txt（完整请求头，用于与网关发出的头做逐字对比）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from mitmproxy import http

OUT = Path(__file__).resolve().parent / "ual-real-headers.txt"
HOST_MARK = "yybadaccess"
PATH_MARK = "/v1/chat/completions"


def _dump(flow: http.HTTPFlow) -> None:
    req = flow.request
    lines = [
        "=" * 72,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {req.method} {req.pretty_url}",
        "-" * 72,
    ]
    for k, v in req.headers.items():
        lines.append(f"{k}: {v}")
    # 记录 body 原文（签名依赖 body，必须逐字还原）
    try:
        body = req.get_text(strict=False) or ""
        if body:
            lines.append("--- BODY_RAW_BEGIN ---")
            lines.append(body)
            lines.append("--- BODY_RAW_END ---")
    except Exception:
        pass

    text = "\n".join(lines) + "\n"
    with OUT.open("a", encoding="utf-8") as f:
        f.write(text)
    print(text[:800], flush=True)


def request(flow: http.HTTPFlow) -> None:
    if HOST_MARK in flow.request.pretty_host:
        _dump(flow)
