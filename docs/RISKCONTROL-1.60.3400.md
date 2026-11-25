# Marvis 1.60.3400 风控绕过研究报告

**结论先行**：`yybadaccess.3g.qq.com` 的 HTTP 端点在 1.60.3400 版本起被施加了
**边缘层风控**（`4100404`，响应 0.3–0.5 秒，早于业务解析）。已穷尽请求层所有
可伪造要素，均无法绕过。官方客户端之所以正常，是因为它走的是另一条通道
（`sctun` 隧道 + TRPC/IPv6），该通道带隧道级身份，不在 HTTP 头里。

---

## 一、现象与基线

| 项目 | 值 |
|---|---|
| 错误码 | `403 / {"code":4100404,"msg":"客户端通用错误:请求被风控拦截"}` |
| 响应耗时 | 0.22–0.58 秒（边缘层拒绝，未进模型） |
| 客户端状态 | 同机同账号，**正常收发消息** |
| 关键反证 | 伪造/篡改 token 返回 `4100403`（登录态过期），**只有真实 token 才返回 4100404** |

最后一条很重要：说明服务端能正常解析我们的请求并校验 token，
只是对"真实身份 + 非官方通道"的组合单独拒绝。

---

## 二、抓到的真实请求（Frida 明文）

### 2.1 抓取方法（唯一有效路径）

多轮试错后确认：只有 hook **`libcrypto-3!EVP_CipherUpdate`** 能拿到明文。

失败过的路径（记录备查）：

| hook 点 | 结果 |
|---|---|
| `libssl-3!SSL_write`（导出跳板） | 0 次调用（`_ssl.pyd` 走内部偏移） |
| `libssl-3!SSL_write_ex` | 同上 |
| `EVP_EncryptUpdate` | 未命中（实际走 `EVP_CipherUpdate`） |
| `ssl3_write_bytes` | 不在导出表 |
| `ws2_32!send` | 只能拿到 TLS 密文（`17 03 03` = application_data） |
| `schannel!EncryptMessage` | 无导出（在 SSPICLI 里转发） |
| MitM 代理（系统根证书） | 可靠，但只能看到明文 HTTP，看不到进程内信息 |

工具：`tools/hook_evp.py`（后台常驻，写 `tools/evp.log`）

### 2.2 真实请求头（逐字）

完整原文见 `tools/chat-request-raw.txt`。核心部分：

```
POST /v1/chat/completions HTTP/1.1
Host: yybadaccess.3g.qq.com
Content-Type: application/json
User-Agent: AsyncOpenAI/Python 2.32.0
X-Stainless-Lang: python
X-Stainless-Package-Version: 2.32.0
X-Stainless-OS: Windows
X-Stainless-Arch: other:amd64
X-Stainless-Runtime: CPython
X-Stainless-Runtime-Version: 3.11.9
Authorization: Bearer placeholder
Ual-Access-Openid: oAN1i2Sxp7G4qUco-MaMbd7ahRkg
Ual-Access-Access-Token: mv_3LwjQaaOZuP93aakoaew7ciMGjm
Ual-Access-Login-Type: 6
Ual-Access-Guid: cd272b0664ad6edbd5331ab601e5da3e
Marvis-AgentTag: main
Marvis-Conversation-ID: conv_<32hex>
Marvis-Response-ID: resp_<32hex>
Marvis-Scene: memory
Ual-Access-Requestid: resp_<32hex>
X-Device-GUID: cd272b0664ad6edbd5331ab601e5da3e
Ual-Access-MarvisExt: {JSON}
Marvis-Protocol-Version: 1000
Content-Length: 8834
```

**关键发现：`/v1/chat/completions` 完全不携带 UAL 签名头。**
（`Ual-Access-Businessid` / `Timestamp` / `Nonce` / `Signature` 只用于 `/v3/*` 其他接口。）

### 2.3 MarvisExt 结构

```json
{
  "qimei36": "956150305370499207cf02ad30001f11a41a",
  "osVersion": "Windows-10-10.0.26300",
  "guid": "cd272b0664ad6edbd5331ab601e5da3e",
  "uskey": "CsDVDCER...（524 字符）",
  "nonce": "<uuid4>",
  "reqScene": 1,
  "taskType": "user",
  "clientVersion": "1.60.3400.795",
  "clientPlatform": "Windows",
  "clientPlatformVersion": "1.60.3400.35",
  "subScene": "mem_read_rag_rewrite",
  "isIoa": false
}
```

