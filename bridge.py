#!/usr/bin/env python3
"""
Marvis Bridge — 把腾讯 Marvis 桌面助手的私有 OpenAI 兼容接口，暴露为标准 OpenAI 网关。

上游：POST https://yybadaccess.3g.qq.com/v1/chat/completions（标准 OpenAI 协议）
鉴权：Ual-Access-* 头（复用本机已登录客户端的 access_token），无需签名/验证码。

用法：
    python bridge.py                  # 读取同目录 config.toml
    python bridge.py --port 8000
然后任意 OpenAI 客户端指向 http://127.0.0.1:8000/v1 即可。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"

app = FastAPI(title="Marvis Bridge", version="1.0.0")
CFG: dict[str, Any] = {}

# 缓存的最近一次 uskey（provider 不可达时兜底；uskey 每请求一变）
LIVE: dict[str, str | None] = {"uskey": None, "updated_at": None}


# ---------------------------------------------------------------- config
def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def _fetch_provider_uskey() -> str | None:
    """从 uskey provider 取一个全新的 uskey（每请求唯一）。"""
    m = CFG["marvis"]
    host = m.get("uskey_provider_host", "127.0.0.1")
    port = int(m.get("uskey_provider_port", 4607))
    url = f"http://{host}:{port}/uskey"
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=3.0)) as c:
            r = c.get(url)
            r.raise_for_status()
            u = r.json().get("uskey")
            if u:
                LIVE["uskey"] = u
                LIVE["updated_at"] = time.strftime("%H:%M:%S")
            return u
    except Exception as e:  # noqa
        print(f"[bridge] provider uskey 获取失败 ({url}): {e}", file=sys.stderr, flush=True)
        return None


def live_uskey() -> str | None:
    """当前 uskey：优先向 provider 取新值，失败时用上次缓存值兜底。"""
    return _fetch_provider_uskey() or LIVE["uskey"] or CFG["marvis"].get("uskey")


def live_token() -> str:
    return CFG["marvis"]["access_token"]


def live_openid() -> str:
    return CFG["marvis"]["openid"]


def builtin_headers(cfg: dict) -> dict[str, str]:
    """构造 Marvis 上游要求的鉴权头（抓包还原）。
    uskey 每请求取新值；qimei36 与 guid 是两个不同的设备标识，勿复用。"""
    m = cfg["marvis"]
    ext = {
        "qimei36": m.get("qimei36") or m["device_guid"],
        "osVersion": m["os_version"],
        "guid": m["device_guid"],
        "uskey": live_uskey() or "",
        "nonce": str(uuid.uuid4()),
        "reqScene": m.get("req_scene", 1),
        "taskType": "user",
        "clientVersion": m["client_version"],
    }
    # Windows 版客户端不发送 platform 字段；仅当显式配置时才带上
    if m.get("client_platform"):
        ext["clientPlatform"] = m["client_platform"]
        ext["clientPlatformVersion"] = m["client_platform_version"]
    ext["isIoa"] = False
    rid = "resp_" + uuid.uuid4().hex
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        # 与真实客户端一致的 UA（服务端可能据此识别）
        "User-Agent": "AsyncOpenAI/Python 2.32.0",
        "X-Stainless-Lang": "python",
        "X-Stainless-Package-Version": "2.32.0",
        "X-Stainless-OS": m.get("stainless_os", "Windows"),
        "X-Stainless-Arch": m.get("stainless_arch", "x64"),
        "X-Stainless-Runtime": "CPython",
        "X-Stainless-Runtime-Version": "3.11.9",
        "X-Stainless-Async": "async:asyncio",
        "X-Stainless-Raw-Response": "true",
        "x-stainless-retry-count": "0",
        "x-stainless-read-timeout": "600.0",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        # 占位 Authorization（真实鉴权走 Ual-Access-*）
        "Authorization": "Bearer placeholder",
        # ---- 核心鉴权 ----
        "Ual-Access-Openid": live_openid(),
        "Ual-Access-Access-Token": live_token(),
        "Ual-Access-Login-Type": m["login_type"],
        "Ual-Access-Guid": m["device_guid"],
        "X-Device-GUID": m["device_guid"],
        "Ual-Access-MarvisExt": json.dumps(ext, ensure_ascii=False, separators=(",", ":")),
        # ---- 场景/关联 ----
        "Marvis-AgentTag": "main",
        "Marvis-Scene": m.get("scene", "agent_chat"),
        "Marvis-Protocol-Version": m.get("protocol_version", "1000"),
        "Marvis-Conversation-ID": "conv_" + uuid.uuid4().hex,
        "Marvis-Response-ID": rid,
        "Ual-Access-Requestid": rid,
    }


# ---------------------------------------------------------------- routes
@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """Marvis 不暴露模型注册表（云端返回空），这里给出协议实测到的可用模型。"""
    now = int(time.time())
    models = [
        # 主对话（服务端默认路由）
        ("main-auto", "auto", "Marvis 主对话（服务端路由）"),
        ("default", "auto", "Marvis 默认档"),
        # 实测上游真实模型（供显式指定时尝试）
        ("ark_deepseek-v4-flash-ga-260731", "deepseek", "DeepSeek V4 Flash (火山ARk)"),
        ("hy3", "hunyuan", "腾讯混元 Hy3"),
        ("deepseek-v4-pro-external", "deepseek", "DeepSeek V4 Pro (external)"),
    ]
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": mid, "object": "model", "created": now, "owned_by": owner, "description": desc}
                for mid, owner, desc in models
            ],
        }
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    return await _proxy_chat(body)


@app.post("/chat/completions")  # 兼容不带 /v1 的客户端
async def chat_completions_nov1(request: Request):
    body = await request.json()
    return await _proxy_chat(body)


async def _proxy_chat(body: dict) -> Any:
    cfg = CFG["marvis"]
    headers = builtin_headers(CFG)
    url = cfg["base_url"].rstrip("/") + cfg["chat_path"]

    # 补齐关键参数（默认与真实客户端一致）
    body.setdefault("model", cfg.get("default_model", "main-auto"))
    body.setdefault("stream", True)
    body.setdefault("temperature", 1)
    body.setdefault("max_tokens", 32000)
    body.setdefault("thinking_enabled", True)
    if body.get("stream"):
        body.setdefault("stream_options", {"include_usage": True})

    if CFG.get("marvis", {}).get("debug"):
        print(f"[bridge] -> {url} model={body.get('model')} stream={body.get('stream')} "
              f"msgs={len(body.get('messages', []))} tools={len(body.get('tools', []))}", flush=True)

    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
    if not body.get("stream"):
        resp = await client.post(url, headers=headers, json=body)
        await client.aclose()
        return _to_openai_response(resp)

    async def gen():
        try:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    raw = await resp.aread()
                    err = raw.decode("utf-8", "replace")
                    print(f"[bridge] upstream {resp.status_code}: {err[:500]}", file=sys.stderr, flush=True)
                    yield f"data: {json.dumps({'error': {'message': err, 'type': 'upstream_error', 'code': resp.status_code}}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                # 原样透传上游 SSE（已是标准 OpenAI 格式）
                async for line in resp.aiter_lines():
                    if line == "":
                        continue
                    yield line + "\n\n"
        finally:
            await client.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


def _to_openai_response(resp: httpx.Response) -> JSONResponse:
    try:
        data = resp.json()
    except Exception:
        data = {"error": {"message": resp.text, "type": "upstream_error", "code": resp.status_code}}
    return JSONResponse(data, status_code=resp.status_code)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "upstream": CFG["marvis"]["base_url"] + CFG["marvis"]["chat_path"],
        "uskey_source": "provider",
        "uskey_live": bool(LIVE["uskey"]),
        "uskey_len": len(LIVE["uskey"] or "") if LIVE["uskey"] else None,
        "uskey_updated_at": LIVE["updated_at"],
    }


@app.get("/uskey")
async def uskey_status():
    """查看当前凭证状态（便于排障）。"""
    u = LIVE["uskey"] or ""
    return {
        "has_uskey": bool(u),
        "uskey_prefix": u[:12],
        "uskey_len": len(u),
        "updated_at": LIVE["updated_at"],
        "openid": live_openid(),
        "token_tail": live_token()[-6:],
    }


# ---------------------------------------------------------------- main
def main():
    global CFG
    ap = argparse.ArgumentParser(description="Marvis Bridge")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    CFG = load_config(Path(args.config))
    host = args.host or CFG["marvis"]["host"]
    port = args.port or int(CFG["marvis"]["port"])

    print(f"[bridge] Marvis Bridge on http://{host}:{port}/v1")
    print(f"[bridge] upstream = {CFG['marvis']['base_url']}{CFG['marvis']['chat_path']}")
    print(f"[bridge] uskey provider = "
          f"{CFG['marvis'].get('uskey_provider_host', '127.0.0.1')}:"
          f"{CFG['marvis'].get('uskey_provider_port', 4607)}")

    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    CFG = load_config(CONFIG_PATH)
    main()
