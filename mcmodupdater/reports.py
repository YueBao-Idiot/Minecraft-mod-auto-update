from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .models import ConfigItem, DependencyItem, ModPlan, UpdatePlan
from .paths import APP_ROOT, OUTPUT_DIR, ensure_within
from .util import write_json

if TYPE_CHECKING:
    from .downloader import ApplyResult, DownloadRecord


def _cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _table(headers: list[str], rows: Iterable[Iterable[object]]) -> str:
    rows = list(rows)
    if not rows:
        return "（无）\n"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines) + "\n"


def _parent_text(item: DependencyItem) -> str:
    if not item.parents:
        return "—"
    if len(item.parents) <= 4:
        return "、".join(item.parents)
    return "、".join(item.parents[:4]) + f" 等 {len(item.parents)} 个"


def _version_text(version) -> str:
    if not version:
        return "—"
    return version.display_version


def _mod_project_link(plan: ModPlan) -> str:
    project = plan.project
    if not project or not project.url:
        return "—"
    return f"[{project.title or project.slug}]({project.url})"


def _write_csv(path: Path, headers: list[str], rows: Iterable[Iterable[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])


def _mod_rows(mods: Iterable[ModPlan], include_status: bool = True):
    for mod in mods:
        project = mod.project
        yield [
            mod.status_label if include_status else "",
            mod.mod.display_name,
            mod.current_version_text,
            mod.target_version_text,
            mod.provider_label,
            project.title if project else "—",
            project.url if project else "",
            mod.mod.filename,
            "；".join(mod.notes),
        ]


def _write_download_list(plan: UpdatePlan, path: Path, result: "ApplyResult | None") -> None:
    lines = [
        "MC 模组更新助手 - 手动下载清单",
        "=" * 40,
        f"目标：Minecraft {plan.game_version} / {plan.loader_label}",
        "",
        "以下文件未自动下载，请按链接手动取得，或检查下载失败原因。",
        "",
    ]
    manual: list[tuple[str, str, str, str, str]] = []
    if result:
        for record in result.records:
            if record.status in ("manual", "failed"):
                manual.append(
                    (
                        "模组" if record.kind == "mod" else "前置",
                        record.name,
                        record.version,
                        record.project_url or record.url,
                        record.error or "需要手动下载",
                    )
                )
    if not manual:
        for mod in plan.selected_updates:
            version = mod.target_version
            if version and not version.download_url:
                manual.append(("模组", mod.mod.display_name, version.display_version, version.manual_url, "无 API 直链"))
        for dep in plan.missing_dependencies:
            version = dep.resolved_version
            if version and not version.download_url:
                manual.append(("前置", dep.display_name, version.display_version, version.manual_url, "无 API 直链"))
    if not manual:
        lines.append("本次没有需要手动下载的文件。")
    else:
        for index, (kind, name, version, url, reason) in enumerate(manual, start=1):
            lines.extend(
                [
                    f"{index}. [{kind}] {name}  {version}",
                    f"   链接：{url or '（更新源未提供）'}",
                    f"   原因：{reason}",
                    "",
                ]
            )
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def write_reports(plan: UpdatePlan, output_dir: Path, result: "ApplyResult | None" = None) -> dict[str, Path]:
    output_dir = ensure_within(APP_ROOT, output_dir)
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    summary = plan.summary()

    mods_csv = reports_dir / "mods.csv"
    deps_csv = reports_dir / "dependencies.csv"
    configs_csv = reports_dir / "configs.csv"
    plan_json = reports_dir / "plan.json"
    downloads_txt = reports_dir / "下载清单.txt"
    report_md = reports_dir / "更新报告.md"

    _write_csv(
        mods_csv,
        ["状态", "模组", "当前版本", "目标版本", "更新源", "项目名", "项目链接", "当前文件名", "备注"],
        _mod_rows(plan.mods),
    )
    _write_csv(
        deps_csv,
        ["状态", "前置", "关系", "目标版本", "需求方", "更新源", "项目ID", "是否自动下载", "备注"],
        (
            [
                dep.state_label,
                dep.display_name,
                dep.relation_label,
                dep.version_text,
                _parent_text(dep),
                dep.provider,
                dep.project_id,
                "是" if dep.download else "否",
                "；".join(dep.notes),
            ]
            for dep in plan.dependencies
        ),
    )
    _write_csv(
        configs_csv,
        ["配置路径", "所属模组", "大小", "迁移", "备注"],
        (
            [item.relative_path, item.owner_text, item.size_text, "是" if item.selected else "否", item.note]
            for item in plan.configs
        ),
    )
    write_json(plan_json, plan.to_dict())
    _write_download_list(plan, downloads_txt, result)

    missing_mods = [
        mod for mod in plan.mods if mod.status in ("not_found", "no_compatible", "error", "unknown", "manual")
    ]
    dep_rows = [
        [
            dep.state_label,
            dep.display_name,
            dep.relation_label,
            dep.version_text,
            _parent_text(dep),
            dep.provider,
            "；".join(dep.notes),
        ]
        for dep in plan.dependencies
        if dep.state != "satisfied"
    ]
    config_rows = [
        [item.relative_path, item.owner_text, item.size_text, "是" if item.selected else "否", item.note]
        for item in plan.configs
    ]

    lines: list[str] = [
        "# MC 模组更新报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 源实例：`{plan.source_root}`",
        f"- 目标：**Minecraft {plan.game_version} / {plan.loader_label}**",
        f"- 更新源：{'、'.join(plan.providers) if plan.providers else '无'}",
        f"- 本地模组数：{summary['total_mods']}",
        f"- 可更新：{summary['updates']}　已是最新：{summary['latest']}　未获得更新：{len(missing_mods)}",
        f"- 新增前置：{summary['missing_dependencies']}　可能可移除：{summary['removable_dependencies']}　未解析前置：{summary['unresolved_dependencies']}",
        f"- 待迁移配置：{summary['configs']}",
        "",
    ]
    if plan.warnings:
        lines.extend(["## 重要提醒", ""])
        lines.extend([f"- {warning}" for warning in plan.warnings])
        lines.append("")

    lines.extend(
        [
            "## 一、可更新（新版本）",
            "",
            _table(
                ["状态", "模组", "当前版本", "目标版本", "更新源", "项目", "备注"],
                (
                    [
                        mod.status_label,
                        mod.mod.display_name,
                        mod.current_version_text,
                        mod.target_version_text,
                        mod.provider_label,
                        _mod_project_link(mod),
                        "；".join(mod.notes),
                    ]
                    for mod in plan.updates
                ),
            ),
            "## 二、已是最新",
            "",
            _table(
                ["状态", "模组", "当前版本", "目标版本", "更新源", "项目", "备注"],
                (
                    [
                        mod.status_label,
                        mod.mod.display_name,
                        mod.current_version_text,
                        mod.target_version_text,
                        mod.provider_label,
                        _mod_project_link(mod),
                        "；".join(mod.notes),
                    ]
                    for mod in plan.latest
                ),
            ),
            "## 三、新版本没有更新的模组 / 需要手动处理",
            "",
            "这一节同时列出未找到、没有兼容版本、查询失败和无法识别的模组。",
            "",
            _table(
                ["状态", "模组", "当前版本", "目标版本", "更新源", "项目", "备注"],
                (
                    [
                        mod.status_label,
                        mod.mod.display_name,
                        mod.current_version_text,
                        mod.target_version_text,
                        mod.provider_label,
                        _mod_project_link(mod),
                        "；".join(mod.notes) or mod.error,
                    ]
                    for mod in plan.mods
                    if mod.status in ("not_found", "no_compatible", "error", "unknown", "manual")
                ),
            ),
            "## 四、前置变化",
            "",
            f"依赖图共解析 {len(plan.dependencies)} 项；下面只显示需要处理的项，完整列表见 `dependencies.csv`。",
            "",
            "### 新增前置（自动加入下载）",
            "",
            _table(
                ["前置", "目标版本", "需求方", "更新源", "自动下载", "备注"],
                (
                    [dep.display_name, dep.version_text, _parent_text(dep), dep.provider, "是" if dep.download else "否", "；".join(dep.notes)]
                    for dep in plan.dependencies
                    if dep.state == "missing"
                ),
            ),
            "### 未解析 / 找不到兼容版本",
            "",
            _table(
                ["前置", "关系", "需求方", "更新源", "备注"],
                (
                    [dep.display_name, dep.relation_label, _parent_text(dep), dep.provider, "；".join(dep.notes)]
                    for dep in plan.dependencies
                    if dep.state == "unresolved"
                ),
            ),
            "### 可能不再需要、可从 mods 移除",
            "",
            _table(
                ["前置", "旧版本需求方", "更新源", "备注"],
                (
                    [dep.display_name, _parent_text(dep), dep.provider, "；".join(dep.notes)]
                    for dep in plan.dependencies
                    if dep.state == "removable"
                ),
            ),
            "### 冲突 / 不兼容",
            "",
            _table(
                ["模组", "关系", "相关模组", "备注"],
                (
                    [dep.display_name, dep.relation_label, _parent_text(dep), "；".join(dep.notes)]
                    for dep in plan.dependencies
                    if dep.state == "incompatible"
                ),
            ),
            "### 可选前置",
            "",
            _table(
                ["前置", "目标版本", "需求方", "更新源", "备注"],
                (
                    [dep.display_name, dep.version_text, _parent_text(dep), dep.provider, "；".join(dep.notes)]
                    for dep in plan.dependencies
                    if dep.state == "optional"
                ),
            ),
        ]
    )

    lines.extend(["## 五、配置迁移", ""])
    if not plan.configs:
        lines.append("没有找到 config / defaultconfigs，或未启用配置迁移。\n")
    else:
        lines.append(f"默认/勾选迁移 {len(plan.selected_configs)} / {len(plan.configs)} 个文件。只迁移能归属到已安装模组的配置。\n")
        lines.append(
            _table(
                ["配置路径", "所属模组", "大小", "迁移", "备注"],
                config_rows,
            )
        )

    lines.extend(["## 六、本次下载结果", ""])
    if result:
        lines.append(f"- 压缩包：`{result.zip_path.name}`")
        lines.append(f"- 输出目录：`{result.output_dir}`")
        lines.append(f"- 成功下载：{len(result.downloaded)}　失败：{len(result.failed)}　需手动：{len(result.manual)}")
        lines.append(f"- 复制旧 mod：{result.copied_old}　迁移配置：{result.configs_copied}")
        lines.append("")
        lines.append(
            _table(
                ["类型", "名称", "版本", "状态", "文件", "说明"],
                (
                    [
                        "模组" if record.kind == "mod" else "前置",
                        record.name,
                        record.version,
                        record.status_label,
                        record.filename,
                        record.error,
                    ]
                    for record in result.records
                ),
            )
        )
    else:
        lines.append("本次只生成计划，没有执行下载。\n")

    lines.extend(
        [
            "## 七、下一步",
            "",
            "1. 打开同目录的 `README_如何安装.txt`，按说明把 `mods/` 与 `config/` 合并到新实例。",
            "2. 按 `下载清单.txt` 处理手动下载项。",
            "3. 重点确认“可能不再需要”的前置和“未解析”的前置。",
            "4. 启动前备份世界；若配置报错，删除对应模组的配置文件让其重新生成。",
            "",
            "---",
            "",
            f"报告由 MC 模组更新助手生成 · {plan.created_display}",
            "",
        ]
    )
    report_md.write_text("\n".join(lines), encoding="utf-8-sig")

    return {
        "markdown": report_md,
        "mods_csv": mods_csv,
        "dependencies_csv": deps_csv,
        "configs_csv": configs_csv,
        "plan_json": plan_json,
        "downloads_txt": downloads_txt,
    }


def write_plan_bundle(plan: UpdatePlan) -> Path:
    """只导出计划清单（不下载）。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    root = OUTPUT_DIR / "plans" / f"plan_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    counter = 2
    while root.exists():
        root = OUTPUT_DIR / "plans" / f"plan_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{counter}"
        counter += 1
    root.mkdir(parents=True, exist_ok=False)
    root = ensure_within(APP_ROOT, root)
    write_reports(plan, root, None)
    plan.report_dir = root / "reports"
    return root
