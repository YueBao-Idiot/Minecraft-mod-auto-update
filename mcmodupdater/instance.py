from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import ModFile
from .util import extract_mc_versions, is_mc_version, is_plausible_mc_version, mc_version_key

LOG_PATTERNS = [
    re.compile(r"Loading Minecraft\s+(\d+\.\d+(?:\.\d+)?)\s+with Fabric Loader", re.I),
    re.compile(r"Starting minecraft server version\s+(\d+\.\d+(?:\.\d+)?)", re.I),
    re.compile(r"Minecraft Version:\s*(\d+\.\d+(?:\.\d+)?)", re.I),
    re.compile(r"Minecraft:\s*(\d+\.\d+(?:\.\d+)?)", re.I),
    re.compile(r"--fml\.mcVersion\s+(\d+\.\d+(?:\.\d+)?)", re.I),
    re.compile(r"minecraft\s+version\s+(\d+\.\d+(?:\.\d+)?)", re.I),
]

LOADER_PATTERNS = [
    (re.compile(r"fabric[- ]loader|fabricloader|fabricmc", re.I), "fabric"),
    (re.compile(r"neoforge", re.I), "neoforge"),
    (re.compile(r"minecraftforge|forge[- ]?version|forgespi|fml", re.I), "forge"),
]

LOADER_LABELS = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}


@dataclass
class DetectedInstance:
    root: Path
    mods_dir: Path
    configs_dir: Path
    loader: str = "unknown"
    game_version: str = ""
    loader_confidence: str = "low"
    version_confidence: str = "low"
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    version_candidates: list[str] = field(default_factory=list)

    @property
    def loader_label(self) -> str:
        return LOADER_LABELS.get(self.loader, "未知")

    @property
    def has_configs(self) -> bool:
        return self.configs_dir.is_dir() and any(self.configs_dir.iterdir())


def resolve_instance_root(path: str | Path) -> tuple[Path, Path]:
    """接受实例根目录或 mods 目录，返回（实例根目录，mods 目录）。"""
    root = Path(path).expanduser().resolve()
    if root.is_file():
        raise ValueError("请选择一个文件夹，而不是文件。")
    if root.name.lower() == "mods" and root.is_dir():
        return root.parent, root
    if (root / "mods").is_dir():
        return root, root / "mods"
    # 允许空目录，首次使用时自动创建 mods。
    if root.exists():
        return root, root / "mods"
    raise ValueError(f"文件夹不存在：{root}")


def ensure_instance_skeleton(root: Path) -> None:
    (root / "mods").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)


