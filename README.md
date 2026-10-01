# 视频内容概括插件

> 当 bot 收到视频 / 视频文件时，自动（或命令触发）生成内容概括，并**只注入 bot 上下文**，不直接对用户发言。

- **插件 ID**：`github.xiexiaojia780.video-summary-plugin`
- **版本**：1.1.1
- **作者**：[xiexiaojia780](https://github.com/xiexiaojia780)
- **License**：`GPL-3.0-or-later`（与 `_manifest.json` / 根目录 `LICENSE` 一致；正文为 GNU GPLv3，允许 any later version）
- **SDK**：`maibot-plugin-sdk` ≥ 2.0（导入名 `maibot_sdk`）
- **Host 能力声明**：`llm.generate`（见 `_manifest.json` → `capabilities`）

## 功能

| 能力 | 说明 |
|---|---|
| 自动检测视频 | Hook `chat.receive.after_process`，识别 `file` / `video` / `dict(type=video)` / NapCat 文本占位 |
| 双模式概括 | `frame_vlm`（默认）：ffmpeg 抽帧 + Host `vlm`；`external_video`：外部视频多模态 API |
| 上下文注入 | **聊天历史注入**（默认）：把 `[视频内容概括] ...` 作为一条上下文消息追加进 Maisaka 聊天历史 |
| 可选注入 | **模型请求前注入**（默认关闭）：改写本次 prompt 的 Context Items；会降低 prompt 缓存命中率 |
| 命令 | `/video_summary`、`/视频概括`、`/视频总结`（可附 URL） |
| Tool | `video_summary_lookup`：查最近概括；`video_summary_ingest`：对指定 URL 立即概括 |
| 稳健性 | 大小限制、超时、并发信号量、**单次处理上限（单条消息最多 N 个视频）**、缓存 TTL、失败写入 `[视频内容概括失败]` |

**默认不 `send.text`**，只让 bot 知道视频内容后自然回复。

## 能力与权限

| 类型 | 内容 | 说明 |
|---|---|---|
| Host 能力 | `llm.generate` | 调用 Host VLM / LLM 生成视频概括（`summary.host_vlm_task`，默认 `vlm`） |
| Host 能力 | `api.call` | 可选优先调用 `adapter.napcat.file.get_file` 取回视频 |
| Host 能力 | `maisaka.context.append` | 把概括追加为聊天历史里的一条上下文消息（默认注入通道） |
| 可选网络 | NapCat OneBot HTTP | 默认**仅本机环回** `napcat.http_base_url`（`127.0.0.1:3002`）上的 `get_file` |
| 可选网络 | Direct API | 仅 `external_video` 模式；`direct.base_url` + `api_key` 由用户配置 |
| SSRF 防护 | 消息外链下载 | 默认拒绝内网/本机 URL；HTTP 重定向目标会再次校验；可用 `summary.allow_private_ips` 放行 |
| 系统依赖 | `ffmpeg` | 仅 `frame_vlm`；**不是** Python 包，需本机安装 |
| 对用户发言 | 默认关闭 | 不主动 `send.text`；只注入 bot 上下文 / 提供 Tool |
| 本地文件 | 受限 | 仅在 NapCat 返回本地路径时，按 `napcat.allowed_local_prefixes` 白名单读取 |

> 本插件**不**申请发送消息能力作为主路径；**不**在代码中硬编码 token / QQ 号 / 群号。  
> 密钥与私有地址只应写在 WebUI 运行时配置（`config.toml` 由 Runner 生成，已被 `.gitignore`）。

## 安装与启用

1. 将本目录放到 MaiBot 的 `plugins/` 下（目录名建议 `video_summary_plugin`）
2. 确认已安装 **ffmpeg**（见下文），Host 已配置 **`vlm`** 任务
3. 若走 QQ / napcat-adapter：在 NapCat **单独新建 HTTP Server**（推荐 `127.0.0.1:3002`）
4. 在 WebUI 启用本插件，按需填写 `napcat` / `direct` 配置
5. **重载插件**或重启 MaiBot

> 本插件取文件只走 OneBot **HTTP** `POST /get_file`。  
> **不要**为 `3002` 再开 WebSocket；现有 MaiBot ↔ napcat-adapter 的收发 WebSocket 保持原样即可。

## 前置条件

### 模式 A：`frame_vlm`（默认）

1. 系统已安装 **ffmpeg**，且在 `PATH` 中可执行  
2. Host `model_config` 已配置 **`vlm`** 任务（可绑定视觉模型）  
3. 入站视频需至少具备：`url` / `base64` / 本地路径 / **NapCat file 引用** 之一  
4. 若使用 **napcat-adapter**：它会把视频压成  
   `[视频] 文件: xxx.mp4，大小: ...` 文本占位。  
   插件会识别该文本，并通过 **NapCat OneBot HTTP `get_file`** 取回真实视频（需在 NapCat 开启 HTTP 服务，并在本插件配置 `napcat.http_base_url`）  

#### 安装 / 检查 ffmpeg

安装后请**新开终端**（或重启 MaiBot 进程），再验证：

```bash
ffmpeg -version
```

能输出版本信息即表示 PATH 可用。常见安装方式：

| 系统 | 示例命令 |
|---|---|
| Windows（winget） | `winget install --id Gyan.FFmpeg -e` |
| Windows（scoop） | `scoop install ffmpeg` |
| Windows（choco） | `choco install ffmpeg` |
| Debian / Ubuntu | `sudo apt update && sudo apt install -y ffmpeg` |
| Fedora | `sudo dnf install -y ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |
| macOS（Homebrew） | `brew install ffmpeg` |
| Docker | 在镜像里 `apt/apk/yum install ffmpeg`，或换自带 ffmpeg 的基础镜像 |

说明：

- **主程序无法通过 `_manifest.json` 自动安装 ffmpeg**（它不是 Python 包，是系统可执行文件）
- 若只使用 **`external_video`** 模式且始终有公网视频 URL，可不依赖 ffmpeg；默认 `frame_vlm` 必须有
- Windows 若 `ffmpeg -version` 仍找不到：检查安装目录是否进了用户/系统 PATH，或重启后再试

### NapCat 取回（QQ 视频必需）

当前官方 `napcat-adapter` **不会**把 `video` 段保留为结构化 `file/video`，只转成文本，例如：

```text
[视频] 文件: 185e4ddc6b437969196485b58ce8bd92.mp4，大小: 2897329
```

因此 QQ 入站视频要自动处理，需要：

1. 在 NapCat **单独新建**一个 **HTTP Server**（推荐 `127.0.0.1:3002`，**不要复用**已有 9998/3000 等端口）
2. **不必**给这个 `3002` 开 WebSocket（插件不走 WS 取文件）
3. 本插件配置：

| 字段 | 示例 | 说明 |
|---|---|---|
| `napcat.enabled` | `true` | 开启文本占位取回 |
| `napcat.http_base_url` | `http://127.0.0.1:3002` | 本插件专用 HTTP，与新建 httpServers 端口一致 |
| `napcat.access_token` | （可空） | 若该 HTTP 配了 token 则填写 |
| `napcat.prefer_adapter_api` | `true` | 优先 `adapter.napcat.file.get_file`，失败回退裸 HTTP |
| `napcat.allowed_local_prefixes` | `C:\Windows\Temp,/tmp,/var/tmp` | `get_file` 返回本地路径时的白名单 |

验证 HTTP：

```bash
curl http://127.0.0.1:3002/get_version_info
```

返回含 `"status":"ok"` 即可。然后重载本插件，再发视频。

### 模式 B：`external_video`

1. 在 WebUI 插件配置填写 Direct API：
   - `base_url`（如 `https://api.example.com/v1`）
   - `api_key`
   - `model`
2. 接口需兼容 OpenAI `POST /chat/completions`，并接受 `video_url` 内容段  
   （若素材有公网 URL 会优先传 URL；否则下载后转 data URL）

## 仓库结构

```
video_summary_plugin/
├── _manifest.json
├── plugin.py          # 配置 / Hook / Command / Tool / 双模式调度
├── media.py           # 视频识别、下载落盘、ffmpeg 抽帧
├── http_client.py     # 下载与外部 API（stdlib）
├── _smoke_test.py     # 离线自检：装载宿主真码校验注入协议与 Tool 契约
├── README.md
├── LICENSE            # GNU GPLv3 正文
└── _locales/          # i18n 占位（当前 zh-CN 为空对象）
```

## 配置要点

配置全部由 `config_model` 声明（**优先在 WebUI 改**；不要长期手写 `config.toml`）：

| 分组 | 关键字段 | 默认 |
|---|---|---|
| `plugin` | `enabled` | `true` |
| `summary` | `mode` | `frame_vlm` |
| `summary` | `auto_process` | `true` |
| `summary` | `enable_command` / `enable_tool` | `true` |
| `summary` | `inject_chat_history` | `true`（推荐通道：append 到聊天历史，不影响缓存） |
| `summary` | `inject_on_model_request` | `false`（可选通道：插到 prompt 前缀，会降低缓存命中） |
| `summary` | `host_vlm_task` | `vlm` |
| `summary` | `max_frames` / `frame_interval_s` | `6` / `2.0` |
| `summary` | `max_video_bytes` | `83886080`（80 MB） |
| `summary` | `download_timeout_s` | `60.0` |
| `summary` | `process_timeout_s` / `max_concurrent` | `180` / `1` |
| `summary` | `max_videos_per_message` | `3`（单次处理上限：一条消息最多处理的视频数） |
| `summary` | `cache_ttl_s` | `3600` |
| `summary` | `allow_private_ips` | `false`（防 SSRF；默认拒内网下载 URL） |
| `summary` | `prompt_template` | 中文概括提示词（默认要求 120~250 字，在 WebUI 编辑） |
| `direct` | `base_url` / `api_key` / `model` | 空（仅 `external_video`） |
| `direct` | `timeout_s` | `120.0` |
| `direct` | `prefer_url` | `true`（素材有 http(s) URL 时优先直接传给外部模型） |
| `napcat` | `enabled` | `true` |
| `napcat` | `http_base_url` | `http://127.0.0.1:3002`（默认仅 loopback） |
| `napcat` | `access_token` | 空 |
| `napcat` | `prefer_adapter_api` | `true` |
| `napcat` | `allow_non_loopback` | `false` |
| `napcat` | `allowed_local_prefixes` | `C:\Windows\Temp,/tmp,/var/tmp` |

> 调用 Host VLM 时插件一律传 `model=<host_vlm_task>`，**不传 `task_name`**。这是为了同时兼容 1.2.4 与 1.2.5：
>
> - **1.2.4** 的 `_cap_llm_generate` **只读 `model`/`model_name`，完全忽略 `task_name`**；
>   若改用 `task_name=`，SDK 会把它塞进 payload 但宿主不认，`model` 为空 → 静默落到"第一个可用任务"，拿到错误的模型。
> - **1.2.5** 起新增 `_resolve_llm_capability_route`：显式带 `task_name` 才按新版协议走；不带时 `model` 仍先按任务名解析。
> - 因此 `model=` 是**唯一在两个版本里都正确**的写法，插件无需按版本分支。

## 工作流

1. 用户发视频 → `chat.receive.after_process` 识别素材  
2. 同步写入一句**事实性**占位：`[视频内容处理中] 检测到 N 个视频，概括结果将作为一条上下文消息另行给出`
   （不写「正在生成」，因此概括晚到或失败时这句话也不会失真）  
3. 后台下载 → 抽帧或外部 API → 得到摘要  
4. 摘要经 **`ctx.maisaka.context.append`** 追加进聊天历史：

```text
[视频内容概括] 文件=demo.mp4
（中文概括正文）
```

5. 它 append 在历史末尾，既有 prompt 前缀不变，因此**不影响 prompt 缓存命中率**  
6. 模型也可主动调用 `video_summary_lookup` / `video_summary_ingest`

### 注入通道与 prompt 缓存

宿主统计缓存命中用的是「最长公共前缀」口径（`src/services/llm_cache_stats.py:230`），
所以**改动越靠前的 token，代价越大**：

| 通道 | 位置 | 对缓存的影响 |
|---|---|---|
| 聊天历史注入（`inject_chat_history`，默认开） | 追加在历史**末尾** | append-only，已有前缀不变 → **基本不影响** |
| 模型请求前注入（`inject_on_model_request`，默认关） | 插在 system 之后、**历史之前** | 插入点之后全部 token 失效 → **明显降低命中率** |

只有确实无法使用聊天历史注入（例如不希望概括长期留在历史里）时，才建议开启后者。

## 命令

```text
/video_summary
/视频概括
/视频总结 https://example.com/a.mp4
```

命令**不直接回复用户**（返回空文本），只负责触发概括生成；概括仍通过聊天历史注入送达。

> `@Command` 返回值第三位在 MaiBot 1.2.4 里是 `intercept_message_level`
> （`src/plugin_runtime/component_query.py:554`、`src/chat/message_receive/bot.py:325`）：
> `0` = 主链继续处理这条消息，非 `0` = 吞掉这条消息。
> 本插件一律返回 `0`，因此 `/video_summary` **不会**把用户消息从主链里吃掉。
> 附带的 URL 参数必须是 `http(s)` 直链，否则只记一条 warning 并忽略。

## 供 LLM 的 Tool

| Tool | 作用 |
|---|---|
| `video_summary_lookup` | 查询当前会话最近一次视频概括（缓存） |
| `video_summary_ingest` | 对指定的 http(s) 视频直链**立即**生成概括并返回文本 |

模型在用户贴出视频链接、或视频未被自动处理时，可调用 `video_summary_ingest(url=..., stream_id=...)`。

> 工具失败一律返回 `{"success": False, "content": ..., "error": ...}`。
> 宿主判定是 `bool(result.get("success", True))`（`src/plugin_runtime/component_query.py:859`），
> **缺 `success` 字段会被当成成功**，错误正文就会被 Planner 当正常输出、不再重试。

## 离线自检

```bash
python _smoke_test.py
```

脚本会**直接装载本机 MaiBot 的真实源码**（`deserialize_prompt_items`、`serialize_context_item_snapshot`、
`_normalize_context_segments`、`_parse_tool_invoke_result`）来校验两种注入载荷与 Tool 契约，
而不是自己造一套假协议；找不到宿主源码时会打印 WARN，不会静默放过。

### 跨 MaiBot 版本验证

`MAIBOT_ROOT` 可以指向任意一份 MaiBot checkout，用来在**不切换本地环境**的前提下验证其它版本：

```bash
# 导出 1.3.1 的源码到临时目录（只读 git 对象，不动工作区）
mkdir -p /tmp/mb131
git -C /path/to/MaiBot archive 1.3.1 src pyproject.toml | tar -x -C /tmp/mb131

MAIBOT_ROOT=/tmp/mb131 python _smoke_test.py
```

已验证通过的组合：MaiBot **1.2.4** 与 **1.3.1**（两者相关契约一致）。

## 常见问题

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 发了视频但完全不处理 | napcat-adapter 只给了文本占位，旧逻辑漏检；或插件未重载 | 确认已是含文本识别的版本；重载插件；看日志有无「检测到 N 个视频素材」 |
| 概括生成了但 bot 不知道 | 未开 `summary.inject_chat_history`；或 `stream_id` 缺失 | 看日志有无「视频概括已追加到聊天历史」；确认配置里注入通道未被关掉 |
| 担心影响 prompt 缓存 | — | 默认的聊天历史注入是 append-only，不破坏前缀；不要随意开 `inject_on_model_request` |
| `video_summary_lookup` 一直「暂无结果」 | 自动处理没启动，或取回/概括失败 | 查插件日志：识别 → NapCat 取回 → 概括完成/失败 |
| NapCat 取回失败 | `3002` HTTP 未开、端口不对、token 不匹配、base_url 非本机 | `curl http://127.0.0.1:3002/get_version_info`；核对 `http_base_url`（默认仅 loopback）/ `access_token` |
| 外链下载被拒 | 消息里是内网 URL，触发 SSRF 防护 | 默认行为；确需时再开 `summary.allow_private_ips` |
| `ffmpeg` / 抽帧失败 | 未安装或不在 PATH | `ffmpeg -version`；安装后重启 MaiBot |
| VLM 返回空 / 概括失败 | Host 未配 `vlm` 任务，或模型不可用 | `summary.host_vlm_task` 应与 `model_config` 里**真实存在的任务名**一致。MaiBot 1.2.4 解析不到会直接报「未找到名为 `vlm` 的模型配置」；1.2.5 起改为退化成「具体模型名」再试，报错位置不同 |
| 要不要开 WebSocket？ | — | **不要**为本插件的 `3002` 开 WS；只开 HTTP Server |
| 命令发了没回用户 | 设计如此 | 命令只注入上下文，不 `send.text` 抢答 |

## 限制与说明

- MaiBot 当前**没有一等公民 VideoComponent**；本插件兼容 `file` / 透传 `dict` / 显式 `video` 段，以及 NapCat 文本占位。
- Host `llm.generate` 无原生 video 入参；`frame_vlm` 必须抽帧。
- 大视频受 `max_video_bytes` 限制；data URL 外发可能很重，优先使用公网 URL。
- 概括结果通过**聊天历史注入**送达：它是一条 append-only 的历史消息，不进 `processed_plain_text`，
  也不会被事后再改写（宿主 Hook 返回后会用 `deserialize_session_message()` 重建消息对象，原始 dict 已不再被引用）。
- 正因如此，概括要等后台任务跑完才出现在历史里；首轮回复可能早于概括落地，此时 bot 会在下一轮才「看到」视频内容。
- 大视频受 `max_video_bytes` 限制；data URL 外发可能很重，优先使用公网 URL。
- 模型也可通过 `video_summary_lookup` 主动查询最近概括（同一轮内即刻可用）。
- 本插件**不会**修改 MaiBot 主程序；QQ 侧依赖独立 HTTP `get_file`，与收发消息用的 WebSocket 无关。
- Python 依赖：仅标准库 + Host 自带 `maibot_sdk`；`_manifest.json` 的 `dependencies` 为空是预期行为。系统依赖 `ffmpeg` 见上文。

## 许可证

本项目以 **GPL-3.0-or-later** 授权：

- SPDX / `_manifest.json`：`GPL-3.0-or-later`
- 根目录 `LICENSE`：GNU General Public License **Version 3** 正文  
- 含义：你可按 GPLv3，或（若适用）任何更新版本的 GPL 使用/分发

完整条款见 [`LICENSE`](./LICENSE)。
