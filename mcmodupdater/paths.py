from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """返回程序根目录。

    源码运行时是本仓库根目录；PyInstaller 打包后是 exe 所在目录。
    这样设置、缓存、输出和实例都可以保持便携。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_path(relative: str) -> Path:
    """打包后资源位于 PyInstaller 临时解包目录。"""
    base = Path(getattr(sys, "_MEIPASS", APP_ROOT))
    return base / relative


APP_ROOT = app_root()
DATA_DIR = APP_ROOT / "data"
CACHE_DIR = APP_ROOT / ".cache"
OUTPUT_DIR = APP_ROOT / "output"
DEFAULT_INSTANCE_DIR = APP_ROOT / "instance"
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_DIR = APP_ROOT / "logs"


def ensure_app_dirs() -> None:
    for directory in (DATA_DIR, CACHE_DIR, OUTPUT_DIR, DEFAULT_INSTANCE_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def ensure_within(root: Path, target: Path) -> Path:
    """拒绝把写入操作落到程序目录之外。"""
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"出于安全考虑，只允许写入程序目录内：{root_resolved}") from exc
    return target_resolved


def portable_path(path: Path) -> str:
    """设置文件中保存相对程序目录的路径，便于整个文件夹搬走。"""
    try:
        return str(path.resolve().relative_to(APP_ROOT.resolve())).replace(os.sep, "/")
    except ValueError:
        return str(path.resolve())


def resolve_portable_path(value: str, fallback: Path) -> Path:
    if not value:
        return fallback
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = APP_ROOT / path
    return path
