#!/usr/bin/env python3
"""v3_probe.py — 实测 /v3/* 各接口可用性

已知签名算法（已用 get_wallet 验证 200）：
  signature = md5(body + ts_ms + access_key + nonce)
  access_key = fpzkeTlcGLVP37qiOWd29MyaOqaJYxMi
  必须头：Ual-Access-Openid / Access-Token / Login-Type / Guid /
          Businessid=marvis_client / Login-Appid=102033112 / Requestid /
          Nonce / Timestamp / Signature

用法：python tools/v3_probe.py
"""
from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ACCESS_KEY = "fpzkeTlcGLVP37qiOWd29MyaOqaJYxMi"
BASE = "https://yybadaccess.3g.qq.com"

CASES = [
    ("/v3/marvis_get_wallet", "{}", "余额"),
    ("/v3/marvis_list_conversation_token_stats",
     '{"conversation_ids":[]}', "会话 token 统计"),
    ("/v3/get_marvis_notice", '{"channel_id":"4100100002"}', "公告"),
    ("/v3/marvis_agentcore_tab_query_v1", '{}', "Agent 核心标签"),
    ("/v3/pcyyb_ai_assistant_create_embeddings",
     '{"model":"bge-small-zh-v1.5","input":["测试"]}', "向量嵌入"),
]


def call(cfg: dict, path: str, body: str) -> tuple[int, str]:
    ts = str(int(time.time() * 1000))
    nonce = str(random.randint(10 ** 9, 10 ** 10 - 1))
    sig = hashlib.md5((body + ts + ACCESS_KEY + nonce).encode()).hexdigest()
    h = {
        "Host": "yybadaccess.3g.qq.com",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, zstd",
        "Connection": "keep-alive",
        "User-Agent": "python-httpx/0.28.1",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Ual-Access-Openid": cfg["openid"],
        "Ual-Access-Access-Token": cfg["access_token"],
        "Ual-Access-Login-Type": cfg["login_type"],
        "Ual-Access-Guid": cfg["device_guid"],
        "Ual-Access-Businessid": "marvis_client",
        "Ual-Access-Login-Appid": "102033112",
        "Ual-Access-Requestid": uuid.uuid4().hex,
        "Ual-Access-Nonce": nonce,
        "Ual-Access-Timestamp": ts,
        "Ual-Access-Signature": sig,
    }
    r = httpx.post(BASE + path, headers=h, content=body.encode(), timeout=30)
    return r.status_code, r.text


def main() -> None:
    import tomllib
    cfg = tomllib.load(open(ROOT / "config.toml", "rb"))["marvis"]
    for path, body, name in CASES:
        try:
            st, txt = call(cfg, path, body)
            mark = " ✓" if st == 200 and '"code":0' in txt else ""
            print(f"[{st}] {name:16} {path}{mark}")
            print(f"      → {txt[:300]}")
        except Exception as e:
            print(f"[EXC] {name}: {type(e).__name__}: {str(e)[:100]}")
        print()
        time.sleep(1.0)


if __name__ == "__main__":
    main()