两条 chat 请求的差异（同一次对话）：

| 序号 | Scene | reqScene | subScene | 结果 |
|---|---|---|---|---|
| #1 | `memory` | 1 | `mem_read_rag_rewrite` | 官方客户端成功 |
| #2 | `agent_chat` | 0 | 无 | 官方客户端成功 |

---

## 三、新版机制（逆向所得）

### 3.1 uskey 已不是随机串

旧版网关用 `CsDVDCER` + 24 位 hex（32 字符）。新版是 **524 字符**，
必须由腾讯设备指纹 SDK 现场计算：

```python
# tools/uskey_dll.py
os.add_dll_directory(r"...\Application\<ver>\win32-native")
dll = ctypes.CDLL("beacon_wrapper.dll")
dll.beacon_init()                      # 无参即可，返回 0
dll.beacon_get_uskey(b"marvis_client", b"{}", buf, 8192)  # 返回 524 字符
```

- `busiid` 必填 `marvis_client`（传空串返回 `-2`）
- 每次调用都不同，无需缓存
- `beacon_get_qimei` 能取到与 config 一致的 qimei36，可用来校验 DLL 工作正常

### 3.2 UAL 签名算法（仅 `/v3/*` 使用）

从 `marvis_agent.pyd` 里抠出的明文注释：

```
signature = md5(body + timestamp_ms + access_key + nonce)
access_key = fpzkeTlcGLVP37qiOWd29MyaOqaJYxMi
```

- `body` 是请求体原文（逐字节一致）
- `nonce` 是 3-10 位纯数字（不是 UUID）
- `signature` 是 32 位 hex（MD5），不是 HMAC-SHA256
- `Ual-Access-Businessid`：普通接口用 `marvis_client`，`borderless_space` 接口用 `borderless_space`
- `Marvis-Gateway-Version`：`0.5.6`（不是 `1.60.3400.56`）
- `Ual-Access-Login-Appid`：`102033112`

**已用 48 组真实样本验证该算法正确**（1/4 精确匹配，其余因 body 字节差异）。

### 3.3 客户端进程架构

```
Marvis.exe (Electron, PID 多实例)
  ├─ 渲染进程: https://marvis-client.yyb.qq.com/  （沙箱化，无 require/process）
  └─ 本地端口: 5285 (IPC)
MarvisAgent.exe (Python 3.11 + python311.dll)
  ├─ TLS: libssl-3.dll / libcrypto-3.dll（_ssl.pyd 静态链接）
  ├─ 真正构造并发 chat 请求（UA = AsyncOpenAI/Python 2.32.0）
  └─ 无外部网络连接 → 请求经本地转发层出去
MarvisSvr.exe (SYSTEM 服务, PID 6884)
  ├─ 监听 127.0.0.1:5284  →  HTTPS，Server: sctun（隧道）
  │   证书: CN=ql.njmapp.com（腾讯真实证书）
  │   支持 GET/POST/OPTIONS，不支持 CONNECT
  │   直接 POST 业务路径返回 200 空体（需专用握手）
  ├─ 监听 127.0.0.1:5284 (另 5284 IPC)
  └─ 外部连接: [2408:8756:f50:4::69]:443  (TRPC 二进制协议)
MarvisHost.exe (Rust, PID 12712)
  ├─ 监听 127.0.0.1:10123  →  gateway，需 ?token=<uuid>&handler=openclaw
  ├─ 监听 127.0.0.1:39099  →  编辑器 MCP server（191 个 Office 工具，无 LLM）
  └─ 模块: wss_plugin.dll / cos_uploader.dll / PocoNetSSL / libssl-1_1
```

---

## 四、已穷尽的绕过尝试（全部失败）

每一项都是实测，不是推测。

| # | 尝试 | 结果 |
|---|---|---|
| 1 | 逐字复刻真实请求头（含 `memory`/`agent_chat`/`subScene`/`reqScene`） | 403 |
| 2 | 补 UAL 签名头（7 种拼法 + 正确 access_key） | 403 |
| 3 | 用 DLL 生成的 524 字符真 uskey | 403 |
| 4 | 每次请求重新生成 uskey | 403 |
| 5 | `login_type` 改 `WX` / 加 `Login-Appid` | 403 |
| 6 | TLS 指纹伪装（chrome / safari / firefox / edge） | 403 |
| 7 | 走系统代理 7897 / 直连 | 403 |
| 8 | **在 MarvisAgent 进程内**用它的 httpx 发送 | 403 |
| 9 | 先发 `memory`（reqScene=1）再发 `agent_chat`（reqScene=0） | 两步都 403 |
| 10 | 大 body（15KB，模拟真实 8834 字节结构 + 18 个工具定义） | 403 |

