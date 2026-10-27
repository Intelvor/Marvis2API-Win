#!/usr/bin/env python3
"""
uskey_provider — 为 bridge 提供每请求唯一的 uskey（Windows 版，纯随机生成）

背景：uskey 是请求头 Ual-Access-MarvisExt 里的设备指纹字段。上游只校验它「每请求唯一」，
不校验内容签名，因此本地随机生成即可通过。Windows 客户端的 uskey 前缀是 CsDVDCER
（macOS 为 CiDVDCER），前缀写错会被边缘层风控拦下（403 / 4100404）。

服务：
  GET /uskey   -> {"uskey": "CsDVDCER...", "len": 976, "source": "random", "ts": "..."}
  GET /health  -> {"status": "ok", "mode": "random", ...}

用法：
  python uskey_provider.py                       # 读 config.toml，默认监听 :4607
  python uskey_provider.py --port 4607
"""
from __future__ import annotations

import argparse
import base64
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
import uvicorn

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"

# Windows 客户端实测前缀；其它平台可改配置 uskey_random_prefix
DEFAULT_PREFIX = "CsDVDCER"

app = FastAPI(title="Marvis uskey provider", version="1.0.0")
STATE: dict[str, Any] = {"cfg": {}, "uskey": None, "ts": None}


def gen_random(prefix: str = DEFAULT_PREFIX) -> str:
    """生成 uskey：固定前缀 + 随机尾串。
    长度对齐真实客户端的量级（约 970 字符），服务端不要求固定长度。"""
    tail = base64.b64encode(os.urandom(240)).decode().rstrip("=")
    return prefix + tail


def make_uskey(cfg: dict) -> str:
    m = cfg.get("marvis", {})
    return gen_random(m.get("uskey_random_prefix", DEFAULT_PREFIX))


# ---------------------------------------------------------------- http
@app.get("/uskey")
async def uskey_endpoint():
    u = make_uskey(STATE["cfg"])
    STATE.update(uskey=u, ts=time.strftime("%H:%M:%S"))
    return {"uskey": u, "len": len(u), "source": "random", "ts": STATE["ts"]}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mode": "random",
        "last_len": len(STATE["uskey"]) if STATE.get("uskey") else None,
        "last_ts": STATE.get("ts"),
    }


def load_config(path: Path) -> dict:
    if path.exists():
        with path.open("rb") as f:
            return tomllib.load(f)
    return {}


def main():
    ap = argparse.ArgumentParser(description="Marvis uskey provider (Windows / random)")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    m = cfg.get("marvis", {})
    host = args.host or m.get("uskey_provider_host", "127.0.0.1")
    port = args.port or int(m.get("uskey_provider_port", 4607))

    STATE["cfg"] = cfg
    print(f"[provider] uskey_provider on http://{host}:{port}/uskey  mode=random", flush=True)

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
