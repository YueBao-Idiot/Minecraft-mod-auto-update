from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from mcmodupdater.jarinfo import curseforge_fingerprint

from .mock_api import MockApi


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def make_fabric_jar(
    path: Path,
    *,
    mod_id: str,
    name: str,
    version: str,
    depends: dict[str, str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schemaVersion": 1,
        "id": mod_id,
        "version": version,
        "name": name,
        "description": f"{name} fixture",
        "authors": ["Test"],
        "depends": {"minecraft": "1.20.1", "fabricloader": ">=0.14.0", **(depends or {})},
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fabric.mod.json", json.dumps(metadata, ensure_ascii=False))
        archive.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\nImplementation-Version: {version}\n")
        archive.writestr("assets/example.txt", f"{mod_id} {version}")
    return path


def make_forge_jar(
    path: Path,
    *,
    mod_id: str,
    name: str,
    version: str,
    depends: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        'modLoader="javafml"',
        'loaderVersion="[47,)"',
        'license="MIT"',
        "[[mods]]",
        f'modId="{mod_id}"',
        f'version="{version}"',
        f'displayName="{name}"',
        'description="Forge fixture"',
        "[[dependencies." + mod_id + "]]",
        'modId="forge"',
        "mandatory=true",
        'versionRange="[47,)"',
        'ordering="NONE"',
        'side="BOTH"',
        "[[dependencies." + mod_id + "]]",
        'modId="minecraft"',
        "mandatory=true",
        'versionRange="[1.20.1,1.21)"',
        'ordering="NONE"',
        'side="BOTH"',
    ]
    for dependency in depends or []:
        lines.extend(
            [
                "[[dependencies." + mod_id + "]]",
                f'modId="{dependency}"',
                "mandatory=true",
                'versionRange="[1.0,)"',
                'ordering="NONE"',
                'side="BOTH"',
            ]
        )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/mods.toml", "\n".join(lines) + "\n")
        archive.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\nImplementation-Version: {version}\n")
        archive.writestr("assets/example.txt", f"{mod_id} {version}")
    return path


def make_neoforge_jar(path: Path, *, mod_id: str, name: str, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        'modLoader="javafml"',
        'loaderVersion="[47,)"',
        'license="MIT"',
        "[[mods]]",
        f'modId="{mod_id}"',
        f'version="{version}"',
        f'displayName="{name}"',
        "[[dependencies." + mod_id + "]]",
        'modId="neoforge"',
        "mandatory=true",
        'versionRange="[47,)"',
        'ordering="NONE"',
        'side="BOTH"',
        "[[dependencies." + mod_id + "]]",
        'modId="minecraft"',
        "mandatory=true",
        'versionRange="[1.20.1,1.21)"',
        'ordering="NONE"',
        'side="BOTH"',
    ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/neoforge.mods.toml", "\n".join(lines) + "\n")
        archive.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\nImplementation-Version: {version}\n")
    return path


def _mr_project(project_id: str, slug: str, title: str) -> dict:
    return {
        "id": project_id,
        "slug": slug,
        "title": title,
        "description": f"{title} description",
        "downloads": 123456,
        "icon_url": "",
        "project_type": "mod",
    }


def _mr_version(
    *,
    project_id: str,
    version_id: str,
    version_number: str,
    file_path: Path,
    base_url: str,
    published: str,
    dependencies: list[dict] | None = None,
    game_versions: list[str] | None = None,
    loaders: list[str] | None = None,
) -> dict:
    return {
        "id": version_id,
        "project_id": project_id,
        "name": f"Version {version_number}",
        "version_number": version_number,
        "game_versions": game_versions or ["1.20.1"],
        "loaders": loaders or ["fabric"],
        "date_published": published,
        "version_type": "release",
        "dependencies": dependencies or [],
        "files": [
            {
                "url": f"{base_url}/files/{file_path.name}",
                "filename": file_path.name,
                "primary": True,
                "size": file_path.stat().st_size,
                "hashes": {"sha1": sha1_file(file_path), "sha512": sha512_file(file_path)},
            }
        ],
    }


def build_modrinth_fixture(api: MockApi, source: Path, files: Path) -> dict:
    mods = source / "mods"
    config = source / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "alpha-common.toml").write_text("# Alpha\nenabled = true\n", encoding="utf-8")
    (config / "beta-lib.toml").write_text("# Beta Library\nlevel = 2\n", encoding="utf-8")
    (config / "delta.toml").write_text("# Delta\nstyle = \"pretty\"\n", encoding="utf-8")
    (config / "unowned.toml").write_text("# nothing here\n", encoding="utf-8")
    (source / "defaultconfigs").mkdir(exist_ok=True)
    (source / "defaultconfigs" / "gamma.toml").write_text("# Gamma defaults\n", encoding="utf-8")

    alpha_cur = make_fabric_jar(mods / "alpha-1.0.0.jar", mod_id="alpha", name="Alpha Core", version="1.0.0")
    beta_cur = make_fabric_jar(mods / "beta-lib-2.0.0.jar", mod_id="beta-lib", name="Beta Library", version="2.0.0")
    fapi_cur = make_fabric_jar(mods / "fabric-api-0.90.0.jar", mod_id="fabric-api", name="Fabric API", version="0.90.0")
    delta_cur = make_fabric_jar(mods / "delta-1.0.0.jar", mod_id="delta", name="Delta Decoration", version="1.0.0")
    sodium_cur = make_fabric_jar(mods / "sodium-0.5.0.jar", mod_id="sodium", name="Sodium", version="0.5.0")
    search_cur = make_fabric_jar(mods / "searchmod-0.1.0.jar", mod_id="searchmod", name="Search Mod", version="0.1.0")
    make_fabric_jar(mods / "localonly-1.0.0.jar", mod_id="localonly", name="Local Only", version="1.0.0")

    alpha_new = make_fabric_jar(files / "alpha-1.1.0.jar", mod_id="alpha", name="Alpha Core", version="1.1.0")
    beta_new = make_fabric_jar(files / "beta-lib-2.0.1.jar", mod_id="beta-lib", name="Beta Library", version="2.0.1")
    fapi_new = make_fabric_jar(files / "fabric-api-0.91.0.jar", mod_id="fabric-api", name="Fabric API", version="0.91.0")
    gamma_new = make_fabric_jar(files / "gamma-1.0.0.jar", mod_id="gamma", name="Gamma Addon", version="1.0.0")
    delta_old = make_fabric_jar(files / "delta-1.0.0-1.19.2.jar", mod_id="delta", name="Delta Decoration", version="1.0.0")
    search_new = make_fabric_jar(files / "searchmod-0.2.0.jar", mod_id="searchmod", name="Search Mod", version="0.2.0")

    base = api.state.base_url
    projects = {
        "P_ALPHA": _mr_project("P_ALPHA", "alpha", "Alpha Core"),
        "P_BETA": _mr_project("P_BETA", "beta-lib", "Beta Library"),
        "P_FAPI": _mr_project("P_FAPI", "fabric-api", "Fabric API"),
        "P_DELTA": _mr_project("P_DELTA", "delta", "Delta Decoration"),
        "P_SODIUM": _mr_project("P_SODIUM", "sodium", "Sodium"),
        "P_SEARCH": _mr_project("P_SEARCH", "searchmod", "Search Mod"),
        "P_GAMMA": _mr_project("P_GAMMA", "gamma", "Gamma Addon"),
    }
    versions = {
        "V_ALPHA_100": _mr_version(
            project_id="P_ALPHA",
            version_id="V_ALPHA_100",
            version_number="1.0.0",
            file_path=alpha_cur,
            base_url=base,
            published="2025-01-01T00:00:00Z",
            dependencies=[
                {"project_id": "P_FAPI", "version_id": "", "dependency_type": "required"},
                {"project_id": "P_BETA", "version_id": "", "dependency_type": "required"},
            ],
        ),
        "V_ALPHA_110": _mr_version(
            project_id="P_ALPHA",
            version_id="V_ALPHA_110",
            version_number="1.1.0",
            file_path=alpha_new,
            base_url=base,
            published="2025-03-01T00:00:00Z",
            dependencies=[
                {"project_id": "P_FAPI", "version_id": "", "dependency_type": "required"},
                {"project_id": "P_GAMMA", "version_id": "", "dependency_type": "required"},
            ],
        ),
        "V_BETA_200": _mr_version(
            project_id="P_BETA",
            version_id="V_BETA_200",
            version_number="2.0.0",
            file_path=beta_cur,
            base_url=base,
            published="2025-01-02T00:00:00Z",
            dependencies=[{"project_id": "P_FAPI", "version_id": "", "dependency_type": "required"}],
        ),
        "V_BETA_201": _mr_version(
            project_id="P_BETA",
            version_id="V_BETA_201",
            version_number="2.0.1",
            file_path=beta_new,
            base_url=base,
            published="2025-03-02T00:00:00Z",
            dependencies=[{"project_id": "P_FAPI", "version_id": "", "dependency_type": "required"}],
        ),
        "V_FAPI_090": _mr_version(
            project_id="P_FAPI",
            version_id="V_FAPI_090",
            version_number="0.90.0",
            file_path=fapi_cur,
            base_url=base,
            published="2025-01-03T00:00:00Z",
        ),
        "V_FAPI_091": _mr_version(
            project_id="P_FAPI",
            version_id="V_FAPI_091",
            version_number="0.91.0",
            file_path=fapi_new,
            base_url=base,
            published="2025-03-03T00:00:00Z",
        ),
        "V_DELTA_100": _mr_version(
            project_id="P_DELTA",
            version_id="V_DELTA_100",
            version_number="1.0.0",
            file_path=delta_cur,
            base_url=base,
            published="2025-01-04T00:00:00Z",
            game_versions=["1.19.2"],
        ),
        "V_DELTA_OLD": _mr_version(
            project_id="P_DELTA",
            version_id="V_DELTA_OLD",
            version_number="1.0.0",
            file_path=delta_old,
            base_url=base,
            published="2024-01-04T00:00:00Z",
            game_versions=["1.19.2"],
        ),
        "V_SODIUM_050": _mr_version(
            project_id="P_SODIUM",
            version_id="V_SODIUM_050",
            version_number="0.5.0",
            file_path=sodium_cur,
            base_url=base,
            published="2025-01-05T00:00:00Z",
        ),
        "V_SEARCH_020": _mr_version(
            project_id="P_SEARCH",
            version_id="V_SEARCH_020",
            version_number="0.2.0",
            file_path=search_new,
            base_url=base,
            published="2025-03-05T00:00:00Z",
        ),
        "V_GAMMA_100": _mr_version(
            project_id="P_GAMMA",
            version_id="V_GAMMA_100",
            version_number="1.0.0",
            file_path=gamma_new,
            base_url=base,
            published="2025-03-06T00:00:00Z",
        ),
    }
    project_versions = {
        "P_ALPHA": [versions["V_ALPHA_100"], versions["V_ALPHA_110"]],
        "P_BETA": [versions["V_BETA_200"], versions["V_BETA_201"]],
        "P_FAPI": [versions["V_FAPI_090"], versions["V_FAPI_091"]],
        "P_DELTA": [versions["V_DELTA_100"], versions["V_DELTA_OLD"]],
        "P_SODIUM": [versions["V_SODIUM_050"]],
        "P_SEARCH": [versions["V_SEARCH_020"]],
        "P_GAMMA": [versions["V_GAMMA_100"]],
    }
    api.state.modrinth.update(
        {
            "projects": projects,
            "versions": versions,
            "project_versions": project_versions,
            "hash_to_version": {
                sha512_file(alpha_cur): "V_ALPHA_100",
                sha512_file(beta_cur): "V_BETA_200",
                sha512_file(fapi_cur): "V_FAPI_090",
                sha512_file(delta_cur): "V_DELTA_100",
                sha512_file(sodium_cur): "V_SODIUM_050",
            },
            "search": [projects["P_SEARCH"]],
        }
    )
    return {"alpha_cur": alpha_cur, "search_cur": search_cur}


def _cf_project(project_id: int, slug: str, name: str) -> dict:
    return {
        "id": project_id,
        "name": name,
        "slug": slug,
        "summary": f"{name} fixture",
        "downloadCount": 1000,
        "links": {"websiteUrl": f"https://www.curseforge.com/minecraft/mc-mods/{slug}"},
        "logo": {"thumbnailUrl": ""},
    }


def _cf_file(
    *,
    file_id: int,
    mod_id: int,
    name: str,
    file_path: Path,
    base_url: str,
    file_date: str,
    dependencies: list[dict] | None = None,
) -> dict:
    return {
        "id": file_id,
        "modId": mod_id,
        "displayName": name,
        "fileName": file_path.name,
        "releaseType": 1,
        "fileDate": file_date,
        "fileLength": file_path.stat().st_size,
        "downloadUrl": f"{base_url}/files/{file_path.name}",
        "gameVersions": ["1.20.1", "Forge"],
        "hashes": [
            {"algo": 1, "value": sha1_file(file_path)},
        ],
        "dependencies": dependencies or [],
    }


def build_curseforge_fixture(api: MockApi, source: Path, files: Path) -> dict:
    mods = source / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    current = make_forge_jar(mods / "cfmod-1.0.0.jar", mod_id="cfmod", name="CF Mod", version="1.0.0")
    target = make_forge_jar(files / "cfmod-1.1.0.jar", mod_id="cfmod", name="CF Mod", version="1.1.0", depends=["cflib"])
    library = make_forge_jar(files / "cflib-1.0.0.jar", mod_id="cflib", name="CF Library", version="1.0.0")
    base = api.state.base_url
    fingerprint = curseforge_fingerprint(current)
    current_file = _cf_file(
        file_id=1001,
        mod_id=101,
        name="CF Mod 1.0.0",
        file_path=current,
        base_url=base,
        file_date="2025-01-01T00:00:00Z",
    )
    current_file["fileFingerprint"] = fingerprint
    target_file = _cf_file(
        file_id=1003,
        mod_id=101,
        name="CF Mod 1.1.0",
        file_path=target,
        base_url=base,
        file_date="2025-03-01T00:00:00Z",
        dependencies=[{"modId": 102, "fileId": 1004, "relationType": 3}],
    )
    library_file = _cf_file(
        file_id=1004,
        mod_id=102,
        name="CF Library 1.0.0",
        file_path=library,
        base_url=base,
        file_date="2025-02-01T00:00:00Z",
    )
    projects = {
        "101": _cf_project(101, "cfmod", "CF Mod"),
        "102": _cf_project(102, "cflib", "CF Library"),
    }
    api.state.curseforge.update(
        {
            "projects": projects,
            "files": {"1001": current_file, "1003": target_file, "1004": library_file},
            "fingerprints": {str(fingerprint): "1001"},
            "search": [projects["101"]],
        }
    )
    (source / "config").mkdir(exist_ok=True)
    (source / "config" / "cfmod-common.toml").write_text("enabled = true\n", encoding="utf-8")
    return {"current": current, "target": target, "library": library}