def _read_text_prefix(path: Path, limit: int = 256 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_text_tail(path: Path, limit: int = 128 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def detect_loader(root: Path, mods: Iterable[ModFile] | None = None, log_text: str = "") -> tuple[str, str, list[str]]:
    evidence: list[str] = []
    scores: Counter[str] = Counter()

    marker_paths = {
        "fabric": [root / "libraries" / "net" / "fabricmc" / "fabric-loader"],
        "forge": [root / "libraries" / "net" / "minecraftforge" / "forge"],
        "neoforge": [root / "libraries" / "net" / "neoforged" / "neoforge"],
    }
    for loader, paths in marker_paths.items():
        if any(path.exists() for path in paths):
            scores[loader] += 10
            evidence.append(f"发现 {LOADER_LABELS[loader]} 的 libraries 目录")

    local_profile = root / f"{root.name}.json"
    if local_profile.is_file():
        try:
            data = json.loads(local_profile.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            data = {}
        profile_id = f"{root.name} {data.get('id', '')}".lower()
        if "neoforge" in profile_id:
            scores["neoforge"] += 5
            evidence.append(f"版本配置 {root.name} 标注 NeoForge")
        elif "fabric-loader" in profile_id or "fabricloader" in profile_id:
            scores["fabric"] += 5
            evidence.append(f"版本配置 {root.name} 标注 Fabric")
        elif "forge" in profile_id:
            scores["forge"] += 5
            evidence.append(f"版本配置 {root.name} 标注 Forge")

    versions_dir = root / "versions"
    if versions_dir.is_dir():
        for profile in versions_dir.glob("*/*.json"):
            try:
                data = json.loads(profile.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            profile_id = f"{profile.parent.name} {data.get('id', '')}".lower()
            if "neoforge" in profile_id:
                scores["neoforge"] += 4
                evidence.append(f"版本配置 {profile.parent.name} 标注 NeoForge")
            elif "fabric-loader" in profile_id or "fabricloader" in profile_id:
                scores["fabric"] += 4
                evidence.append(f"版本配置 {profile.parent.name} 标注 Fabric")
            elif "forge" in profile_id:
                scores["forge"] += 4
                evidence.append(f"版本配置 {profile.parent.name} 标注 Forge")
            elif "quilt-loader" in profile_id:
                scores["quilt"] += 4

    if log_text:
        head = log_text[:512 * 1024]
        if re.search(r"fabricloader|fabric loader", head, re.I):
            scores["fabric"] += 3
            evidence.append("日志中出现 Fabric Loader")
        if re.search(r"neoforge", head, re.I):
            scores["neoforge"] += 3
            evidence.append("日志中出现 NeoForge")
        elif re.search(r"minecraftforge|forge mod loader|forgespi", head, re.I):
            scores["forge"] += 3
            evidence.append("日志中出现 Forge")

    mod_loaders = Counter()
    if mods:
        for mod in mods:
            for loader in mod.loaders or ([mod.loader] if mod.loader != "unknown" else []):
                mod_loaders[loader] += 1
        for loader, count in mod_loaders.items():
            if loader in ("fabric", "forge", "neoforge", "quilt"):
                scores[loader] += min(count, 8)
        if mod_loaders:
            summary = "、".join(f"{LOADER_LABELS.get(k, k)} {v}" for k, v in mod_loaders.most_common())
            evidence.append(f"mods 元数据统计：{summary}")

    if not scores:
        return "unknown", "low", evidence
    ordered = scores.most_common()
    best, best_score = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else 0
    confidence = "high" if best_score >= 8 and best_score >= second * 2 else ("medium" if best_score > second else "low")
    return best, confidence, evidence


def _version_from_log(text: str) -> str:
    for pattern in LOG_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            return str(matches[-1] if pattern.pattern.startswith("Starting") else matches[0])
    return ""


def detect_game_version(root: Path, mods: Iterable[ModFile] | None = None, log_text: str = "") -> tuple[str, str, list[str], list[str]]:
    evidence: list[str] = []
    candidates: list[str] = []
    log_version = _version_from_log(log_text)
    if log_version:
        candidates.append(log_version)
        evidence.append(f"日志记录的 Minecraft 版本：{log_version}")

    crash_dir = root / "crash-reports"
    if crash_dir.is_dir():
        reports = sorted(crash_dir.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for report in reports[:2]:
            text = _read_text_prefix(report, 128 * 1024)
            version = _version_from_log(text)
            if version:
                candidates.append(version)
                evidence.append(f"崩溃报告记录的版本：{version}")
                break

    local_profile = root / f"{root.name}.json"
    if local_profile.is_file():
        try:
            data = json.loads(local_profile.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            data = {}
        inherited = str(data.get("inheritsFrom") or "")
        if is_mc_version(inherited):
            candidates.append(inherited)
            evidence.append(f"版本配置文件记录的目标版本：{inherited}")
        elif is_mc_version(str(data.get("id") or "")):
            candidates.append(str(data["id"]))
            evidence.append(f"版本配置文件的 id：{data['id']}")

    versions_dir = root / "versions"
    if versions_dir.is_dir():
        profile_versions: list[tuple[float, str]] = []
        for profile in versions_dir.glob("*/*.json"):
            data: dict = {}
            try:
                data = json.loads(profile.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                pass
            inherited = str(data.get("inheritsFrom") or "")
            if is_mc_version(inherited):
                profile_versions.append((profile.stat().st_mtime, inherited))
            elif is_mc_version(profile.parent.name):
                profile_versions.append((profile.stat().st_mtime, profile.parent.name))
        if profile_versions:
            profile_versions.sort(reverse=True)
            candidates.append(profile_versions[0][1])
            evidence.append(f"已安装版本文件：{profile_versions[0][1]}")

    for mod in mods or []:
        candidates.extend(mod.mc_versions)
    if log_version:
        return log_version, "high", evidence, candidates
    if candidates:
        counter = Counter(candidates)
        best, count = counter.most_common(1)[0]
        if count > 1:
            evidence.append(f"多个来源指向 {best}（{count} 次）")
            return best, "high", evidence, candidates
        if log_version:
            return log_version, "high", evidence, candidates
        if candidates:
            return candidates[0], "medium", evidence, candidates

    # 文件名是最弱证据，只在没有其他线索时使用。
    filename_candidates: list[str] = []
    for path in root.glob("mods/*.jar"):
        filename_candidates.extend(
            value for value in extract_mc_versions(path.name) if is_plausible_mc_version(value)
        )
    if filename_candidates:
        best = Counter(filename_candidates).most_common(1)[0][0]
        evidence.append(f"从 jar 文件名推断：{best}")
        return best, "low", evidence, filename_candidates

    return "", "low", evidence, []


def detect_instance(path: str | Path, mods: Iterable[ModFile] | None = None) -> DetectedInstance:
    root, mods_dir = resolve_instance_root(path)
    log_text = ""
    log_candidates = [root / "logs" / "latest.log"]
    if root.parent.name.lower() == "versions":
        log_candidates.append(root.parent.parent / "logs" / "latest.log")
    for latest_log in log_candidates:
        if latest_log.is_file():
            log_text = _read_text_prefix(latest_log) + "\n" + _read_text_tail(latest_log)
            break
    loader, loader_confidence, loader_evidence = detect_loader(root, mods, log_text)
    version, version_confidence, version_evidence, candidates = detect_game_version(root, mods, log_text)
    warnings: list[str] = []
    if not root.exists():
        warnings.append(f"源实例不存在，请把 mods 放入：{root / 'mods'}")
    elif not mods_dir.is_dir():
        warnings.append(f"没有找到 mods 文件夹：{mods_dir}")
    if loader == "unknown":
        warnings.append("未能自动识别加载器，请在界面上手动选择。")
    if not version:
        warnings.append("未能自动识别游戏版本，请手动填写目标版本。")
    return DetectedInstance(
        root=root,
        mods_dir=mods_dir,
        configs_dir=root / "config",
        loader=loader,
        game_version=version,
        loader_confidence=loader_confidence,
        version_confidence=version_confidence,
        evidence=loader_evidence + version_evidence,
        warnings=warnings,
        version_candidates=sorted(set(candidates), key=mc_version_key, reverse=True),
    )


def read_mods_folder(path: str | Path) -> list[Path]:
    root, mods_dir = resolve_instance_root(path)
    if not mods_dir.is_dir():
        return []
    return sorted(
        [item for item in mods_dir.rglob("*.jar") if item.is_file()],
        key=lambda item: item.name.lower(),
    )
