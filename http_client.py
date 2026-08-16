"""轻量 HTTP 客户端（stdlib），用于下载视频与直调外部多模态 API。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.response import addinfourl


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


_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_BLOCKED_LITERAL_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
}


def _host_from_netloc(netloc: str) -> str:
    host = (netloc or "").strip()
    if not host:
        return ""
    if host.startswith("["):
        # [IPv6]:port
        end = host.find("]")
        if end > 0:
            return host[1:end].strip().lower()
        return host.strip("[]").lower()
    if ":" in host and _IPV4_RE.match(host.split(":", 1)[0]):
        return host.split(":", 1)[0].strip().lower()
    # bare hostname or IPv4 without port; IPv6 without brackets is invalid for URL netloc
    if host.count(":") == 1 and not host.startswith(":"):
        # hostname:port
        return host.split(":", 1)[0].strip().lower()
    return host.strip().lower()


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """是否应拦截的地址（私网/环回/链路本地/元数据等）。"""

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    # 部分实现把 CGNAT 100.64/10 标为 is_private；再显式兜底
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return True
        if ip in ipaddress.ip_network("169.254.0.0/16"):
            return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in ipaddress.ip_network("fc00::/7") or ip in ipaddress.ip_network("fe80::/10"):
            return True
    return False


def is_private_or_local_host(host: str) -> bool:
    """判断 host 是否像内网/本机/云元数据地址。"""

    hostname = (host or "").strip().lower().rstrip(".")
    if not hostname:
        return True
    if hostname in _BLOCKED_LITERAL_HOSTS:
        return True
    if hostname.endswith(".localhost") or hostname.endswith(".local"):
        return True
    # 去 IPv6 区号
    if "%" in hostname:
        hostname = hostname.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(hostname)
        return _is_blocked_ip(ip)
    except ValueError:
        pass
    # 解析 DNS，防止 localhost / 内网域名绕过。
    # 解析失败不在此处拦截（交给真实请求报错），避免 DNS 抖动把公网域名误杀。
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            return True
    return False


def is_loopback_host(host: str) -> bool:
    """是否本机环回地址（允许作为 NapCat 专用入口）。"""

    hostname = (host or "").strip().lower().rstrip(".")
    if not hostname:
        return False
    if hostname in {"localhost", "localhost.localdomain"}:
        return True
    if hostname.endswith(".localhost"):
        return True
    if "%" in hostname:
        hostname = hostname.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(hostname)
        return bool(ip.is_loopback)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return False
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if not ip.is_loopback:
            return False
    return True


def validate_http_url(
    url: str,
    *,
    allow_private: bool = False,
    require_loopback: bool = False,
) -> None:
    """校验 http/https URL，并可拦截内网/本机地址。"""

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HttpClientError(f"非法 URL：{url!r}（需要 http/https）")
    host = _host_from_netloc(parsed.netloc)
    if not host:
        raise HttpClientError(f"非法 URL：{url!r}（缺少 host）")

    if require_loopback:
        if not is_loopback_host(host):
            raise HttpClientError(
                f"仅允许本机环回地址（127.0.0.1 / localhost / ::1），拒绝：{host!r}"
            )
        return

    if not allow_private and is_private_or_local_host(host):
        raise HttpClientError(
            f"拒绝访问内网/本机/元数据地址：{host!r}（如需例外请开启 allow_private_ips）"
        )


_MAX_REDIRECTS = 5


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跟随重定向前再次校验目标 URL，避免公网 302 打到内网。"""

    def __init__(self, *, allow_private: bool, require_loopback: bool, max_redirects: int) -> None:
        super().__init__()
        self._allow_private = allow_private
        self._require_loopback = require_loopback
        self._max_redirects = max_redirects
        self._hops = 0

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: addinfourl,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._hops += 1
        if self._hops > self._max_redirects:
            raise HttpClientError(f"重定向次数超过限制（{self._max_redirects}）")
        validate_http_url(
            newurl,
            allow_private=self._allow_private,
            require_loopback=self._require_loopback,
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(*, allow_private: bool, require_loopback: bool) -> urllib.request.OpenerDirector:
    redirect = _ValidatingRedirectHandler(
        allow_private=allow_private,
        require_loopback=require_loopback,
        max_redirects=_MAX_REDIRECTS,
    )
    return urllib.request.build_opener(redirect)


def _request_bytes_sync(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_s: float,
    max_bytes: int | None = None,
    allow_private: bool = False,
    require_loopback: bool = False,
) -> tuple[int, bytes, dict[str, str]]:
    """同步请求，返回 status/body/headers。"""

    validate_http_url(
        url,
        allow_private=allow_private,
        require_loopback=require_loopback,
    )
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    opener = _build_opener(allow_private=allow_private, require_loopback=require_loopback)
    try:
        with opener.open(
            request,
            timeout=max(1.0, float(timeout_s)),
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
    except HttpClientError:
        raise
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise HttpClientError(f"HTTP {exc.code}：{err_body or exc.reason}") from exc
    except urllib.error.URLError as extra:
        reason = getattr(extra, "reason", extra)
        if isinstance(reason, HttpClientError):
            raise reason from extra
        raise HttpClientError(f"网络错误：{reason}") from extra
    except TimeoutError as extra:
        raise HttpClientError(f"请求超时（{timeout_s}s）") from extra

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
    allow_private: bool = False,
    require_loopback: bool = False,
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
        allow_private=allow_private,
        require_loopback=require_loopback,
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
    allow_private: bool = False,
    require_loopback: bool = False,
) -> dict[str, Any]:
    """异步 JSON 请求。"""

    return await asyncio.to_thread(
        _request_json_sync,
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout_s=timeout_s,
        allow_private=allow_private,
        require_loopback=require_loopback,
    )


async def download_bytes(
    *,
    url: str,
    headers: dict[str, str] | None = None,
    timeout_s: float = 60.0,
    max_bytes: int | None = None,
    allow_private: bool = False,
) -> bytes:
    """下载二进制内容（默认拒绝内网/本机地址，防 SSRF）。"""

    _status, raw, _headers = await asyncio.to_thread(
        _request_bytes_sync,
        method="GET",
        url=url,
        headers=headers or {"User-Agent": "video-summary-plugin/1.0"},
        body=None,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        allow_private=allow_private,
        require_loopback=False,
    )
    return raw


async def post_json(
    *,
    url: str,
    body: dict[str, Any] | None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 60.0,
    allow_private: bool = False,
    require_loopback: bool = False,
) -> dict[str, Any]:
    """POST JSON 并返回对象。"""

    return await request_json(
        method="POST",
        url=url,
        headers=headers or {"User-Agent": "video-summary-plugin/1.0"},
        body=body,
        timeout_s=timeout_s,
        allow_private=allow_private,
        require_loopback=require_loopback,
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
