from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import format_datetime, human_size, utc_now

LOADER_LABELS = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
    "unknown": "未知",
}

PROVIDER_LABELS = {
    "modrinth": "Modrinth",
    "curseforge": "CurseForge",
}

STATUS_LABELS = {
    "update": "可更新",
    "latest": "已是最新",
    "no_compatible": "无兼容版本",
    "not_found": "未找到",
    "error": "查询失败",
    "unknown": "无法识别",
    "manual": "需手动处理",
    "skipped": "已跳过",
}

DEPENDENCY_STATE_LABELS = {
    "missing": "新增前置",
    "satisfied": "已满足",
    "optional": "可选前置",
    "removable": "可能可移除",
    "unresolved": "未解析",
    "incompatible": "冲突/不兼容",
    "included": "已内置",
}

RELATION_LABELS = {
    "required": "必需",
    "optional": "可选",
    "embedded": "内置",
    "tool": "工具",
    "incompatible": "不兼容",
    "include": "包含",
    "unknown": "未知",
}


@dataclass
class ModDependency:
    """从本地 jar 元数据中读出的依赖声明。"""

    mod_id: str
    relation: str = "required"
    version_range: str = ""
    side: str = ""
    loader: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "relation": self.relation,
            "version_range": self.version_range,
            "side": self.side,
            "loader": self.loader,
            "reason": self.reason,
        }


@dataclass
class ModFile:
    path: Path
    filename: str
    size: int
    sha1: str = ""
    sha512: str = ""
    murmur2: int | None = None
    mod_ids: list[str] = field(default_factory=list)
    name: str = ""
    version: str = ""
    loader: str = "unknown"
    loaders: list[str] = field(default_factory=list)
    mc_versions: list[str] = field(default_factory=list)
    dependencies: list[ModDependency] = field(default_factory=list)
    metadata_kind: str = "unknown"
    metadata_error: str = ""
    manifest: dict[str, str] = field(default_factory=dict)

    @property
    def mod_id(self) -> str:
        return self.mod_ids[0] if self.mod_ids else ""

    @property
    def display_name(self) -> str:
        return self.name or self.mod_id or self.filename

    @property
    def label(self) -> str:
        version = f" {self.version}" if self.version else ""
        return f"{self.display_name}{version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "filename": self.filename,
            "size": self.size,
            "sha1": self.sha1,
            "sha512": self.sha512,
            "murmur2": self.murmur2,
            "mod_ids": self.mod_ids,
            "mod_id": self.mod_id,
            "name": self.display_name,
            "version": self.version,
            "loader": self.loader,
            "loaders": self.loaders,
            "mc_versions": self.mc_versions,
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "metadata_kind": self.metadata_kind,
            "metadata_error": self.metadata_error,
        }


@dataclass
class RemoteDependency:
    project_id: str = ""
    version_id: str = ""
    dependency_type: str = "required"
    file_name: str = ""
    mod_id: str = ""

    @property
    def is_required(self) -> bool:
        return self.dependency_type == "required"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "version_id": self.version_id,
            "dependency_type": self.dependency_type,
            "file_name": self.file_name,
            "mod_id": self.mod_id,
        }


@dataclass
class ProjectInfo:
    provider: str
    project_id: str
    slug: str
    title: str
    description: str = ""
    url: str = ""
    downloads: int = 0
    icon_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "project_id": self.project_id,
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "downloads": self.downloads,
            "icon_url": self.icon_url,
        }


