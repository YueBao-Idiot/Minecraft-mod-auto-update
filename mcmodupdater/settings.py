from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import DEFAULT_INSTANCE_DIR, SETTINGS_PATH, portable_path, resolve_portable_path
from .util import read_json, write_json

VALID_LOADERS = ("fabric", "forge", "neoforge")
VALID_PROVIDERS = ("modrinth", "curseforge")


@dataclass
class AppSettings:
    instance: str = "instance"
    loader: str = "fabric"
    game_version: str = ""
    providers: list[str] = field(default_factory=lambda: ["modrinth", "curseforge"])
    prefer_provider: str = "auto"
    curseforge_api_key: str = ""
    curseforge_use_proxy: bool = False
    modrinth_api_base: str = "https://api.modrinth.com/v2"
    curseforge_api_base: str = "https://api.curseforge.com/v1"
    allow_prerelease: bool = False
    copy_configs: bool = True
    config_scope: str = "matched"
    full_instance: bool = True
    max_workers: int = 4
    last_output: str = ""

    @property
    def instance_path(self) -> Path:
        return resolve_portable_path(self.instance, DEFAULT_INSTANCE_DIR)

    def effective_curseforge_key(self) -> str:
        return os.environ.get("CURSEFORGE_API_KEY", "").strip() or self.curseforge_api_key.strip()

    @property
    def effective_modrinth_base(self) -> str:
        return os.environ.get("MODRINTH_API_BASE", "").strip() or self.modrinth_api_base

    @property
    def effective_curseforge_base(self) -> str:
        env_base = os.environ.get("CURSEFORGE_API_BASE", "").strip()
        if env_base:
            return env_base
        # 可选：没有 Key 时走公开只读代理，避免 CurseForge 完全不可用。
        if self.curseforge_use_proxy and not self.curseforge_api_key.strip():
            return "https://api.curse.tools/v1"
        return self.curseforge_api_base

    def normalized(self) -> "AppSettings":
        if self.loader not in VALID_LOADERS:
            self.loader = "fabric"
        self.providers = [item for item in self.providers if item in VALID_PROVIDERS]
        if not self.providers:
            self.providers = ["modrinth"]
        if self.prefer_provider not in ("auto", *VALID_PROVIDERS):
            self.prefer_provider = "auto"
        if self.config_scope not in ("matched", "all", "none"):
            self.config_scope = "matched"
        self.max_workers = max(1, min(8, int(self.max_workers or 4)))
        self.game_version = (self.game_version or "").strip()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "loader": self.loader,
            "game_version": self.game_version,
            "providers": self.providers,
            "prefer_provider": self.prefer_provider,
            "curseforge_api_key": self.curseforge_api_key,
            "curseforge_use_proxy": self.curseforge_use_proxy,
            "modrinth_api_base": self.modrinth_api_base,
            "curseforge_api_base": self.curseforge_api_base,
            "allow_prerelease": self.allow_prerelease,
            "copy_configs": self.copy_configs,
            "config_scope": self.config_scope,
            "full_instance": self.full_instance,
            "max_workers": self.max_workers,
            "last_output": self.last_output,
        }

    def save(self, path: Path = SETTINGS_PATH) -> None:
        write_json(path, self.to_dict())


def load_settings(path: Path = SETTINGS_PATH) -> AppSettings:
    data = read_json(path, {}) or {}
    if not isinstance(data, dict):
        data = {}
    settings = AppSettings()
    for key in (
        "instance",
        "loader",
        "game_version",
        "prefer_provider",
        "curseforge_api_key",
        "modrinth_api_base",
        "curseforge_api_base",
        "config_scope",
        "last_output",
    ):
        if key in data and isinstance(data[key], str):
            setattr(settings, key, data[key])
    if isinstance(data.get("providers"), list):
        settings.providers = [str(item) for item in data["providers"]]
    for key in ("allow_prerelease", "copy_configs", "full_instance", "curseforge_use_proxy"):
        if isinstance(data.get(key), bool):
            setattr(settings, key, data[key])
    if isinstance(data.get("max_workers"), int):
        settings.max_workers = data["max_workers"]
    return settings.normalized()


def make_provider_configs(settings: AppSettings, cache_root: Path):
    from .providers import CurseForgeProvider, ModrinthProvider, ProviderConfig

    providers = []
    for name in settings.providers:
        if name == "modrinth":
            config = ProviderConfig(
                name="modrinth",
                base_url=settings.effective_modrinth_base,
                cache_dir=cache_root / "modrinth",
                allow_prerelease=settings.allow_prerelease,
            )
            providers.append(ModrinthProvider(config))
        elif name == "curseforge":
            config = ProviderConfig(
                name="curseforge",
                base_url=settings.effective_curseforge_base,
                cache_dir=cache_root / "curseforge",
                api_key=settings.effective_curseforge_key(),
                allow_prerelease=settings.allow_prerelease,
            )
            providers.append(CurseForgeProvider(config))
    return providers


def settings_for_display(settings: AppSettings) -> dict[str, Any]:
    data = settings.to_dict()
    data["instance"] = portable_path(settings.instance_path)
    if data.get("curseforge_api_key"):
        data["curseforge_api_key"] = "***"
    return data
