from __future__ import annotations

import json
import urllib.parse
from typing import Any, Iterable

from ..models import MatchResult, ModFile, ProjectInfo, RemoteDependency, VersionInfo
from ..util import chunked, normalize_name
from .base import ModProvider, ProviderConfig, select_latest_version
from .http import ProviderError


class ModrinthProvider(ModProvider):
    name = "modrinth"
    label = "Modrinth"

    def __init__(self, config: ProviderConfig) -> None:
        config.base_url = (config.base_url or "https://api.modrinth.com/v2").rstrip("/")
        super().__init__(config)

    # ---------- 解析 ----------

    @staticmethod
    def _project_from_json(data: dict[str, Any]) -> ProjectInfo:
        slug = str(data.get("slug") or data.get("id") or "")
        return ProjectInfo(
            provider="modrinth",
            project_id=str(data.get("id") or data.get("project_id") or ""),
            slug=slug,
            title=str(data.get("title") or slug or data.get("name") or ""),
            description=str(data.get("description") or data.get("summary") or ""),
            url=f"https://modrinth.com/mod/{slug}" if slug else "",
            downloads=int(data.get("downloads") or 0),
            icon_url=str(data.get("icon_url") or ""),
        )

    @staticmethod
    def _parse_dependencies(raw: Any) -> list[RemoteDependency]:
        dependencies: list[RemoteDependency] = []
        if not isinstance(raw, list):
            return dependencies
        for item in raw:
            if not isinstance(item, dict):
                continue
            dep_type = str(item.get("dependency_type") or "required").lower()
            dependencies.append(
                RemoteDependency(
                    project_id=str(item.get("project_id") or ""),
                    version_id=str(item.get("version_id") or ""),
                    dependency_type=dep_type,
                    file_name=str(item.get("file_name") or ""),
                )
            )
        return dependencies

    def _version_from_json(self, data: dict[str, Any], project: ProjectInfo | None = None) -> VersionInfo:
        files = data.get("files") if isinstance(data.get("files"), list) else []
        primary = next((item for item in files if isinstance(item, dict) and item.get("primary")), None)
        file_info = primary or next((item for item in files if isinstance(item, dict)), {})
        hashes = file_info.get("hashes") if isinstance(file_info.get("hashes"), dict) else {}
        project_id = str(data.get("project_id") or (project.project_id if project else ""))
        if project is None:
            project = self._project_cache.get(project_id)
        return VersionInfo(
            provider="modrinth",
            project_id=project_id,
            version_id=str(data.get("id") or ""),
            version_number=str(data.get("version_number") or data.get("name") or ""),
            name=str(data.get("name") or ""),
            game_versions=[str(x) for x in (data.get("game_versions") or []) if x],
            loaders=[str(x).lower() for x in (data.get("loaders") or []) if x],
            published=str(data.get("date_published") or data.get("date_created") or ""),
            download_url=str(file_info.get("url") or ""),
            filename=str(file_info.get("filename") or ""),
            dependencies=self._parse_dependencies(data.get("dependencies")),
            hashes={str(k): str(v) for k, v in hashes.items()},
            file_size=int(file_info.get("size") or 0),
            release_type=str(data.get("version_type") or "release").lower(),
            project_slug=project.slug if project else "",
            project_title=project.title if project else "",
            changelog=str(data.get("changelog") or ""),
        )

    # ---------- 精确匹配 ----------

    def match_by_hashes(self, mods: Iterable[ModFile]) -> dict[str, MatchResult]:
        mods = list(mods)
        by_hash: dict[str, ModFile] = {mod.sha512: mod for mod in mods if mod.sha512}
        if not by_hash:
            return {}
        version_map: dict[str, dict[str, Any]] = {}
        for hashes in chunked(by_hash.keys(), 100):
            try:
                response = self._json(
                    "POST",
                    f"{self.config.base_url}/version_files",
                    json_body={"hashes": hashes, "algorithm": "sha512"},
                )
            except ProviderError:
                raise
            if isinstance(response, dict):
                for key, value in response.items():
                    if isinstance(value, dict):
                        version_map[str(key)] = value
        results: dict[str, MatchResult] = {}
        project_ids = list({str(item.get("project_id") or "") for item in version_map.values()} - {""})
        projects = self._get_projects_batch(project_ids)
        for hash_value, version_data in version_map.items():
            mod = by_hash.get(hash_value)
            if not mod:
                continue
            project_id = str(version_data.get("project_id") or "")
            project = projects.get(project_id)
            if project is None:
                project = ProjectInfo(
                    provider="modrinth",
                    project_id=project_id,
                    slug="",
                    title=project_id,
                    url=f"https://modrinth.com/mod/{project_id}" if project_id else "",
                )
            current = self._version_from_json(version_data, project)
            results[str(mod.path.resolve())] = MatchResult(
                project=project,
                current_version=current,
                method="sha512",
                confidence="high",
            )
        return results

    def _get_projects_batch(self, project_ids: Iterable[str]) -> dict[str, ProjectInfo]:
        ids = [project_id for project_id in project_ids if project_id and project_id not in self._project_cache]
        for chunk in chunked(ids, 50):
            encoded = urllib.parse.quote(json.dumps(chunk, separators=(",", ":")), safe="")
            try:
                data = self._json("GET", f"{self.config.base_url}/projects?ids={encoded}")
            except ProviderError:
                data = None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        project = self._project_from_json(item)
                        self._project_cache[project.project_id] = project
            if not isinstance(data, list):
                for project_id in chunk:
                    try:
                        project = self.get_project(project_id)
                    except ProviderError:
                        project = None
                    if project:
                        self._project_cache[project.project_id] = project
        return {project_id: self._project_cache[project_id] for project_id in project_ids if project_id in self._project_cache}

    # ---------- 搜索 ----------

    def _search(self, query: str, game_version: str, loader: str, *, with_version: bool) -> list[dict[str, Any]]:
        facets = [["project_type:mod"], [f"categories:{loader}"]]
        if with_version and game_version:
            facets.append([f"versions:{game_version}"])
        params = {
            "query": query,
            "facets": json.dumps(facets, separators=(",", ":")),
            "limit": 10,
            "index": "relevance",
        }
        url = f"{self.config.base_url}/search?{urllib.parse.urlencode(params)}"
        try:
            data = self._json("GET", url)
        except ProviderError:
            raise
        if isinstance(data, dict) and isinstance(data.get("hits"), list):
            return [item for item in data["hits"] if isinstance(item, dict)]
        return []

    @staticmethod
    def _score_candidate(mod: ModFile, hit: dict[str, Any]) -> int:
        keys = [normalize_name(mod.mod_id), normalize_name(mod.name)]
        keys = [key for key in keys if len(key) >= 3]
        slug = normalize_name(str(hit.get("slug") or ""))
        title = normalize_name(str(hit.get("title") or ""))
        candidates = [value for value in (slug, title) if value]
        # 只接受完整相等。历史上按子串匹配会把 R-Tweaks 匹配到
        # VillagerTweaks、把 Painting 匹配到 Macaw's Paintings，进而下载
        # 完全无关的模组；宁可标成“未找到”，交给人工确认。
        return 100 if any(key == candidate for key in keys for candidate in candidates) else 0

    def search_mod(self, mod: ModFile, game_version: str, loader: str) -> MatchResult | None:
        queries: list[str] = []
        for value in (mod.mod_id, mod.name):
            value = (value or "").strip().replace("_", " ")
            if len(value) >= 3 and value.lower() not in {query.lower() for query in queries}:
                queries.append(value)
        best_hit: dict[str, Any] | None = None
        best_score = 0
        for query in queries:
            for with_version in (True, False):
                if not with_version and best_hit is not None:
                    break
                hits = self._search(query, game_version, loader, with_version=with_version)
                for hit in hits:
                    score = self._score_candidate(mod, hit)
                    # 结果顺序是相关性，给靠前的结果一点加权。
                    score += max(0, 9 - hits.index(hit)) // 3
                    if score > best_score:
                        best_score = score
                        best_hit = hit
                if best_hit is not None and best_score >= 100:
                    break
            if best_hit is not None and best_score >= 100:
                break
        if not best_hit or best_score < 100:
            return None
        project = self._project_from_json(best_hit)
        self._project_cache[project.project_id] = project
        return MatchResult(
            project=project,
            current_version=None,
            method="search",
            confidence="high",
        )

    # ---------- 项目/版本 ----------

    def get_project(self, project_id: str) -> ProjectInfo | None:
        if not project_id:
            return None
        if project_id in self._project_cache:
            return self._project_cache[project_id]
        try:
            data = self._json("GET", f"{self.config.base_url}/project/{urllib.parse.quote(project_id)}")
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            return None
        project = self._project_from_json(data)
        self._project_cache[project.project_id] = project
        return project

    def get_versions(self, project_id: str, game_version: str, loader: str) -> list[VersionInfo]:
        if not project_id:
            return []
        params = {}
        if game_version:
            params["game_versions"] = json.dumps([game_version])
        if loader:
            params["loaders"] = json.dumps([loader])
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        try:
            data = self._json(
                "GET",
                f"{self.config.base_url}/project/{urllib.parse.quote(project_id)}/version{query}",
            )
        except ProviderError as exc:
            if exc.status == 404:
                return []
            raise
        if not isinstance(data, list):
            return []
        project = self._project_cache.get(project_id)
        versions = []
        for item in data:
            if not isinstance(item, dict):
                continue
            version = self._version_from_json(item, project)
            if version.version_id:
                self._version_cache[version.version_id] = version
                versions.append(version)
        return versions

    def get_version(self, version_id: str) -> VersionInfo | None:
        if not version_id:
            return None
        if version_id in self._version_cache:
            return self._version_cache[version_id]
        try:
            data = self._json("GET", f"{self.config.base_url}/version/{urllib.parse.quote(version_id)}")
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            return None
        project = self.get_project(str(data.get("project_id") or ""))
        version = self._version_from_json(data, project)
        self._version_cache[version.version_id] = version
        return version

    def search_url(self, query: str, game_version: str, loader: str) -> str:
        params = {"q": query}
        if game_version:
            params["g"] = game_version
        if loader:
            params["l"] = loader
        return "https://modrinth.com/mods?" + urllib.parse.urlencode(params)
