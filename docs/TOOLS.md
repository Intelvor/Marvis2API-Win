# tools/ 工具索引

按用途分类。**日常只需要 `start.ps1`**，其余是逆向过程留存与排错工具。

---

## 一、生产在用（日常）

| 文件 | 用途 |
|---|---|
| `tools/marvis_auto.py` | **注入通道**，端口 4512。Frida 注入 `MarvisAgent`，捕获凭证、提供 OpenAI 接口、403 自动刷新重试 |
| `tools/marvis_hub.py` | **监控面板 + 接口聚合**，端口 4800。WebUI、v3 透传、状态诊断 |
| `tools/auto-start-admin.ps1` | 启动注入通道（`start.ps1` 会调用） |
| `tools/win_focus.py` | Win32 强制窗口置前（CDP 点击前必须，否则拿不到焦点） |
| `tools/cdp_input.py` | CDP 输入框侦察与自动发送（`--list` / `--send`） |
| `tools/restart-agent-admin.ps1` | 重启 `MarvisAgent`，清除注入残留 |

---

## 二、凭证与指纹

| 文件 | 用途 |
|---|---|
| `tools/scan-token-admin.ps1` | 扫描 `MarvisAgent` 进程内存，提取 openid / access_token / guid |
| `tools/scan-token-fresh-admin.ps1` | 同上，强制取最新的一份 |
| `tools/pick_token.py` | 内存里常残留多个旧 token，逐个探测上游挑出有效的那个 |
| `tools/sync_fingerprint.py` | 同步 `osVersion` / `clientVersion` 到真实值（`--check` 只检查） |
| `tools/uskey_dll.py` | 调用 `beacon_wrapper.dll` 生成 524 字符真 uskey |
| `tools/apply_creds.py` | 把扫描到的凭证写进 `config.toml` |

> 客户端升级后这几样必失效，顺序：`scan-token` → `sync_fingerprint` → `pick_token`。

---

## 三、抓包与逆向（排查用）

| 文件 | 用途 |
|---|---|
| `tools/hook_evp.py` | **主力抓包**：hook `libcrypto-3!EVP_CipherUpdate` 拿 TLS 明文 |
| `tools/hook_full.py` | 双向 TLS 明文完整落盘（`tools/full.jsonl`），不截断 |
| `tools/hook_chain.py` | 追踪请求去向：hook `connect` / `send` / `WSASend` / EVP |
| `tools/hook_svr_recv.py` | hook `MarvisSvr` 的 recv / SSL_read / SSL_write |
| `tools/hook_v3.py` | 专抓 `/v3/*` 请求的真实明文头 |
| `tools/dump_values.py` | 按值形态扫进程内存（支持 `--find-hex`） |
| `tools/dump_headers.py` | 从抓包结果里提取请求头 |
| `tools/ual_capture.py` | mitmproxy 插件，记录完整请求头 |
| `tools/marvis_gateway.py` | 经 CDP 取 gateway token 与服务端口表 |
| `tools/cdp_probe.py` | 在渲染进程执行任意 JS |
| `tools/cdp_netspy.py` | 监听渲染进程网络请求 |

各 hook 的启动脚本：`*-start-admin.ps1`（都需要管理员）。

---

## 四、注入执行

| 文件 | 用途 |
|---|---|
| `tools/inject_py.py` | 在 `MarvisAgent` 进程内执行任意 Python 代码 |
| `tools/inject_seq.py` | 进程内按顺序连发多次请求 |
| `tools/inject_body.py` | 请求体就地替换（第一条打通的路径） |
| `tools/inject_free.py` | 凭证复用验证（证明同凭证可反复用） |

---

## 五、接口探测

| 文件 | 用途 |
|---|---|
| `tools/v3_probe.py` | 实测 `/v3/*` 各接口可用性 |
| `tools/probe_models.py` | 尝试从 `model_gateway` 拿模型列表（未成功） |
| `tools/probe_agent.py` | 探测 Agent 侧接口 |

---

## 六、环境辅助

| 文件 | 用途 |
|---|---|
| `tools/start-cdp-admin.ps1` | 带 `--remote-debugging-port=9222` 重启客户端 |
| `tools/proxy-restore-admin.ps1` | 还原系统代理 + 移除 mitm 根证书 |
| `tools/dump-headers-admin.ps1` | `dump_headers.py` 的启动包装 |

---

## 七、实验留存（已证明失败，仅供复现）

这些是排查 4100404 时逐个试过的方案。**不要再跑**，保留是为了文档里那些对照数据可复现。

| 文件 | 试的是什么 | 结论 |
|---|---|---|
| `inject_pair.py` | 成对请求（先 memory 再 agent_chat，共用 response-id） | 两步都 403 |
| `inject_diff.py` | 字节级 header diff | 除 content-length 外逐字段一致，仍 403 |
| `inject_clone.py` | 克隆 Request extensions / stream:true 对照 | 均 403 |
| `inject_probe2.py` | body 裁剪二分 | 全部 403（因当时刷新了 response-id） |
| `inject_probe3.py` | **唯一变量对照**（这一组才定位到真因） | 原 id → 200；新 id → 403 |
| `inject_api.py` | 进程内 OpenAI 入口 + 中继 | 中继 403 |
| `marvis_bridge.py` | CDP 搭载 + 就地换 body | 可用但每次需搭载 |
| `marvis_free.py` | 免搭载版（`marvis_auto` 的前身） | 可用 |

---

## 常见问题

**Q: 改了 `marvis_auto.py` 但行为没变？**
4512 端口被 `MarvisAgent` 占着说明跑的是旧代码。重启 Agent：
`tools/restart-agent-admin.ps1`，或直接 `.\start.ps1 -Restart`。

**Q: 提示「拒绝访问」杀不掉进程？**
`MarvisAgent` 以 SYSTEM 权限运行，必须管理员。`start.ps1` / `stop.ps1` 会自动提权。

**Q: 注入后 `RecursionError`？**
多次注入导致 `_marvis_orig_send` 被自我覆盖。重启 Agent 是唯一可靠解法。

**Q: 凭证一直刷新失败？**
检查 CDP（9222）是否可访问、客户端窗口是否被最小化。面板会给出对应提示。
