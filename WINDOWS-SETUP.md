# Marvis2API — Windows 从零部署手册

本手册只讲 **Windows 系统** 的完整落地流程：装环境 → 取凭证 → 写配置 → 启动 → 验收 → 日常维护。
全部脚本都是 `.ps1` / `.py`，不需要 macOS、不需要 dylib、不需要抓包证书。

> 本项目只支持 Windows（要求本机已安装并登录 Marvis 客户端）。
> 下面每一步都可直接复制粘贴执行。

---

## 一、你需要什么

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10/11 x64 | 本手册在 Windows 11 26200 + Python 3.14 实测通过 |
| Python | 3.11+（含 `tomllib`） | 验证：`python --version` |
| 依赖 | `fastapi` `uvicorn` `httpx` | 一键装：`python -m pip install -r requirements.txt` |
| Marvis 客户端 | 已安装并**已登录** | 凭证从这里来，见「第三步」 |
| 管理员权限 | 取 token 时需要 | 其它步骤普通用户即可 |

---

## 二、安装依赖

```powershell
python -m pip install -q -r requirements.txt
```

验证：

```powershell
python -c "import fastapi, uvicorn, httpx; print('ok')"
```

---

## 三、取凭证（关键一步）

网关要向上游证明「你就是本机已登录的那个 Marvis 用户」，需要 4 个值：

| 字段 | 含义 | 形态 |
|---|---|---|
| `openid` | 登录账号标识 | `o` 开头，约 28 字符 |
| `access_token` | 登录态硬凭据 | `mv_` 开头，约 30 字符 |
| `device_guid` | 客户端实例 ID | 32 位 hex |
| `qimei36` | 设备指纹（Qimei SDK） | **40 位** hex，与 `device_guid` 是两个不同的值 |

其中 `openid` / `device_guid` / `qimei36` 落盘在客户端数据目录、可免提权读取；
**只有 `access_token` 不落明文盘**，它只存在于 `MarvisSvr.exe`（SYSTEM 服务）的内存里，需要管理员权限才能读到。

### 方式 A：一条命令自动取（推荐）

```powershell
.\tools\scan-token-admin.ps1
```

它会：提权扫描 `MarvisSvr.exe` 内存 → 取 `mv_` token → 自动发现 openid / guid / qimei36 → 写进 `config.toml` → 重启服务 → 打一次上游验证。

- 会弹 UAC，点「是」。
- 全程约 20 秒。输出末尾 `[test] HTTP 200` 即成功。
- 如果提示「未找到 access_token」，先在 Marvis 客户端里**发一句话**（让 token 进入内存）再重跑。

参数：

```powershell
.\tools\scan-token-admin.ps1 -NoApply     # 只扫描、打印候选 token，不改配置
.\tools\scan-token-admin.ps1 -AllProcs    # 连 GUI 进程一起扫（很慢，一般用不到）
.\tools\scan-token-admin.ps1 -ProcessName MarvisSvr2.exe              # 版本改名时显式指定进程
.\tools\scan-token-admin.ps1 -MarvisDir "D:\Data\Tencent\Marvis"      # 数据目录非标准位置时指定
```

> **安装目录在哪不影响。** 扫描器按进程**映像名**匹配（`tasklist`），不读安装路径；
> 凭证目录由 `%APPDATA%` 环境变量定位。装在 `C:\`、`D:\` 还是其它盘符都一样。

### 方式 B：手工抓包后写入

从已登录客户端的请求头里抄出 4 个值（`Ual-Access-Openid` / `Ual-Access-Access-Token` / `Ual-Access-Guid` / `Ual-Access-MarvisExt` JSON 里的 `qimei36`），然后：

```powershell
.\set-credentials.ps1 -Openid o_xxx -Token mv_xxx -Guid xxxxxxxxxxxxxxxx -Qimei36 40位指纹 -Test
```

---

## 四、启动与验收

```powershell
.\deploy.ps1            # 启动 provider(:4607) + bridge(:4605)
.\acceptance.ps1 10     # 连续 10 次请求，全部 200 = PASS
```

看到这个即成功：

```
== 结果 ==
  200: 10 / 10   (序列: 200 200 200 200 200 200 200 200 200 200)
  VERDICT: PASS
