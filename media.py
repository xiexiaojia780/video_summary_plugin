"""视频识别、下载与抽帧工具。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from . import http_client as http_mod
except ImportError:  # PluginLoader 文件加载回退
    import http_client as http_mod  # type: ignore


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".m4v",
    ".flv",
    ".mpeg",
    ".mpg",
    ".3gp",
    ".ts",
}

VIDEO_MIME_PREFIXES = ("video/",)

# NapCat 适配器会把 video/file 压成纯文本占位，例如：
# [视频] 文件: xxx.mp4，大小: 2897329
# [文件] xxx.mp4，大小: 123，链接: https://...
_VIDEO_TEXT_RE = re.compile(
    r"\[视频\](?:\s*文件[:：]\s*(?P<name>[^，,；;\n]+))?"
    r"(?:[，,]\s*大小[:：]\s*(?P<size>[^，,；;\n]+))?"
    r"(?:[，,]\s*(?:链接|url)[:：]\s*(?P<url>\S+))?",
    re.IGNORECASE,
)
_FILE_TEXT_RE = re.compile(
    r"\[文件\]\s*(?:(?P<name>[^，,；;\n]+?))?"
    r"(?:[，,]\s*大小[:：]\s*(?P<size>[^，,；;\n]+))?"
    r"(?:[，,]\s*(?:链接|url)[:：]\s*(?P<url>\S+))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VideoAsset:
    """识别到的视频素材描述。"""

    source_kind: str
    name: str = ""
    url: str = ""
    file_id: str = ""
    mime_type: str = ""
    size: str = ""
    base64_data: str = ""
    local_path: str = ""
    raw_type: str = ""

    @property
    def cache_key(self) -> str:
        """稳定缓存键。"""

        material = "|".join(
            [
                self.source_kind,
                self.url,
                self.file_id,
                self.local_path,
                self.name,
                self.mime_type,
                self.size,
                self.base64_data[:128],
            ]
        )
        return hashlib.sha256(material.encode("utf-8", errors="ignore")).hexdigest()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _guess_ext(name: str = "", mime_type: str = "", url: str = "") -> str:
    for candidate in (name, urlparse(url).path):
        suffix = Path(str(candidate or "")).suffix.lower()
        if suffix:
            return suffix
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type.split(";")[0].strip().lower())
        if guessed:
            return guessed.lower()
    return ".mp4"


def is_video_like(
    *,
    name: str = "",
    mime_type: str = "",
    url: str = "",
    explicit_type: str = "",
) -> bool:
    """判断一段素材是否像视频。"""

    raw_type = str(explicit_type or "").strip().lower()
    if raw_type in {"video", "short_video", "video_file"}:
        return True

    mime = str(mime_type or "").strip().lower()
    if any(mime.startswith(prefix) for prefix in VIDEO_MIME_PREFIXES):
        return True

    for candidate in (name, urlparse(url).path):
        suffix = Path(str(candidate or "")).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            return True
    return False


def _video_from_file_payload(payload: dict[str, Any], *, source_kind: str, raw_type: str = "file") -> VideoAsset | None:
    name = str(
        payload.get("name")
        or payload.get("file")
        or payload.get("file_name")
        or payload.get("filename")
        or ""
    ).strip()
    url = str(payload.get("url") or payload.get("file_url") or payload.get("path") or "").strip()
    local_path = str(payload.get("local_path") or payload.get("file_path") or "").strip()
    if url and not url.lower().startswith(("http://", "https://", "data:")):
        # 某些适配器把本地路径塞进 url
        if Path(url).exists():
            local_path = local_path or url
            url = ""
    mime_type = str(payload.get("mime_type") or payload.get("mimeType") or "").strip()
    size = str(payload.get("size") or payload.get("file_size") or "").strip()
    file_id = str(payload.get("file_id") or payload.get("id") or "").strip()
    base64_data = str(payload.get("base64") or payload.get("base64_data") or "").strip()

    if not is_video_like(name=name, mime_type=mime_type, url=url or local_path, explicit_type=raw_type):
        return None

    return VideoAsset(
        source_kind=source_kind,
        name=name,
        url=url,
        file_id=file_id,
        mime_type=mime_type,
        size=size,
        base64_data=base64_data,
        local_path=local_path,
        raw_type=raw_type or "file",
    )


def _video_from_placeholder_text(text: str) -> list[VideoAsset]:
    """从 NapCat 文本占位中提取视频素材。"""

    raw = str(text or "")
    if not raw.strip():
        return []

    assets: list[VideoAsset] = []
    for match in _VIDEO_TEXT_RE.finditer(raw):
        name = str(match.group("name") or "").strip()
        size = str(match.group("size") or "").strip()
        url = str(match.group("url") or "").strip()
        # 文件名本身常是 hash.mp4，也可当 file_id 去 NapCat get_file
        file_id = name if name and not name.lower().startswith(("http://", "https://")) else ""
        candidate = _video_from_file_payload(
            {
                "name": name,
                "size": size,
                "url": url,
                "file_id": file_id,
                "file": name,
            },
            source_kind="text_placeholder",
            raw_type="video",
        )
        if candidate is not None:
            assets.append(candidate)

    # 仅当明确是视频扩展名/mime 时，才把 [文件] 占位当视频
    for match in _FILE_TEXT_RE.finditer(raw):
        name = str(match.group("name") or "").strip()
        size = str(match.group("size") or "").strip()
        url = str(match.group("url") or "").strip()
        if not is_video_like(name=name, url=url):
            continue
        file_id = name if name and not name.lower().startswith(("http://", "https://")) else ""
        candidate = _video_from_file_payload(
            {
                "name": name,
                "size": size,
                "url": url,
                "file_id": file_id,
                "file": name,
            },
            source_kind="text_placeholder",
            raw_type="file",
        )
        if candidate is not None:
            assets.append(candidate)

    return assets


def extract_video_assets(message: dict[str, Any]) -> list[VideoAsset]:
    """从序列化 SessionMessage 中提取视频素材。

    兼容：
    1. 结构化 ``file`` / ``video`` / ``dict`` 段
    2. NapCat 适配器压平后的 ``[视频] 文件: ...`` / ``[文件] ...mp4`` 文本占位
    """

    assets: list[VideoAsset] = []
    seen: set[str] = set()

    def _add(candidate: VideoAsset | None) -> None:
        if candidate is None:
            return
        key = candidate.cache_key
        if key in seen:
            return
        seen.add(key)
        assets.append(candidate)

    raw_message = message.get("raw_message") or []
    if isinstance(raw_message, list):
        for item in raw_message:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            data = item.get("data")

            candidate: VideoAsset | None = None
            if item_type == "file":
                candidate = _video_from_file_payload(_as_dict(data), source_kind="file", raw_type="file")
            elif item_type in {"video", "short_video", "video_file"}:
                if isinstance(data, str):
                    payload = {"url": data, "name": Path(urlparse(data).path).name}
                else:
                    payload = _as_dict(data)
                candidate = _video_from_file_payload(payload, source_kind="video", raw_type=item_type)
            elif item_type == "text":
                text = data if isinstance(data, str) else str(_as_dict(data).get("text") or "")
                for text_asset in _video_from_placeholder_text(text):
                    _add(text_asset)
                continue
            elif item_type in {"dict", "custom", ""}:
                payload = _as_dict(data)
                nested_type = str(payload.get("type") or "").strip().lower()
                nested_data = payload.get("data", payload)
                if nested_type in {"video", "short_video", "video_file", "file"}:
                    if isinstance(nested_data, str):
                        nested_payload = {
                            "url": nested_data,
                            "name": Path(urlparse(nested_data).path).name,
                        }
                    else:
                        nested_payload = _as_dict(nested_data)
                    candidate = _video_from_file_payload(
                        nested_payload,
                        source_kind="dict",
                        raw_type=nested_type or "video",
                    )
                elif nested_type == "text" or isinstance(nested_data, str):
                    text = nested_data if isinstance(nested_data, str) else str(payload.get("text") or "")
                    for text_asset in _video_from_placeholder_text(text):
                        _add(text_asset)
                    continue

            _add(candidate)

    # 兜底：processed_plain_text 也可能只剩文本占位
    plain = str(message.get("processed_plain_text") or "")
    if plain:
        for text_asset in _video_from_placeholder_text(plain):
            _add(text_asset)

    return assets


def decode_base64_payload(raw: str) -> bytes:
    """解码可能带 data URL 前缀的 base64。"""

    text = (raw or "").strip()
    if not text:
        raise ValueError("base64 为空")
    if text.startswith("data:") and ";base64," in text:
        text = text.split(";base64,", maxsplit=1)[1].strip()
    # 去掉空白
    text = re.sub(r"\s+", "", text)
    return base64.b64decode(text, validate=False)


async def materialize_video(
    asset: VideoAsset,
    *,
    target_dir: Path,
    timeout_s: float,
    max_bytes: int,
    allow_private: bool = False,
) -> Path:
    """把视频落到本地文件。"""

    target_dir.mkdir(parents=True, exist_ok=True)
    ext = _guess_ext(asset.name, asset.mime_type, asset.url or asset.local_path)
    out_path = target_dir / f"{asset.cache_key[:16]}{ext}"

    if asset.local_path:
        src = Path(asset.local_path)
        if src.exists() and src.is_file():
            size = src.stat().st_size
            if size > max_bytes:
                raise ValueError(f"本地视频过大：{size} > {max_bytes}")
            if src.resolve() != out_path.resolve():
                await asyncio.to_thread(shutil.copy2, src, out_path)
            return out_path

    if asset.base64_data:
        raw = decode_base64_payload(asset.base64_data)
        if len(raw) > max_bytes:
            raise ValueError(f"base64 视频过大：{len(raw)} > {max_bytes}")
        await asyncio.to_thread(out_path.write_bytes, raw)
        return out_path

    if asset.url:
        raw = await http_mod.download_bytes(
            url=asset.url,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            allow_private=allow_private,
        )
        await asyncio.to_thread(out_path.write_bytes, raw)
        return out_path

    raise ValueError("视频缺少可获取的 url / base64 / local_path")


def find_ffmpeg() -> str | None:
    """查找 ffmpeg 可执行文件。"""

    return shutil.which("ffmpeg")


async def extract_frames(
    video_path: Path,
    *,
    output_dir: Path,
    max_frames: int,
    interval_s: float,
    timeout_s: float,
) -> list[Path]:
    """用 ffmpeg 抽帧，返回图片路径列表。"""

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，无法抽帧。请安装 ffmpeg 并确保在 PATH 中。")

    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "frame_%03d.jpg"
    # fps 过滤：每 interval_s 抽 1 帧，最多 max_frames
    fps = 1.0 / max(0.1, float(interval_s))
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={fps}",
        "-frames:v",
        str(max(1, int(max_frames))),
        str(pattern),
    ]

    def _run() -> None:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_s)),
            check=False,
        )
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()[:500]
            raise RuntimeError(f"ffmpeg 抽帧失败：{err or completed.returncode}")

    await asyncio.to_thread(_run)
    frames = sorted(output_dir.glob("frame_*.jpg"))
    if not frames:
        raise RuntimeError("ffmpeg 未产出任何帧")
    return frames[: max(1, int(max_frames))]


def image_path_to_data_url(path: Path) -> str:
    """把本地图片转成 data URL。"""

    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64,{b64}"


def image_path_to_llm_part(path: Path) -> dict[str, Any]:
    """构造成 Host llm.generate 可识别的图片片段。"""

    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    image_format = mime.split("/")[-1] if "/" in mime else "jpeg"
    return {
        "type": "image",
        "image_format": image_format,
        "image_base64": b64,
        "image_url": {
            "url": f"data:{mime};base64,{b64}",
        },
    }
