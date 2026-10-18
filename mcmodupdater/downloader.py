from __future__ import annotations

import hashlib
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .configs import copy_configs
from .models import LOADER_LABELS, UpdatePlan, VersionInfo
from .paths import APP_ROOT, CACHE_DIR, OUTPUT_DIR, ensure_within
from .settings import AppSettings
from .util import write_json

ProgressCallback = Callable[[str, int, int], None]
DOWNLOAD_USER_AGENT = f"MinecraftModAutoUpdater/{__version__} (local portable tool)"
PREVIEW_DIR = CACHE_DIR / "report_previews"


@dataclass
class DownloadRecord:
    kind: str
    name: str
    version: str
    filename: str
    url: str = ""
    status: str = "pending"
    error: str = ""
    project_url: str = ""
    old_path: str = ""

    @property
    def status_label(self) -> str:
        return {
            "downloaded": "已下载",
            "failed": "下载失败",
            "manual": "需手动下载",
            "skipped": "已跳过",
            "pending": "等待中",
        }.get(self.status, self.status)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "filename": self.filename,
            "url": self.url,
            "status": self.status,
            "error": self.error,
            "project_url": self.project_url,
        }


@dataclass
class ApplyResult:
    output_dir: Path
    zip_path: Path
    records: list[DownloadRecord] = field(default_factory=list)
    copied_old: int = 0
    configs_copied: int = 0
    config_errors: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report_paths: dict[str, Path] = field(default_factory=dict)
    mod_downloaded_paths: list[str] = field(default_factory=list)

    @property
    def downloaded(self) -> list[DownloadRecord]:
        return [record for record in self.records if record.status == "downloaded"]

    @property
    def failed(self) -> list[DownloadRecord]:
        return [record for record in self.records if record.status == "failed"]

    @property
    def manual(self) -> list[DownloadRecord]:
        return [record for record in self.records if record.status == "manual"]

    @property
    def zip_size(self) -> int:
        try:
            return self.zip_path.stat().st_size
        except OSError:
            return 0

    def to_dict(self) -> dict:
        return {
            "output_dir": str(self.output_dir),
            "zip_path": str(self.zip_path),
            "zip_size": self.zip_size,
            "records": [record.to_dict() for record in self.records],
            "copied_old": self.copied_old,
            "configs_copied": self.configs_copied,
            "config_errors": self.config_errors,
            "errors": self.errors,
            "report_paths": {key: str(value) for key, value in self.report_paths.items()},
            "mod_downloaded_paths": self.mod_downloaded_paths,
        }


def _safe_filename(name: str, fallback: str) -> str:
    name = Path(name or "").name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = fallback
    if not name.lower().endswith(".jar"):
        name += ".jar"
    return name


def _clean_component(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z._+\-]+", "-", value or "").strip(".-_")
    return value or "unknown"