```

任何 OpenAI 兼容客户端指向：

```
Base URL : http://127.0.0.1:4605/v1
Model    : main-auto
```

命令行自测：

```powershell
curl -s http://127.0.0.1:4605/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"main-auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}],\"stream\":false}'
```

---

## 五、日常维护

```powershell
.\undeploy.ps1                 # 停止
.\deploy.ps1                   # 启动
.\acceptance.ps1 5             # 快速体检
.\tools\scan-token-admin.ps1   # token 过期时刷新凭证（会弹 UAC）
```

---

## 六、文件清单

| 文件 | 作用 |
|---|---|
| `bridge.py` | OpenAI 兼容网关（:4605） |
| `uskey_provider.py` | uskey 生成服务（:4607，随机模式） |
| `config.example.toml` | 配置模板 |
| `config.toml` | 实际配置（含真实凭证，**已被 gitignore，勿提交**） |
| `deploy.ps1` | 一键启动 |
| `undeploy.ps1` | 一键停止 |
| `acceptance.ps1` | 验收（连续 N 次 200） |
| `set-credentials.ps1` | 手工写入凭证 |
| `tools/scan-token-admin.ps1` | 提权取 token 并自动写配置（一条命令收尾） |
| `tools/scan_proc_token.py` | 进程内存扫描器（被上面的 ps1 调用） |
| `tools/apply_creds.py` | 凭证写入 + 自动发现 + 自测（被上面的 ps1 调用） |
| `tools/dump_headers.py` | 从客户端内存提取真实请求头（排障用） |

整个仓库就这些，没有 macOS 脚本、没有 dylib、没有抓包证书。

---

## 七、排错对照表

| 现象 | 含义 | 处理 |
|---|---|---|
| `HTTP 200` | 正常 | — |
| `HTTP 401` + `code:4100403` | 登录态过期 / token 失效 | 跑 `.\tools\scan-token-admin.ps1` 刷新 |
| `HTTP 403` + `code:4100404` | 风控拦截 | 见下「风控排查」 |
| `provider 启动失败` | 端口被占 / python 不在 PATH | 先 `.\undeploy.ps1` 再启；检查 `python --version` |
| 扫描「未生成结果文件」 | UAC 被取消 | 重跑并点「是」 |
| 扫描「未找到 access_token」 | token 尚未进内存 | 先在客户端发一句话再重跑 |
| 扫描「未找到 MarvisSvr.exe」 | 版本改了进程名 | 脚本会自动改用其它 `Marvis*` 进程；仍不行用 `-ProcessName xxx.exe` |
| `[creds] 未找到 Marvis 用户数据目录` | `%APPDATA%` 被重定向 / 便携版 / 多用户 | 用 `-MarvisDir <路径>` 或设 `MARVIS_DATA_DIR`；数据目录与安装目录无关 |
| `param` 识别为 cmdlet | 脚本被改坏了（BOM 被插到 `param` 前） | 用 `git checkout -- *.ps1` 还原，或用 UTF-8 BOM 重存 |

### 风控排查（4100404 专项）

`4100404` 有两大类原因，**先确认不是字段问题再怀疑节奏**：

1. **字段错**（最常踩）：`Ual-Access-MarvisExt` 里任何一个值对不上真实客户端就会被边缘层秒拒（0.2s 内返回）。
   已知 Windows 客户端的正确值：

   | 字段 | Windows 正确值 |
   |---|---|
   | `osVersion` | `Windows-10-10.0.26200` |
   | `clientVersion` | `1.60.2600.614` |
   | `reqScene` | `1` |
   | uskey 前缀 | `CsDVDCER` |
   | `qimei36` | 40 位独立指纹（不是 `device_guid`） |
   | `clientPlatform` | Windows 版**不发送** |

   `config.example.toml` 已按 Windows 值写好；若你从别处拷贝了旧配置，核对这几项。

2. **节奏触发**：密集/重复请求会触发账号级自适应风控，冷却后自愈。
   `acceptance.ps1` 已内置 1.2s 间隔，**不要压测**；大样本连续请求必然 `4100404`。

---

## 八、原理速览（可选读）

```
OpenAI Client ──► bridge.py :4605 ──每请求取新 uskey──► uskey_provider.py :4607
                          │                                      │
                          │ Ual-Access-* 头                      │ 随机生成
                          ▼                                      │ 前缀 CsDVDCER
              https://yybadaccess.3g.qq.com/v1/chat/completions
```

上游鉴权只有三层：

1. `access_token`（硬凭据，失效 → `4100403`）
2. `uskey` 新鲜度（每请求唯一即可，内容当前不校验 → 本地随机生成，前缀必须是 `CsDVDCER`）
3. 请求节奏（账号级风控，密集 → `4100404`）

上游本身就是标准 OpenAI 协议，网关只拼鉴权头、原样透传，所以 SSE / `tool_calls` / 多模态都不需要转换。

---

## 九、注意事项

- `config.toml` 含真实凭证，已被 `.gitignore` 忽略，**不要提交、不要外发**。
- token 会过期，`4100403` 就是信号，刷新即可。
- 服务默认只监听 `127.0.0.1`，未暴露到局域网。
- 本项目仅用于**互操作性研究与学习**，请自行确认符合目标服务条款与当地法律。