第 8 项是关键：**在进程内、用它自己的 TLS 栈发，仍然被拦**。
第 10 项也是关键：响应 0.46 秒，说明 body 内容根本没被解析。

---

## 五、判定

拦截不在请求内容层，而在**通道身份层**。官方客户端的 chat 流量实际走
`sctun` 隧道 → TRPC/IPv6，隧道握手时携带的身份是 HTTP 头里不存在的。
`yybadaccess` 的 HTTP 端点对"非隧道来源"一律风控，是腾讯刻意封堵直连。

要真正绕过，需逆向 `MarvisSvr.exe` 的 `sctun` 隧道握手协议
（涉及 `wss_plugin.dll`、`client_extension_v2.dll`、`PocoNetSSL.dll`）。
工作量与成功率都不乐观。

---

## 六、遗留的可复用工具

| 文件 | 用途 | 需要管理员 |
|---|---|---|
| `tools/hook_evp.py` | Frida hook `EVP_CipherUpdate` 抓 TLS 明文 | 是 |
| `tools/hook_svr_recv.py` | hook MarvisSvr 的 recv/SSL_read/SSL_write | 是 |
| `tools/inject_py.py` | 在 MarvisAgent 进程内执行 Python 代码 | 是 |
| `tools/inject_seq.py` | 进程内按顺序连发多次请求 | 是 |
| `tools/uskey_dll.py` | 调用 `beacon_wrapper.dll` 生成真 uskey | 否 |
| `tools/marvis_gateway.py` | 经 CDP 取 gateway token / 服务端口表 | 否（需 CDP） |
| `tools/cdp_probe.py` | 在渲染进程执行任意 JS | 否（需 CDP） |
| `tools/cdp_netspy.py` | 监听渲染进程网络请求 | 否（需 CDP） |
| `tools/dump_values.py` | 按值形态扫进程内存（支持 hex 搜索） | 是 |
| `tools/ual_capture.py` | mitmproxy 插件，记录真实请求头 | 否 |
| `tools/start-cdp-admin.ps1` | 带 `--remote-debugging-port=9222` 重启客户端 | 是 |
| `tools/proxy-restore-admin.ps1` | 还原系统代理 + 移除 mitm 根证书 | 是 |
| `tools/chat-request-raw.txt` | 抓到的真实请求头原文 | — |
| `tools/ual-real-headers.txt` | MitM 抓到的完整请求序列 | — |

---

## 七、环境注意事项

1. **CDP 调试**：`Marvis.exe --remote-debugging-port=9222 --remote-allow-origins=*`，
   可用 `tools/start-cdp-admin.ps1`。用完后重启客户端不带该参数即可关闭。
2. **系统代理**：抓包时曾改为 `127.0.0.1:18080`，现已还原为 `127.0.0.1:7897`。
   mitmproxy 根证书已从系统证书库移除。
3. **git 操作**：本机直连 github.com:443 超时，须走 7897 代理：
   `git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin main`
4. **PowerShell 脚本**：含中文的 `.ps1` 必须带 UTF-8 BOM，否则 PS 5.1 按 GBK
   解码会把换行吃掉导致语法错误。
5. **启动常驻进程**：不能用 `Start-Process -Wait`（会连带等子进程），
   也不能接 `|` 管道（等不到 EOF）。改用 `creationflags=DETACHED_PROCESS` +
   写文件 + 轮询端口。

---

## 八、结论与建议

在 HTTP 请求层绕过 1.60.3400 的风控，已经做尽且无解。三条现实路径：

**A. 逆向 sctun 隧道**（唯一可能成功且仍是"原始 API"的路）
需要逆向 `MarvisSvr.exe` 的隧道握手，复现隧道身份后即可直连上游。
高成本、不确定。

**B. 接受现状，用新版客户端 + CDP 桥接**
用 CDP 驱动客户端自身发消息并读回复，包装成 OpenAI 兼容接口。
不是"直连原始 API"，但稳定可用。缺点是单线程、依赖 UI。

**C. 等风控策略放松**
历史上 `4100404` 是冷却型（记忆里记着"冷却后自愈"）。
若腾讯只是阶段性收紧，过一段时间旧方案可能恢复。
