"""视频内容概括插件。

检测入站视频 / 视频文件，支持：
- frame_vlm：ffmpeg 抽帧 + Host VLM（ctx.llm.generate model=vlm）
- external_video：直调外部视频多模态 API

概括结果默认通过 **聊天历史注入**（``ctx.maisaka.context.append``）追加为一条真实上下文消息，
不直接对用户 ``send.text``。可选开启 **模型请求前注入**（``maisaka.replyer.before_model_request``），
但该方式会改写 prompt 前缀、牺牲 prompt 缓存命中率，故默认关闭。

两种注入通道的差别（宿主 prompt 缓存按「最长公共前缀」计算，见
``src/services/llm_cache_stats.py:230``）：

- 聊天历史注入：追加在历史末尾，是 append-only，已有前缀不变 → 缓存命中不受影响；
- 模型请求前注入：插在 system 之后、历史之前，插入点之后的全部 token 缓存失效 → 明显降低命中率。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import asyncio
import hashlib
import re
import time

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

try:
    from . import http_client as http_mod
    from . import media as media_mod
except ImportError:  # PluginLoader 以文件方式加载时相对导入可能失败
    import http_client as http_mod  # type: ignore
    import media as media_mod  # type: ignore


_CMD_PATTERN = re.compile(
    r"^/(?:video_summary|视频概括|视频总结)(?:\s+(?P<rest>.+))?\s*$",
    re.DOTALL | re.IGNORECASE,
)
_SUMMARY_MARKER = "[视频内容概括]"
_PENDING_MARKER = "[视频内容处理中]"
_FAIL_MARKER = "[视频内容概括失败]"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.1.0",
        description="配置版本",
        json_schema_extra={"label": "配置版本"},
    )


class SummarySectionConfig(PluginConfigBase):
    """概括策略与触发配置。"""

    __ui_label__ = "概括"
    __ui_icon__ = "clapperboard"
    __ui_order__ = 1

    mode: Literal["frame_vlm", "external_video"] = Field(
        default="frame_vlm",
        description="处理模式：frame_vlm=抽帧+Host VLM；external_video=外部视频多模态 API",
        json_schema_extra={
            "label": "处理模式",
            "hint": "默认抽帧走 Host vlm 任务；外部模式需配置 Direct API",
            "order": 10,
        },
    )
    auto_process: bool = Field(
        default=True,
        description="收到视频时自动概括并注入 bot 上下文",
        json_schema_extra={"label": "自动处理视频", "order": 20},
    )
    enable_command: bool = Field(
        default=True,
        description="是否启用 /video_summary 命令手动触发",
        json_schema_extra={"label": "启用命令", "order": 30},
    )
    enable_tool: bool = Field(
        default=True,
        description="是否向 LLM 暴露 video_summary_lookup 工具",
        json_schema_extra={"label": "启用 Tool", "order": 40},
    )
    inject_chat_history: bool = Field(
        default=True,
        description="概括完成后，把结果作为一条上下文消息追加进 Maisaka 聊天历史",
        json_schema_extra={
            "label": "追加到聊天历史",
            "hint": "推荐通道：追加在历史末尾，后续回合都能看到，且不破坏 prompt 前缀缓存",
            "order": 50,
        },
    )
    inject_on_model_request: bool = Field(
        default=False,
        description="在模型请求前把概括注入为一条 System Item（会改写 prompt 前缀）",
        json_schema_extra={
            "label": "模型请求前注入",
            "hint": "默认关闭：它插在历史之前，会使插入点之后的全部 token 缓存失效、明显降低命中率；"
            "仅在无法使用聊天历史注入时开启",
            "order": 55,
        },
    )
    host_vlm_task: str = Field(
        default="vlm",
        description="Host VLM 任务名（仅 frame_vlm）",
        json_schema_extra={
            "label": "Host VLM 任务名",
            "depends_on": "mode",
            "depends_value": "frame_vlm",
            "order": 60,
        },
    )
    max_frames: int = Field(
        default=6,
        ge=1,
        le=24,
        description="抽帧数量上限",
        json_schema_extra={
            "label": "最大抽帧数",
            "depends_on": "mode",
            "depends_value": "frame_vlm",
            "order": 70,
        },
    )
    frame_interval_s: float = Field(
        default=2.0,
        ge=0.2,
        le=60.0,
        description="抽帧间隔秒数",
        json_schema_extra={
            "label": "抽帧间隔（秒）",
            "depends_on": "mode",
            "depends_value": "frame_vlm",
            "order": 80,
        },
    )
    max_video_bytes: int = Field(
        default=80 * 1024 * 1024,
        ge=1 * 1024 * 1024,
        le=512 * 1024 * 1024,
        description="允许处理的最大视频字节数",
        json_schema_extra={"label": "最大视频大小（字节）", "order": 90},
    )
    download_timeout_s: float = Field(
        default=60.0,
        ge=5.0,
        le=600.0,
        description="下载视频超时秒数",
        json_schema_extra={"label": "下载超时（秒）", "order": 100},
    )
    process_timeout_s: float = Field(
        default=180.0,
        ge=10.0,
        le=900.0,
        description="单次概括总超时秒数（含抽帧/模型）",
        json_schema_extra={"label": "处理超时（秒）", "order": 110},
    )
    max_concurrent: int = Field(
        default=1,
        ge=1,
        le=8,
        description="同时处理的视频任务数",
        json_schema_extra={"label": "最大并发", "order": 120},
    )
    max_videos_per_message: int = Field(
        default=3,
        ge=1,
        le=20,
        description="单条消息最多处理的视频数；超出部分跳过（避免一次太多视频）",
        json_schema_extra={
            "label": "单条消息视频上限",
            "hint": "同一消息里视频过多时只处理前 N 个",
            "order": 125,
        },
    )
    cache_ttl_s: float = Field(
        default=3600.0,
        ge=0.0,
        le=86400.0,
        description="概括缓存 TTL 秒；0 表示仅进程内不过期直到重启",
        json_schema_extra={"label": "缓存 TTL（秒）", "order": 130},
    )
    allow_private_ips: bool = Field(
        default=False,
        description="是否允许下载消息/外链中的内网或本机 URL（默认关闭，防 SSRF）",
        json_schema_extra={
            "label": "允许私网下载 URL",
            "hint": "仅在可信环境且确需拉取内网视频时开启；默认拒绝 127.0.0.1/10.x/192.168.x 等",
            "order": 135,
        },
    )
    prompt_template: str = Field(
        default=(
            "请根据以下视频关键帧，用中文概括视频内容。"
            "说明主要画面、人物/物体、发生的事情与可能意图；"
            "不确定处请标明；控制在 120~250 字。"
        ),
        description="概括提示词模板",
        json_schema_extra={
            "label": "提示词",
            "x-widget": "textarea",
            "rows": 4,
            "order": 140,
        },
    )


class DirectApiSectionConfig(PluginConfigBase):
    """外部视频多模态 API 配置。"""

    __ui_label__ = "外部 API"
    __ui_icon__ = "globe"
    __ui_order__ = 2

    base_url: str = Field(
        default="",
        description="OpenAI 兼容根地址，如 https://api.example.com/v1",
        json_schema_extra={
            "label": "接口地址（Base URL）",
            "x-widget": "textarea",
            "rows": 1,
            "order": 10,
        },
    )
    api_key: str = Field(
        default="",
        description="接口密钥，以 Bearer 头发送",
        json_schema_extra={"label": "接口密钥（API Key）", "order": 20},
    )
    model: str = Field(
        default="",
        description="外部模型名",
        json_schema_extra={"label": "模型名", "order": 30},
    )
    timeout_s: float = Field(
        default=120.0,
        ge=5.0,
        le=900.0,
        description="外部 API 超时秒数",
        json_schema_extra={"label": "超时（秒）", "order": 40},
    )
    prefer_url: bool = Field(
        default=True,
        description="若素材有 http(s) URL，优先把 URL 直接传给外部模型",
        json_schema_extra={"label": "优先传视频 URL", "order": 50},
    )


class NapcatSectionConfig(PluginConfigBase):
    """NapCat OneBot HTTP：用于把文本占位里的 file/file_id 取回真实视频。"""

    __ui_label__ = "NapCat 取回"
    __ui_icon__ = "plug"
    __ui_order__ = 3

    enabled: bool = Field(
        default=True,
        description="当视频被适配器压成文本占位时，是否通过 NapCat HTTP get_file 取回",
        json_schema_extra={"label": "启用 NapCat 取回", "order": 10},
    )
    http_base_url: str = Field(
        default="http://127.0.0.1:3002",
        description="NapCat OneBot HTTP 地址（默认仅允许本机环回；建议为本插件单独开端口）",
        json_schema_extra={
            "label": "HTTP 地址（Base URL）",
            "hint": "仅建议 127.0.0.1 / localhost / ::1，例如 http://127.0.0.1:3002",
            "order": 20,
        },
    )
    access_token: str = Field(
        default="",
        description="NapCat HTTP access token；未设置则留空",
        json_schema_extra={"label": "访问令牌（Access Token）", "order": 30},
    )
    prefer_adapter_api: bool = Field(
        default=True,
        description="优先走 adapter.napcat.file.get_file（若可用），失败再回退裸 HTTP",
        json_schema_extra={"label": "优先 Adapter API", "order": 40},
    )
    allow_non_loopback: bool = Field(
        default=False,
        description="是否允许 napcat.http_base_url 指向非本机地址（默认关闭，仅 loopback）",
        json_schema_extra={
            "label": "允许非本机 NapCat",
            "hint": "默认只允许 127.0.0.1/localhost/::1；跨机器部署才开启",
            "order": 45,
        },
    )
    allowed_local_prefixes: str = Field(
        default="C:\\Windows\\Temp,/tmp,/var/tmp",
        description="允许读取的本地路径前缀（逗号分隔）；NapCat 返回本地缓存路径时使用",
        json_schema_extra={
            "label": "本地路径白名单",
            "hint": "仅在 get_file 返回本地 path/file 时生效；勿写过宽目录",
            "order": 50,
        },
    )


class VideoSummaryPluginConfig(PluginConfigBase):
    """视频内容概括插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    summary: SummarySectionConfig = Field(default_factory=SummarySectionConfig)
    direct: DirectApiSectionConfig = Field(default_factory=DirectApiSectionConfig)
    napcat: NapcatSectionConfig = Field(default_factory=NapcatSectionConfig)


