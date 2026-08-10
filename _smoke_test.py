"""Offline smoke tests for video_summary_plugin (no Host required)."""

from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

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
        "session_id": "qq_private_483403354",
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


def test_injection_flow() -> None:
    plugin = plugin_mod.create_plugin()
    paths = PluginPaths(
        data_dir=Path("data/plugins/video_summary_smoke"),
        runtime_dir=Path("temp/plugins/video_summary_smoke"),
    )

    async def rpc(*_a, **_k):  # noqa: ANN001
        return {}

    ctx = PluginContext(
        plugin_id="github.xiexiaojia780.video-summary-plugin",
        rpc_call=rpc,
        paths=paths,
    )
    plugin._set_context(ctx)
    plugin.set_plugin_config(plugin.get_default_config())

    async def fake_process_and_store(asset, stream_id="", message=None):  # noqa: ANN001
        record = {
            "success": True,
            "summary": "一只猫在玩球",
            "asset_name": asset.name,
            "ts": time.time(),
            "error": "",
            "mode": "frame_vlm",
        }
        plugin._remember_session(stream_id, record)
        if message is not None:
            plugin._merge_summary_into_message(message, record)
        return record

    plugin._process_and_store = fake_process_and_store  # type: ignore[method-assign]

    async def _run() -> None:
        msg = {
            "processed_plain_text": "看看这个",
            "stream_id": "s1",
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
        await asyncio.sleep(0.05)
        assert plugin_mod._SUMMARY_MARKER in msg["processed_plain_text"]
        assert "一只猫在玩球" in msg["processed_plain_text"]

        hook = await plugin.on_before_model_request(
            messages=[
                {"role": "system", "content": "base"},
                {"role": "user", "content": "hi"},
            ],
            session_id="s1",
        )
        assert hook is not None
        injected = hook["modified_kwargs"]["messages"]
        assert any(plugin_mod._SUMMARY_MARKER in str(m.get("content")) for m in injected)
        assert await plugin.on_before_model_request(messages=injected, session_id="s1") is None

        tool = await plugin.tool_video_summary_lookup(stream_id="s1")
        assert "一只猫在玩球" in tool["content"]

        cmd = await plugin.cmd_video_summary(stream_id="s1", message="/video_summary")
        assert cmd == (True, "", True)

        # 默认配置
        default = plugin.get_default_config()
        assert default["summary"]["mode"] == "frame_vlm"
        assert default["summary"]["auto_process"] is True

        plugin.config.summary.auto_process = False
        assert (
            await plugin.on_after_process(
                message={
                    "processed_plain_text": "x",
                    "stream_id": "s2",
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
    print("OK injection flow")


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


def main() -> None:
    test_video_recognition()
    test_http_helpers()
    test_materialize_base64()
    test_components_and_hooks()
    test_injection_flow()
    test_format_and_cache()
    print("ALL PASSED")


if __name__ == "__main__":
    main()
