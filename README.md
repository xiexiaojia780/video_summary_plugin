# 视频内容概括插件

> 当 bot 收到视频 / 视频文件时，自动（或命令触发）生成内容概括，并**只注入 bot 上下文**，不直接对用户发言。

- **插件 ID**：`github.xiexiaojia780.video-summary-plugin`
- **版本**：1.0.0
- **作者**：[xiexiaojia780](https://github.com/xiexiaojia780)
- **License**：`GPL-3.0-or-later`
- **SDK**：`maibot-plugin-sdk` ≥ 2.0（导入名 `maibot_sdk`）

## 功能

| 能力 | 说明 |
|---|---|
| 自动检测视频 | Hook `chat.receive.after_process`，识别 `file` / `video` / `dict(type=video)` |
| 双模式概括 | `frame_vlm`（默认）：ffmpeg 抽帧 + Host `vlm`；`external_video`：外部视频多模态 API |
| 上下文注入 | 改写 `processed_plain_text` 为 `[视频内容概括] ...`；可选模型前 system 注入 |
| 命令 | `/video_summary`、`/视频概括`、`/视频总结`（可附 URL） |
| Tool | `video_summary_lookup`：供模型查询最近概括 |
| 稳健性 | 大小限制、超时、并发信号量、缓存 TTL、失败写入 `[视频内容概括失败]` |

**默认不 `send.text`**，只让 bot 知道视频内容后自然回复。

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

当前官方 `napcat-adapter` **不会**把 `video` 段保留为结构化 `file/video`，只转成文本。  
因此 QQ 入站视频要自动处理，需要：

1. 在 NapCat **单独新建**一个 HTTP Server（推荐 `127.0.0.1:3002`，不要复用已有服务端口）
2. 本插件配置：

| 字段 | 示例 | 说明 |
|---|---|---|
| `napcat.enabled` | `true` | 开启文本占位取回 |
| `napcat.http_base_url` | `http://127.0.0.1:3002` | 本插件专用 HTTP，与新建 httpServers 端口一致 |
| `napcat.access_token` | （可空） | 若该 HTTP 配了 token 则填写 |
| `napcat.prefer_adapter_api` | `true` | 优先 `adapter.napcat.file.get_file`，失败回退裸 HTTP |

验证 HTTP：

```bash
curl http://127.0.0.1:3002/get_version_info
```

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
├── _smoke_test.py
├── README.md
└── _locales/
```

## 配置要点

配置全部由 `config_model` 声明（**不要手写 config.toml**）：

| 分组 | 关键字段 | 默认 |
|---|---|---|
| `plugin` | `enabled` | `true` |
| `summary` | `mode` | `frame_vlm` |
| `summary` | `auto_process` | `true` |
| `summary` | `enable_command` / `enable_tool` | `true` |
| `summary` | `inject_on_model_request` | `true` |
| `summary` | `host_vlm_task` | `vlm` |
| `summary` | `max_frames` / `frame_interval_s` | `6` / `2.0` |
| `summary` | `max_video_bytes` | 80MB |
| `summary` | `process_timeout_s` / `max_concurrent` | `180` / `1` |
| `summary` | `cache_ttl_s` | `3600` |
| `direct` | `base_url` / `api_key` / `model` | 空（仅 external） |

## 工作流

1. 用户发视频 → `after_process` 识别素材  
2. 先写入 `[视频内容处理中] ...` 占位  
3. 后台下载 → 抽帧或外部 API → 得到摘要  
4. 合并为：

```text
[视频内容概括] 文件=demo.mp4
（中文概括正文）
```

5. 若主链时序上来不及改同一消息对象，`maisaka.replyer.before_model_request` 会按 session 再注入一次  
6. 模型也可主动调用 `video_summary_lookup`

## 命令

```text
/video_summary
/视频概括
/视频总结 https://example.com/a.mp4
```

命令默认**不抢答用户**（返回空文本并继续主链），只确保概括进入 bot 可见上下文。

## 离线自检

```bash
python _smoke_test.py
```

## 限制与说明

- MaiBot 当前**没有一等公民 VideoComponent**；本插件兼容 `file` / 透传 `dict` / 显式 `video` 段。
- Host `llm.generate` 无原生 video 入参；`frame_vlm` 必须抽帧。
- 大视频受 `max_video_bytes` 限制；data URL 外发可能很重，优先使用公网 URL。
- 异步概括与回复竞速时，依赖「处理中占位 + 模型前注入 + Tool」三重兜底。
