from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import __version__
from ..models import MatchResult, ModFile, ProjectInfo, VersionInfo
from ..util import parse_datetime
from .http import HttpClient, ProviderError

USER_AGENT = f"MinecraftModAutoUpdater/{__version__} (local portable tool)"


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    cache_dir: Path
    api_key: str = ""
    allow_prerelease: bool = False
    timeout: float = 30.0
    cache_ttl: int = 6 * 60 * 60
    headers: dict[str, str] = field(default_factory=dict)


def select_latest_version(
    versions: Iterable[VersionInfo],
    game_version: str,
    loader: str,
    *,
    allow_prerelease: bool = False,
) -> VersionInfo | None:
    compatible: list[VersionInfo] = []
    for version in versions:
        if not version.supports(game_version, loader):
            continue
        if not version.download_url:
            # 仍保留为手动下载候选，但优先级最低。
            pass
        compatible.append(version)
    if not compatible:
        return None

    def sort_key(item: VersionInfo):
        published = parse_datetime(item.published)
        timestamp = published.timestamp() if published else 0.0
        release_rank = 2 if item.is_release else (1 if item.release_type in ("beta", "2") else 0)
        return (release_rank, timestamp, item.version_id)

    if not allow_prerelease:
        releases = [item for item in compatible if item.is_release]
        if not releases:
            return None
        compatible = releases
        return max(compatible, key=sort_key)

    # 允许测试版时按发布时间取最新；时间缺失时再优先稳定版。
    def prerelease_key(item: VersionInfo):
        published = parse_datetime(item.published)
        timestamp = published.timestamp() if published else 0.0
        release_rank = 2 if item.is_release else (1 if item.release_type in ("beta", "2") else 0)
        return (timestamp, release_rank, item.version_id)

    return max(compatible, key=prerelease_key)


class ModProvider(ABC):
    name = "base"
    label = "Base"

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.http = HttpClient(
            config.cache_dir,
            user_agent=USER_AGENT,
            timeout=config.timeout,
            cache_ttl=config.cache_ttl,
        )
        self.allow_prerelease = config.allow_prerelease
        self._project_cache: dict[str, ProjectInfo] = {}
        self._version_cache: dict[str, VersionInfo] = {}

    @property
    def available(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str:
        return ""

    @abstractmethod
    def match_by_hashes(self, mods: Iterable[ModFile]) -> dict[str, MatchResult]:
        """按文件哈希精确匹配，返回以 jar 绝对路径为键的结果。"""

    @abstractmethod
    def search_mod(self, mod: ModFile, game_version: str, loader: str) -> MatchResult | None:
        """在缺少哈希匹配时按 mod id / 名称搜索。"""

    @abstractmethod
    def get_project(self, project_id: str) -> ProjectInfo | None:
        ...

    @abstractmethod
    def get_versions(self, project_id: str, game_version: str, loader: str) -> list[VersionInfo]:
        ...

    @abstractmethod
    def get_version(self, version_id: str) -> VersionInfo | None:
        ...

    def latest_version(self, project_id: str, game_version: str, loader: str) -> VersionInfo | None:
        versions = self.get_versions(project_id, game_version, loader)
        return select_latest_version(
            versions,
            game_version,
            loader,
            allow_prerelease=self.allow_prerelease,
        )

    def search_url(self, query: str, game_version: str, loader: str) -> str:
        return ""

    def describe_error(self, exc: Exception) -> str:
        if isinstance(exc, ProviderError):
            return f"{self.label}：{exc}"
        return f"{self.label}：{type(exc).__name__}: {exc}"

    def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        return self.http.request_json(method, url, **kwargs)

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
