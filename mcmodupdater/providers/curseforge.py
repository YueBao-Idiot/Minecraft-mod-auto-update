from __future__ import annotations

import re
import urllib.parse
from typing import Any, Iterable

from ..models import MatchResult, ModFile, ProjectInfo, RemoteDependency, VersionInfo
from ..util import chunked, normalize_name
from .base import ModProvider, ProviderConfig
from .http import ProviderError

MOD_LOADER_TYPES = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
RELATION_TYPES = {
    1: "embedded",
    2: "optional",
    3: "required",
    4: "tool",
    5: "incompatible",
    6: "include",
}
MC_VERSION_PATTERN = re.compile(r"^(?:1\.\d{1,2}(?:\.\d{1,2})?|2\d\.\d{1,2}(?:\.\d{1,2})?)$")


class CurseForgeProvider(ModProvider):
    name = "curseforge"
    label = "CurseForge"

    def __init__(self, config: ProviderConfig) -> None:
        config.base_url = (config.base_url or "https://api.curseforge.com/v1").rstrip("/")
        super().__init__(config)
        if config.api_key:
            self.config.headers = {**config.headers, "x-api-key": config.api_key}

    @property
    def available(self) -> bool:
        return bool(self.config.api_key) or "api.curseforge.com" not in self.config.base_url

    @property
    def unavailable_reason(self) -> str:
        return (
            "未填写 CurseForge API Key（可在 https://console.curseforge.com/ 免费申请），"
            "也未启用公开代理"
        )

    # ---------- 解析 ----------

    @staticmethod
    def _project_from_json(data: dict[str, Any]) -> ProjectInfo:
        slug = str(data.get("slug") or data.get("id") or "")
        links = data.get("links") if isinstance(data.get("links"), dict) else {}
        logo = data.get("logo") if isinstance(data.get("logo"), dict) else {}
        url = str(links.get("websiteUrl") or "")
        if not url and slug:
            url = f"https://www.curseforge.com/minecraft/mc-mods/{slug}"
        return ProjectInfo(
            provider="curseforge",
            project_id=str(data.get("id") or ""),
            slug=slug,
            title=str(data.get("name") or slug),
            description=str(data.get("summary") or ""),
            url=url,
            downloads=int(data.get("downloadCount") or 0),
            icon_url=str(logo.get("thumbnailUrl") or ""),
        )

    @staticmethod
    def _release_type(value: Any) -> str:
        return {1: "release", 2: "beta", 3: "alpha"}.get(int(value or 1), "release")

    @staticmethod
    def _game_versions(file_data: dict[str, Any]) -> tuple[list[str], list[str]]:
        raw = [str(item) for item in (file_data.get("gameVersions") or []) if item]
        known_loaders = {"forge", "fabric", "neoforge", "quilt"}
        loaders: list[str] = []
        versions: list[str] = []
        for value in raw:
            lower = value.lower()
            if lower in known_loaders:
                loaders.append(lower)
            elif MC_VERSION_PATTERN.match(value):
                versions.append(value)
            elif lower == "neo" and "neoforge" not in loaders:
                loaders.append("neoforge")
        return versions, loaders

    def _version_from_file(self, data: dict[str, Any], project: ProjectInfo | None = None) -> VersionInfo:
        mod_id = str(data.get("modId") or (project.project_id if project else "") or "")
        if project is None:
            project = self._project_cache.get(mod_id)
        game_versions, loaders = self._game_versions(data)
        dependencies: list[RemoteDependency] = []
        for item in data.get("dependencies", []) if isinstance(data.get("dependencies"), list) else []:
            if not isinstance(item, dict):
                continue
            relation = RELATION_TYPES.get(int(item.get("relationType") or 3), "required")
            dependencies.append(
                RemoteDependency(
                    project_id=str(item.get("modId") or ""),
                    version_id=str(item.get("fileId") or ""),
                    dependency_type=relation,
                )
            )
        hashes: dict[str, str] = {}
        for item in data.get("hashes", []) if isinstance(data.get("hashes"), list) else []:
            if not isinstance(item, dict):
                continue
            algorithm = int(item.get("algo") or 0)
            if algorithm == 1:
                hashes["sha1"] = str(item.get("value") or "")
            elif algorithm == 2:
                hashes["md5"] = str(item.get("value") or "")
        download_url = str(data.get("downloadUrl") or "")
        return VersionInfo(
            provider="curseforge",
            project_id=mod_id,
            version_id=str(data.get("id") or ""),
            version_number=str(data.get("displayName") or data.get("fileName") or ""),
            name=str(data.get("displayName") or ""),
            game_versions=game_versions,
            loaders=loaders,
            published=str(data.get("fileDate") or ""),
            download_url=download_url,
            filename=str(data.get("fileName") or ""),
            dependencies=dependencies,
            hashes=hashes,
            file_size=int(data.get("fileLength") or 0),
            release_type=self._release_type(data.get("releaseType")),
            project_slug=project.slug if project else "",
            project_title=project.title if project else "",
        )

    # ---------- 基础请求 ----------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.available:
            raise ProviderError(self.unavailable_reason)
        return self._json(
            method,
            f"{self.config.base_url}{path}",
            headers=self.config.headers,
            **kwargs,
        )

    def _get_mods_batch(self, mod_ids: Iterable[str]) -> dict[str, ProjectInfo]:
        wanted = [str(mod_id) for mod_id in mod_ids if mod_id]
        missing = [mod_id for mod_id in wanted if mod_id not in self._project_cache]
        for chunk in chunked(missing, 50):
            try:
                data = self._request("POST", "/mods", json_body={"modIds": [int(item) for item in chunk]})
            except (ProviderError, ValueError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                for item in data["data"]:
                    if isinstance(item, dict):
                        project = self._project_from_json(item)
                        self._project_cache[project.project_id] = project
            else:
                for mod_id in chunk:
                    project = self.get_project(mod_id)
                    if project:
                        self._project_cache[project.project_id] = project
        return {mod_id: self._project_cache[mod_id] for mod_id in wanted if mod_id in self._project_cache}

    def get_project(self, project_id: str) -> ProjectInfo | None:
        if not project_id:
            return None
        if project_id in self._project_cache:
            return self._project_cache[project_id]
        try:
            data = self._request("GET", f"/mods/{urllib.parse.quote(str(project_id))}")
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            return None
        project = self._project_from_json(data["data"])
        self._project_cache[project.project_id] = project
        return project

    # ---------- 精确匹配 ----------

    def match_by_hashes(self, mods: Iterable[ModFile]) -> dict[str, MatchResult]:
        if not self.available:
            return {}
        by_fingerprint: dict[int, ModFile] = {
            int(mod.murmur2): mod for mod in mods if mod.murmur2 is not None
        }
        if not by_fingerprint:
            return {}
        matches: dict[int, dict[str, Any]] = {}
        for fingerprints in chunked(by_fingerprint.keys(), 50):
            try:
                data = self._request(
                    "POST",
                    "/fingerprints",
                    json_body={"fingerprints": list(fingerprints)},
                )
            except ProviderError:
                raise
            if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
                continue
            for item in data["data"].get("exactMatches", []):
                if not isinstance(item, dict) or not isinstance(item.get("file"), dict):
                    continue
                file_data = item["file"]
                fingerprint = int(file_data.get("fileFingerprint") or 0) & 0xFFFFFFFF
                if fingerprint in by_fingerprint:
                    matches[fingerprint] = file_data
        project_ids = {str(file_data.get("modId") or "") for file_data in matches.values()}
        projects = self._get_mods_batch(project_ids)
        results: dict[str, MatchResult] = {}
        for fingerprint, file_data in matches.items():
            mod = by_fingerprint.get(fingerprint)
            if not mod:
                continue
            project_id = str(file_data.get("modId") or "")
            project = projects.get(project_id) or ProjectInfo(
                provider="curseforge",
                project_id=project_id,
                slug="",
                title=f"CurseForge 项目 {project_id}",
                url=f"https://www.curseforge.com/minecraft/mc-mods/{project_id}",
            )
            current = self._version_from_file(file_data, project)
            results[str(mod.path.resolve())] = MatchResult(
                project=project,
                current_version=current,
                method="fingerprint",
                confidence="high",
            )
        return results

    # ---------- 搜索 ----------

    @staticmethod
    def _score_candidate(mod: ModFile, hit: dict[str, Any]) -> int:
        keys = [normalize_name(mod.mod_id), normalize_name(mod.name)]
        keys = [key for key in keys if len(key) >= 3]
        slug = normalize_name(str(hit.get("slug") or ""))
        title = normalize_name(str(hit.get("name") or hit.get("title") or ""))
        # 与 Modrinth 相同：只接受完整相等，避免子串误匹配。
        return 100 if any(key == candidate for key in keys for candidate in (slug, title) if candidate) else 0

    def _search(self, query: str, game_version: str, loader: str, *, filtered: bool) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "gameId": 432,
            "classId": 6,
            "searchFilter": query,
            "sortField": 2,
            "sortOrder": "desc",
            "pageSize": 10,
        }
        if filtered:
            if game_version:
                params["gameVersion"] = game_version
            loader_type = MOD_LOADER_TYPES.get(loader)
            if loader_type:
                params["modLoaderType"] = loader_type
        url = f"/mods/search?{urllib.parse.urlencode(params)}"
        data = self._request("GET", url)
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [item for item in data["data"] if isinstance(item, dict)]
        return []

    def search_mod(self, mod: ModFile, game_version: str, loader: str) -> MatchResult | None:
        queries: list[str] = []
        for value in (mod.mod_id, mod.name):
            value = (value or "").strip().replace("_", " ")
            if len(value) >= 3 and value.lower() not in {query.lower() for query in queries}:
                queries.append(value)
        best_hit: dict[str, Any] | None = None
        best_score = 0
        for query in queries:
            for filtered in (True, False):
                if not filtered and best_hit is not None:
                    break
                try:
                    hits = self._search(query, game_version, loader, filtered=filtered)
                except ProviderError:
                    raise
                for index, hit in enumerate(hits):
                    score = self._score_candidate(mod, hit) + max(0, 9 - index) // 3
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

    # ---------- 版本 ----------

    def _files_page(self, mod_id: str, page: int, game_version: str, loader: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"pageSize": 50, "index": page}
        if game_version:
            params["gameVersion"] = game_version
        loader_type = MOD_LOADER_TYPES.get(loader)
        if loader_type:
            params["modLoaderType"] = loader_type
        data = self._request(
            "GET",
            f"/mods/{urllib.parse.quote(str(mod_id))}/files?{urllib.parse.urlencode(params)}",
        )
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [item for item in data["data"] if isinstance(item, dict)]
        return []

    def get_versions(self, project_id: str, game_version: str, loader: str) -> list[VersionInfo]:
        if not project_id:
            return []
        files: list[dict[str, Any]] = []
        try:
            files = self._files_page(project_id, 0, game_version, loader)
        except ProviderError as exc:
            if exc.status == 404:
                return []
            raise
        if not files:
            # 部分旧项目没有被过滤器命中，回退到最近文件再本地过滤。
            for page in range(3):
                try:
                    files.extend(self._files_page(project_id, page, "", ""))
                except ProviderError:
                    break
        project = self._project_cache.get(project_id)
        if project is None:
            project = self.get_project(project_id)
        versions: list[VersionInfo] = []
        seen: set[str] = set()
        for item in files:
            version = self._version_from_file(item, project)
            if not version.version_id or version.version_id in seen:
                continue
            seen.add(version.version_id)
            self._version_cache[version.version_id] = version
            versions.append(version)
        return versions

    def get_version(self, version_id: str) -> VersionInfo | None:
        if not version_id:
            return None
        cached = self._version_cache.get(version_id)
        if cached:
            return cached
        return None

    def get_version_for_project(self, project_id: str, version_id: str) -> VersionInfo | None:
        if not project_id or not version_id:
            return None
        if version_id in self._version_cache:
            return self._version_cache[version_id]
        try:
            data = self._request(
                "GET",
                f"/mods/{urllib.parse.quote(str(project_id))}/files/{urllib.parse.quote(str(version_id))}",
            )
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            return None
        project = self._project_cache.get(str(project_id)) or self.get_project(str(project_id))
        version = self._version_from_file(data["data"], project)
        self._version_cache[version.version_id] = version
        return version

    def search_url(self, query: str, game_version: str, loader: str) -> str:
        params = {"search": query}
        if game_version:
            params["gameVersion"] = game_version
        return "https://www.curseforge.com/minecraft/mc-mods/search?" + urllib.parse.urlencode(params)
