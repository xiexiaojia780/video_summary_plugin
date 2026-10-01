"""Offline smoke tests for video_summary_plugin (no Host required)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncio
import base64
import importlib
import os
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 宿主源码根目录：默认用工作区里的 MaiBot；可用 MAIBOT_ROOT 指向另一份 checkout
# （例如同时验证 MaiBot 1.3.1），自检会直接装载该目录里的真实代码来校验注入协议。
HOST_ROOT = Path(os.environ.get("MAIBOT_ROOT") or (ROOT.parent / "MaiBot")).expanduser().resolve()

import http_client as http_mod  # noqa: E402
import media as media_mod  # noqa: E402
import plugin as plugin_mod  # noqa: E402
from maibot_sdk.components import collect_components  # noqa: E402
from maibot_sdk.context import PluginContext, PluginPaths  # noqa: E402


def test_video_recognition() -> None:
    msg = {
        "raw_message": [
            {
                "type": "file",
                "data": {
                    "name": "a.mp4",
                    "url": "https://example.com/a.mp4",
                    "mime_type": "video/mp4",
                },
            },
            {"type": "video", "data": "https://example.com/b.mov"},
            {
                "type": "dict",
                "data": {
                    "type": "video",
                    "data": {"name": "c.webm", "url": "https://example.com/c.webm"},
                },
            },
            {
                "type": "file",
                "data": {
                    "name": "doc.pdf",
                    "url": "https://example.com/d.pdf",
                    "mime_type": "application/pdf",
                },
            },
            {
                "type": "file",
                "data": {
                    "filename": "clip.MKV",
                    "file_url": "https://example.com/clip",
                },
            },
        ]
    }
    assets = media_mod.extract_video_assets(msg)
    assert len(assets) == 4, assets
    assert media_mod.is_video_like(name="x.MP4")
    assert media_mod.is_video_like(mime_type="video/webm")
    assert not media_mod.is_video_like(name="x.txt", mime_type="text/plain")

    # NapCat 真实入站：video 被压成纯文本
    napcat_msg = {
        "processed_plain_text": "[视频] 文件: 185e4ddc6b437969196485b58ce8bd92.mp4，大小: 2897329",
        "raw_message": [
            {
                "type": "text",
                "data": "[视频] 文件: 185e4ddc6b437969196485b58ce8bd92.mp4，大小: 2897329",
            }
        ],
        "session_id": "qq_private_100000000",
    }
    napcat_assets = media_mod.extract_video_assets(napcat_msg)
    assert len(napcat_assets) == 1, napcat_assets
    assert napcat_assets[0].name.endswith(".mp4")
    assert napcat_assets[0].file_id.endswith(".mp4")
    assert napcat_assets[0].source_kind == "text_placeholder"
    print("OK recognition")


def test_http_helpers() -> None:
    assert (
        http_mod.extract_chat_text(
            {"choices": [{"message": {"content": " 你好 "}}]}
        )
        == "你好"
    )
    assert http_mod.join_api("https://api.example.com/v1", "chat/completions").endswith(
        "/chat/completions"
    )
    assert http_mod.join_api("https://api.example.com/v1/", "chat/completions").endswith(
        "/chat/completions"
    )

    # 公网 OK；内网默认拒绝；loopback 专用通道 OK
    http_mod.validate_http_url("https://example.com/a.mp4")
    for bad in (
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/x",
    ):
        try:
            http_mod.validate_http_url(bad)
            raise AssertionError(f"should block {bad}")
        except http_mod.HttpClientError:
            pass
    http_mod.validate_http_url("http://127.0.0.1:3002", allow_private=True, require_loopback=True)
    http_mod.validate_http_url("http://localhost:3002", allow_private=True, require_loopback=True)
    try:
        http_mod.validate_http_url(
            "http://192.168.1.8:3002",
            allow_private=True,
            require_loopback=True,
        )
        raise AssertionError("non-loopback should fail require_loopback")
    except http_mod.HttpClientError:
        pass
    # 显式放行私网
    http_mod.validate_http_url("http://10.1.2.3/v.mp4", allow_private=True)

    # 重定向到内网必须被二次校验拦住
    handler = http_mod._ValidatingRedirectHandler(
        allow_private=False,
        require_loopback=False,
        max_redirects=5,
    )
    fake_req = type("Req", (), {})()
    try:
        handler.redirect_request(
            fake_req,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            302,
            "Found",
            {},
            "http://127.0.0.1/secret",
        )
        raise AssertionError("redirect to loopback should be blocked")
    except http_mod.HttpClientError:
        pass
    print("OK http helpers")


def test_materialize_base64() -> None:
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            raw = b"0123456789"
            asset = media_mod.VideoAsset(
                source_kind="test",
                name="t.mp4",
                base64_data=base64.b64encode(raw).decode("ascii"),
            )
            path = await media_mod.materialize_video(
                asset,
                target_dir=Path(td),
                timeout_s=5,
                max_bytes=100,
            )
            assert path.exists() and path.read_bytes() == raw
            try:
                await media_mod.materialize_video(
                    asset,
                    target_dir=Path(td),
                    timeout_s=5,
                    max_bytes=5,
                )
                raise AssertionError("oversize should fail")
            except ValueError:
                pass

    asyncio.run(_run())
    print("OK materialize base64")


def test_components_and_hooks() -> None:
    plugin = plugin_mod.create_plugin()
    comps = collect_components(plugin.__class__)
    types = sorted({c.get("type") for c in comps})
    assert types == ["COMMAND", "HOOK_HANDLER", "TOOL"]
    hooks = {
        c["metadata"]["hook"]
        for c in comps
        if c.get("type") == "HOOK_HANDLER"
    }
    assert hooks == {
        "chat.receive.after_process",
        "maisaka.replyer.before_model_request",
    }
    names = {c.get("name") for c in comps}
    assert "video_summary" in names
    assert "video_summary_lookup" in names
    print("OK components")


def _import_host(module_names: list[str], *, probe: Path) -> list[Any] | None:
    """在临时工作目录中导入宿主模块。

    宿主的日志初始化会把 ``logs/`` 写到当前工作目录，所以必须在临时目录里导入，
    否则每次自检都会在插件仓库里留下 ``logs/app_*.log.jsonl``。

    找不到宿主源码时返回 None；调用方必须打印跳过提示，不能静默放过。
    可用 ``MAIBOT_ROOT`` 指向别的 checkout 来验证其它 MaiBot 版本。
    """

    mai_bot_root = HOST_ROOT
    if not probe.is_file():
        print(f"WARN 未找到宿主源码（{mai_bot_root}），本次不做真实代码校验")
        return None
    sys.path.insert(0, str(mai_bot_root))
    original_cwd = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as scratch:
            os.chdir(scratch)
            try:
                # 必须先导入 hook_payloads：直接导入 request_snapshot 会撞上宿主的循环导入。
                return [importlib.import_module(name) for name in module_names]
            except Exception as exc:  # noqa: BLE001
                print(f"WARN 找到宿主源码但导入失败，跳过该校验: {exc}")
                return None
            finally:
                os.chdir(original_cwd)
    finally:
        sys.path.remove(str(mai_bot_root))


def _host_item_validator():  # noqa: ANN202
    """装载宿主真实的 items 反序列化链路，用于校验 ContextItem 快照协议。"""

    modules = _import_host(
        [
            "src.plugin_runtime.hook_payloads",
            "src.llm_models.payload_content.context_protocol",
            "src.llm_models.request_snapshot",
        ],
        probe=HOST_ROOT / "src" / "plugin_runtime" / "hook_payloads.py",
    )
    if modules is None:
        return None
    hook_payloads, context_protocol, request_snapshot = modules

    def _validate(raw_items: list[dict[str, Any]], item_schema_version: Any):  # noqa: ANN202
        items = hook_payloads.deserialize_prompt_items(
            raw_items,
            item_schema_version=item_schema_version,
            mode=context_protocol.ContextProtocolMode.REQUEST_CONTEXT,
            original_items=(),
        )
        return [request_snapshot.serialize_context_item_snapshot(item) for item in items]

    return _validate


def _host_segment_reader():  # noqa: ANN202
    """装载宿主的消息段规范化与还原链路，校验聊天历史注入的段结构。"""

    modules = _import_host(
        [
            "src.plugin_runtime.hook_payloads",
            "src.plugin_runtime.capabilities.core",
            "src.plugin_runtime.host.message_utils",
        ],
        probe=HOST_ROOT / "src" / "plugin_runtime" / "capabilities" / "core.py",
    )
    if modules is None:
        return None
    _, capabilities_core, message_utils = modules

    def _read(raw_segments: list[dict[str, Any]]) -> list[str]:
        segments = capabilities_core._normalize_context_segments(raw_segments)
        sequence = message_utils.PluginMessageUtils._message_sequence_from_dict(segments)
        return [component.text for component in sequence.components if hasattr(component, "text")]

    return _read


def _host_tool_result_parser():  # noqa: ANN202
    """装载宿主把插件工具返回值转成执行结果的那一步，用于校验 success 语义。"""

    modules = _import_host(
        ["src.plugin_runtime.hook_payloads", "src.plugin_runtime.component_query"],
        probe=HOST_ROOT / "src" / "plugin_runtime" / "component_query.py",
    )
    if modules is None:
        return None
    component_query = modules[1]

    class _StubEntry:
        name = "video_summary_smoke"
        plugin_id = "github.xiexiaojia780.video-summary-plugin"

    def _parse(payload: dict[str, Any]):  # noqa: ANN202
        return component_query.ComponentQueryService._parse_tool_invoke_result(_StubEntry(), payload)

    return _parse


def _summary_items() -> list[dict[str, Any]]:
    """构造一组符合宿主协议的 ContextItem 快照，模拟模型请求前的 items。"""

    stamp = "2026-01-01T00:00:00"
    return [
        {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": "sys-1", "logical_turn_id": None, "timestamp": stamp},
            "parts": [{"type": "text", "text": "base prompt"}],
        },
        {
            "item_type": "UserMessageItem",
            "meta": {"item_id": "user-1", "logical_turn_id": None, "timestamp": stamp},
            "parts": [{"type": "text", "text": "看看这个视频"}],
        },
    ]


def test_injection_flow() -> None:
    plugin = plugin_mod.create_plugin()
    paths = PluginPaths(
        data_dir=Path("data/plugins/video_summary_smoke"),
        runtime_dir=Path("temp/plugins/video_summary_smoke"),
    )

    # 捕获 ctx.maisaka.context.append 的真实调用载荷
    appended: list[dict[str, Any]] = []

    async def rpc(method, plugin_id, payload, **_kwargs):  # noqa: ANN001
        del plugin_id
        if method == "cap.call" and str((payload or {}).get("capability")) == "maisaka.context.append":
            appended.append(dict((payload or {}).get("args") or {}))
            return {"success": True, "index": 0}
        return {}

    ctx = PluginContext(
        plugin_id="github.xiexiaojia780.video-summary-plugin",
        rpc_call=rpc,
        paths=paths,
    )
    plugin._set_context(ctx)
    plugin.set_plugin_config(plugin.get_default_config())

    # 只替换最内层概括实现，从而真实走通 _process_and_store → _publish_summary 路径
    summarized: list[str] = []

    async def fake_summarize(asset):  # noqa: ANN001
        summarized.append(asset.name)
        return "一只猫在玩球"

    plugin._summarize_asset = fake_summarize  # type: ignore[method-assign]

    validator = _host_item_validator()
    segment_reader = _host_segment_reader()
    tool_result_parser = _host_tool_result_parser()

    async def _run() -> None:
        msg = {
            "processed_plain_text": "看看这个",
            "session_id": "s1",
            "raw_message": [
                {
                    "type": "file",
                    "data": {
                        "name": "a.mp4",
                        "url": "https://example.com/a.mp4",
                    },
                }
            ],
        }
        result = await plugin.on_after_process(message=msg)
        assert result is not None and result["action"] == "continue"
        plain = result["modified_kwargs"]["message"]["processed_plain_text"]
        assert plugin_mod._PENDING_MARKER in plain
        # 占位文案不得承诺“正在生成”，否则概括完成/失败后它会永久失真
        assert "正在生成" not in plain

        await asyncio.sleep(0.1)

        # ---- C 通道：概括必须作为一条上下文消息追加进聊天历史 ----
        assert len(appended) == 1, appended
        call = appended[0]
        assert call["stream_id"] == "s1", call
        assert call["segments"] == [
            {"type": "text", "data": f"{plugin_mod._SUMMARY_MARKER} 文件=a.mp4\n一只猫在玩球"}
        ], call
        assert "一只猫在玩球" in call["visible_text"]
        assert str(call["source_kind"]).startswith("plugin:")

        # 用宿主真实链路还原段结构，确认模型最终读到的是概括正文
        if segment_reader is None:
            print(f"WARN 宿主源码不可用（{HOST_ROOT}），跳过聊天历史段结构校验")
        else:
            recovered = segment_reader(call["segments"])
            assert recovered == [f"{plugin_mod._SUMMARY_MARKER} 文件=a.mp4\n一只猫在玩球"], recovered

        # 关闭聊天历史注入后不得再追加
        plugin.config.summary.inject_chat_history = False
        before = len(appended)
        await plugin._publish_summary("s3", plugin._get_session_latest("s1"))
        assert len(appended) == before
        plugin.config.summary.inject_chat_history = True

        # 同一 (session, cache_key) 不得重复入库
        await plugin._publish_summary("s1", plugin._get_session_latest("s1"))
        assert len(appended) == 1, appended

        # ---- B 通道：默认关闭时完全不注入 ----
        items = _summary_items()
        assert (
            await plugin.on_before_model_request(items=items, item_schema_version=1, session_id="s1") is None
        )

        plugin.config.summary.inject_on_model_request = True
        hook = await plugin.on_before_model_request(items=items, item_schema_version=1, session_id="s1")
        assert hook is not None
        modified = hook["modified_kwargs"]
        # 键名必须是宿主读回的 items / item_schema_version；写成 messages 会静默失效
        assert set(modified) == {"items", "item_schema_version"}, modified
        assert modified["item_schema_version"] == 1
        new_items = modified["items"]
        assert len(new_items) == 3
        assert new_items[0]["parts"][0]["text"] == "base prompt"
        assert plugin_mod._SUMMARY_MARKER in new_items[1]["parts"][0]["text"]
        assert new_items[2]["parts"][0]["text"] == "看看这个视频"

        # ---- 用宿主真实反序列化器做往返校验 ----
        if validator is None:
            print(f"WARN 宿主源码不可用（{HOST_ROOT}），跳过 items 快照协议往返校验")
        else:
            round_trip = validator(new_items, 1)
            assert len(round_trip) == 3, round_trip
            assert any(plugin_mod._SUMMARY_MARKER in str(item) for item in round_trip), round_trip
            # schema 版本不匹配时宿主会抛 ValueError 并忽略整个改动
            try:
                validator(new_items, 99)
                raise AssertionError("schema 版本不匹配应当抛 ValueError")
            except ValueError:
                pass

        # 已含概括则不再重复注入
        assert (
            await plugin.on_before_model_request(items=new_items, item_schema_version=1, session_id="s1")
            is None
        )

        # 工具路径：结果直接返回给模型，不应再追加一遍聊天历史
        before = len(appended)
        tool = await plugin.tool_video_summary_lookup(stream_id="s1")
        assert tool["success"] is True, tool
        assert "一只猫在玩球" in tool["content"]
        assert len(appended) == before

        # ---- Tool 失败必须显式 success=False，否则宿主会记成成功 ----
        saved_sessions = dict(plugin._session_latest)
        plugin._session_latest.clear()
        miss = await plugin.tool_video_summary_lookup(stream_id="s1")
        assert miss["success"] is False, miss
        plugin._session_latest.update(saved_sessions)

        bad_url = await plugin.tool_video_summary_ingest(url="not-a-url")
        assert bad_url["success"] is False, bad_url

        if tool_result_parser is None:
            print(f"WARN 宿主源码不可用（{HOST_ROOT}），跳过 Tool success 语义校验")
        else:
            # 正向：显式失败必须被宿主判定为失败，且正文仍保留
            for payload in (miss, bad_url):
                parsed = tool_result_parser(payload)
                assert parsed.success is False, (payload, parsed.success)
                assert parsed.error_message, (payload, parsed.error_message)
                assert parsed.content, (payload, parsed.content)
            assert tool_result_parser(tool).success is True
            # 反向对照：这正是修掉的那个 bug —— 缺 success 字段会被宿主当成功
            assert tool_result_parser({"content": "插件未启用"}).success is True

        # ---- 命令契约 ----
        # 第三位是 intercept_message_level（host: component_query.py:554 / bot.py:325）：
        # 必须是 0，否则这条消息会被主链吞掉、根本不进 Maisaka。
        assert await plugin.cmd_video_summary(text="/video_summary", stream_id="s1") == (True, "", 0)

        # 历史 bug：message 键是序列化字典，被当成命令文本用会让正则永远匹配不上
        assert await plugin.cmd_video_summary(
            text="/video_summary",
            message={"processed_plain_text": "/video_summary", "session_id": "s1"},
            stream_id="s1",
        ) == (True, "", 0)

        # 附 URL 时应真正触发处理
        appended_before = len(appended)
        assert await plugin.cmd_video_summary(
            text="/video_summary https://example.com/x.mp4",
            stream_id="s3",
        ) == (True, "", 0)
        await asyncio.sleep(0.1)
        assert "x.mp4" in summarized, summarized
        assert len(appended) == appended_before + 1, appended

        # 非 URL 参数必须被忽略，不得拿去 NapCat 瞎抓
        summarized_before = list(summarized)
        assert await plugin.cmd_video_summary(text="/video_summary 你好", stream_id="s3") == (True, "", 0)
        await asyncio.sleep(0.05)
        assert summarized == summarized_before, summarized

        # 命令关闭时返回 0，同样不能拦截主链
        plugin.config.summary.enable_command = False
        assert await plugin.cmd_video_summary(text="/video_summary", stream_id="s1") == (False, "", 0)
        plugin.config.summary.enable_command = True

        # 默认配置
        default = plugin.get_default_config()
        assert default["summary"]["mode"] == "frame_vlm"
        assert default["summary"]["auto_process"] is True
        assert default["summary"]["inject_chat_history"] is True
        assert default["summary"]["inject_on_model_request"] is False

        plugin.config.summary.auto_process = False
        assert (
            await plugin.on_after_process(
                message={
                    "processed_plain_text": "x",
                    "session_id": "s2",
                    "raw_message": [
                        {
                            "type": "file",
                            "data": {
                                "name": "b.mp4",
                                "url": "https://example.com/b.mp4",
                            },
                        }
                    ],
                }
            )
            is None
        )

    asyncio.run(_run())
    print("OK injection flow (C=聊天历史 / B=items 快照)")


def test_format_and_cache() -> None:
    plugin = plugin_mod.create_plugin()
    paths = PluginPaths(
        data_dir=Path("data/plugins/video_summary_smoke"),
        runtime_dir=Path("temp/plugins/video_summary_smoke"),
    )

    async def rpc(*_a, **_k):  # noqa: ANN001
        return {}

    plugin._set_context(
        PluginContext(
            plugin_id="github.xiexiaojia780.video-summary-plugin",
            rpc_call=rpc,
            paths=paths,
        )
    )
    plugin.set_plugin_config(plugin.get_default_config())

    ok = {
        "success": True,
        "summary": "摘要A",
        "asset_name": "a.mp4",
        "ts": time.time(),
        "error": "",
    }
    bad = {
        "success": False,
        "summary": "",
        "asset_name": "b.mp4",
        "ts": time.time(),
        "error": "boom",
    }
    text_ok = plugin._format_summary_block(ok)
    text_bad = plugin._format_summary_block(bad)
    assert plugin_mod._SUMMARY_MARKER in text_ok and "摘要A" in text_ok
    assert plugin_mod._FAIL_MARKER in text_bad and "boom" in text_bad

    plugin._set_cache("k1", ok)
    assert plugin._get_cache("k1") is not None

    # 过期：TTL=1 秒，写入旧时间戳后应 miss
    plugin.config.summary.cache_ttl_s = 1.0
    plugin._cache["k1"] = {**ok, "ts": time.time() - 10}
    assert plugin._get_cache("k1") is None
    print("OK format/cache helpers")


def test_network_guard_and_cleanup() -> None:
    """校验 NapCat URL 校验不阻塞事件循环，以及 on_unload 能真正收尾后台任务。"""

    async def _run() -> None:
        plugin = plugin_mod.create_plugin()

        async def rpc(*_a, **_k):  # noqa: ANN001
            return {}

        plugin._set_context(
            PluginContext(
                plugin_id="github.xiexiaojia780.video-summary-plugin",
                rpc_call=rpc,
                paths=PluginPaths(
                    data_dir=Path("data/plugins/video_summary_smoke"),
                    runtime_dir=Path("temp/plugins/video_summary_smoke"),
                ),
            )
        )
        plugin.set_plugin_config(plugin.get_default_config())

        # 非 loopback 的 NapCat 地址必须被拒（该校验内部是阻塞 DNS，已挪进线程并加了超时）
        plugin.config.napcat.http_base_url = "http://192.168.1.9:3002"
        try:
            await plugin._fetch_via_napcat_http("a.mp4")
            raise AssertionError("非 loopback 的 NapCat 地址应当被拒绝")
        except RuntimeError as exc:
            assert "非法" in str(exc), exc

        # 自己配的 loopback 地址应通过校验，只会在真正连接时失败（确认没有误杀）
        plugin.config.napcat.http_base_url = "http://127.0.0.1:59999"
        try:
            await plugin._fetch_via_napcat_http("a.mp4")
            raise AssertionError("不该真的连上")
        except Exception as exc:  # noqa: BLE001
            assert "非法" not in str(exc), exc

        # on_unload 必须取消**并等待**后台任务结束，而不是只发个 cancel 就返回
        async def _sleep_forever() -> None:
            await asyncio.sleep(30)

        task = asyncio.create_task(_sleep_forever())
        plugin._bg_tasks.add(task)
        await asyncio.sleep(0)
        await plugin.on_unload()
        assert task.cancelled(), "on_unload 返回后后台任务仍未被取消"

    asyncio.run(_run())
    print("OK network guard + unload cleanup")


def main() -> None:
    test_video_recognition()
    test_http_helpers()
    test_materialize_base64()
    test_components_and_hooks()
    test_injection_flow()
    test_format_and_cache()
    test_network_guard_and_cleanup()
    print("ALL PASSED")


if __name__ == "__main__":
    main()
