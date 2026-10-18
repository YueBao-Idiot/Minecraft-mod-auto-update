from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MC_VERSION_RE = re.compile(
    r"(?<!\d)((?:1\.\d{1,2}(?:\.\d{1,2})?)|(?:2\d\.\d{1,2}(?:\.\d{1,2})?))(?!\d)"
)
VERSION_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z][0-9A-Za-z.+_-]*)?)"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_mc_version(value: str) -> bool:
    return bool(re.fullmatch(r"(?:1\.\d{1,2}(?:\.\d{1,2})?|2\d\.\d{1,2}(?:\.\d{1,2})?)", value.strip()))


def is_plausible_mc_version(value: str) -> bool:
    """过滤掉 1.0.0 这类模组版本号，减少从文件名误判游戏版本。"""
    if not is_mc_version(value):
        return False
    parts = mc_version_key(value)
    if len(parts) < 2:
        return False
    major, minor = parts[0], parts[1]
    if major >= 20:
        return True  # 2026 起 Mojang 使用 26.x 版本号
    if minor >= 7:
        return True
    return value in {"1.0", "1.0.1", "1.1", "1.2.5", "1.3.2", "1.4.7", "1.5.2", "1.6.4"}


def mc_version_key(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in re.findall(r"\d+", value)[:3])
    except ValueError:
        return (0,)


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def normalize_id(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "", (value or "").lower())


def clean_filename(name: str) -> str:
    stem = name[:-4] if name.lower().endswith(".jar") else name
    stem = re.sub(r"\.jar\.disabled$", "", stem, flags=re.I)
    return stem


def guess_name_and_version(filename: str) -> tuple[str, str]:
    """从 jar 文件名猜测项目名与版本，作为元数据缺失时的回退。"""
    stem = clean_filename(filename)
    # 去掉常见的加载器/平台后缀
    tokens = re.split(r"[-_+ ]+", stem)
    version_index = None
    for index, token in enumerate(tokens):
        if re.fullmatch(r"\d+(?:\.\d+)+(?:[-+].*)?", token):
            version_index = index
            break
    if version_index is None:
        return stem, ""
    name = "-".join(tokens[:version_index]).strip("-_ ")
    version = "-".join(tokens[version_index:]).strip("-_ ")
    if not name:
        name = stem
    return name, version


def extract_mc_versions(text: str) -> list[str]:
    found: list[str] = []
    for match in MC_VERSION_RE.finditer(text or ""):
        version = match.group(1)
        if version not in found:
            found.append(version)
    return found


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return None


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    chunk: list[Any] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def retry_delay(attempt: int, base: float = 1.0) -> float:
    return min(20.0, base * (2 ** attempt))


class Stopwatch:
    def __init__(self) -> None:
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def __str__(self) -> str:
        seconds = int(self.elapsed)
        return f"{seconds // 60}分{seconds % 60}秒" if seconds >= 60 else f"{seconds}秒"