class VideoSummaryPlugin(MaiBotPlugin):
    """视频内容概括插件。"""

    config_model = VideoSummaryPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore | None = None
        self._cache: dict[str, dict[str, Any]] = {}
        self._session_latest: dict[str, dict[str, Any]] = {}
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        # 已追加进聊天历史的 (session, cache_key)，避免同一概括重复入库
        self._published: set[str] = set()

    async def on_load(self) -> None:
        """插件加载。"""

        self._semaphore = asyncio.Semaphore(max(1, int(self.config.summary.max_concurrent)))
        runtime = Path(self.ctx.paths.runtime_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        self.ctx.logger.info(
            "视频内容概括插件已加载 mode=%s auto=%s 聊天历史注入=%s 模型前注入=%s",
            self.config.summary.mode,
            self.config.summary.auto_process,
            self.config.summary.inject_chat_history,
            self.config.summary.inject_on_model_request,
        )

    async def on_unload(self) -> None:
        """插件卸载：取消后台任务、等待其结束，并清理临时目录。"""

        # bg 任务与 inflight 任务都要取消：await 一个 Task 时取消外层协程并不会连带取消
        # 被等待的 Task，所以两者都得管。取消后必须再 gather 等它们真正结束，
        # 否则 on_unload() 返回时任务还在跑，热重载/退出会留下悬挂回调。
        pending: list[asyncio.Task[Any]] = [*self._bg_tasks, *self._inflight.values()]
        for task in pending:
            task.cancel()
        self._bg_tasks.clear()
        self._inflight.clear()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            await self._cleanup_runtime_dir()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("清理视频临时目录失败: %s", exc)

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载。"""

        del scope, config_data, version
        self._semaphore = asyncio.Semaphore(max(1, int(self.config.summary.max_concurrent)))
        self.ctx.logger.info(
            "视频内容概括插件配置已更新 mode=%s auto=%s 聊天历史注入=%s 模型前注入=%s",
            self.config.summary.mode,
            self.config.summary.auto_process,
            self.config.summary.inject_chat_history,
            self.config.summary.inject_on_model_request,
        )

    # ------------------------------------------------------------------
    # Hooks / Command / Tool
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.after_process",
        name="video_summary_after_process",
        description="检测视频并异步概括，概括结果通过聊天历史注入给出",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_after_process(self, message: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        """自动检测视频并启动概括。"""

        del kwargs
        if not self.config.plugin.enabled or not self.config.summary.auto_process:
            return None
        if not isinstance(message, dict):
            return None

        assets = media_mod.extract_video_assets(message)
        if not assets:
            # 便于排查：若文本像视频但未识别，打 debug
            plain = str(message.get("processed_plain_text") or "")
            if "[视频]" in plain or ".mp4" in plain.lower():
                self.ctx.logger.info(
                    "after_process 看到疑似视频文本但未抽出素材 plain=%s raw_types=%s",
                    plain[:200],
                    [
                        (item.get("type") if isinstance(item, dict) else type(item).__name__)
                        for item in (message.get("raw_message") or [])[:8]
                    ],
                )
            return None

        stream_id = self._extract_stream_id(message)
        limit = max(1, int(self.config.summary.max_videos_per_message))
        total = len(assets)
        if total > limit:
            self.ctx.logger.info(
                "单条消息视频数 %s 超过上限 %s，仅处理前 %s 个 session=%s",
                total,
                limit,
                limit,
                stream_id or "-",
            )
            assets = assets[:limit]

        self.ctx.logger.info(
            "检测到 %s 个视频素材，启动概括 session=%s names=%s",
            len(assets),
            stream_id or "-",
            [a.name or a.file_id or a.url for a in assets],
        )
        # 写入事实性占位：只声明“已检测到视频、概括将另行给出”，不写“正在生成”。
        # 概括结果改由聊天历史注入送达，本条文本不会再被回写，因此措辞必须始终成立。
        plain = str(message.get("processed_plain_text") or "").strip()
        pending_line = f"{_PENDING_MARKER} 检测到 {len(assets)} 个视频，概括结果将作为一条上下文消息另行给出"
        modified = False
        if _PENDING_MARKER not in plain and _SUMMARY_MARKER not in plain:
            message["processed_plain_text"] = f"{plain}\n{pending_line}".strip() if plain else pending_line
            modified = True

        for asset in assets:
            self._spawn_background(self._process_and_store(asset, stream_id=stream_id))

        # 宿主会用 deserialize_session_message() 造一个**新的** SessionMessage，
        # 所以这里只做同步改写；未改写就返回 None，省掉宿主一次无谓的反序列化。
        if not modified:
            return None
        return {"action": "continue", "modified_kwargs": {"message": message}}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="video_summary_before_model_request",
        description="在模型请求前把最近视频概括注入为一条 System Item（可选，会降低 prompt 缓存命中率）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_before_model_request(
        self,
        items: Any = None,
        item_schema_version: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """按 ContextItem 快照协议注入最近概括。

        宿主实际传入 ``items``（ContextItem 快照列表）+ ``item_schema_version``，
        并只读回 ``modified_kwargs["items"]`` / ``modified_kwargs["item_schema_version"]``
        （见 ``src/chat/replyer/maisaka_generator_base.py:764-796``）。
        键名写成 ``messages`` 会入参恒为 None、返回键被忽略，且不报任何错。
        """

        if not self.config.plugin.enabled or not self.config.summary.inject_on_model_request:
            return None
        if not isinstance(items, list):
            return None

        session_id = str(kwargs.get("session_id") or "").strip()
        record = self._get_session_latest(session_id) if session_id else None
        if record is None and self._session_latest:
            # 无 session 时退回全局最近一条（仍有效）
            record = max(self._session_latest.values(), key=lambda item: float(item.get("ts") or 0.0))
        if not record or not str(record.get("summary") or "").strip():
            return None
        if self._items_contain_summary(items):
            return None

        block = self._format_summary_block(record)
        new_items = list(items)
        # 插在开头连续的 SystemMessageItem 之后、历史之前。
        # 注意：这会改动 prompt 前缀，插入点之后的 token 缓存全部失效。
        insert_pos = 0
        for index, item in enumerate(items):
            if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
                insert_pos = index + 1
            else:
                break
        new_items.insert(insert_pos, self._build_summary_item(record, block))
        return {
            "action": "continue",
            "modified_kwargs": {
                "items": new_items,
                "item_schema_version": item_schema_version,
            },
        }

    @Command(
        "video_summary",
        description="手动触发最近视频概括查询/处理：/video_summary",
        pattern=r"^/(?:video_summary|视频概括|视频总结)(?:\s+.*)?$",
        aliases=["视频概括", "视频总结"],
    )
    async def cmd_video_summary(self, **kwargs: Any) -> tuple[bool, str, int]:
        """命令：查询缓存或提示如何使用。

        返回值第三位在 MaiBot 1.2.4 里是 ``intercept_message_level``
        （``src/plugin_runtime/component_query.py:554``、``src/chat/message_receive/bot.py:325``）：
        0 = 让主链继续处理这条消息，非 0 = 吞掉这条消息。
        本命令只负责触发概括（概括经聊天历史注入送达），所以一律返回 0，不抢走用户消息。
        """

        if not self.config.plugin.enabled or not self.config.summary.enable_command:
            return False, "", 0

        # 宿主把命令原文放在 text（component_query.py:519）。
        # 注意：message 键是序列化后的 SessionMessage **字典**，当文本用会让正则永远匹配不上。
        text = str(kwargs.get("text") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        match = _CMD_PATTERN.match(text) if text else None
        rest = ((match.group("rest") if match else "") or "").strip()

        # 命令本身不直接回复用户；主链继续，概括通过聊天历史注入给模型看。
        record = self._get_session_latest(stream_id) if stream_id else None
        if record and str(record.get("summary") or "").strip():
            self.ctx.logger.info("命令触发：会话已有视频概括，已确保可被模型看到")
            return True, "", 0

        if rest:
            # 允许用户贴 URL：/video_summary https://...
            if not rest.lower().startswith(("http://", "https://")):
                self.ctx.logger.warning("命令参数不是 http(s) 视频直链，已忽略：%s", rest[:120])
                return True, "", 0
            asset = media_mod.VideoAsset(source_kind="command", url=rest, name=Path(rest).name or "video")
            self._spawn_background(self._process_and_store(asset, stream_id=stream_id))
            return True, "", 0

        self.ctx.logger.info("命令触发：当前会话尚无视频概括；请先发送视频或附带 URL")
        return True, "", 0

    @Tool(
        "video_summary_lookup",
        description=(
            "查询当前会话最近一次视频内容概括。"
            "当用户刚发送视频、或对话涉及视频内容时调用；"
            "不传 stream_id 时回退到最近一次全局结果。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=False,
            ),
        ],
    )
    async def tool_video_summary_lookup(self, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        """供模型查询最近视频概括。

        失败必须显式 ``"success": False``：宿主判定是
        ``bool(result.get("success", True))``（``src/plugin_runtime/component_query.py:859``），
        缺字段会被当成成功，错误正文就会被 Planner 当正常工具输出、不再重试。
        """

        del kwargs
        if not self.config.plugin.enabled or not self.config.summary.enable_tool:
            return {
                "success": False,
                "content": "视频概括插件未启用",
                "error": "插件或 Tool 未启用",
            }

        sid = str(stream_id or "").strip()
        record = self._get_session_latest(sid) if sid else None
        if record is None and self._session_latest:
            record = max(self._session_latest.values(), key=lambda item: float(item.get("ts") or 0.0))
        if not record:
            return {
                "success": False,
                "content": "暂无视频概括结果。请等待自动处理完成，或让用户重新发送视频。",
                "error": "当前没有可用的视频概括结果",
            }
        # 记录存在但该次概括失败时仍算「查询成功」：正文里已写明失败原因，
        # 标成失败只会让 Planner 反复重试同一个缓存结果，反而更糟。
        return {"success": True, "content": self._format_summary_block(record)}

    @Tool(
        "video_summary_ingest",
        description=(
            "对指定的公网 http(s) 视频直链立即生成内容概括并返回文本。"
            "适用于用户在对话中贴出视频链接、或需要重试某个视频的场景；"
            "stream_id 用于把结果归属到会话。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="视频直链（http/https）",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=False,
            ),
        ],
    )
    async def tool_video_summary_ingest(self, url: str = "", stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        """供模型手动对指定 URL 生成视频概括（同步等待结果）。"""

        del kwargs
        if not self.config.plugin.enabled:
            return {
                "success": False,
                "content": "视频概括插件未启用",
                "error": "插件未启用",
            }

        raw_url = str(url or "").strip()
        if not raw_url.lower().startswith(("http://", "https://")):
            return {
                "success": False,
                "content": "参数 url 需要是 http(s) 视频直链",
                "error": f"非法 url：{raw_url or '<空>'}",
            }

        asset = media_mod.VideoAsset(
            source_kind="tool",
            url=raw_url,
            name=Path(raw_url).name or "video",
        )
        # 直接等待结果（走与自动流程相同的缓存/inflight/概括逻辑）
        # publish=False：本次结果马上作为工具返回值进入本轮上下文，再追加进聊天历史只会重复一遍。
        try:
            record = await self._process_and_store(asset, stream_id=stream_id or "", publish=False)
        except Exception as exc:  # noqa: BLE001
            # 宿主对「工具抛异常」只会包成 {"content": ...}（component_query.py:603），
            # 那种结果没有 success 键、会被判成成功，所以这里必须自己转成显式失败。
            return {"success": False, "content": f"视频概括失败：{exc}", "error": str(exc)}

        if not record.get("success"):
            error = str(record.get("error") or "未知错误")
            return {"success": False, "content": f"视频概括失败：{error}", "error": error}
        return {"success": True, "content": self._format_summary_block(record)}

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def _spawn_background(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            self._bg_tasks.discard(done)
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                self.ctx.logger.warning("视频概括后台任务失败: %s", exc)

        task.add_done_callback(_done)

    async def _process_and_store(
        self,
        asset: media_mod.VideoAsset,
        *,
        stream_id: str,
        publish: bool = True,
    ) -> dict[str, Any]:
        """处理单个视频、写入缓存，并把结果发布到已启用的注入通道。

        Args:
            asset: 待处理的视频素材。
            stream_id: 目标聊天流 ID，用作会话归属与注入目标。
            publish: 是否把结果发布到聊天历史；工具同步返回结果时传 False 以免重复注入。
        """

        cache_key = asset.cache_key
        cached = self._get_cache(cache_key)
        if cached is not None:
            self._remember_session(stream_id, cached)
            if publish:
                await self._publish_summary(stream_id, cached)
            return cached

        async with self._lock:
            existing = self._inflight.get(cache_key)
            if existing is not None:
                task = existing
            else:
                task = asyncio.create_task(self._run_summarize(asset))
                self._inflight[cache_key] = task

        try:
            result = await task
        finally:
            async with self._lock:
                if self._inflight.get(cache_key) is task:
                    self._inflight.pop(cache_key, None)

        self._set_cache(cache_key, result)
        self._remember_session(stream_id, result)
        if publish:
            await self._publish_summary(stream_id, result)
        return result

    async def _publish_summary(self, stream_id: str, record: dict[str, Any]) -> None:
        """把概括追加进 Maisaka 聊天历史（主注入通道）。

        走 ``ctx.maisaka.context.append``：宿主把它 append 到 ``runtime._chat_history``
        （见 ``src/plugin_runtime/capabilities/core.py:191-207``），成为一条真实上下文消息，
        后续回合都能看到；因为是 append-only，已有 prompt 前缀不变，不影响缓存命中。
        """

        if not self.config.summary.inject_chat_history:
            return

        sid = str(stream_id or "").strip()
        if not sid:
            self.ctx.logger.warning(
                "视频概括已完成但缺少 stream_id，无法追加到聊天历史 name=%s",
                record.get("asset_name") or record.get("asset_url") or record.get("cache_key"),
            )
            return

        publish_key = f"{sid}|{record.get('cache_key') or ''}"
        if publish_key in self._published:
            return

        block = self._format_summary_block(record)
        try:
            await self.ctx.maisaka.context.append(
                sid,
                [{"type": "text", "data": block}],
                visible_text=block,
                source_kind=f"plugin:{self.ctx.plugin_id}",
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("追加视频概括到聊天历史失败 session=%s: %s", sid, exc, exc_info=True)
            return

        self._published.add(publish_key)
        self.ctx.logger.info("视频概括已追加到聊天历史 session=%s name=%s", sid, record.get("asset_name") or "-")

    async def _run_summarize(self, asset: media_mod.VideoAsset) -> dict[str, Any]:
        """实际执行概括。"""

        sem = self._semaphore or asyncio.Semaphore(1)
        started = time.time()
        async with sem:
            try:
                summary = await asyncio.wait_for(
                    self._summarize_asset(asset),
                    timeout=float(self.config.summary.process_timeout_s),
                )
                result = {
                    "success": True,
                    "summary": summary,
                    "mode": self.config.summary.mode,
                    "asset_name": asset.name,
                    "asset_url": asset.url,
                    "error": "",
                    "ts": time.time(),
                    "elapsed_s": round(time.time() - started, 3),
                    "cache_key": asset.cache_key,
                }
                self.ctx.logger.info(
                    "视频概括完成 name=%s mode=%s elapsed=%.2fs",
                    asset.name or asset.url or asset.cache_key[:8],
                    result["mode"],
                    result["elapsed_s"],
                )
                return result
            except Exception as exc:  # noqa: BLE001
                result = {
                    "success": False,
                    "summary": "",
                    "mode": self.config.summary.mode,
                    "asset_name": asset.name,
                    "asset_url": asset.url,
                    "error": str(exc),
                    "ts": time.time(),
                    "elapsed_s": round(time.time() - started, 3),
                    "cache_key": asset.cache_key,
                }
                self.ctx.logger.warning(
                    "视频概括失败 name=%s err=%s",
                    asset.name or asset.url or asset.cache_key[:8],
                    exc,
                )
                return result

    async def _summarize_asset(self, asset: media_mod.VideoAsset) -> str:
        mode = str(self.config.summary.mode or "frame_vlm").strip()
        if mode == "external_video":
            return await self._summarize_external(asset)
        return await self._summarize_frame_vlm(asset)

    async def _summarize_frame_vlm(self, asset: media_mod.VideoAsset) -> str:
        runtime = Path(self.ctx.paths.runtime_dir) / "videos" / asset.cache_key[:16]
        runtime.mkdir(parents=True, exist_ok=True)
        video_path = await self._materialize_video_with_napcat(asset, runtime=runtime)
        frames_dir = runtime / "frames"
        frames = await media_mod.extract_frames(
            video_path,
            output_dir=frames_dir,
            max_frames=int(self.config.summary.max_frames),
            interval_s=float(self.config.summary.frame_interval_s),
            timeout_s=max(10.0, float(self.config.summary.process_timeout_s) * 0.5),
        )

        prompt_text = str(self.config.summary.prompt_template or "").strip() or "请概括这些视频帧的内容。"
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for frame in frames:
            content_parts.append(media_mod.image_path_to_llm_part(frame))

        prompt = [{"role": "user", "content": content_parts}]
        result = await self.ctx.llm.generate(
            prompt,
            model=str(self.config.summary.host_vlm_task or "vlm"),
        )
        text = ""
        if isinstance(result, dict):
            text = str(result.get("response") or result.get("content") or result.get("text") or "").strip()
            if not text and result.get("success") is False:
                raise RuntimeError(str(result.get("error") or "Host VLM 调用失败"))
        if not text:
            raise RuntimeError("Host VLM 返回空概括")
        return text

    async def _summarize_external(self, asset: media_mod.VideoAsset) -> str:
        endpoint = http_mod.DirectEndpoint(
            base_url=self.config.direct.base_url,
            api_key=self.config.direct.api_key,
            model=self.config.direct.model,
            timeout_s=float(self.config.direct.timeout_s),
        )
        prompt_text = str(self.config.summary.prompt_template or "").strip() or "请概括该视频内容。"

        video_url = ""
        if self.config.direct.prefer_url and asset.url.lower().startswith(("http://", "https://")):
            video_url = asset.url
        else:
            runtime = Path(self.ctx.paths.runtime_dir) / "videos" / asset.cache_key[:16]
            video_path = await self._materialize_video_with_napcat(asset, runtime=runtime)
            # 多数兼容网关仍更吃 URL；无公网 URL 时退化为 data URL（可能很大，受 max_video_bytes 限制）
            raw = await asyncio.to_thread(video_path.read_bytes)
            import base64
            import mimetypes

            mime = asset.mime_type or mimetypes.guess_type(str(video_path))[0] or "video/mp4"
            video_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

        body = {
            "model": endpoint.require_model(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {
                            "type": "video_url",
                            "video_url": {"url": video_url},
                        },
                    ],
                }
            ],
        }
        url = http_mod.join_api(endpoint.normalized_base(), "chat/completions")
        # Direct API 默认也走公网校验；若用户把 base_url 配成内网，需显式 allow_private_ips
        payload = await http_mod.request_json(
            method="POST",
            url=url,
            headers=http_mod.auth_headers(endpoint.api_key),
            body=body,
            timeout_s=float(endpoint.timeout_s),
            allow_private=bool(self.config.summary.allow_private_ips),
        )
        text = http_mod.extract_chat_text(payload)
        if not text:
            raise RuntimeError("外部视频 API 返回空概括")
        return text

    # ------------------------------------------------------------------
    # NapCat recovery for text placeholders
    # ------------------------------------------------------------------

    async def _materialize_video_with_napcat(
        self,
        asset: media_mod.VideoAsset,
        *,
        runtime: Path,
    ) -> Path:
        """落盘视频；若只有 file_id/文件名，则经 NapCat 取回。"""

        # 已有 url/base64/local 时直接走原逻辑
        if asset.url or asset.base64_data or asset.local_path:
            try:
                return await media_mod.materialize_video(
                    asset,
                    target_dir=runtime,
                    timeout_s=float(self.config.summary.download_timeout_s),
                    max_bytes=int(self.config.summary.max_video_bytes),
                    allow_private=bool(self.config.summary.allow_private_ips),
                )
            except Exception as exc:  # noqa: BLE001
                # 文本占位常只剩 hash.mp4 文件名；再尝试 NapCat
                if not (asset.file_id or asset.name):
                    raise
                self.ctx.logger.info("直接落盘失败，尝试 NapCat 取回: %s", exc)

        file_ref = str(asset.file_id or asset.name or "").strip()
        if not file_ref:
            raise ValueError("视频缺少可获取的 url / base64 / local_path / file_id")

        raw = await self._fetch_via_napcat(file_ref)
        max_bytes = int(self.config.summary.max_video_bytes)
        if len(raw) > max_bytes:
            raise ValueError(f"NapCat 取回视频过大：{len(raw)} > {max_bytes}")

        runtime.mkdir(parents=True, exist_ok=True)
        ext = Path(asset.name or file_ref).suffix.lower() or ".mp4"
        out_path = runtime / f"{asset.cache_key[:16]}{ext}"
        await asyncio.to_thread(out_path.write_bytes, raw)
        self.ctx.logger.info("已通过 NapCat 取回视频 ref=%s bytes=%s", file_ref, len(raw))
        return out_path

    async def _fetch_via_napcat(self, file_ref: str) -> bytes:
        """通过 adapter API 或裸 HTTP get_file 取回文件字节。"""

        if not self.config.napcat.enabled:
            raise RuntimeError("NapCat 取回未启用；请在插件配置中开启，并配置 HTTP Base URL")

        errors: list[str] = []

        if self.config.napcat.prefer_adapter_api:
            try:
                return await self._fetch_via_adapter_api(file_ref)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"adapter_api: {exc}")
                self.ctx.logger.info("adapter.napcat.file.get_file 失败，回退 HTTP: %s", exc)

        try:
            return await self._fetch_via_napcat_http(file_ref)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"http: {exc}")
            raise RuntimeError("；".join(errors) if errors else str(exc)) from exc

    async def _fetch_via_adapter_api(self, file_ref: str) -> bytes:
        """调用公开 API adapter.napcat.file.get_file。"""

        params = {"file_id": file_ref, "file": file_ref}
        result = await self.ctx.api.call("adapter.napcat.file.get_file", params=params)
        if not isinstance(result, dict):
            raise RuntimeError(f"adapter get_file 返回非对象：{type(result)}")
        data = http_mod.unwrap_onebot_data(result)
        return await self._bytes_from_napcat_data(data)

    async def _fetch_via_napcat_http(self, file_ref: str) -> bytes:
        """裸 HTTP 调用 NapCat OneBot get_file。"""

        base = str(self.config.napcat.http_base_url or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("NapCat http_base_url 为空")

        # 默认强制 loopback，防止把 base_url 配成任意内网探测点
        require_loopback = not bool(self.config.napcat.allow_non_loopback)
        try:
            # validate_http_url 内部要走 socket.getaddrinfo 做 DNS 解析：既阻塞又没有超时参数，
            # 直接在协程里调用会卡住 Runner 的事件循环，所以放进线程并加一道超时。
            await asyncio.wait_for(
                asyncio.to_thread(
                    http_mod.validate_http_url,
                    base if "://" in base else f"http://{base}",
                    allow_private=True,  # loopback 本身属 private；用 require_loopback 约束
                    require_loopback=require_loopback,
                ),
                timeout=float(self.config.summary.download_timeout_s),
            )
        except TimeoutError as exc:
            raise RuntimeError(f"校验 napcat.http_base_url 超时（DNS 未返回）：{base}") from exc
        except http_mod.HttpClientError as exc:
            raise RuntimeError(f"非法 napcat.http_base_url：{exc}") from exc

        headers = {"User-Agent": "video-summary-plugin/1.0", "Content-Type": "application/json"}
        token = str(self.config.napcat.access_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload = await http_mod.post_json(
            url=f"{base}/get_file",
            body={"file_id": file_ref, "file": file_ref},
            headers=headers,
            timeout_s=float(self.config.summary.download_timeout_s),
            allow_private=True,
            require_loopback=require_loopback,
        )
        data = http_mod.unwrap_onebot_data(payload)
        return await self._bytes_from_napcat_data(data)

    async def _bytes_from_napcat_data(self, data: dict[str, Any]) -> bytes:
        """从 get_file data 中提取字节：base64 → url → 本地 path。"""

        import base64

        max_bytes = int(self.config.summary.max_video_bytes)

        b64 = str(data.get("base64") or data.get("base64_data") or "").strip()
        if b64:
            raw = media_mod.decode_base64_payload(b64)
            if len(raw) > max_bytes:
                raise ValueError(f"base64 视频过大：{len(raw)} > {max_bytes}")
            return raw

        file_url = str(data.get("url") or data.get("file_url") or "").strip()
        if file_url.lower().startswith(("http://", "https://")):
            # NapCat 常返回本机/内网临时链；允许私网，但仍受超时与大小限制
            return await http_mod.download_bytes(
                url=file_url,
                timeout_s=float(self.config.summary.download_timeout_s),
                max_bytes=max_bytes,
                allow_private=True,
            )

        local_path = str(data.get("file") or data.get("path") or data.get("file_path") or "").strip()
        if local_path:
            return self._read_local_file_allowed(local_path, max_bytes=max_bytes)

        raise RuntimeError(f"NapCat get_file 无可用 base64/url/file 字段：{list(data.keys())}")

    def _read_local_file_allowed(self, local_path: str, *, max_bytes: int) -> bytes:
        """白名单约束下读取本地文件。"""

        path = Path(local_path).expanduser()
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"无法解析本地路径：{exc}") from exc
        if not resolved.is_file():
            raise ValueError(f"本地文件不存在：{resolved}")

        prefixes = [
            p.strip()
            for p in str(self.config.napcat.allowed_local_prefixes or "").replace("\n", ",").split(",")
            if p.strip()
        ]
        if not prefixes:
            raise ValueError("本地路径白名单为空，拒绝读取本地文件")

        resolved_text = str(resolved)
        allowed = False
        for prefix in prefixes:
            try:
                prefix_resolved = str(Path(prefix).expanduser().resolve(strict=False))
            except OSError:
                prefix_resolved = prefix
            if resolved_text.lower().startswith(prefix_resolved.lower()):
                allowed = True
                break
        if not allowed:
            raise ValueError(f"本地路径不在白名单内：{resolved}")

        size = resolved.stat().st_size
        if size > max_bytes:
            raise ValueError(f"本地视频过大：{size} > {max_bytes}")
        return resolved.read_bytes()

    # ------------------------------------------------------------------
    # Cache / formatting helpers
    # ------------------------------------------------------------------

    def _get_cache(self, key: str) -> dict[str, Any] | None:
        item = self._cache.get(key)
        if not item:
            return None
        ttl = float(self.config.summary.cache_ttl_s or 0.0)
        if ttl > 0 and (time.time() - float(item.get("ts") or 0.0)) > ttl:
            self._cache.pop(key, None)
            return None
        return item

    def _set_cache(self, key: str, value: dict[str, Any]) -> None:
        self._cache[key] = value
        # 简单上限，避免无限增长
        if len(self._cache) > 256:
            oldest = sorted(self._cache.items(), key=lambda kv: float(kv[1].get("ts") or 0.0))
            for drop_key, _ in oldest[:64]:
                self._cache.pop(drop_key, None)

    def _remember_session(self, stream_id: str, record: dict[str, Any]) -> None:
        sid = str(stream_id or "").strip() or "_global"
        self._session_latest[sid] = record
        if len(self._session_latest) > 128:
            oldest = sorted(self._session_latest.items(), key=lambda kv: float(kv[1].get("ts") or 0.0))
            for drop_key, _ in oldest[:32]:
                self._session_latest.pop(drop_key, None)

    def _get_session_latest(self, stream_id: str) -> dict[str, Any] | None:
        sid = str(stream_id or "").strip()
        if not sid:
            return None
        record = self._session_latest.get(sid)
        if not record:
            return None
        ttl = float(self.config.summary.cache_ttl_s or 0.0)
        if ttl > 0 and (time.time() - float(record.get("ts") or 0.0)) > ttl:
            self._session_latest.pop(sid, None)
            return None
        return record

    @staticmethod
    def _format_summary_block(record: dict[str, Any]) -> str:
        if record.get("success"):
            name = str(record.get("asset_name") or "").strip()
            prefix = f"{_SUMMARY_MARKER}"
            if name:
                prefix += f" 文件={name}"
            return f"{prefix}\n{str(record.get('summary') or '').strip()}"
        err = str(record.get("error") or "未知错误").strip()
        return f"{_FAIL_MARKER} {err}"

    @staticmethod
    def _items_contain_summary(items: list[Any]) -> bool:
        """判断 items 里是否已有本插件的概括块，避免重复注入。"""

        for item in items:
            if not isinstance(item, dict):
                continue
            for part in item.get("parts") or []:
                if isinstance(part, dict) and _SUMMARY_MARKER in str(part.get("text") or ""):
                    return True
        return False

    @staticmethod
    def _build_summary_item(record: dict[str, Any], block: str) -> dict[str, Any]:
        """构造模型前注入用的 ContextItem 快照。

        结构对齐 ``serialize_context_item_snapshot``（``src/llm_models/request_snapshot.py:273``）：
        ``item_id`` 非空、``logical_turn_id`` 可为 null 但键必须存在、``timestamp`` 为合法 ISO 时间。
        """

        digest = hashlib.sha256(
            f"{record.get('cache_key') or ''}|{block}".encode("utf-8", errors="ignore")
        ).hexdigest()[:16]
        timestamp = datetime.fromtimestamp(float(record.get("ts") or time.time())).isoformat()
        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"video_summary_{digest}",
                "logical_turn_id": None,
                "timestamp": timestamp,
            },
            "parts": [{"type": "text", "text": block}],
        }

    @staticmethod
    def _extract_stream_id(message: dict[str, Any]) -> str:
        """从序列化 SessionMessage 提取会话键。

        Host 序列化字段优先是 ``session_id``；兼容 ``stream_id`` / ``chat_id``。
        """

        for key in ("session_id", "stream_id", "chat_id"):
            value = message.get(key)
            if value:
                return str(value).strip()
        info = message.get("message_info") or {}
        if isinstance(info, dict):
            for key in ("session_id", "stream_id", "chat_id"):
                value = info.get(key)
                if value:
                    return str(value).strip()
            additional = info.get("additional_config") or {}
            if isinstance(additional, dict):
                for key in ("session_id", "stream_id", "chat_id"):
                    value = additional.get(key)
                    if value:
                        return str(value).strip()
        return ""

    async def _cleanup_runtime_dir(self) -> None:
        runtime = Path(self.ctx.paths.runtime_dir) / "videos"
        if not runtime.exists():
            return

        def _rm() -> None:
            import shutil

            shutil.rmtree(runtime, ignore_errors=True)

        await asyncio.to_thread(_rm)


def create_plugin() -> VideoSummaryPlugin:
    """插件工厂入口。"""

    return VideoSummaryPlugin()
