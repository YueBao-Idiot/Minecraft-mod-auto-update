from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .downloader import Applier
from .instance import detect_instance, ensure_instance_skeleton
from .paths import APP_ROOT, DEFAULT_INSTANCE_DIR, ensure_app_dirs, ensure_within
from .planner import PlannerError, UpdatePlanner
from .reports import write_plan_bundle
from .settings import AppSettings, VALID_LOADERS, VALID_PROVIDERS, load_settings


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _progress_factory(quiet: bool, json_mode: bool):
    def progress(message: str, current: int = 0, total: int = 0) -> None:
        if quiet:
            return
        stream = sys.stderr if json_mode else sys.stdout
        if total and 0 < current < total and current % 25 != 0:
            return
        if total:
            print(f"[{current}/{total}] {message}", file=stream, flush=True)
        else:
            print(message, file=stream, flush=True)

    return progress


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", help="源版本/实例文件夹或 mods 文件夹（只读）")
    parser.add_argument("--loader", choices=VALID_LOADERS, help="目标加载器")
    parser.add_argument("--game-version", help="目标游戏版本，例如 1.20.1")
    parser.add_argument(
        "--provider",
        action="append",
        choices=VALID_PROVIDERS,
        dest="providers",
        help="更新源，可重复：modrinth / curseforge",
    )
    parser.add_argument("--prefer", choices=("auto", *VALID_PROVIDERS), help="优先更新源")
    parser.add_argument("--allow-prerelease", action="store_true", default=None, help="允许 beta/alpha")
    parser.add_argument("--no-configs", action="store_true", help="不迁移配置")
    parser.add_argument("--config-scope", choices=("matched", "all", "none"), help="配置迁移范围")
    parser.add_argument("--patch-only", action="store_true", help="只输出新下载文件，不复制旧 mod")
    parser.add_argument("--curseforge-key", help="CurseForge API Key（也可用环境变量 CURSEFORGE_API_KEY）")
    parser.add_argument(
        "--curseforge-proxy",
        action="store_true",
        default=None,
        help="没有 Key 时使用第三方公共只读代理 api.curse.tools",
    )
    parser.add_argument("--max-workers", type=int, help="并发查询数（1-8）")
    parser.add_argument("--quiet", action="store_true", help="不输出进度")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")


def _apply_overrides(settings: AppSettings, args: argparse.Namespace) -> AppSettings:
    if args.instance:
        settings.instance = str(Path(args.instance).expanduser())
    if args.loader:
        settings.loader = args.loader
    if args.game_version:
        settings.game_version = args.game_version
    if args.providers:
        settings.providers = list(dict.fromkeys(args.providers))
    if args.prefer:
        settings.prefer_provider = args.prefer
    if args.allow_prerelease is not None:
        settings.allow_prerelease = bool(args.allow_prerelease)
    if args.no_configs:
        settings.copy_configs = False
    if args.config_scope:
        settings.config_scope = args.config_scope
    if args.patch_only:
        settings.full_instance = False
    if args.curseforge_key:
        settings.curseforge_api_key = args.curseforge_key.strip()
    if args.curseforge_proxy is not None:
        settings.curseforge_use_proxy = bool(args.curseforge_proxy)
    if args.max_workers:
        settings.max_workers = args.max_workers
    return settings.normalized()


def _print_summary(plan) -> None:
    summary = plan.summary()
    print()
    print(f"目标：Minecraft {plan.game_version} / {plan.loader_label}")
    print(f"源实例：{plan.source_root}")
    print(
        f"模组 {summary['total_mods']} 个：可更新 {summary['updates']}，已是最新 {summary['latest']}，"
        f"未获得更新 {summary['not_found'] + summary['no_compatible'] + summary['errors']}"
    )
    print(
        f"前置：新增 {summary['missing_dependencies']}，可选 {summary['optional_dependencies']}，"
        f"可能可移除 {summary['removable_dependencies']}，未解析 {summary['unresolved_dependencies']}"
    )
    print(f"配置：待迁移 {summary['configs']}，预计下载 {summary['downloads']}")
    if plan.warnings:
        print("\n提醒：")
        for warning in plan.warnings:
            print(f"  - {warning}")
    if plan.updates:
        print("\n可更新：")
        for mod in plan.updates:
            print(f"  [{mod.provider_label}] {mod.mod.display_name}: {mod.current_version_text} -> {mod.target_version_text}")
    no_update = [mod for mod in plan.mods if mod.status != "update"]
    if no_update:
        print("\n没有获得更新：")
        for mod in no_update:
            note = "；".join(mod.notes)
            print(f"  {mod.status_label}: {mod.mod.display_name} ({mod.current_version_text}) {note}")
    missing = plan.missing_dependencies
    if missing:
        print("\n新增前置：")
        for dep in missing:
            print(f"  {dep.display_name} {dep.version_text} <- {'、'.join(dep.parents[:3])}")
    unresolved = plan.unresolved_dependencies
    if unresolved:
        print("\n未解析前置：")
        for dep in unresolved:
            print(f"  {dep.display_name}: {'；'.join(dep.notes)}")
    removable = plan.removable_dependencies
    if removable:
        print("\n可能可移除：")
        for dep in removable:
            print(f"  {dep.display_name}（旧版本被 {'、'.join(dep.parents[:3])} 需要）")


