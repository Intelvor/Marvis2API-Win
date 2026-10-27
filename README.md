# Marvis2API

把腾讯 **Marvis** 桌面助手的私有接口，封装成**标准 OpenAI 兼容网关**。

本机装好并登录了 Marvis 客户端，就能用任意 OpenAI 客户端（Cherry Studio、ChatBox、NextChat、
LangChain、OpenAI SDK……）调用它背后的模型，接口是标准 `/v1/chat/completions`。

**只支持 Windows。** 无需 macOS、无需 dylib、无需抓包证书。

---

## 快速开始

```powershell
# 1. 装依赖（Python 3.11+）
python -m pip install -r requirements.txt

# 2. 自动取凭证并写配置（会弹 UAC，点"是"）
.\tools\scan-token-admin.ps1

# 3. 启动
.\deploy.ps1

# 4. 验收
.\acceptance.ps1 10
```

看到 `VERDICT: PASS` 就好了。然后把客户端指向：

```
Base URL : http://127.0.0.1:4605/v1
API Key  : 随便填
Model    : main-auto
```

> 详细的分步说明、排错对照表、风控排查见 [`WINDOWS-SETUP.md`](./WINDOWS-SETUP.md)。

---

## 它是怎么工作的

```
                    ┌──────────────────────────────────┐
 OpenAI Client ────►│  bridge.py  :4605                │
                    │  OpenAI 兼容（透传上游协议）       │
                    └──────────────┬───────────────────┘
                                   │ 每请求取一个新 uskey
                                   ▼
                    ┌──────────────────────────────────┐
                    │  uskey_provider.py  :4607        │
                    │  生成随机 uskey（前缀 CsDVDCER）  │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
         https://yybadaccess.3g.qq.com/v1/chat/completions
```

上游本身就是标准 OpenAI 协议（含 SSE、`tool_calls`、`file`/`image`），网关只做鉴权头拼装和原样透传，
所以不会有格式转换损耗。

鉴权只有三层：

1. `access_token`（`mv_` 硬凭据，过期 → `401 / 4100403`）
2. `uskey` 新鲜度（每请求唯一即可，内容不校验 → 本地随机生成）
3. 请求节奏（账号级风控，密集 → `403 / 4100404`）

---

## 凭证从哪来

网关要向上游证明「你就是本机已登录的那个用户」，需要 4 个值：

| 字段 | 含义 | 怎么拿 |
|---|---|---|
| `openid` | 账号标识 | `%APPDATA%\Tencent\Marvis\User\<openid>\` 目录名 |
| `device_guid` | 客户端实例 ID | `%APPDATA%\Tencent\Marvis\db\bc_*.db` 的 `beacon_events` 表 |
| `qimei36` | 40 位设备指纹 | 同上（`A153` 字段），**与 guid 是两个不同的值** |
| `access_token` | 登录态硬凭据 | **只在 `MarvisSvr.exe` 内存里**，需管理员权限读 |

前三项落盘、可免提权读取；只有 `access_token` 需要 UAC 提权扫内存。
`.\tools\scan-token-admin.ps1` 把这四步全包了。

数据目录跟随 Windows 用户（`%APPDATA%`），**与客户端装在哪个盘无关**；
扫描也是按进程名匹配的。若你的数据目录被重定向到别处，用 `-MarvisDir <路径>` 指定。
一台机器登录过多个账号时，自动取最近登录的那个，也可用 `--openid` 显式指定。

---

## 命令一览

```powershell
.\deploy.ps1                     # 启动 provider + bridge
.\undeploy.ps1                   # 停止、释放端口
.\acceptance.ps1 10              # 连续 10 次请求验收
.\tools\scan-token-admin.ps1     # token 过期时刷新凭证（会弹 UAC）
.\tools\scan-token-admin.ps1 -NoApply   # 只扫描，不改配置
.\set-credentials.ps1 -Openid o_xxx -Token mv_xxx -Guid xxx -Qimei36 xxx -Test   # 手工写凭证
```

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `401` + `4100403` | 登录态过期，跑 `.\tools\scan-token-admin.ps1` 刷新 |
| `403` + `4100404` | 风控。先核对 `config.toml` 里的 Windows 指纹字段，再考虑降频；详见 `WINDOWS-SETUP.md` |
| `未找到 access_token` | 先在 Marvis 客户端里发一句话让 token 进内存，再重跑 |
| provider 启动失败 | `.\undeploy.ps1` 释放端口后重试；确认 `python --version` 可用 |

**不要压测**。密集请求会触发账号级风控（`4100404`），冷却后自愈。

---

## 目录结构

```
marvis2api/
├── bridge.py                 # OpenAI 兼容网关 (:4605)
├── uskey_provider.py         # uskey 生成服务 (:4607)
├── requirements.txt          # Python 依赖
├── config.example.toml       # 配置模板（真实凭证写入 config.toml，已 gitignore）
├── deploy.ps1                # 启动
├── undeploy.ps1              # 停止
├── acceptance.ps1            # 验收
├── set-credentials.ps1       # 手工写凭证
├── WINDOWS-SETUP.md          # 从零部署手册
└── tools/
    ├── scan-token-admin.ps1  # 一条命令：取 token → 写配置 → 重启 → 验证
    ├── scan_proc_token.py    # 进程内存扫描器（被上面调用）
    ├── apply_creds.py        # 凭证写入 + 自动发现 + 自测
    └── dump_headers.py       # 从客户端内存提取真实请求头（排障用）
```

---

## 已知边界

- `access_token` 会过期，需要定期刷新（`4100403` 就是信号）。
- 上游可能升级客户端版本、改变指纹算法，届时需要重新对齐 `config.toml` 里的指纹字段。
- 本项目仅用于**互操作性研究与学习**，请自行确认符合目标服务条款与当地法律。
