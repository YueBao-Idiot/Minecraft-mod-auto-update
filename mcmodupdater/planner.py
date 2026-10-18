from __future__ import annotations

import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from .configs import enrich_config_owners, scan_configs
from .instance import DetectedInstance, detect_instance, resolve_instance_root
from .jarinfo import HashCache, is_builtin_dependency, scan_mods
from .models import (
    LOADER_LABELS,
    PROVIDER_LABELS,
    DependencyItem,
    MatchResult,
    ModFile,
    ModPlan,
    ProjectInfo,
    RemoteDependency,
    UpdatePlan,
    VersionInfo,
)
from .paths import CACHE_DIR
from .providers import CurseForgeProvider, ModProvider, ProviderError
from .providers.base import select_latest_version
from .settings import AppSettings, VALID_LOADERS, make_provider_configs
from .util import chunked, normalize_name

ProgressCallback = Callable[[str, int, int], None]
Progress = ProgressCallback


class PlannerError(RuntimeError):
    pass


def canonical_version(value: str) -> str:
    text = (value or "").lower().strip()
    if not text:
        return ""
    # 去掉 Minecraft 版本前缀，兼容 1.x 和 2026 起的 26.x。
    text = re.sub(r"^(?:mc[-_.]?)?(?:1|2\d)\.\d{1,2}(?:\.\d{1,2})?[-+]", "", text)
    text = re.sub(r"(?<![0-9])mc[-_.]?1\.\d+(?:\.\d+)?(?![0-9])", "", text)
    text = re.sub(r"(?<![0-9])1\.\d{2}(?:\.\d+)?(?![0-9])", "", text)
    text = re.sub(r"fabric|forge|neoforge|quilt|loader", "", text)
    numbers = re.findall(r"\d+(?:\.\d+)+", text)
    if numbers:
        return numbers[0].rstrip(".")
    return normalize_name(text)


