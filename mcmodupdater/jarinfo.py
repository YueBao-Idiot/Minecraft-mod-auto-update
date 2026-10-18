from __future__ import annotations

import hashlib
import json
import re
import struct
import tomllib
import zipfile
from pathlib import Path
from typing import Any, Callable

from .models import ModDependency, ModFile
from .util import extract_mc_versions, guess_name_and_version, is_plausible_mc_version, read_json, write_json

BUILTIN_MOD_IDS = {
    "minecraft",
    "java",
    "fabricloader",
    "fabric-loader",
    "fabric_loader",
    "fabric",
    "quilt",
    "forge",
    "neoforge",
    "neo",
    "quilt_loader",
    "quiltloader",
    "quilt-loader",
    "javafml",
    "lowcodefml",
    "mixin",
    "mixinextras",
    "fml",
    "fmlloader",
    "forge_gradle",
}

LOADER_DIRS = {
    "fabric": "fabric",
    "quilt": "quilt",
    "neoforge": "neoforge",
    "forge": "forge",
}


class HashCache:
    """jar 哈希缓存。内容不变时避免重复读取大文件。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict[str, Any]] = read_json(path, {}) or {}
        self.dirty = False

    def key(self, path: Path) -> str:
        try:
            stat = path.stat()
            return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        except OSError:
            return str(path.resolve())

    def get(self, path: Path) -> dict[str, Any] | None:
        return self.data.get(self.key(path))

    def put(self, path: Path, values: dict[str, Any]) -> None:
        self.data[self.key(path)] = values
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        # 只保留最近 5000 条，避免缓存无限增长。
        if len(self.data) > 5000:
            self.data = dict(list(self.data.items())[-5000:])
        write_json(self.path, self.data)
        self.dirty = False


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _murmur2_stream(handle, length: int, seed: int = 1) -> int:
    """MurmurHash2，32 位，与 CurseForge 指纹算法一致。"""
    m = 0x5BD1E995
    r = 24
    h = (seed ^ length) & 0xFFFFFFFF
    remaining = length
    while remaining >= 4:
        data = handle.read(4)
        if len(data) < 4:
            break
        k = struct.unpack("<I", data)[0]
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        remaining -= 4
    if remaining:
        data = handle.read(remaining)
        if len(data) >= 3:
            h ^= data[2] << 16
        if len(data) >= 2:
            h ^= data[1] << 8
        if len(data) >= 1:
            h ^= data[0]
            h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    # CurseForge API 的 int32 指纹按无符号整数返回、按无符号整数提交。
    return h & 0xFFFFFFFF


def curseforge_fingerprint(path: Path) -> int:
    """计算 CurseForge 文件指纹。

    算法为 MurmurHash2(seed=1)，并在哈希前剔除空格、制表符、CR、LF。
    """
    whitespace = b"\t\n\r "
    filtered_length = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            filtered_length += len(block) - sum(block.count(ch) for ch in whitespace)
    with path.open("rb") as handle:
        result = _murmur2_stream(_WhitespaceFilter(handle), filtered_length)
    return result


class _WhitespaceFilter:
    """只向 MurmurHash2 暴露过滤后的字节。"""

    def __init__(self, handle) -> None:
        self.handle = handle
        self.buffer = bytearray()
        self.eof = False

    def read(self, size: int) -> bytes:
        while len(self.buffer) < size and not self.eof:
            block = self.handle.read(max(4096, size * 2))
            if not block:
                self.eof = True
                break
            self.buffer.extend(block.translate(None, b"\t\n\r "))
        out = bytes(self.buffer[:size])
        del self.buffer[:size]
        return out


def parse_manifest(raw: bytes) -> dict[str, str]:
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    logical: list[str] = []
    for line in text.split("\n"):
        if line.startswith(" ") and logical:
            logical[-1] += line[1:]
        elif line.strip():
            logical.append(line)
    result: dict[str, str] = {}
    for line in logical:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _manifest_version(manifest: dict[str, str]) -> str:
    for key in (
        "Implementation-Version",
        "Specification-Version",
        "Bundle-Version",
        "Maven-Version",
    ):
        value = manifest.get(key, "").strip()
        if value and "${" not in value and "@" not in value:
            return value
    return ""


def _good_meta_version(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or value.startswith("${") or (value.startswith("@") and value.endswith("@")):
        return ""
    return value


def _dep_relation(*, mandatory: bool = True, kind: str = "") -> str:
    if kind:
        return kind
    return "required" if mandatory else "optional"


def _parse_fabric(data: dict[str, Any], loader: str, manifest: dict[str, str], filename: str) -> dict[str, Any]:
    mod_id = str(data.get("id") or "").strip()
    name = str(data.get("name") or mod_id).strip()
    version = _good_meta_version(data.get("version")) or _manifest_version(manifest)
    dependencies: list[ModDependency] = []
    relation_keys = {
        "depends": "required",
        "recommends": "optional",
        "suggests": "optional",
        "breaks": "incompatible",
        "conflicts": "incompatible",
    }
    mc_versions: list[str] = []
    for key, relation in relation_keys.items():
        group = data.get(key)
        if not isinstance(group, dict):
            continue
        for dep_id, constraint in group.items():
            dep_id = str(dep_id)
            if dep_id.lower() in BUILTIN_MOD_IDS:
                if dep_id.lower() == "minecraft":
                    mc_versions.extend(extract_mc_versions(str(constraint)))
                continue
            if isinstance(constraint, list):
                constraint = ", ".join(str(item) for item in constraint)
            dependencies.append(
                ModDependency(
                    mod_id=dep_id,
                    relation=relation,
                    version_range=str(constraint or ""),
                    loader=loader,
                    reason=key,
                )
            )
    minecraft_dep = data.get("depends", {}).get("minecraft") if isinstance(data.get("depends"), dict) else None
    if minecraft_dep:
        mc_versions.extend(extract_mc_versions(str(minecraft_dep)))
    return {
        "mod_ids": [mod_id] if mod_id else [],
        "name": name,
        "version": version,
        "dependencies": dependencies,
        "mc_versions": mc_versions,
    }


def _parse_quilt(data: dict[str, Any], manifest: dict[str, str]) -> dict[str, Any]:
    loader_data = data.get("quilt_loader") if isinstance(data.get("quilt_loader"), dict) else data
    mod_id = str(loader_data.get("id") or "").strip()
    metadata = loader_data.get("metadata") if isinstance(loader_data.get("metadata"), dict) else {}
    name = str(metadata.get("name") or mod_id).strip()
    version = _good_meta_version(loader_data.get("version")) or _manifest_version(manifest)
    dependencies: list[ModDependency] = []
    mc_versions: list[str] = []
    depends = loader_data.get("depends") if isinstance(loader_data.get("depends"), list) else []
    for item in depends:
        if not isinstance(item, dict):
            continue
        dep_id = str(item.get("id") or "").strip()
        if not dep_id:
            continue
        if dep_id.lower() == "minecraft":
            mc_versions.extend(extract_mc_versions(str(item.get("versions") or "")))
            continue
        if dep_id.lower() in BUILTIN_MOD_IDS:
            continue
        dependencies.append(
            ModDependency(
                mod_id=dep_id,
                relation="required",
                version_range=str(item.get("versions") or ""),
                loader="quilt",
                reason="depends",
            )
        )
    return {
        "mod_ids": [mod_id] if mod_id else [],
        "name": name,
        "version": version,
        "dependencies": dependencies,
        "mc_versions": mc_versions,
    }


def _toml_structures(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    raw_mods = data.get("mods")
    mods: list[dict[str, Any]] = []
    if isinstance(raw_mods, list):
        mods = [item for item in raw_mods if isinstance(item, dict)]
    elif isinstance(raw_mods, dict):
        mods = [raw_mods]
    dependencies: dict[str, list[dict[str, Any]]] = {}
    raw_deps = data.get("dependencies")
    if isinstance(raw_deps, dict):
        for key, value in raw_deps.items():
            if isinstance(value, list):
                dependencies[str(key)] = [item for item in value if isinstance(item, dict)]
            elif isinstance(value, dict):
                dependencies[str(key)] = [value]
    return mods, dependencies


def _parse_forge_toml(
    data: dict[str, Any], loader: str, manifest: dict[str, str], filename: str
) -> dict[str, Any]:
    mods, dependency_groups = _toml_structures(data)
    mod_ids: list[str] = []
    mod_versions: list[str] = []
    name = ""
    for entry in mods:
        mod_id = str(entry.get("modId") or "").strip()
        if mod_id:
            mod_ids.append(mod_id)
        version = _good_meta_version(entry.get("version")) or _manifest_version(manifest)
        if version:
            mod_versions.append(version)
        if not name:
            name = str(entry.get("displayName") or entry.get("modId") or "").strip()
    dependencies: list[ModDependency] = []
    mc_versions: list[str] = []
    for owner_id, entries in dependency_groups.items():
        if mod_ids and owner_id not in mod_ids:
            # 依赖表通常按模组 id 分组；仍保留全部，避免多模组 jar 漏项。
            pass
        for item in entries:
            dep_id = str(item.get("modId") or "").strip()
            if not dep_id:
                continue
            range_text = str(item.get("versionRange") or "")
            mandatory = bool(item.get("mandatory", True))
            if dep_id.lower() == "minecraft":
                mc_versions.extend(extract_mc_versions(range_text))
                continue
            if dep_id.lower() in BUILTIN_MOD_IDS:
                continue
            dependencies.append(
                ModDependency(
                    mod_id=dep_id,
                    relation=_dep_relation(mandatory=mandatory),
                    version_range=range_text,
                    side=str(item.get("side") or ""),
                    loader=loader,
                    reason="dependencies",
                )
            )
    return {
        "mod_ids": mod_ids,
        "name": name,
        "version": mod_versions[0] if mod_versions else _manifest_version(manifest),
        "dependencies": dependencies,
        "mc_versions": mc_versions,
    }


def _parse_mcmod_info(data: Any, manifest: dict[str, str]) -> dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("modList"), list):
        data = data["modList"]
    entries = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    mod_ids: list[str] = []
    name = ""
    version = ""
    mc_versions: list[str] = []
    dependencies: list[ModDependency] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mod_id = str(entry.get("modid") or entry.get("modId") or "").strip()
        if mod_id:
            mod_ids.append(mod_id)
        if not name:
            name = str(entry.get("name") or mod_id).strip()
        if not version:
            version = _good_meta_version(entry.get("version")) or _manifest_version(manifest)
        mc_versions.extend(extract_mc_versions(str(entry.get("mcversion") or entry.get("mcVersion") or "")))
        for key, relation in (("requiredMods", "required"), ("dependencies", "required")):
            raw_deps = entry.get(key)
            if isinstance(raw_deps, list):
                for dep in raw_deps:
                    dep_id = str(dep).split("@", 1)[0].strip()
                    if dep_id and dep_id.lower() not in BUILTIN_MOD_IDS:
                        dependencies.append(
                            ModDependency(mod_id=dep_id, relation=relation, loader="forge", reason=key)
                        )
    return {
        "mod_ids": mod_ids,
        "name": name,
        "version": version,
        "dependencies": dependencies,
        "mc_versions": mc_versions,
    }


def _merge_metadata(parts: list[tuple[str, dict[str, Any]]], manifest: dict[str, str], filename: str) -> dict[str, Any]:
    mod_ids: list[str] = []
    loaders: list[str] = []
    dependencies: list[ModDependency] = []
    mc_versions: list[str] = []
    name = ""
    version = ""
    for loader, part in parts:
        if loader not in loaders:
            loaders.append(loader)
        for mod_id in part.get("mod_ids", []):
            if mod_id and mod_id not in mod_ids:
                mod_ids.append(mod_id)
        for dep in part.get("dependencies", []):
            if not any(
                existing.mod_id == dep.mod_id
                and existing.relation == dep.relation
                and existing.version_range == dep.version_range
                for existing in dependencies
            ):
                dependencies.append(dep)
        for mc_version in part.get("mc_versions", []):
            if mc_version not in mc_versions:
                mc_versions.append(mc_version)
        if not name and part.get("name"):
            name = str(part["name"])
        if not version and part.get("version"):
            version = str(part["version"])
    guessed_name, guessed_version = guess_name_and_version(filename)
    if not name:
        name = guessed_name
    if not version:
        version = _manifest_version(manifest) or guessed_version
    if not mc_versions:
        mc_versions = [value for value in extract_mc_versions(filename) if is_plausible_mc_version(value)]
    loader = "unknown"
    for candidate in ("neoforge", "forge", "fabric", "quilt"):
        if candidate in loaders:
            loader = candidate
            break
    return {
        "mod_ids": mod_ids,
        "name": name,
        "version": version,
        "loaders": loaders,
        "loader": loader,
        "dependencies": dependencies,
        "mc_versions": mc_versions,
    }


def inspect_jar(
    path: Path,
    *,
    compute_murmur: bool = False,
    hash_cache: HashCache | None = None,
) -> ModFile:
    stat = path.stat()
    manifest: dict[str, str] = {}
    metadata_parts: list[tuple[str, dict[str, Any]]] = []
    metadata_kind = "unknown"
    error = ""
    entry_names: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            entry_names = set(archive.namelist())
            if "META-INF/MANIFEST.MF" in entry_names:
                manifest = parse_manifest(archive.read("META-INF/MANIFEST.MF"))
            if "fabric.mod.json" in entry_names:
                metadata_kind = "fabric.mod.json"
                raw = archive.read("fabric.mod.json")
                data = json.loads(raw.decode("utf-8-sig", errors="replace"))
                metadata_parts.append(("fabric", _parse_fabric(data, "fabric", manifest, path.name)))
            if "quilt.mod.json" in entry_names:
                metadata_kind = "quilt.mod.json" if metadata_kind == "unknown" else metadata_kind + "+quilt.mod.json"
                raw = archive.read("quilt.mod.json")
                data = json.loads(raw.decode("utf-8-sig", errors="replace"))
                metadata_parts.append(("quilt", _parse_quilt(data, manifest)))
            toml_name = ""
            if "META-INF/neoforge.mods.toml" in entry_names:
                toml_name = "META-INF/neoforge.mods.toml"
                toml_loader = "neoforge"
            elif "META-INF/mods.toml" in entry_names:
                toml_name = "META-INF/mods.toml"
                toml_loader = "forge"
            else:
                toml_loader = ""
            if toml_name:
                metadata_kind = toml_name.split("/")[-1] if metadata_kind == "unknown" else metadata_kind + "+" + toml_name.split("/")[-1]
                raw = archive.read(toml_name)
                data = tomllib.loads(raw.decode("utf-8-sig", errors="replace"))
                metadata_parts.append((toml_loader, _parse_forge_toml(data, toml_loader, manifest, path.name)))
            if "mcmod.info" in entry_names:
                metadata_kind = "mcmod.info" if metadata_kind == "unknown" else metadata_kind + "+mcmod.info"
                raw = archive.read("mcmod.info")
                data = json.loads(raw.decode("utf-8-sig", errors="replace"))
                metadata_parts.append(("forge", _parse_mcmod_info(data, manifest)))
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # 第三方 jar 的元数据经常不规范，不能因此中断全量扫描
        error = f"{type(exc).__name__}: {exc}"

    merged = _merge_metadata(metadata_parts, manifest, path.name)
    cached = hash_cache.get(path) if hash_cache else None
    if cached and cached.get("size") == stat.st_size:
        sha1 = str(cached.get("sha1", ""))
        sha512 = str(cached.get("sha512", ""))
        murmur2 = cached.get("murmur2") if compute_murmur else None
    else:
        sha1 = _sha1_file(path)
        sha512 = _sha512_file(path)
        murmur2 = None
    if compute_murmur and murmur2 is None:
        try:
            murmur2 = curseforge_fingerprint(path)
        except OSError:
            murmur2 = None
    if hash_cache:
        hash_cache.put(
            path,
            {
                "size": stat.st_size,
                "sha1": sha1,
                "sha512": sha512,
                "murmur2": murmur2,
            },
        )
    return ModFile(
        path=path,
        filename=path.name,
        size=stat.st_size,
        sha1=sha1,
        sha512=sha512,
        murmur2=murmur2,
        mod_ids=merged["mod_ids"],
        name=merged["name"],
        version=merged["version"],
        loader=merged["loader"],
        loaders=merged["loaders"],
        mc_versions=merged["mc_versions"],
        dependencies=merged["dependencies"],
        metadata_kind=metadata_kind,
        metadata_error=error,
        manifest=manifest,
    )


def scan_mods(
    mods_dir: Path,
    *,
    compute_murmur: bool = False,
    hash_cache: HashCache | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[ModFile], list[str]]:
    errors: list[str] = []
    if not mods_dir.exists():
        return [], [f"mods 文件夹不存在：{mods_dir}"]
    paths = sorted(
        [
            path
            for path in mods_dir.rglob("*.jar")
            if path.is_file() and not path.name.startswith(".")
        ],
        key=lambda item: item.name.lower(),
    )
    mods: list[ModFile] = []
    total = len(paths)
    for index, path in enumerate(paths, start=1):
        if progress:
            progress(index, total, path.name)
        try:
            mod = inspect_jar(path, compute_murmur=compute_murmur, hash_cache=hash_cache)
        except OSError as exc:
            errors.append(f"{path.name}: 无法读取（{exc}）")
            continue
        if mod.metadata_error:
            errors.append(f"{path.name}: {mod.metadata_error}")
        mods.append(mod)
    return mods, errors


def is_builtin_dependency(mod_id: str) -> bool:
    return (mod_id or "").lower() in BUILTIN_MOD_IDS


def find_jar_owner(path: Path) -> str:
    try:
        return str(path.relative_to(path.parents[1]))
    except (ValueError, IndexError):
        return path.name