def build_package_name(plan: UpdatePlan, now: datetime | None = None) -> str:
    """生成可读、可排序、跨平台安全的包名。

    例：ModPack_1.20.1_to_1.21.1_Fabric_20261018-1530
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    source = _clean_component(plan.detected_game_version or "unknown")
    target = _clean_component(plan.game_version or "unknown")
    loader = _clean_component(plan.loader_label)
    if source == target:
        core = f"ModPack_{target}_{loader}"
    else:
        core = f"ModPack_{source}_to_{target}_{loader}"
    if plan.detected_loader and plan.detected_loader != plan.loader:
        core += f"_from_{_clean_component(LOADER_LABELS.get(plan.detected_loader, plan.detected_loader))}"
    return f"{core}_{stamp}"


def create_output_dir(package_name: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = OUTPUT_DIR / package_name
    candidate = base
    counter = 2
    while candidate.exists():
        candidate = OUTPUT_DIR / f"{base.name}_{counter}"
        counter += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return ensure_within(APP_ROOT, candidate)


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_hashes(path: Path, expected: dict[str, str]) -> str:
    checks = [key for key in ("sha512", "sha1", "md5") if expected.get(key)]
    for algorithm in checks:
        if not hasattr(hashlib, algorithm):
            continue
        actual = _hash_file(path, algorithm)
        if actual.lower() != expected[algorithm].lower():
            return f"{algorithm} 校验失败（期望 {expected[algorithm][:12]}…，实际 {actual[:12]}…）"
    return ""


def download_file(url: str, destination: Path, expected_hashes: dict[str, str], retries: int = 3) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    last_error = ""
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})
            with urllib.request.urlopen(request, timeout=90) as response, part.open("wb") as handle:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    handle.write(block)
            error = verify_hashes(part, expected_hashes)
            if error:
                part.unlink(missing_ok=True)
                raise ValueError(error)
            part.replace(destination)
            return
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            part.unlink(missing_ok=True)
            if exc.code in (408, 429) or 500 <= exc.code < 600:
                if attempt < retries:
                    time.sleep(min(15, 2 ** attempt))
                    continue
            raise RuntimeError(last_error) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = str(exc)
            part.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(min(15, 2 ** attempt))
                continue
            raise RuntimeError(last_error) from exc
    raise RuntimeError(last_error or "下载失败")


class Applier:
    def __init__(self, plan: UpdatePlan, settings: AppSettings, progress: ProgressCallback | None = None) -> None:
        self.plan = plan
        self.settings = settings
        self.progress = progress
        self.package_name = build_package_name(plan)
        self.output_dir = create_output_dir(self.package_name)
        # 若同一分钟发生重名，目录会带 _2 后缀，zip 名跟随目录名保持一致。
        self.package_name = self.output_dir.name
        self.zip_path = self.output_dir / f"{self.package_name}.zip"
        self.build_dir = self.output_dir / ".build"
        self.temp_dir = self.output_dir / ".tmp"
        self.mods_dir = self.build_dir / "mods"
        self.configs_dir = self.build_dir / "config"
        self.reports_dir = self.build_dir / "reports"
        for directory in (self.mods_dir, self.configs_dir, self.reports_dir, self.temp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.result = ApplyResult(output_dir=self.output_dir, zip_path=self.zip_path)

    def _emit(self, message: str, current: int = 0, total: int = 0) -> None:
        if self.progress:
            try:
                self.progress(message, current, total)
            except Exception:
                pass

    def apply(self) -> ApplyResult:
        jobs: list[tuple[str, str, VersionInfo, str, str]] = []
        used_versions: set[tuple[str, str]] = set()
        for mod_plan in self.plan.selected_updates:
            version = mod_plan.target_version
            if not version:
                continue
            signature = (version.provider, version.version_id)
            if signature in used_versions:
                continue
            used_versions.add(signature)
            jobs.append(
                (
                    "mod",
                    mod_plan.mod.display_name,
                    version,
                    mod_plan.project.url if mod_plan.project else "",
                    str(mod_plan.mod.path.resolve()),
                )
            )
            if not version.download_url:
                self.result.records.append(
                    DownloadRecord(
                        kind="mod",
                        name=mod_plan.mod.display_name,
                        version=version.display_version,
                        filename=version.filename,
                        status="manual",
                        project_url=version.manual_url,
                        error="作者未开放 API 直链",
                        old_path=str(mod_plan.mod.path.resolve()),
                    )
                )
        for dep in self.plan.missing_dependencies:
            version = dep.resolved_version
            if not version:
                continue
            signature = (version.provider, version.version_id)
            if signature in used_versions:
                continue
            used_versions.add(signature)
            jobs.append(("dependency", dep.display_name, version, "", ""))
            if not version.download_url:
                self.result.records.append(
                    DownloadRecord(
                        kind="dependency",
                        name=dep.display_name,
                        version=version.display_version,
                        filename=version.filename,
                        status="manual",
                        project_url=version.manual_url,
                        error="作者未开放 API 直链",
                    )
                )

        downloaded_files: list[tuple[Path, DownloadRecord]] = []
        download_jobs = [job for job in jobs if job[2].download_url]
        if download_jobs:
            self._emit(f"准备下载 {len(download_jobs)} 个文件…", 0, len(download_jobs))
        with ThreadPoolExecutor(max_workers=min(3, max(1, len(download_jobs)))) as executor:
            future_map = {}
            for index, (kind, name, version, project_url, old_path) in enumerate(download_jobs):
                filename = _safe_filename(
                    version.filename,
                    f"{version.project_slug or 'mod'}-{version.version_number or version.version_id}",
                )
                staging_path = self.temp_dir / f"{index:04d}_{filename}"
                record = DownloadRecord(
                    kind=kind,
                    name=name,
                    version=version.display_version,
                    filename=filename,
                    url=version.download_url,
                    project_url=project_url or version.manual_url,
                    old_path=old_path,
                )
                self.result.records.append(record)
                future = executor.submit(download_file, version.download_url, staging_path, version.hashes)
                future_map[future] = (record, staging_path)
            completed = 0
            for future in as_completed(future_map):
                completed += 1
                record, staging_path = future_map[future]
                try:
                    future.result()
                    record.status = "downloaded"
                    downloaded_files.append((staging_path, record))
                    self._emit(f"已下载 {record.name}", completed, len(download_jobs))
                except Exception as exc:
                    record.status = "failed"
                    record.error = str(exc)
                    self._emit(f"下载失败：{record.name}（{exc}）", completed, len(download_jobs))

        # 只有成功下载了替换文件，才移除对应的旧 jar；失败或需要手动下载时保留旧版本。
        successful_old_paths = {
            record.old_path
            for record in self.result.records
            if record.kind == "mod" and record.status == "downloaded" and record.old_path
        }
        if self.settings.full_instance:
            self._copy_old_mods(successful_old_paths)
        for staging_path, record in downloaded_files:
            destination = self._unique_destination(self.mods_dir / record.filename)
            staging_path.replace(destination)
            record.filename = destination.name
            self.result.mod_downloaded_paths.append(str(destination))

        if self.settings.copy_configs:
            self._emit("复制功能性模组配置…")
            copied, errors = copy_configs(self.plan.source_root, self.build_dir, self.plan.selected_configs)
            self.result.configs_copied = copied
            self.result.config_errors = errors
            self.result.errors.extend(errors)

        self.plan.output_dir = self.output_dir
        self.plan.report_dir = self.output_dir
        self._write_readme()
        from .reports import write_reports

        written_reports = write_reports(self.plan, self.build_dir, self.result)
        self.result.report_paths = written_reports
        write_json(self.build_dir / "manifest.json", self._manifest())

        self._emit("正在压缩 mods、配置和清单…")
        self._create_zip()
        self._write_checksum()
        preview = self._copy_report_preview()

        final_report_paths: dict[str, Path] = {"zip": self.zip_path}
        if preview:
            final_report_paths["markdown"] = preview
        self.result.report_paths = final_report_paths
        self.plan.report_dir = preview.parent if preview else self.output_dir

        shutil.rmtree(self.build_dir, ignore_errors=True)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self._emit(f"移植压缩包已生成：{self.zip_path}")
        return self.result

    def _copy_old_mods(self, successful_old_paths: set[str]) -> None:
        if not self.plan.mods_dir.is_dir():
            return
        copied = 0
        for source in self.plan.mods_dir.rglob("*"):
            if not source.is_file():
                continue
            if str(source.resolve()) in successful_old_paths:
                continue
            relative = source.relative_to(self.plan.mods_dir)
            destination = self.mods_dir / relative
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied += 1
            except OSError as exc:
                self.result.errors.append(f"复制旧 mod 失败：{relative}（{exc}）")
        self.result.copied_old = copied

    @staticmethod
    def _unique_destination(path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        for index in range(2, 1000):
            candidate = path.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
        raise OSError(f"无法找到可用文件名：{path}")

    def _create_zip(self) -> None:
        with zipfile.ZipFile(
            self.zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
            compresslevel=6,
        ) as archive:
            # 确保即使 config 为空，压缩包里也有对应目录。
            for directory_name in ("mods", "config", "reports"):
                info = zipfile.ZipInfo(f"{directory_name}/")
                info.external_attr = (0o40775 << 16) | 0x10
                archive.writestr(info, b"")
            for path in sorted(self.build_dir.rglob("*")):
                if not path.is_file():
                    continue
                arcname = path.relative_to(self.build_dir).as_posix()
                if path.suffix.lower() in (".jar", ".zip"):
                    compression = zipfile.ZIP_STORED
                else:
                    compression = zipfile.ZIP_DEFLATED
                archive.write(path, arcname=arcname, compress_type=compression)

    def _write_checksum(self) -> None:
        digest = _hash_file(self.zip_path, "sha256")
        (self.output_dir / "SHA256.txt").write_text(
            f"{digest}  {self.zip_path.name}\n",
            encoding="utf-8",
        )

    def _copy_report_preview(self) -> Path | None:
        source = self.reports_dir / "更新报告.md"
        if not source.is_file():
            return None
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        destination = PREVIEW_DIR / f"{self.package_name}_更新报告.md"
        try:
            shutil.copy2(source, destination)
            return destination
        except OSError:
            return None

    def _write_readme(self) -> None:
        loader = self.plan.loader_label
        full_note = (
            "mods 文件夹是完整集合：保留未更新/未找到的旧模组，替换成功更新的模组，并加入新增前置。"
            if self.settings.full_instance
            else "mods 文件夹只包含本次成功下载的更新和新增前置，请自行与旧 mods 合并。"
        )
        config_note = (
            f"config 已迁移 {self.result.configs_copied} 个配置；未勾选或未识别归属的配置没有复制。"
            if self.settings.copy_configs
            else "本次未启用配置迁移。"
        )
        text = f"""MC 模组更新助手 - 移植包说明