def versions_equivalent(left: str, right: str) -> bool:
    left_key = canonical_version(left)
    right_key = canonical_version(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    # 0.5.11 与 0.5.11.1 视为不同；只有包含关系且长度至少 5 才放宽。
    return min(len(left_key), len(right_key)) >= 5 and (
        left_key.startswith(right_key + ".") or right_key.startswith(left_key + ".")
    )


def _local_dependencies(mod: ModFile) -> list[RemoteDependency]:
    result: list[RemoteDependency] = []
    for dep in mod.dependencies:
        if is_builtin_dependency(dep.mod_id):
            continue
        relation = dep.relation if dep.relation in ("required", "optional", "incompatible") else "required"
        result.append(
            RemoteDependency(
                project_id="",
                version_id="",
                dependency_type=relation,
                mod_id=dep.mod_id,
                file_name=dep.mod_id,
            )
        )
    return result


class DependencyResolver:
    def __init__(
        self,
        plan: UpdatePlan,
        providers: dict[str, ModProvider],
        progress: Progress | None = None,
    ) -> None:
        self.plan = plan
        self.providers = providers
        self.progress = progress
        self.game_version = plan.game_version
        self.loader = plan.loader
        self.items: dict[str, DependencyItem] = {}
        self.effective_keys: set[str] = set()
        self.old_parents: dict[str, set[str]] = {}
        self.installed_by_project: dict[tuple[str, str], ModPlan] = {}
        self.installed_by_modid: dict[str, ModPlan] = {}
        self.project_cache: dict[tuple[str, str], ProjectInfo | None] = {}
        self.versions_cache: dict[tuple[str, str], list[VersionInfo]] = {}
        self.queue: deque[tuple[str, VersionInfo, str, int]] = deque()
        self.seen_versions: set[tuple[str, str]] = set()
        self.conflicts: dict[str, DependencyItem] = {}

    def resolve(self) -> list[DependencyItem]:
        self._index_installed()
        self._collect_old_graph()
        self._collect_effective_graph()
        self._process_queue()
        self._collect_conflicts()
        self._collect_removals()
        return self._sorted_items()

    def _index_installed(self) -> None:
        for mod_plan in self.plan.mods:
            match = mod_plan.match
            if not match:
                continue
            project = match.project
            self.installed_by_project[(project.provider, project.project_id)] = mod_plan
            for value in (mod_plan.mod.mod_id, mod_plan.mod.name, project.slug):
                key = normalize_name(value)
                if len(key) >= 3:
                    self.installed_by_modid.setdefault(key, mod_plan)

    def _provider(self, name: str) -> ModProvider | None:
        return self.providers.get(name)

    @staticmethod
    def _dep_key(provider: str, dep: RemoteDependency) -> str:
        if dep.project_id:
            return f"{provider}:project:{dep.project_id}"
        name = dep.mod_id or dep.file_name
        return f"{provider}:name:{normalize_name(name) or 'unknown'}"

    @staticmethod
    def _dep_display(dep: RemoteDependency) -> str:
        value = dep.file_name or dep.mod_id or dep.project_id
        return re.sub(r"\.jar$", "", value, flags=re.I)

    def _project(self, provider: str, project_id: str) -> ProjectInfo | None:
        if not project_id:
            return None
        cache_key = (provider, project_id)
        if cache_key in self.project_cache:
            return self.project_cache[cache_key]
        client = self._provider(provider)
        if client is None:
            return None
        try:
            project = client.get_project(project_id)
        except Exception:
            project = None
        self.project_cache[cache_key] = project
        return project

    def _versions(self, provider: str, project_id: str) -> list[VersionInfo]:
        cache_key = (provider, project_id)
        if cache_key in self.versions_cache:
            return self.versions_cache[cache_key]
        client = self._provider(provider)
        if client is None:
            return []
        try:
            versions = client.get_versions(project_id, self.game_version, self.loader)
        except Exception as exc:
            versions = []
            self.plan.warnings.append(f"{PROVIDER_LABELS.get(provider, provider)} 查询 {project_id} 版本失败：{exc}")
        self.versions_cache[cache_key] = versions
        return versions

    def _find_installed(self, provider: str, dep: RemoteDependency) -> ModPlan | None:
        if dep.project_id:
            found = self.installed_by_project.get((provider, dep.project_id))
            if found:
                return found
        for value in (dep.mod_id, dep.file_name, self._dep_display(dep)):
            key = normalize_name(value)
            if len(key) >= 3 and key in self.installed_by_modid:
                return self.installed_by_modid[key]
        return None

    def _ensure_item(
        self,
        key: str,
        *,
        provider: str,
        project_id: str,
        name: str,
        relation: str,
        parent: str,
        source_mod_id: str = "",
    ) -> DependencyItem:
        item = self.items.get(key)
        if item is None:
            item = DependencyItem(
                key=key,
                name=name,
                provider=provider,
                project_id=project_id,
                relation=relation,
                source_mod_id=source_mod_id,
            )
            self.items[key] = item
        if relation == "required":
            item.relation = "required"
        elif item.relation != "required":
            item.relation = "optional"
        if parent and parent not in item.parents:
            item.parents.append(parent)
        if project_id and not item.project_id:
            item.project_id = project_id
        if name and (not item.name or item.name.startswith("项目 ")):
            item.name = name
        if source_mod_id and not item.source_mod_id:
            item.source_mod_id = source_mod_id
        return item

    def _resolve_project_by_name(self, provider: str, dep: RemoteDependency) -> ProjectInfo | None:
        name = dep.mod_id or self._dep_display(dep)
        if not name:
            return None
        client = self._provider(provider)
        if client is None:
            return None
        fake = ModFile(
            path=Path(name),
            filename=name,
            size=0,
            mod_ids=[name],
            name=name,
            loader=self.loader,
            loaders=[self.loader],
        )
        try:
            result = client.search_mod(fake, self.game_version, self.loader)
        except Exception:
            result = None
        if result:
            self.project_cache[(provider, result.project.project_id)] = result.project
            return result.project
        return None

    def _resolve_dependency_project(
        self, provider: str, dep: RemoteDependency
    ) -> tuple[ProjectInfo | None, RemoteDependency, str]:
        """补全依赖的项目 id；返回项目、更新后的依赖和失败原因。"""
        client = self._provider(provider)
        if client is None:
            return None, dep, "当前未启用该更新源"
        if dep.project_id:
            project = self._project(provider, dep.project_id)
            if project is None:
                # 项目可能是被删除或 API 不返回详情，保留 id 供报告使用。
                return ProjectInfo(provider=provider, project_id=dep.project_id, slug="", title=dep.project_id), dep, ""
            return project, dep, ""
        if dep.version_id:
            try:
                version = client.get_version(dep.version_id)
            except Exception:
                version = None
            if version and version.project_id:
                dep = replace(dep, project_id=version.project_id)
                project = self._project(provider, version.project_id)
                return project, dep, ""
        project = self._resolve_project_by_name(provider, dep)
        if project:
            return project, dep, ""
        return None, dep, "无法确定前置对应的项目"

    def _resolve_dependency_version(
        self,
        provider: str,
        dep: RemoteDependency,
        project: ProjectInfo,
    ) -> tuple[VersionInfo | None, str]:
        client = self._provider(provider)
        if client is None:
            return None, "当前未启用该更新源"
        notes = ""
        if dep.version_id:
            try:
                version = None
                if isinstance(client, CurseForgeProvider):
                    version = client.get_version_for_project(project.project_id, dep.version_id)
                else:
                    version = client.get_version(dep.version_id)
            except Exception:
                version = None
            if version and version.supports(self.game_version, self.loader):
                return version, "使用前置指定的版本"
            if version:
                notes = "指定版本与目标游戏版本/加载器不兼容，改用最新兼容版本"
        versions = self._versions(provider, project.project_id)
        target = select_latest_version(
            versions,
            self.game_version,
            self.loader,
            allow_prerelease=self.plan.allow_prerelease,
        )
        if target:
            return target, notes
        compatible = [v for v in versions if v.supports(self.game_version, self.loader)]
        if compatible and not self.plan.allow_prerelease:
            notes = (notes + "；" if notes else "") + "只有测试版可用"
        elif versions:
            notes = (notes + "；" if notes else "") + "没有兼容目标版本的文件"
        return None, notes

    def _collect_old_graph(self) -> None:
        for mod_plan in self.plan.mods:
            if not mod_plan.match or not mod_plan.current_version:
                continue
            provider = mod_plan.match.project.provider
            for dep in mod_plan.current_version.dependencies:
                if dep.dependency_type != "required":
                    continue
                if not dep.project_id and not dep.mod_id and not dep.file_name:
                    continue
                key = self._dep_key(provider, dep)
                self.old_parents.setdefault(key, set()).add(mod_plan.mod.display_name)

    def _collect_effective_graph(self) -> None:
        for mod_plan in self.plan.mods:
            provider = ""
            version: VersionInfo | None = None
            if mod_plan.match:
                provider = mod_plan.match.project.provider
                version = mod_plan.target_version or mod_plan.current_version
            if version:
                dependencies = version.dependencies
            else:
                dependencies = _local_dependencies(mod_plan.mod)
            for dep in dependencies:
                self._process_edge(
                    provider,
                    dep,
                    parent=mod_plan.mod.display_name,
                    source_mod_id=mod_plan.mod.mod_id,
                    depth=1,
                )

    def _process_edge(
        self,
        provider: str,
        dep: RemoteDependency,
        *,
        parent: str,
        source_mod_id: str = "",
        depth: int = 1,
    ) -> DependencyItem:
        if dep.dependency_type in ("embedded", "include", "tool"):
            return DependencyItem(key="", name="")
        key = self._dep_key(provider, dep)
        display = self._dep_display(dep)
        relation = dep.dependency_type if dep.dependency_type in ("required", "optional", "incompatible") else "required"

        if relation == "incompatible":
            installed = self._find_installed(provider, dep)
            if installed:
                conflict_key = f"{provider}:conflict:{dep.project_id or normalize_name(display)}"
                item = self.conflicts.get(conflict_key)
                if item is None:
                    item = DependencyItem(
                        key=conflict_key,
                        name=installed.mod.display_name,
                        provider=provider,
                        project_id=dep.project_id,
                        state="incompatible",
                        relation="incompatible",
                        parents=[parent],
                        notes=["目标模组声明与这个已安装模组不兼容，请确认或移除它。"],
                        download=False,
                        source_mod_id=source_mod_id,
                    )
                    self.conflicts[conflict_key] = item
                elif parent not in item.parents:
                    item.parents.append(parent)
                self.effective_keys.add(key)
            return DependencyItem(key="", name="")

        installed = self._find_installed(provider, dep)
        if installed:
            project = installed.project
            item = self._ensure_item(
                key,
                provider=provider,
                project_id=project.project_id if project else dep.project_id,
                name=installed.mod.display_name,
                relation=relation,
                parent=parent,
                source_mod_id=source_mod_id,
            )
            item.state = "satisfied"
            item.download = False
            if installed.status == "no_compatible" and relation == "required":
                note = "已安装，但前置在目标版本没有兼容更新，将保留当前文件"
                if note not in item.notes:
                    item.notes.append(note)
            self.effective_keys.add(key)
            return item

        project, dep, reason = self._resolve_dependency_project(provider, dep)
        if project:
            name = project.title or display
        else:
            name = display or dep.project_id or "未知前置"
        item = self._ensure_item(
            key,
            provider=provider,
            project_id=dep.project_id,
            name=name,
            relation=relation,
            parent=parent,
            source_mod_id=source_mod_id,
        )
        # 同一前置可能同时被多个模组以 required / optional 引用，必需关系优先。
        relation = item.relation
        self.effective_keys.add(key)
        if item.state in ("satisfied", "removable", "incompatible"):
            return item

        if not project:
            item.state = "unresolved"
            item.download = False
            note = reason or "未找到对应项目"
            if note not in item.notes:
                item.notes.append(note)
            self._add_manual_hint(item, provider, display)
            return item

        version, version_note = self._resolve_dependency_version(provider, dep, project)
        if version_note and version_note not in item.notes:
            item.notes.append(version_note)
        if not version:
            item.state = "unresolved"
            item.download = False
            note = f"没有找到适用于 {self.game_version} / {LOADER_LABELS.get(self.loader, self.loader)} 的版本"
            if note not in item.notes:
                item.notes.append(note)
            self._add_manual_hint(item, provider, project.title or display)
            return item

        item.resolved_version = version
        item.name = project.title or name
        item.download = relation == "required" and bool(version.download_url)
        if relation == "optional":
            item.state = "optional"
            item.download = False
            note = "可选前置；默认不自动下载"
            if note not in item.notes:
                item.notes.append(note)
        else:
            item.state = "missing"
            if version.download_url:
                note = "将随移植包自动下载"
            else:
                note = "作者未开放 API 下载，请使用手动链接"
            if note not in item.notes:
                item.notes.append(note)
        if relation == "required" and depth < 5:
            signature = (provider, version.version_id)
            if signature not in self.seen_versions:
                self.seen_versions.add(signature)
                self.queue.append((provider, version, item.name, depth + 1))
        return item

    def _add_manual_hint(self, item: DependencyItem, provider: str, query: str) -> None:
        client = self._provider(provider)
        if not client or not query:
            return
        try:
            url = client.search_url(query, self.game_version, self.loader)
        except Exception:
            url = ""
        if url:
            note = f"手动查找：{url}"
            if note not in item.notes:
                item.notes.append(note)

    def _process_queue(self) -> None:
        while self.queue:
            provider, version, parent, depth = self.queue.popleft()
            if depth > 5:
                continue
            for dep in version.dependencies:
                self._process_edge(
                    provider,
                    dep,
                    parent=parent,
                    source_mod_id="",
                    depth=depth,
                )

    def _collect_conflicts(self) -> None:
        for item in self.conflicts.values():
            self.items[item.key] = item

    def _collect_removals(self) -> None:
        for mod_plan in self.plan.mods:
            if not mod_plan.match:
                continue
            project = mod_plan.match.project
            key = f"{project.provider}:project:{project.project_id}"
            if key not in self.old_parents or key in self.effective_keys:
                continue
            if key in self.items:
                continue
            old_parents = sorted(self.old_parents[key])
            item = DependencyItem(
                key=key,
                name=mod_plan.mod.display_name,
                provider=project.provider,
                project_id=project.project_id,
                state="removable",
                relation="required",
                parents=old_parents,
                notes=[
                    "旧版本被这些模组依赖，但目标版本不再声明它，且更新后的依赖图中没有其他模组需要它。",
                    "请确认没有手动用途后再从 mods 中删除。",
                ],
                download=False,
                source_mod_id=mod_plan.mod.mod_id,
            )
            self.items[key] = item

    def _sorted_items(self) -> list[DependencyItem]:
        priority = {
            "missing": 0,
            "unresolved": 1,
            "incompatible": 2,
            "removable": 3,
            "optional": 4,
            "satisfied": 5,
        }
        return sorted(
            self.items.values(),
            key=lambda item: (priority.get(item.state, 9), item.display_name.lower()),
        )


class UpdatePlanner:
    def __init__(self, settings: AppSettings, progress: Progress | None = None) -> None:
        self.settings = settings.normalized()
        self.progress = progress
        self.providers: dict[str, ModProvider] = {}
        self.plan: UpdatePlan | None = None

    def _emit(self, message: str, current: int = 0, total: int = 0) -> None:
        if self.progress:
            try:
                self.progress(message, current, total)
            except Exception:
                pass

    def build(
        self,
        instance_path: str | Path | None = None,
        *,
        loader: str | None = None,
        game_version: str | None = None,
        providers: list[str] | None = None,
    ) -> UpdatePlan:
        path = Path(instance_path) if instance_path else self.settings.instance_path
        target_loader = (loader or self.settings.loader or "").lower()
        target_version = (game_version if game_version is not None else self.settings.game_version).strip()
        if target_loader not in VALID_LOADERS:
            raise PlannerError(f"不支持的加载器：{target_loader}（仅支持 Fabric / Forge / NeoForge）")
        if not target_version:
            raise PlannerError("请填写目标游戏版本，例如 1.20.1。")

        self._emit("读取实例与 mods 列表…")
        self.plan = None
        try:
            detected: DetectedInstance = detect_instance(path)
            root, mods_dir = resolve_instance_root(path)
        except (OSError, ValueError) as exc:
            raise PlannerError(f"无法读取源实例：{exc}") from exc
        if not mods_dir.is_dir():
            raise PlannerError(f"没有找到 mods 文件夹：{mods_dir}\n请把整合包的 mods 放入该目录，或选择正确的实例文件夹。")

        hash_cache = HashCache(CACHE_DIR / "hashes.json")
        use_curseforge = "curseforge" in (providers or self.settings.providers)
        mods, scan_errors = scan_mods(
            mods_dir,
            compute_murmur=use_curseforge,
            hash_cache=hash_cache,
            progress=lambda current, total, name: self._emit(f"读取 jar 元数据：{name}", current, total),
        )
        hash_cache.save()
        detected = detect_instance(root, mods)
        if detected.loader not in ("unknown", target_loader):
            self._emit(f"检测到源加载器为 {detected.loader_label}，目标为 {LOADER_LABELS.get(target_loader)}")
        plan = UpdatePlan(
            source_root=root,
            mods_dir=mods_dir,
            loader=target_loader,
            game_version=target_version,
            providers=list(providers or self.settings.providers),
            detected_loader=detected.loader,
            detected_game_version=detected.game_version,
            cross_loader=detected.loader not in ("unknown", target_loader),
            allow_prerelease=self.settings.allow_prerelease,
        )
        plan.mods = [ModPlan(mod=mod) for mod in mods]
        self.plan = plan
        plan.errors.extend(scan_errors)
        if not mods:
            plan.warnings.append("mods 文件夹中没有找到 .jar 文件。")
            plan.configs = scan_configs(root, [], scope="none")
            self.plan = plan
            return plan
        if detected.game_version and detected.game_version != target_version:
            plan.warnings.append(
                f"源实例检测到的游戏版本是 {detected.game_version}，目标设为 {target_version}。"
            )
        if plan.cross_loader:
            plan.warnings.append(
                f"跨加载器迁移：源为 {LOADER_LABELS.get(detected.loader, detected.loader)}，"
                f"目标为 {LOADER_LABELS.get(target_loader)}。不保证兼容，建议优先保持同一加载器。"
            )
        self._check_duplicate_mods(plan, mods)

        self._build_providers(plan)
        self._match_mods(plan)
        self._resolve_targets(plan)
        self._finalize_statuses(plan)
        self._emit("解析前置依赖…")
        resolver = DependencyResolver(plan, self.providers)
        plan.dependencies = resolver.resolve()
        self._scan_configs(plan, mods)
        self._add_final_warnings(plan)
        self._emit("更新计划已生成。", len(plan.mods), len(plan.mods))
        self.plan = plan
        return plan

    def _check_duplicate_mods(self, plan: UpdatePlan, mods: Iterable[ModFile]) -> None:
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for mod in mods:
            for mod_id in mod.mod_ids:
                key = mod_id.lower()
                if key in seen and seen[key] != mod.filename:
                    duplicates.append(f"{mod_id}（{seen[key]} / {mod.filename}）")
                else:
                    seen[key] = mod.filename
        if duplicates:
            plan.warnings.append("检测到重复的 mod id，游戏可能无法启动：" + "；".join(duplicates[:8]))
        broken = [mod.filename for mod in mods if mod.metadata_error]
        if broken:
            plan.warnings.append(f"{len(broken)} 个 jar 的元数据无法完整读取，已改用文件名推断。")

    def _build_providers(self, plan: UpdatePlan) -> None:
        configured = make_provider_configs(self.settings, CACHE_DIR)
        for provider in configured:
            if provider.name not in plan.providers:
                continue
            if not provider.available:
                plan.warnings.append(f"已跳过 {provider.label}：{provider.unavailable_reason}")
                continue
            self.providers[provider.name] = provider
        if not self.providers:
            plan.warnings.append("没有可用的更新源。仍会生成本地清单，但无法查询在线版本。")
        order = [name for name in plan.providers if name in self.providers]
        if self.settings.prefer_provider in order:
            order.remove(self.settings.prefer_provider)
            order.insert(0, self.settings.prefer_provider)
        plan.providers = order

    def _ordered_providers(self) -> list[ModProvider]:
        assert self.plan is not None
        return [self.providers[name] for name in self.plan.providers if name in self.providers]

    def _choose_match(self, candidates: list[tuple[str, MatchResult]]) -> MatchResult | None:
        if not candidates:
            return None
        assert self.plan is not None
        order = {name: index for index, name in enumerate(self.plan.providers)}
        candidates.sort(key=lambda item: (order.get(item[0], 99), item[1].method != "sha512", item[1].method != "fingerprint"))
        return candidates[0][1]

    def _match_mods(self, plan: UpdatePlan) -> None:
        hash_matches: dict[str, list[tuple[str, MatchResult]]] = {}
        for provider in self._ordered_providers():
            self._emit(f"使用 {provider.label} 按文件哈希精确匹配…")
            try:
                results = provider.match_by_hashes([mod_plan.mod for mod_plan in plan.mods])
            except Exception as exc:
                plan.warnings.append(f"{provider.label} 哈希匹配失败：{exc}")
                continue
            for path, result in results.items():
                hash_matches.setdefault(path, []).append((provider.name, result))

        for mod_plan in plan.mods:
            candidates = hash_matches.get(str(mod_plan.mod.path.resolve()), [])
            match = self._choose_match(candidates)
            if match:
                mod_plan.match = match
            else:
                mod_plan.notes.append("哈希未命中，尝试按名称搜索")

        unmatched = [mod_plan for mod_plan in plan.mods if not mod_plan.match]
        if not unmatched:
            return
        self._emit(f"按名称搜索 {len(unmatched)} 个未精确匹配的模组…", 0, len(unmatched))
        ordered = self._ordered_providers()
        errors: dict[str, list[str]] = {}

        def search_one(mod_plan: ModPlan) -> tuple[ModPlan, MatchResult | None, list[str]]:
            local_errors: list[str] = []
            for provider in ordered:
                try:
                    result = provider.search_mod(mod_plan.mod, plan.game_version, plan.loader)
                except Exception as exc:
                    local_errors.append(f"{provider.label}：{exc}")
                    continue
                if result:
                    return mod_plan, result, local_errors
            return mod_plan, None, local_errors

        with ThreadPoolExecutor(max_workers=self.settings.max_workers) as executor:
            future_to_plan = {executor.submit(search_one, mod_plan): mod_plan for mod_plan in unmatched}
            completed = 0
            for future in as_completed(future_to_plan):
                completed += 1
                pending_plan = future_to_plan[future]
                try:
                    mod_plan, match, local_errors = future.result()
                except Exception as exc:
                    mod_plan = pending_plan
                    match = None
                    local_errors = [f"搜索异常：{exc}"]
                mod_plan.match = match
                if local_errors:
                    errors[str(mod_plan.mod.path)] = local_errors
                if match:
                    mod_plan.notes.append(f"按名称匹配到 {match.project.title}")
                self._emit(
                    f"搜索进度：{mod_plan.mod.display_name}",
                    completed,
                    len(unmatched),
                )
        for mod_plan in unmatched:
            if mod_plan.match:
                continue
            mod_plan.status = "not_found"
            if not ordered:
                mod_plan.notes.append("没有可用的在线更新源")
            elif errors.get(str(mod_plan.mod.path)):
                mod_plan.status = "error"
                mod_plan.error = "；".join(errors[str(mod_plan.mod.path)][:2])
                mod_plan.notes.append("所有更新源查询均失败")
            elif not mod_plan.mod.mod_id and not mod_plan.mod.name:
                mod_plan.status = "unknown"
                mod_plan.notes.append("无法从 jar 读取 mod id 或名称")
            else:
                mod_plan.notes.append("Modrinth / CurseForge 均未找到可信匹配")

    def _resolve_targets(self, plan: UpdatePlan) -> None:
        targets = [mod_plan for mod_plan in plan.mods if mod_plan.match]
        if not targets:
            return
        self._emit(f"查询 {len(targets)} 个项目的兼容版本…", 0, len(targets))

        def resolve_one(mod_plan: ModPlan) -> None:
            assert mod_plan.match is not None
            project = mod_plan.match.project
            provider = self.providers.get(project.provider)
            if provider is None:
                mod_plan.status = "error"
                mod_plan.error = "对应更新源未启用"
                return
            try:
                if not project.slug or project.title in ("", project.project_id):
                    full_project = provider.get_project(project.project_id)
                    if full_project:
                        project = full_project
                        mod_plan.match.project = project
                versions = provider.get_versions(project.project_id, plan.game_version, plan.loader)
            except Exception as exc:
                mod_plan.status = "error"
                mod_plan.error = f"{provider.label}：{exc}"
                mod_plan.notes.append("查询目标版本失败")
                return
            target = select_latest_version(
                versions,
                plan.game_version,
                plan.loader,
                allow_prerelease=plan.allow_prerelease,
            )
            if target is None:
                compatible = [version for version in versions if version.supports(plan.game_version, plan.loader)]
                if compatible and not plan.allow_prerelease:
                    mod_plan.status = "no_compatible"
                    mod_plan.notes.append("只有测试版，未启用测试版更新")
                elif versions:
                    mod_plan.status = "no_compatible"
                    mod_plan.notes.append(
                        f"没有适用于 {plan.game_version} / {LOADER_LABELS.get(plan.loader, plan.loader)} 的文件"
                    )
                else:
                    mod_plan.status = "no_compatible"
                    mod_plan.notes.append("项目下没有可用版本")
                return
            mod_plan.target_version = target
            if not target.download_url:
                mod_plan.notes.append("作者未开放 API 直链，需手动下载")
            if target.release_type not in ("release", "1", ""):
                mod_plan.notes.append(f"目标为 {target.release_type} 版本")

            current = mod_plan.current_version
            if current:
                if current.version_id == target.version_id or versions_equivalent(
                    current.version_number, target.version_number
                ):
                    mod_plan.status = "latest"
                    mod_plan.notes.append("当前文件已是目标版本")
                elif current.published and target.published:
                    from .util import parse_datetime

                    current_date = parse_datetime(current.published)
                    target_date = parse_datetime(target.published)
                    if current_date and target_date and target_date <= current_date and current.supports(
                        plan.game_version, plan.loader
                    ):
                        mod_plan.status = "latest"
                        mod_plan.notes.append("远端没有比当前更新的稳定版本")
                    else:
                        mod_plan.status = "update"
                else:
                    mod_plan.status = "update"
            else:
                local_version = mod_plan.mod.version
                if local_version and versions_equivalent(local_version, target.version_number):
                    mod_plan.status = "latest"
                    mod_plan.notes.append("按版本号判断已是最新")
                else:
                    mod_plan.status = "update"
                    if not local_version:
                        mod_plan.notes.append("当前版本未知，将按最新版处理")

        with ThreadPoolExecutor(max_workers=self.settings.max_workers) as executor:
            futures = [executor.submit(resolve_one, mod_plan) for mod_plan in targets]
            completed = 0
            for future in as_completed(futures):
                completed += 1
                try:
                    future.result()
                except Exception as exc:
                    plan.warnings.append(f"版本查询异常：{exc}")
                self._emit(f"版本查询进度 {completed}/{len(targets)}", completed, len(targets))
        for mod_plan in plan.mods:
            if mod_plan.status in ("update", "latest", "no_compatible", "error"):
                continue
            if mod_plan.match:
                # 匹配到了项目，但没有走完版本查询。
                if mod_plan.status == "unknown":
                    mod_plan.status = "error"
                    mod_plan.error = "版本查询未完成"

    def _finalize_statuses(self, plan: UpdatePlan) -> None:
        for mod_plan in plan.mods:
            if mod_plan.status in ("update", "latest", "not_found", "no_compatible", "error", "unknown"):
                continue
            if not mod_plan.match:
                mod_plan.status = "not_found" if mod_plan.status == "unknown" else mod_plan.status
            else:
                mod_plan.status = "no_compatible"
                mod_plan.notes.append("未找到目标版本")
            if plan.cross_loader and mod_plan.status == "no_compatible":
                mod_plan.notes.append("目标加载器不同，原加载器版本不能直接使用")

    def _scan_configs(self, plan: UpdatePlan, mods: Iterable[ModFile]) -> None:
        scope = "none" if not self.settings.copy_configs else self.settings.config_scope
        self._emit("扫描 config / defaultconfigs…")
        plan.configs = scan_configs(plan.source_root, mods, scope=scope)
        if scope == "matched":
            enrich_config_owners(plan.configs, plan.mods)
        if scope != "none" and not plan.configs:
            plan.warnings.append("没有找到 config 或 defaultconfigs 文件夹，配置迁移列表为空。")

    def _add_final_warnings(self, plan: UpdatePlan) -> None:
        unresolved = [dep for dep in plan.dependencies if dep.state == "unresolved"]
        if unresolved:
            plan.warnings.append(f"{len(unresolved)} 个前置未能自动解析，请查看“前置变化”页。")
        missing = [dep for dep in plan.dependencies if dep.state == "missing"]
        if missing:
            plan.warnings.append(f"发现 {len(missing)} 个需要补充的前置，将自动加入下载列表。")
        if plan.updates:
            no_download = [mod for mod in plan.updates if not mod.target_version or not mod.target_version.download_url]
            if no_download:
                plan.warnings.append(f"{len(no_download)} 个可更新模组需要手动下载（作者未开放直链）。")
