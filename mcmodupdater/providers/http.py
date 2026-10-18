from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..util import read_json, retry_delay, utc_now, write_json


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class HttpError(ProviderError):
    pass


class HttpClient:
    """带磁盘缓存、退避重试和线程锁的 JSON HTTP 客户端。

    运行时只使用 Python 标准库，方便打包成单文件 exe。
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        user_agent: str,
        timeout: float = 30.0,
        cache_ttl: int = 6 * 60 * 60,
        min_interval: float = 0.08,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request = 0.0

    def _cache_path(self, method: str, url: str, body: bytes | None) -> Path:
        digest = hashlib.sha256()
        digest.update(method.encode())
        digest.update(b"\0")
        digest.update(url.encode())
        digest.update(b"\0")
        if body:
            digest.update(body)
        return self.cache_dir / f"{digest.hexdigest()}.json"

    def _read_cache(self, path: Path) -> Any | None:
        payload = read_json(path)
        if not isinstance(payload, dict):
            return None
        try:
            age = time.time() - float(payload.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if age > self.cache_ttl:
            return None
        return payload.get("data")

    def _write_cache(self, path: Path, data: Any) -> None:
        try:
            write_json(path, {"ts": time.time(), "saved_at": utc_now().isoformat(), "data": data})
        except OSError:
            pass

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        cache: bool = True,
        retries: int = 3,
    ) -> Any:
        body: bytes | None = None
        request_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if headers:
            request_headers.update(headers)
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        cache_path = self._cache_path(method, url, body)
        if cache:
            cached = self._read_cache(cache_path)
            if cached is not None:
                return cached

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle()
            request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProviderError(f"接口返回了无法解析的数据：{url}", url=url) from exc
                if cache:
                    self._write_cache(cache_path, data)
                return data
            except urllib.error.HTTPError as exc:
                status = exc.code
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                except OSError:
                    pass
                last_error = HttpError(
                    f"HTTP {status}：{self._friendly_status(status, detail)}",
                    status=status,
                    url=url,
                )
                if status in (429, 408) or 500 <= status < 600:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else retry_delay(attempt)
                    time.sleep(min(30.0, delay + random.uniform(0, 0.4)))
                    continue
                raise last_error from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = ProviderError(f"网络连接失败：{exc}", url=url)
                if attempt < retries:
                    time.sleep(retry_delay(attempt) + random.uniform(0, 0.4))
                    continue
                raise last_error from exc
        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderError(f"请求失败：{url}", url=url)

    @staticmethod
    def _friendly_status(status: int, detail: str) -> str:
        if status == 401:
            return "API Key 无效或缺失"
        if status == 403:
            return "没有访问权限（可能是 API Key 失效或达到限额）"
        if status == 404:
            return "资源不存在"
        if status == 429:
            return "请求过于频繁，请稍后再试"
        if status >= 500:
            return "远端服务暂时不可用"
        return detail or "请求失败"

    @staticmethod
    def quote_json(value: Any) -> str:
        return urllib.parse.quote(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
