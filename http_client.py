"""轻量 HTTP 客户端（stdlib），用于下载视频与直调外部多模态 API。"""

from __future__ import annotations

import asyncio
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse


class HttpClientError(Exception):
    """HTTP / 协议层错误。"""


@dataclass(frozen=True)
class DirectEndpoint:
    """外部视频多模态接口参数。"""

    base_url: str
    api_key: str
    model: str
    timeout_s: float = 120.0

    def normalized_base(self) -> str:
        raw = (self.base_url or "").strip()
        if not raw:
            raise HttpClientError("base_url 不能为空")
        return raw if raw.endswith("/") else raw + "/"

    def require_model(self) -> str:
        model = (self.model or "").strip()
        if not model:
            raise HttpClientError("model 不能为空")
        return model


def join_api(base_url: str, path: str) -> str:
    """拼接 base_url 与相对路径。"""

    base = base_url if base_url.endswith("/") else base_url + "/"
    return urljoin(base, path.lstrip("/"))


def validate_http_url(url: str) -> None:
    """校验 http/https URL。"""

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HttpClientError(f"非法 URL：{url!r}（需要 http/https）")


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _request_bytes_sync(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_s: float,
    max_bytes: int | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """同步请求，返回 status/body/headers。"""

    validate_http_url(url)
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(1.0, float(timeout_s)),
            context=_ssl_context() if url.lower().startswith("https") else None,
        ) as resp:
            status = int(getattr(resp, "status", None) or resp.getcode() or 0)
            resp_headers = {str(k).lower(): str(v) for k, v in dict(resp.headers).items()}
            if max_bytes is None:
                raw = resp.read()
            else:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise HttpClientError(f"响应体超过大小限制（{max_bytes} bytes）")
                    chunks.append(chunk)
                raw = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise HttpClientError(f"HTTP {exc.code}：{err_body or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise HttpClientError(f"网络错误：{exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpClientError(f"请求超时（{timeout_s}s）") from exc

    if status >= 400:
        text = raw.decode("utf-8", errors="replace")[:500]
        raise HttpClientError(f"HTTP {status}：{text}")
    return status, raw, resp_headers


def _request_json_sync(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout_s: float,
) -> dict[str, Any]:
    """同步 JSON 请求。"""

    data = None
    req_headers = dict(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")

    _status, raw, _headers = _request_bytes_sync(
        method=method,
        url=url,
        headers=req_headers,
        body=data,
        timeout_s=timeout_s,
    )
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text) if text else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpClientError(f"响应不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise HttpClientError("响应 JSON 根节点必须是对象")
    return parsed


async def request_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout_s: float,
) -> dict[str, Any]:
    """异步 JSON 请求。"""

    return await asyncio.to_thread(
        _request_json_sync,
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout_s=timeout_s,
    )


async def download_bytes(
    *,
    url: str,
    headers: dict[str, str] | None = None,
    timeout_s: float = 60.0,
    max_bytes: int | None = None,
) -> bytes:
    """下载二进制内容。"""

    _status, raw, _headers = await asyncio.to_thread(
        _request_bytes_sync,
        method="GET",
        url=url,
        headers=headers or {"User-Agent": "video-summary-plugin/1.0"},
        body=None,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
    )
    return raw


async def post_json(
    *,
    url: str,
    body: dict[str, Any] | None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """POST JSON 并返回对象。"""

    return await request_json(
        method="POST",
        url=url,
        headers=headers or {"User-Agent": "video-summary-plugin/1.0"},
        body=body,
        timeout_s=timeout_s,
    )


def unwrap_onebot_data(payload: dict[str, Any]) -> dict[str, Any]:
    """从 OneBot/NapCat 响应中提取 data 对象。"""

    if not isinstance(payload, dict):
        raise HttpClientError("NapCat 响应不是对象")
    status = str(payload.get("status") or "").strip().lower()
    if status in {"failed", "error"}:
        raise HttpClientError(f"NapCat 返回失败：{payload.get('message') or payload.get('wording') or payload}")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    # 某些实现直接把字段摊在根上
    return payload


def auth_headers(api_key: str) -> dict[str, str]:
    """构造带 Authorization 的请求头。"""

    headers = {
        "Accept": "application/json",
        "User-Agent": "video-summary-plugin/1.0",
    }
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def extract_chat_text(payload: dict[str, Any]) -> str:
    """从 chat/completions 风格响应中提取文本。"""

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    texts: list[str] = []
                    for part in content:
                        if isinstance(part, dict):
                            text = part.get("text")
                            if isinstance(text, str) and text.strip():
                                texts.append(text.strip())
                    if texts:
                        return "\n".join(texts)
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    for key in ("response", "content", "output", "text", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
