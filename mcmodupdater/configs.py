from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterable

from .models import ConfigItem, ModFile, ModPlan
from .util import normalize_name

CONFIG_ROOTS = ("config", "defaultconfigs")
IGNORED_DIRS = {"cache", "logs", "backup", "backups", ".cache", "__pycache__"}
IGNORED_SUFFIXES = {".log", ".bak", ".tmp", ".old", ".lock", ".pid", ".dat_old"}
IGNORED_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
GENERIC_KEYS = {"config", "defaultconfigs", "minecraft", "forge", "fabric", "neoforge", "common", "client", "server"}


def _mod_keys(mods: Iterable[ModFile]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for mod in mods:
        for value in [*mod.mod_ids, mod.name]:
            key = normalize_name(value)
            if len(key) >= 3 and key not in GENERIC_KEYS:
                result.append((key, mod.mod_id or mod.display_name))
    # 长键优先，避免 "create" 抢走 "createaddition"。
    result.sort(key=lambda item: len(item[0]), reverse=True)
    return result


def _owners_from_name(relative: Path, keys: list[tuple[str, str]]) -> list[str]:
    components = [relative.stem, *[parent.stem for parent in relative.parents if parent.name not in CONFIG_ROOTS]]
    candidates = []
    for component in components:
        normalized = normalize_name(component)
        if normalized:
            candidates.append(normalized)
        for token in re.split(r"[-_.\s]+", component):
            token = normalize_name(token)
            if token:
                candidates.append(token)
    owners: list[str] = []
    for candidate in candidates:
        for key, owner in keys:
            if candidate == key or candidate.startswith(key):
                if owner not in owners:
                    owners.append(owner)
                break
    return owners


def _owners_from_content(path: Path, keys: list[tuple[str, str]]) -> list[str]:
    if path.stat().st_size > 2 * 1024 * 1024:
        return []
    try:
        with path.open("rb") as handle:
            raw = handle.read(256 * 1024)
    except OSError:
        return []
    text = raw.decode("utf-8", errors="ignore").lower()
    best: tuple[int, str] | None = None
    for key, owner in keys:
        if len(key) < 4 or key in GENERIC_KEYS:
            continue
        count = len(re.findall(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text))
        if count and (best is None or count > best[0]):
            best = (count, owner)
    return [best[1]] if best and best[0] >= 1 else []


def scan_configs(
    root: Path,
    mods: Iterable[ModFile],
    *,
    scope: str = "matched",
    max_files: int = 5000,
) -> list[ConfigItem]:
    """扫描配置并将其归属到本地 mod。

    scope:
      - matched：只默认勾选能归属到已安装 mod 的配置
      - all：默认勾选全部配置
      - none：全部不勾选
    """
    keys = _mod_keys(mods)
    items: list[ConfigItem] = []
    root = root.resolve()
    for folder_name in CONFIG_ROOTS:
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if len(items) >= max_files:
                break
            if not path.is_file():
                continue
            lower_name = path.name.lower()
            if lower_name in IGNORED_NAMES or path.suffix.lower() in IGNORED_SUFFIXES:
                continue
            try:
                relative = path.relative_to(root)
                size = path.stat().st_size
            except OSError:
                continue
            if any(part.lower() in IGNORED_DIRS for part in relative.parts):
                continue
            owners = _owners_from_name(relative.relative_to(folder_name), keys)
            if not owners:
                owners = _owners_from_content(path, keys)
            if scope == "all":
                selected = True
                note = "已选择迁移全部配置" if not owners else "归属：" + ", ".join(owners)
            elif scope == "none":
                selected = False
                note = "未选择迁移"
            else:
                selected = bool(owners)
                note = "功能性模组配置" if owners else "未识别归属，默认不迁移"
            if owners and len(owners) > 1:
                note += "；可能被多个模组共用"
            items.append(
                ConfigItem(
                    relative_path=str(relative).replace("\\", "/"),
                    size=size,
                    owners=owners,
                    selected=selected,
                    note=note,
                )
            )
    items.sort(key=lambda item: item.relative_path.lower())
    return items


def enrich_config_owners(items: Iterable[ConfigItem], plans: Iterable[ModPlan]) -> None:
    """用在线项目 slug 补充配置归属，例如 config/sodium-options.json。"""
    key_to_display: dict[str, str] = {}
    for plan in plans:
        project = plan.project
        if not project:
            continue
        for value in (project.slug, project.title, plan.mod.mod_id, plan.mod.name):
            key = normalize_name(value)
            if len(key) >= 3 and key not in GENERIC_KEYS:
                key_to_display.setdefault(key, plan.mod.mod_id or project.title)
    for item in items:
        if item.owners:
            continue
        relative = Path(item.relative_path)
        stem = normalize_name(relative.stem)
        for key, owner in key_to_display.items():
            if stem == key or stem.startswith(key):
                item.owners = [owner]
                item.selected = True
                item.note = "根据在线项目名识别为功能性模组配置"
                break


def copy_configs(source_root: Path, destination_root: Path, items: Iterable[ConfigItem]) -> tuple[int, list[str]]:
    copied = 0
    errors: list[str] = []
    for item in items:
        if not item.selected:
            continue
        source = source_root / item.relative_path
        if not source.is_file():
            errors.append(f"{item.relative_path}: 源文件不存在")
            continue
        destination = destination_root / item.relative_path
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied += 1
        except OSError as exc:
            errors.append(f"{item.relative_path}: {exc}")
    return copied, errors