@dataclass
class VersionInfo:
    provider: str
    project_id: str
    version_id: str
    version_number: str
    name: str = ""
    game_versions: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    published: str = ""
    download_url: str = ""
    filename: str = ""
    dependencies: list[RemoteDependency] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)
    file_size: int = 0
    release_type: str = "release"
    project_slug: str = ""
    project_title: str = ""
    changelog: str = ""

    @property
    def display_version(self) -> str:
        return self.version_number or self.name or self.version_id

    @property
    def is_release(self) -> bool:
        return self.release_type in ("release", "1", "")

    @property
    def manual_url(self) -> str:
        if self.provider == "curseforge" and self.project_slug:
            suffix = f"/files/{self.version_id}" if self.version_id else ""
            return f"https://www.curseforge.com/minecraft/mc-mods/{self.project_slug}{suffix}"
        if self.provider == "modrinth" and self.project_slug:
            return f"https://modrinth.com/mod/{self.project_slug}/version/{self.version_id}"
        return self.download_url

    def supports(self, game_version: str, loader: str) -> bool:
        game_ok = not game_version or game_version in self.game_versions
        loader_aliases = {
            "fabric": {"fabric"},
            "forge": {"forge"},
            "neoforge": {"neoforge", "neo"},
        }
        accepted = loader_aliases.get(loader, {loader})
        loader_ok = not loader or not self.loaders or bool(accepted.intersection({x.lower() for x in self.loaders}))
        return game_ok and loader_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "project_id": self.project_id,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "name": self.name,
            "game_versions": self.game_versions,
            "loaders": self.loaders,
            "published": self.published,
            "download_url": self.download_url,
            "filename": self.filename,
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "hashes": self.hashes,
            "file_size": self.file_size,
            "release_type": self.release_type,
            "project_slug": self.project_slug,
            "project_title": self.project_title,
            "changelog": self.changelog,
        }


@dataclass
class MatchResult:
    project: ProjectInfo
    current_version: VersionInfo | None = None
    method: str = "search"
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.to_dict(),
            "current_version": self.current_version.to_dict() if self.current_version else None,
            "method": self.method,
            "confidence": self.confidence,
        }


@dataclass
class ModPlan:
    mod: ModFile
    status: str = "unknown"
    match: MatchResult | None = None
    target_version: VersionInfo | None = None
    selected: bool = True
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def project(self) -> ProjectInfo | None:
        return self.match.project if self.match else None

    @property
    def current_version(self) -> VersionInfo | None:
        return self.match.current_version if self.match else None

    @property
    def current_version_text(self) -> str:
        if self.current_version:
            return self.current_version.display_version
        return self.mod.version or "—"

    @property
    def target_version_text(self) -> str:
        return self.target_version.display_version if self.target_version else "—"

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.project.provider, "") if self.project else "—"

    @property
    def can_download(self) -> bool:
        return bool(self.selected and self.target_version and self.target_version.download_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod": self.mod.to_dict(),
            "status": self.status,
            "status_label": self.status_label,
            "match": self.match.to_dict() if self.match else None,
            "target_version": self.target_version.to_dict() if self.target_version else None,
            "selected": self.selected,
            "notes": self.notes,
            "error": self.error,
        }


@dataclass
class DependencyItem:
    key: str
    name: str
    provider: str = ""
    project_id: str = ""
    state: str = "unresolved"
    relation: str = "required"
    parents: list[str] = field(default_factory=list)
    resolved_version: VersionInfo | None = None
    notes: list[str] = field(default_factory=list)
    download: bool = False
    source_mod_id: str = ""

    @property
    def state_label(self) -> str:
        return DEPENDENCY_STATE_LABELS.get(self.state, self.state)

    @property
    def relation_label(self) -> str:
        return RELATION_LABELS.get(self.relation, self.relation)

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.project_id:
            return self.project_id
        return self.key

    @property
    def version_text(self) -> str:
        return self.resolved_version.display_version if self.resolved_version else "—"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.display_name,
            "provider": self.provider,
            "project_id": self.project_id,
            "state": self.state,
            "state_label": self.state_label,
            "relation": self.relation,
            "relation_label": self.relation_label,
            "parents": self.parents,
            "resolved_version": self.resolved_version.to_dict() if self.resolved_version else None,
            "notes": self.notes,
            "download": self.download,
            "source_mod_id": self.source_mod_id,
        }


@dataclass
class ConfigItem:
    relative_path: str
    size: int = 0
    owners: list[str] = field(default_factory=list)
    selected: bool = True
    note: str = ""

    @property
    def owner_text(self) -> str:
        return ", ".join(self.owners) if self.owners else "未识别归属"

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "owners": self.owners,
            "selected": self.selected,
            "note": self.note,
        }


