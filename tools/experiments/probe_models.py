#!/usr/bin/env python3
"""probe_models.py — 尝试获取模型列表

线索：客户端启动时调 /v3/pcyyb_ai_assistant_model_gateway，
      body {"model":"qu_model","inputs":{"query":"$预加载#！~","tokens":"预加载"}}
      这个 body 看起来是让网关返回可用模型清单的探测请求。

/v3/* 接口需要 UAL 签名：
  signature = md5(body + timestamp_ms + access_key + nonce)
  access_key = fpzkeTlcGLVP37qiOWd29MyaOqaJYxMi
  Ual-Access-Businessid: pc_yyb（注意 chat 是 marvis_client，这里不同）

用法：python tools/probe_models.py
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ACCESS_KEY = "fpzkeTlcGLVP37qiOWd29MyaOqaJYxMi"
URL = "https://yybadaccess.3g.qq.com/v3/pcyyb_ai_assistant_model_gateway"


def load_cfg() -> dict:
    import tomllib
    return tomllib.load(open(ROOT / "config.toml", "rb"))["marvis"]


def signed_headers(cfg: dict, body: str, businessid: str) -> dict:
    ts = str(int(time.time() * 1000))
    nonce = str(random.randint(1000000000, 9999999999))
    raw = (body + ts + ACCESS_KEY + nonce).encode()
    sig = hashlib.md5(raw).hexdigest()
    return {
        "Host": "yybadaccess.3g.qq.com",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, zstd",
        "Connection": "keep-alive",
        "User-Agent": "python-httpx/0.28.1",
        "Ual-Access-Requestid": __import__("uuid").uuid4().hex,
        "Ual-Access-Businessid": businessid,
        "Ual-Access-Guid": cfg["device_guid"],
        "Ual-Access-Timestamp": ts,
        "Ual-Access-Nonce": nonce,
        "Ual-Access-Signature": sig,
        "Content-Type": "application/json; charset=UTF-8",
    }


def try_one(label: str, body_obj: dict, businessid: str, cfg: dict) -> None:
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    h = signed_headers(cfg, body, businessid)
    try:
        r = httpx.post(URL, headers=h, content=body.encode(), timeout=30)
        txt = r.text[:900]
        print(f"[{r.status_code}] {label}")
        print(f"       body={body[:90]}")
        print(f"       -> {txt[:400]}")
    except Exception as e:
        print(f"[EXC] {label}: {type(e).__name__}: {str(e)[:120]}")
    print()


def main() -> None:
    cfg = load_cfg()
    print(f"guid={cfg['device_guid']}\n")
    cases = [
        ("原样复刻（qu_model）",
         {"model": "qu_model", "inputs": {"query": "$预加载#！~", "tokens": "预加载"}},
         "pc_yyb"),
        ("businessid 改 marvis_client",
         {"model": "qu_model", "inputs": {"query": "$预加载#！~", "tokens": "预加载"}},
         "marvis_client"),
        ("问模型清单",
         {"model": "qu_model", "inputs": {"query": "模型列表", "tokens": "模型列表"}},
         "pc_yyb"),
        ("空 inputs",
         {"model": "qu_model", "inputs": {}},
         "pc_yyb"),
        ("只要 model",
         {"model": "qu_model"},
         "pc_yyb"),
    ]
    for label, body, bid in cases:
        try_one(label, body, bid, cfg)
        time.sleep(1.2)


if __name__ == "__main__":
    main()