=================================

压缩包：{self.zip_path.name}
目标：Minecraft {self.plan.game_version} / {loader}
源版本：{self.plan.detected_game_version or '未识别'}
生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
源实例：{self.plan.source_root}

压缩包内容
----------
mods/            更新后的模组文件
config/          迁移的模组配置（可能为空）
reports/         完整清单：更新报告、CSV、计划 JSON、下载清单
README_如何安装.txt  本说明
manifest.json    本次操作的机器可读记录

使用方法
--------
1. 解压整个压缩包。
2. 在启动器中安装 Minecraft {self.plan.game_version} 和 {loader} 加载器。
3. 完全退出游戏，并备份原来的 mods 与 config 文件夹。
4. {full_note}
5. 把解压后的 mods 和 config 合并到新实例的对应目录。
6. 先看 reports/更新报告.md：未找到更新、需要手动下载、可能可移除的前置、未解析前置都在里面。
7. 首次启动若某个模组报配置错误，删除该模组在 config 下的配置文件让它重新生成。

注意
----
- 本工具不会修改源实例；源实例只读。
- 跨加载器迁移不保证兼容，建议 Fabric / Forge / NeoForge 各自保持同一加载器。
- 配置迁移是原样复制。模组跨大版本时配置格式可能变化，出问题请删除对应配置。
- 更新前请务必备份世界存档。
- {config_note}
"""
        (self.build_dir / "README_如何安装.txt").write_text(text, encoding="utf-8-sig")

    def _manifest(self) -> dict:
        return {
            "app": "MC 模组更新助手",
            "version": __version__,
            "created_at": datetime.now().isoformat(),
            "package": self.package_name,
            "zip": self.zip_path.name,
            "source_root": str(self.plan.source_root),
            "target": {"loader": self.plan.loader, "game_version": self.plan.game_version},
            "full_instance": self.settings.full_instance,
            "copy_configs": self.settings.copy_configs,
            "summary": self.plan.summary(),
            "result": {
                "downloaded": [record.to_dict() for record in self.result.downloaded],
                "failed": [record.to_dict() for record in self.result.failed],
                "manual": [record.to_dict() for record in self.result.manual],
                "copied_old": self.result.copied_old,
                "configs_copied": self.result.configs_copied,
                "config_errors": self.result.config_errors,
                "errors": self.result.errors,
                "mod_files": [Path(path).name for path in self.result.mod_downloaded_paths],
                "reports": [
                    "reports/更新报告.md",
                    "reports/mods.csv",
                    "reports/dependencies.csv",
                    "reports/configs.csv",
                    "reports/plan.json",
                    "reports/下载清单.txt",
                ],
            },
            "warnings": self.plan.warnings,
        }
