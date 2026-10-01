# 更新日志

本文件记录视频内容概括插件的版本变更。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)：
新增能力升 MINOR，修复升 PATCH；只有改动 `config_model` 的字段/默认值时才同时提升 `plugin.config_version`。

## 1.1.1

### 主要功能

无（本版为修复与规范整改）。

### 细节

**上下文注入**

- 模型请求前注入改用宿主真实协议：传入并返回 `items` + `item_schema_version`。
  旧实现使用宿主从不传入的 `messages`，导致入参恒为 `None`、注入完全不生效，且宿主不报任何错。
- 聊天历史注入（`ctx.maisaka.context.append`）补全 `maisaka.context.append` 能力声明，并加入
  `(session, cache_key)` 去重，避免同一概括重复入库。

**命令（`@Command`）**

- 返回值第三位修正为 `0`。该位在 MaiBot 1.2.4 起是 `intercept_message_level`，
  旧实现返回 `True` 会导致 `/video_summary` 吞掉用户消息、不再进入主链。
- 命令文本改从 `text` 读取。旧实现误用 `message` 键（序列化后的 SessionMessage 字典），
  导致 `/video_summary <url>` 分支永不触发。
- 附带的 URL 参数增加 `http(s)` 直链校验，非直链只记 warning 并忽略；
  旧实现会拿任意文本去 NapCat 取文件。

**工具（`@Tool`）**

- 所有失败分支补齐显式 `"success": False` 与 `"error"`。宿主判定为
  `bool(result.get("success", True))`，缺字段会被记成成功，错误正文会被 Planner 当作正常输出。
- `video_summary_ingest` 对宿主「工具抛异常」的包装结果也补上显式失败（宿主包装结果无 `success` 键）。
- 工具描述改用 SDK 推荐的 `description` 字段。

**网络与生命周期**

- NapCat `http_base_url` 校验内的阻塞 DNS（`socket.getaddrinfo`）移入线程并加超时，
  不再阻塞 Runner 事件循环。
- `on_unload()` 改为取消后 `await` 后台任务结束（`asyncio.gather(..., return_exceptions=True)`），
  避免卸载返回时任务仍在运行。
- 文件句柄统一由 `with` 管理。

**WebUI**

- 7 处配置文案改为中文为主：`外部 API`、`接口地址（Base URL）`、`接口密钥（API Key）`、
  `HTTP 地址（Base URL）`、`访问令牌（Access Token）`、`NapCat 取回`。

**代码规范**

- `plugin.py` / `media.py` / `http_client.py` / `_smoke_test.py` 的导入顺序调整为项目规范：
  标准库 `from` → 标准库 `import` → 第三方 → 本地。

**自检（`_smoke_test.py`）**

- 移除测试夹具中硬编码的个人 QQ 号。
- 新增「网络守卫 + 卸载清理」回归用例（非 loopback 必拒、loopback 不误杀、卸载后任务确已取消）。

**文档**

- README 补齐 4 个遗漏的配置项：`summary.download_timeout_s`、`summary.prompt_template`、
  `direct.timeout_s`、`direct.prefer_url`，并给出精确默认值（`max_video_bytes` 等）。
- README 新增「跨 MaiBot 版本验证」说明（`MAIBOT_ROOT` 环境变量）。

## 1.1.0

### 主要功能

- **聊天历史注入**：概括完成后经 `ctx.maisaka.context.append` 追加为 Maisaka 聊天历史中的一条上下文消息。
  该通道是 append-only，追加在历史末尾，不改变已有 prompt 前缀，因此**不影响 prompt 缓存命中率**。
- **模型请求前注入改为可选**：新增 `summary.inject_on_model_request`（默认 `false`）。
  该通道会把 System Item 插在历史之前，使插入点之后的 token 缓存失效，故默认关闭。
- 新增 `summary.inject_chat_history` 配置（默认 `true`）。
- 新增 `maisaka.context.append` 能力声明。

### 细节

**上下文注入**

- 移除失效的事后回写逻辑。宿主在 Hook 返回后会调用 `deserialize_session_message()` 重建消息对象，
  插件持有的原字典已不再被引用，事后改写 `processed_plain_text` 不会生效。
- 入站占位文案改为事实性表述（`概括结果将作为一条上下文消息另行给出`），不再写「正在生成」，
  避免概括晚到或失败后该文本永久失真。

**配置**

- `plugin.config_version` 由 `1.0.0` 提升至 `1.1.0`（新增配置字段，配置形状发生变化）。

## 1.0.0

### 主要功能

- 自动检测入站视频 / 视频文件：Hook `chat.receive.after_process`，识别 `file` / `video` /
  `dict(type=video)` 段，以及 NapCat 适配器压平后的 `[视频] 文件: xxx.mp4` 文本占位。
- 双模式概括：
  - `frame_vlm`（默认）：ffmpeg 抽帧 + Host VLM；
  - `external_video`：直调外部视频多模态 API。
- 命令 `/video_summary`、`/视频概括`、`/视频总结`。
- 工具 `video_summary_lookup`（查询最近概括）、`video_summary_ingest`（对指定 URL 立即概括）。
- NapCat OneBot HTTP `get_file` 取回：当适配器只给出文本占位时，取回真实视频。
- 稳健性：大小限制、下载/处理超时、并发信号量、单条消息视频数上限、缓存 TTL。
- SSRF 防护：默认拒绝内网/本机下载 URL，HTTP 重定向目标二次校验。