@dataclass
class UpdatePlan:
    source_root: Path
    mods_dir: Path
    loader: str
    game_version: str
    providers: list[str]
    detected_loader: str = ""
    detected_game_version: str = ""
    cross_loader: bool = False
    allow_prerelease: bool = False
    created_at: str = field(default_factory=lambda: utc_now().isoformat())
    mods: list[ModPlan] = field(default_factory=list)
    dependencies: list[DependencyItem] = field(default_factory=list)
    configs: list[ConfigItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    report_dir: Path | None = None
    output_dir: Path | None = None

    @property
    def loader_label(self) -> str:
        return LOADER_LABELS.get(self.loader, self.loader)

    @property
    def updates(self) -> list[ModPlan]:
        return [mod for mod in self.mods if mod.status == "update"]

    @property
    def latest(self) -> list[ModPlan]:
        return [mod for mod in self.mods if mod.status == "latest"]

    @property
    def missing(self) -> list[ModPlan]:
        return [
            mod
            for mod in self.mods
            if mod.status in ("not_found", "no_compatible", "error", "unknown", "manual")
        ]

    @property
    def selected_updates(self) -> list[ModPlan]:
        return [mod for mod in self.updates if mod.selected and mod.target_version]

    @property
    def missing_dependencies(self) -> list[DependencyItem]:
        return [dep for dep in self.dependencies if dep.state == "missing"]

    @property
    def optional_dependencies(self) -> list[DependencyItem]:
        return [dep for dep in self.dependencies if dep.state == "optional"]

    @property
    def removable_dependencies(self) -> list[DependencyItem]:
        return [dep for dep in self.dependencies if dep.state == "removable"]

    @property
    def unresolved_dependencies(self) -> list[DependencyItem]:
        return [dep for dep in self.dependencies if dep.state == "unresolved"]

    @property
    def selected_configs(self) -> list[ConfigItem]:
        return [item for item in self.configs if item.selected]

    @property
    def download_count(self) -> int:
        direct = len([mod for mod in self.selected_updates if mod.target_version and mod.target_version.download_url])
        dependencies = len(
            [
                dep
                for dep in self.dependencies
                if dep.state == "missing" and dep.download and dep.resolved_version and dep.resolved_version.download_url
            ]
        )
        return direct + dependencies

    def summary(self) -> dict[str, Any]:
        return {
            "loader": self.loader_label,
            "game_version": self.game_version,
            "total_mods": len(self.mods),
            "updates": len(self.updates),
            "selected_updates": len(self.selected_updates),
            "latest": len(self.latest),
            "not_found": len([m for m in self.mods if m.status == "not_found"]),
            "no_compatible": len([m for m in self.mods if m.status == "no_compatible"]),
            "errors": len([m for m in self.mods if m.status == "error"]),
            "missing_dependencies": len(self.missing_dependencies),
            "optional_dependencies": len(self.optional_dependencies),
            "removable_dependencies": len(self.removable_dependencies),
            "unresolved_dependencies": len(self.unresolved_dependencies),
            "configs": len(self.selected_configs),
            "downloads": self.download_count,
            "warnings": len(self.warnings),
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": "MC 模组更新助手",
            "summary": self.summary(),
            "source_root": str(self.source_root),
            "mods_dir": str(self.mods_dir),
            "loader": self.loader,
            "loader_label": self.loader_label,
            "game_version": self.game_version,
            "providers": self.providers,
            "detected_loader": self.detected_loader,
            "detected_game_version": self.detected_game_version,
            "cross_loader": self.cross_loader,
            "allow_prerelease": self.allow_prerelease,
            "created_at": self.created_at,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "mods": [mod.to_dict() for mod in self.mods],
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "configs": [item.to_dict() for item in self.configs],
            "warnings": self.warnings,
            "errors": self.errors,
            "report_dir": str(self.report_dir) if self.report_dir else "",
            "output_dir": str(self.output_dir) if self.output_dir else "",
        }

    def row_for(self, mod: ModPlan) -> tuple[str, ...]:
        project = mod.project
        return (
            mod.status_label,
            mod.mod.display_name,
            mod.current_version_text,
            mod.target_version_text,
            mod.provider_label,
            project.title if project else "—",
            (project.slug if project else ""),
            " / ".join(mod.notes),
        )

    @property
    def created_display(self) -> str:
        try:
            return datetime.fromisoformat(self.created_at).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return format_datetime(self.created_at)