def cmd_plan(args: argparse.Namespace) -> int:
    settings = _apply_overrides(load_settings(), args)
    planner = UpdatePlanner(settings, _progress_factory(args.quiet, args.json))
    try:
        plan = planner.build(
            Path(args.instance) if args.instance else None,
            loader=args.loader,
            game_version=args.game_version,
            providers=args.providers,
        )
    except PlannerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_summary(plan)
    if args.export:
        root = write_plan_bundle(plan)
        if not args.json:
            print(f"\n清单已导出：{root}")
        else:
            print(json.dumps({"export_dir": str(root)}, ensure_ascii=False), file=sys.stderr)
    if not args.json:
        print("\n提示：使用 `python -m mcmodupdater apply` 生成 zip 移植包。")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    settings = _apply_overrides(load_settings(), args)
    planner = UpdatePlanner(settings, _progress_factory(args.quiet, args.json))
    try:
        plan = planner.build(
            Path(args.instance) if args.instance else None,
            loader=args.loader,
            game_version=args.game_version,
            providers=args.providers,
        )
    except PlannerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if not args.json:
        _print_summary(plan)
    if not args.yes:
        if not sys.stdin.isatty():
            print("非交互环境执行 apply 需要显式加 --yes。", file=sys.stderr)
            return 2
        print(f"\n将下载 {plan.download_count} 个文件，并把结果写到 output/ 下。继续？[y/N] ", end="", flush=True)
        answer = input().strip().lower()
        if answer not in ("y", "yes"):
            print("已取消。")
            return 1
    applier = Applier(plan, settings, _progress_factory(args.quiet, args.json))
    result = applier.apply()
    settings.last_output = str(result.output_dir)
    settings.save()
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("\n移植压缩包已生成：")
        print(f"  {result.zip_path}")
        print(f"  输出文件夹：{result.output_dir}")
        print(f"  已下载 {len(result.downloaded)}，失败 {len(result.failed)}，需手动 {len(result.manual)}")
        print(f"  报告预览：{result.report_paths.get('markdown', '')}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else DEFAULT_INSTANCE_DIR
    if not path.is_absolute():
        path = APP_ROOT / path
    try:
        ensure_within(APP_ROOT, path)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    ensure_instance_skeleton(path)
    readme = path / "把mods放这里.txt"
    readme.write_text(
        "把整合包或客户端实例里的 mods 文件夹内容复制到本目录的 mods 中，\n"
        "把 config 文件夹内容复制到本目录的 config 中。\n"
        "然后启动 MC 模组更新助手，源实例选择这个文件夹。\n"
        "程序只读取这里，不会修改源文件。\n",
        encoding="utf-8-sig",
    )
    detected = detect_instance(path)
    print(f"已创建：{path}")
    print(f"检测加载器：{detected.loader_label}　游戏版本：{detected.game_version or '未识别'}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    ensure_app_dirs()
    print(f"{APP_NAME} {__version__}")
    print(f"程序目录：{APP_ROOT}")
    print(f"默认实例：{DEFAULT_INSTANCE_DIR}")
    print("运行 `python -m mcmodupdater gui` 打开界面。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcmodupdater", description=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")
    plan = sub.add_parser("plan", aliases=["scan"], help="只扫描并生成更新计划/清单")
    _add_common(plan)
    plan.add_argument("--export", action="store_true", help="把清单导出到 output/plans/")
    plan.set_defaults(func=cmd_plan)
    apply_parser = sub.add_parser("apply", help="生成计划并输出含 mods/配置/清单的 zip 移植包")
    _add_common(apply_parser)
    apply_parser.add_argument("-y", "--yes", action="store_true", help="不询问直接执行")
    apply_parser.set_defaults(func=cmd_apply)
    init_parser = sub.add_parser("init", help="创建便携实例目录")
    init_parser.add_argument("--path", help="实例目录（必须位于程序目录内）")
    init_parser.set_defaults(func=cmd_init)
    info_parser = sub.add_parser("info", help="显示程序信息")
    info_parser.set_defaults(func=cmd_info)
    gui_parser = sub.add_parser("gui", help="打开图形界面")
    gui_parser.set_defaults(func=lambda args: __import__("mcmodupdater.gui", fromlist=["main"]).main())
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    ensure_app_dirs()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["gui"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
