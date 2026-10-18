from __future__ import annotations

import os
import queue
import re
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Text, Tk, filedialog, messagebox, ttk
import tkinter as tk

from . import APP_NAME, __version__
from .downloader import Applier, ApplyResult
from .instance import detect_instance, ensure_instance_skeleton, resolve_instance_root
from .paths import APP_ROOT, CACHE_DIR, DEFAULT_INSTANCE_DIR, OUTPUT_DIR, ensure_app_dirs, portable_path, resource_path
from .planner import PlannerError, UpdatePlanner
from .providers.http import HttpClient
from .reports import write_plan_bundle
from .settings import AppSettings, load_settings
from .util import is_mc_version, mc_version_key

STATIC_VERSIONS = [
    "26.3",
    "26.2",
    "26.1.2",
    "26.1.1",
    "26.1",
    "1.21.11",
    "1.21.10",
    "1.21.9",
    "1.21.8",
    "1.21.7",
    "1.21.6",
    "1.21.5",
    "1.21.4",
    "1.21.3",
    "1.21.1",
    "1.21",
    "1.20.6",
    "1.20.5",
    "1.20.4",
    "1.20.3",
    "1.20.2",
    "1.20.1",
    "1.20",
    "1.19.4",
    "1.19.3",
    "1.19.2",
    "1.19.1",
    "1.19",
    "1.18.2",
    "1.18.1",
    "1.18",
    "1.17.1",
    "1.16.5",
    "1.16.4",
    "1.16.3",
    "1.16.2",
    "1.16.1",
    "1.15.2",
    "1.14.4",
    "1.12.2",
    "1.10.2",
    "1.8.9",
    "1.7.10",
]

CONFIG_SCOPE_LABELS = {
    "仅迁移已识别归属的配置": "matched",
    "迁移全部 config 文件": "all",
}
CONFIG_SCOPE_REVERSE = {value: key for key, value in CONFIG_SCOPE_LABELS.items()}
PREFER_LABELS = {"自动（优先哈希命中）": "auto", "优先 Modrinth": "modrinth", "优先 CurseForge": "curseforge"}
LOADER_DISPLAY = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}
PREFER_REVERSE = {value: key for key, value in PREFER_LABELS.items()}


def _set_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _open_path(path: Path) -> None:
    path = path.resolve()
    if not path.exists():
        messagebox.showwarning(APP_NAME, f"路径不存在：\n{path}")
        return
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.as_uri())
    except Exception as exc:
        messagebox.showerror(APP_NAME, f"无法打开：{exc}")


class App:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.settings = load_settings()
        self.plan = None
        self.busy = False
        self.queue: queue.Queue = queue.Queue()
        self.apply_result: ApplyResult | None = None

        self.source_var = StringVar(value=str(self.settings.instance_path))
        self.loader_var = StringVar(
            value={"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge"}.get(
                self.settings.loader, "Fabric"
            )
        )
        self.version_var = StringVar(value=self.settings.game_version)
        self.prefer_var = StringVar(value=PREFER_REVERSE.get(self.settings.prefer_provider, "自动（优先哈希命中）"))
        self.modrinth_var = BooleanVar(value="modrinth" in self.settings.providers)
        self.curseforge_var = BooleanVar(value="curseforge" in self.settings.providers)
        self.key_var = StringVar(value=self.settings.curseforge_api_key)
        self.cf_proxy_var = BooleanVar(value=self.settings.curseforge_use_proxy)
        self.show_key_var = BooleanVar(value=False)
        self.prerelease_var = BooleanVar(value=self.settings.allow_prerelease)
        self.configs_var = BooleanVar(value=self.settings.copy_configs)
        self.scope_var = StringVar(value=CONFIG_SCOPE_REVERSE.get(self.settings.config_scope, "仅迁移已识别归属的配置"))
        self.full_var = BooleanVar(value=self.settings.full_instance)
        self.loader_user_selected = False
        self.status_var = StringVar(value="就绪")
        self.detect_var = StringVar(value="尚未检测")
        self.summary_var = StringVar(value="尚未生成计划")
        self.filter_var = StringVar()
        self.dep_summary_var = StringVar(value="前置依赖尚未解析")
        self.config_summary_var = StringVar(value="配置尚未扫描")
        self.last_output = StringVar(value=self.settings.last_output or str(OUTPUT_DIR))

        self._configure_window()
        self._configure_style()
        self._build_ui()
        self._fit_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._drain_queue)
        self.root.after(300, self._detect_source_async)

    def _fit_window(self) -> None:
        """按控件实际请求尺寸和屏幕大小设置初始窗口，避免高 DPI 下右侧被裁掉。"""
        self.root.update_idletasks()
        req_w = self.root.winfo_reqwidth()
        req_h = self.root.winfo_reqheight()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(max(1100, req_w + 64), screen_w - 80)
        height = min(max(760, req_h + 40), screen_h - 100)
        width = max(900, width)
        height = max(650, height)
        x = max(10, (screen_w - width) // 2)
        y = max(10, (screen_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(980, width), min(680, height))

    # ---------- 外观 ----------

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME} {__version__}")
        self.root.geometry("1280x900")
        self.root.minsize(1000, 720)
        try:
            self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        except Exception:
            pass
        try:
            icon = resource_path("assets/app.ico")
            if icon.exists():
                self.root.iconbitmap(default=str(icon))
        except Exception:
            pass

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 9))
        style.configure("TFrame", background="#f5f7fa")
        style.configure("Card.TLabelframe", background="#ffffff", bordercolor="#d6dee8", relief="solid")
        style.configure(
            "Card.TLabelframe.Label",
            background="#ffffff",
            foreground="#17324d",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure("TLabel", background="#f5f7fa", foreground="#243b53")
        style.configure("Card.TLabel", background="#ffffff", foreground="#243b53")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#6b7c93")
        style.configure("Status.TLabel", background="#e9eef5", foreground="#44546a")
        style.configure("Accent.TButton", background="#2f80ed", foreground="#ffffff", padding=(12, 6))
        style.map("Accent.TButton", background=[("active", "#1f6fd8"), ("disabled", "#a9c4e8")])
        style.configure("Primary.TButton", background="#1f9d55", foreground="#ffffff", padding=(12, 6))
        style.map("Primary.TButton", background=[("active", "#178747"), ("disabled", "#a9d8bd")])
        style.configure("TButton", padding=(8, 4))
        style.configure(
            "Treeview",
            rowheight=26,
            font=("Microsoft YaHei UI", 9),
            background="#ffffff",
            fieldbackground="#ffffff",
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            font=("Microsoft YaHei UI", 9, "bold"),
            background="#eaf0f7",
            foreground="#17324d",
            padding=(6, 5),
        )
        style.map("Treeview", background=[("selected", "#d8e8fb")], foreground=[("selected", "#17324d")])
        style.configure("TNotebook", background="#f5f7fa", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 8), font=("Microsoft YaHei UI", 9))
        style.map("TNotebook.Tab", background=[("selected", "#ffffff")], foreground=[("selected", "#17324d")])

    def _build_ui(self) -> None:
        self.root.configure(background="#f5f7fa")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        header = tk.Frame(self.root, background="#17324d", height=76)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text=APP_NAME,
            background="#17324d",
            foreground="#ffffff",
            font=("Microsoft YaHei UI", 18, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(13, 0))
        tk.Label(
            header,
            text="扫描 mods · 查询 Modrinth / CurseForge · 解析前置 · 迁移配置 · 只读源实例",
            background="#17324d",
            foreground="#b9cfe6",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=23, pady=(0, 10))
        tk.Button(
            header,
            text="使用说明",
            command=lambda: _open_path(APP_ROOT / "README.md"),
            background="#244866",
            foreground="#ffffff",
            activebackground="#315a7d",
            activeforeground="#ffffff",
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
        ).grid(row=0, column=1, rowspan=2, padx=(20, 28))

        self._build_config_card()
        self._build_action_bar()
        self._build_notebook()
        self._build_status_bar()

    def _build_config_card(self) -> None:
        card = ttk.LabelFrame(self.root, text=" 1. 源实例与更新目标 ", style="Card.TLabelframe", padding=10)
        card.grid(row=1, column=0, sticky="ew", padx=16, pady=(8, 4))
        card.columnconfigure(0, weight=1)

        def row(index: int) -> ttk.Frame:
            frame = ttk.Frame(card)
            frame.grid(row=index, column=0, sticky="ew", pady=(5 if index else 0, 0))
            return frame

        source_row = row(0)
        ttk.Label(source_row, text="源版本目录", style="Card.TLabel").pack(side="left", padx=(0, 8))
        ttk.Entry(source_row, textvariable=self.source_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(source_row, text="选择文件夹…", command=self._choose_source).pack(side="left", padx=(0, 6))
        ttk.Button(source_row, text="创建示例目录", command=self._create_sample).pack(side="left", padx=(0, 6))
        ttk.Button(source_row, text="重新检测", command=self._detect_source_async).pack(side="left")

        target_row = row(1)
        ttk.Label(target_row, text="目标加载器", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.loader_combo = ttk.Combobox(
            target_row,
            textvariable=self.loader_var,
            values=("Fabric", "Forge", "NeoForge"),
            state="readonly",
            width=9,
        )
        self.loader_combo.pack(side="left", padx=(0, 14))
        self.loader_combo.bind("<<ComboboxSelected>>", lambda _event: setattr(self, "loader_user_selected", True))
        ttk.Label(target_row, text="游戏版本", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.version_combo = ttk.Combobox(
            target_row,
            textvariable=self.version_var,
            values=STATIC_VERSIONS,
            state="normal",
            width=10,
        )
        self.version_combo.pack(side="left", padx=(0, 6))
        ttk.Button(target_row, text="获取版本", command=self._fetch_versions).pack(side="left", padx=(0, 14))
        ttk.Label(target_row, text="优先", style="Card.TLabel").pack(side="left", padx=(0, 5))
        ttk.Combobox(
            target_row,
            textvariable=self.prefer_var,
            values=tuple(PREFER_LABELS.keys()),
            state="readonly",
            width=16,
        ).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(target_row, text="允许 beta/alpha", variable=self.prerelease_var).pack(side="left")

        provider_row = row(2)
        ttk.Label(provider_row, text="更新源", style="Card.TLabel").pack(side="left", padx=(0, 8))
        ttk.Checkbutton(provider_row, text="Modrinth", variable=self.modrinth_var).pack(
            side="left", padx=(0, 12)
        )
        ttk.Checkbutton(provider_row, text="CurseForge", variable=self.curseforge_var).pack(
            side="left", padx=(0, 12)
        )
        ttk.Label(provider_row, text="API Key", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.key_entry = ttk.Entry(provider_row, textvariable=self.key_var, width=20, show="•")
        self.key_entry.pack(side="left", padx=(0, 5))
        ttk.Checkbutton(provider_row, text="显示", variable=self.show_key_var, command=self._toggle_key).pack(
            side="left"
        )
        ttk.Button(provider_row, text="获取 Key", command=self._open_cf_console).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(provider_row, text="用公共代理", variable=self.cf_proxy_var).pack(
            side="left", padx=(12, 0)
        )

        option_row = row(3)
        ttk.Checkbutton(option_row, text="迁移功能性模组配置", variable=self.configs_var).pack(
            side="left", padx=(0, 6)
        )
        self.scope_combo = ttk.Combobox(
            option_row,
            textvariable=self.scope_var,
            values=tuple(CONFIG_SCOPE_LABELS.keys()),
            state="readonly",
            width=24,
        )
        self.scope_combo.pack(side="left", padx=(0, 18))
        ttk.Checkbutton(option_row, text="完整 mods 集合", variable=self.full_var).pack(
            side="left"
        )

    def _build_action_bar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 8))
        bar.columnconfigure(0, weight=1)

        left = ttk.Frame(bar)
        left.grid(row=0, column=0, sticky="w")
        self.scan_btn = ttk.Button(left, text="扫描并生成计划", style="Accent.TButton", command=self._start_scan)
        self.scan_btn.pack(side="left", padx=(0, 8))
        self.export_btn = ttk.Button(left, text="导出当前清单", command=self._export_plan, state="disabled")
        self.export_btn.pack(side="left", padx=(0, 8))
        self.apply_btn = ttk.Button(
            left, text="生成移植压缩包", style="Primary.TButton", command=self._start_apply, state="disabled"
        )
        self.apply_btn.pack(side="left", padx=(0, 8))
        ttk.Button(left, text="打开输出目录", command=lambda: _open_path(OUTPUT_DIR)).pack(side="left")

        right = ttk.Frame(bar)
        right.grid(row=0, column=1, sticky="e", padx=(16, 12))
        ttk.Label(
            right, textvariable=self.detect_var, style="Muted.TLabel", anchor="e", justify="right", wraplength=340
        ).pack(anchor="e")
        ttk.Label(
            right, textvariable=self.summary_var, style="Muted.TLabel", anchor="e", justify="right", wraplength=340
        ).pack(anchor="e")

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 10))

        self.mods_tab = ttk.Frame(self.notebook)
        self.unupdated_tab = ttk.Frame(self.notebook)
        self.deps_tab = ttk.Frame(self.notebook)
        self.configs_tab = ttk.Frame(self.notebook)
        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.mods_tab, text="模组总览")
        self.notebook.add(self.unupdated_tab, text="未更新清单")
        self.notebook.add(self.deps_tab, text="前置变化")
        self.notebook.add(self.configs_tab, text="配置迁移")
        self.notebook.add(self.log_tab, text="运行日志")

        self._build_mod_tab(self.mods_tab, show_filter=True)
        self._build_mod_tab(self.unupdated_tab, show_filter=False)
        self._build_deps_tab()
        self._build_configs_tab()
        self._build_log_tab()

    def _make_tree(self, parent, columns, widths, *, height=8):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=8, pady=(6, 8))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height)
        for column, width in zip(columns, widths):
            tree.heading(column, text=column, anchor="w")
            tree.column(
                column,
                width=width,
                minwidth=60,
                stretch=column in ("模组", "当前版本", "目标版本", "项目", "备注"),
            )
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree.tag_configure("update", background="#e9f7ef")
        tree.tag_configure("latest", background="#f7f9fb")
        tree.tag_configure("no_compatible", background="#fff4e5")
        tree.tag_configure("not_found", background="#fdecec")
        tree.tag_configure("error", background="#fdecec")
        tree.tag_configure("unknown", background="#f0eef8")
        tree.tag_configure("missing", background="#e9f2fd")
        tree.tag_configure("unresolved", background="#fdecec")
        tree.tag_configure("incompatible", background="#fde2e1")
        tree.tag_configure("removable", background="#fff7df")
        tree.tag_configure("optional", background="#f8f5eb")
        tree.tag_configure("satisfied", background="#ffffff")
        tree.tag_configure("selected", background="#eef8f1")
        tree.tag_configure("unselected", background="#f7f7f7", foreground="#8a97a8")
        return tree

    def _build_mod_tab(self, parent, *, show_filter: bool) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=8, pady=(8, 0))
        if show_filter:
            ttk.Label(top, text="筛选：").pack(side="left")
            entry = ttk.Entry(top, textvariable=self.filter_var, width=40)
            entry.pack(side="left", padx=(0, 10))
            self.filter_var.trace_add("write", lambda *_: self._populate_mods())
            ttk.Label(top, text="点击列标题可排序；状态颜色：绿色可更新，橙色无兼容，红色未找到/失败", style="Muted.TLabel").pack(side="left")
        else:
            ttk.Label(
                top,
                text="这里列出新版本没有获得更新的模组：已是最新、无兼容版本、未找到、查询失败。",
                style="Muted.TLabel",
            ).pack(side="left")
        columns = ("状态", "模组", "当前版本", "目标版本", "更新源", "项目", "备注")
        widths = (95, 250, 230, 230, 100, 220, 470)
        if show_filter:
            self.mods_tree = self._make_tree(parent, columns, widths)
        else:
            self.unupdated_tree = self._make_tree(parent, columns, widths)

    def _build_deps_tab(self) -> None:
        top = ttk.Frame(self.deps_tab)
        top.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(top, textvariable=self.dep_summary_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(
            top,
            text="新增前置会自动下载；可选前置只列出；可能可移除项请人工确认",
            style="Muted.TLabel",
        ).pack(side="right")
        self.deps_tree = self._make_tree(
            self.deps_tab,
            ("状态", "前置", "关系", "目标版本", "需求方", "更新源", "备注"),
            (110, 260, 80, 220, 300, 110, 560),
        )

    def _build_configs_tab(self) -> None:
        top = ttk.Frame(self.configs_tab)
        top.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(top, textvariable=self.config_summary_var, style="Muted.TLabel").pack(side="left")
        ttk.Button(top, text="全选", command=lambda: self._set_all_configs(True)).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="全不选", command=lambda: self._set_all_configs(False)).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="只选已识别归属", command=self._select_owned_configs).pack(side="right", padx=(6, 0))
        ttk.Label(top, text="双击一行切换是否迁移", style="Muted.TLabel").pack(side="right", padx=(0, 12))
        self.configs_tree = self._make_tree(
            self.configs_tab,
            ("迁移", "配置路径", "所属模组", "大小", "备注"),
            (80, 430, 200, 100, 500),
        )
        self.configs_tree.bind("<Double-1>", self._toggle_config)
        self.configs_tree.bind("<space>", self._toggle_config)

    def _build_log_tab(self) -> None:
        frame = ttk.Frame(self.log_tab)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text = Text(
            frame,
            wrap="word",
            background="#101c2b",
            foreground="#dbe7f3",
            insertbackground="#ffffff",
            relief="flat",
            font=("Consolas", 9),
            padx=10,
            pady=8,
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.grid(row=4, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel", anchor="w").grid(
            row=0, column=0, sticky="ew", padx=(16, 8), pady=6
        )
        self.progress = ttk.Progressbar(bar, mode="determinate", length=260)
        self.progress.grid(row=0, column=1, padx=(0, 10), pady=6)
        ttk.Button(bar, text="打开报告", command=self._open_last_report).grid(row=0, column=2, padx=(0, 16), pady=4)

    # ---------- 设置/检测 ----------

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key_var.get() else "•")

    def _open_cf_console(self) -> None:
        webbrowser.open("https://console.curseforge.com/")

    def _choose_source(self) -> None:
        path = filedialog.askdirectory(
            title="选择 Minecraft 实例目录（只读）",
            initialdir=str(self.settings.instance_path if self.settings.instance_path.exists() else APP_ROOT),
        )
        if not path:
            return
        self.source_var.set(path)
        self.loader_user_selected = False
        self._detect_source_async()

    def _create_sample(self) -> None:
        ensure_instance_skeleton(DEFAULT_INSTANCE_DIR)
        self.source_var.set(str(DEFAULT_INSTANCE_DIR))
        self._log(f"已创建示例目录：{DEFAULT_INSTANCE_DIR}")
        self._detect_source_async()

    def _detect_source_async(self) -> None:
        path = self.source_var.get().strip()
        if not path:
            return
        if self.plan is None:
            self.detect_var.set("正在检测…")
        thread = threading.Thread(target=self._detect_source_worker, args=(path,), daemon=True)
        thread.start()

    def _detect_source_worker(self, path: str) -> None:
        try:
            detected = detect_instance(path)
            self.queue.put(("detected", detected))
        except Exception as exc:
            self.queue.put(("detected_error", str(exc)))

    def _on_detected(self, detected) -> None:
        loader_label = detected.loader_label
        version = detected.game_version or "未识别"
        jar_count = len(list(detected.mods_dir.glob("*.jar"))) if detected.mods_dir.is_dir() else 0
        if self.plan is None:
            self.detect_var.set(f"检测：{loader_label} · {version} · {jar_count} jar")
        if (
            self.plan is None
            and detected.loader in ("fabric", "forge", "neoforge")
            and not self.loader_user_selected
        ):
            self.loader_var.set(detected.loader.capitalize() if detected.loader != "neoforge" else "NeoForge")
        if detected.game_version and not self.version_var.get().strip():
            self.version_var.set(detected.game_version)
        for warning in detected.warnings:
            self._log(f"检测提示：{warning}")
        if detected.evidence:
            self._log("检测依据：" + "；".join(detected.evidence[:6]))

    def _fetch_versions(self) -> None:
        base = self.settings.effective_modrinth_base
        self.status_var.set("正在从 Modrinth 获取游戏版本列表…")

        def worker() -> None:
            try:
                client = HttpClient(CACHE_DIR / "gui", user_agent=f"MCModUpdater/{__version__}")
                data = client.request_json("GET", f"{base.rstrip('/')}/tag/game_version")
                versions = []
                if isinstance(data, list):
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        value = str(item.get("version") or "")
                        if not is_mc_version(value):
                            continue
                        versions.append(value)
                versions = sorted(set(versions), key=mc_version_key, reverse=True)
                self.queue.put(("versions", versions))
            except Exception as exc:
                self.queue.put(("log", f"获取版本列表失败：{exc}"))
                self.queue.put(("status", "版本列表获取失败，可直接手动输入"))

        threading.Thread(target=worker, daemon=True).start()

    def _settings_from_ui(self) -> AppSettings:
        path = Path(self.source_var.get().strip() or str(DEFAULT_INSTANCE_DIR)).expanduser()
        if not path.is_absolute():
            path = APP_ROOT / path
        loader = self.loader_var.get().strip().lower()
        if loader == "neoforge":
            loader = "neoforge"
        self.settings.instance = portable_path(path)
        self.settings.loader = loader if loader in ("fabric", "forge", "neoforge") else "fabric"
        self.settings.game_version = self.version_var.get().strip()
        self.settings.providers = [
            name
            for name, enabled in (("modrinth", self.modrinth_var.get()), ("curseforge", self.curseforge_var.get()))
            if enabled
        ] or ["modrinth"]
        self.settings.prefer_provider = PREFER_LABELS.get(self.prefer_var.get(), "auto")
        self.settings.curseforge_api_key = self.key_var.get().strip()
        self.settings.curseforge_use_proxy = bool(self.cf_proxy_var.get())
        self.settings.allow_prerelease = bool(self.prerelease_var.get())
        self.settings.copy_configs = bool(self.configs_var.get())
        self.settings.config_scope = (
            CONFIG_SCOPE_LABELS.get(self.scope_var.get(), "matched") if self.settings.copy_configs else "none"
        )
        self.settings.full_instance = bool(self.full_var.get())
        return self.settings.normalized()

    # ---------- 扫描/下载 ----------

    def _can_start(self) -> bool:
        if self.busy:
            messagebox.showinfo(APP_NAME, "当前任务还在进行，请稍候。")
            return False
        return True

    def _start_scan(self) -> None:
        if not self._can_start():
            return
        if not self.modrinth_var.get() and not self.curseforge_var.get():
            messagebox.showwarning(APP_NAME, "请至少启用一个更新源：Modrinth 或 CurseForge。")
            return
        if (
            self.curseforge_var.get()
            and not self.key_var.get().strip()
            and not self.cf_proxy_var.get()
            and not self.modrinth_var.get()
        ):
            messagebox.showwarning(
                APP_NAME,
                "只启用了 CurseForge，但没有 API Key。\n\n请填写 Key、勾选“用公共代理”，或同时启用 Modrinth。",
            )
            return
        settings = self._settings_from_ui()
        if not settings.game_version:
            messagebox.showwarning(APP_NAME, "请填写目标游戏版本，例如 1.20.1。")
            return
        try:
            _, mods_dir = resolve_instance_root(settings.instance_path)
        except (OSError, ValueError) as exc:
            messagebox.showwarning(APP_NAME, f"无法读取源实例：{exc}")
            return
        if not mods_dir.is_dir():
            messagebox.showwarning(
                APP_NAME,
                f"没有找到 mods 文件夹：\n{mods_dir}\n\n"
                "请选择实例根目录，或点击“创建示例目录”后把 mods 放进去。",
            )
            return
        try:
            settings.save()
        except OSError as exc:
            self._log(f"设置保存失败：{exc}")
        self._set_busy(True)
        self.plan = None
        self.apply_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        self._clear_tables()
        self.summary_var.set("正在生成计划…")
        self.status_var.set("开始扫描…")
        self._log("=" * 60)
        self._log(f"开始扫描：{settings.instance_path}")
        self._log(f"目标：{settings.game_version} / {settings.loader}，更新源：{', '.join(settings.providers)}")
        thread = threading.Thread(target=self._scan_worker, args=(settings,), daemon=True)
        thread.start()

    def _scan_worker(self, settings: AppSettings) -> None:
        try:
            planner = UpdatePlanner(settings, progress=lambda message, current, total: self.queue.put(("progress", message, current, total)))
            plan = planner.build(settings.instance_path)
            self.queue.put(("plan", plan))
        except PlannerError as exc:
            self.queue.put(("error", str(exc)))
        except Exception as exc:
            import traceback

            self.queue.put(("error", f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"))

    def _on_plan(self, plan) -> None:
        self.plan = plan
        self._populate_mods()
        self._populate_deps()
        self._populate_configs()
        counts = plan.summary()
        self.summary_var.set(
            f"{counts['total_mods']} 模组 · {counts['updates']} 更新 · {counts['configs']} 配置"
        )
        self.detect_var.set(
            f"{LOADER_DISPLAY.get(plan.detected_loader, plan.detected_loader or '未知')} "
            f"{plan.detected_game_version or '?'} → {plan.loader_label} {plan.game_version}"
        )
        self._update_tab_titles()
        if plan.warnings:
            for warning in plan.warnings:
                self._log(f"提醒：{warning}")
        self._log(
            f"计划完成：可更新 {counts['updates']}，已最新 {counts['latest']}，"
            f"新增前置 {counts['missing_dependencies']}，可能可移除 {counts['removable_dependencies']}，"
            f"未解析 {counts['unresolved_dependencies']}，配置 {counts['configs']}"
        )
        self.export_btn.configure(state="normal")
        self.apply_btn.configure(state="normal" if plan.mods else "disabled")
        self.status_var.set("计划已生成，可以检查清单后生成移植压缩包")
        self.notebook.select(self.mods_tab)
        if not plan.mods:
            messagebox.showinfo(APP_NAME, "mods 文件夹中没有找到 .jar 文件。")
        if plan.cross_loader:
            messagebox.showwarning(
                APP_NAME,
                f"检测到跨加载器：{plan.detected_loader or '未知'} → {plan.loader}。\n\n"
                "程序仍会查询目标加载器版本，但不保证模组可用。建议保持同一加载器。",
            )

    def _start_apply(self) -> None:
        if not self._can_start():
            return
        if not self.plan:
            messagebox.showwarning(APP_NAME, "请先扫描并生成计划。")
            return
        settings = self._settings_from_ui()
        try:
            current_root, _ = resolve_instance_root(settings.instance_path)
        except (OSError, ValueError) as exc:
            messagebox.showwarning(APP_NAME, f"无法读取源实例：{exc}")
            return
        if (
            current_root != self.plan.source_root.resolve()
            or settings.loader != self.plan.loader
            or settings.game_version != self.plan.game_version
        ):
            messagebox.showwarning(
                APP_NAME,
                "源实例、目标加载器或目标游戏版本已经改变。\n\n"
                "请重新点击“扫描并生成计划”，再生成移植压缩包。",
            )
            return
        settings.save()
        count = self.plan.download_count
        question = (
            f"将在 output/ 下生成一个命名文件夹，里面放 zip 移植包：\n"
            f"· 预计下载 {count} 个文件\n"
            f"· 迁移 {len(self.plan.selected_configs)} 个配置\n"
            f"· 完整 mods 集合：{'是' if settings.full_instance else '否（只打包新文件）'}\n"
            f"· zip 内含 mods/、config/、reports/ 和安装说明\n\n"
            "源实例不会被修改。继续？"
        )
        if not messagebox.askyesno(APP_NAME, question):
            return
        self._set_busy(True)
        self.status_var.set("开始下载并生成移植压缩包…")
        self._log(f"开始生成移植压缩包，预计下载 {count} 个文件…")
        thread = threading.Thread(target=self._apply_worker, args=(settings,), daemon=True)
        thread.start()

    def _apply_worker(self, settings: AppSettings) -> None:
        try:
            applier = Applier(self.plan, settings, progress=lambda message, current, total: self.queue.put(("progress", message, current, total)))
            result = applier.apply()
            self.queue.put(("applied", result))
        except Exception as exc:
            import traceback

            self.queue.put(("error", f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"))

    def _on_applied(self, result: ApplyResult) -> None:
        self.apply_result = result
        self.last_output.set(str(result.output_dir))
        self.settings.last_output = str(result.output_dir)
        try:
            self.settings.save()
        except OSError:
            pass
        self._log(f"输出文件夹：{result.output_dir}")
        self._log(f"移植压缩包：{result.zip_path}")
        self._log(f"下载成功 {len(result.downloaded)}，失败 {len(result.failed)}，需手动 {len(result.manual)}")
        self._log(f"报告预览：{result.report_paths.get('markdown', '')}")
        self.status_var.set(f"移植包已生成：{result.zip_path.name}")
        open_now = messagebox.askyesno(
            APP_NAME,
            f"移植压缩包已生成。\n\n{result.zip_path}\n\n"
            f"成功下载 {len(result.downloaded)}，失败 {len(result.failed)}，需手动 {len(result.manual)}。\n"
            "zip 里包含 mods、config、全部清单和安装说明。\n\n"
            "现在打开输出文件夹吗？",
        )
        if open_now:
            _open_path(result.output_dir)

    def _export_plan(self) -> None:
        if not self.plan:
            return
        try:
            root = write_plan_bundle(self.plan)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"导出失败：{exc}")
            return
        self._log(f"清单已导出：{root}")
        self.status_var.set(f"清单已导出：{root}")
        if messagebox.askyesno(APP_NAME, f"清单已导出：\n{root}\n\n打开目录吗？"):
            _open_path(root)

    def _open_last_report(self) -> None:
        candidates = []
        if self.apply_result and self.apply_result.report_paths.get("markdown"):
            candidates.append(self.apply_result.report_paths["markdown"])
        if self.plan and self.plan.report_dir:
            candidates.append(self.plan.report_dir / "更新报告.md")
        candidate_dir = CACHE_DIR / "report_previews"
        if candidate_dir.is_dir():
            candidates.extend(sorted(candidate_dir.glob("*_更新报告.md"), key=lambda p: p.stat().st_mtime, reverse=True))
        for candidate in candidates:
            if candidate.exists():
                _open_path(candidate)
                return
        messagebox.showinfo(APP_NAME, "还没有生成报告。请先扫描并导出清单，或生成移植压缩包。")

    # ---------- 表格 ----------

    def _clear_tables(self) -> None:
        for tree in (self.mods_tree, self.unupdated_tree, self.deps_tree, self.configs_tree):
            tree.delete(*tree.get_children())

    @staticmethod
    def _short_version(text: str) -> str:
        text = re.sub(r"^mc\d+\.\d+(?:\.\d+)?[-+]", "", text, flags=re.I)
        text = re.sub(r"\s+for\s+(?:Fabric|Forge|NeoForge|Quilt)\b.*$", "", text, flags=re.I)
        return text

    def _mod_rows(self, *, only_unupdated: bool):
        if not self.plan:
            return []
        needle = self.filter_var.get().strip().lower()
        rows = []
        for mod in self.plan.mods:
            if only_unupdated and mod.status == "update":
                continue
            text = " ".join(
                [
                    mod.status_label,
                    mod.mod.display_name,
                    mod.current_version_text,
                    mod.target_version_text,
                    mod.provider_label,
                    mod.project.title if mod.project else "",
                    " ".join(mod.notes),
                ]
            ).lower()
            if needle and needle not in text:
                continue
            rows.append(
                (
                    mod.status,
                    (
                        mod.status_label,
                        mod.mod.display_name,
                        self._short_version(mod.current_version_text),
                        self._short_version(mod.target_version_text),
                        mod.provider_label,
                        mod.project.title if mod.project else "—",
                        "；".join(mod.notes) or mod.error,
                    ),
                )
            )
        return rows

    def _populate_mods(self) -> None:
        for tree, only_unupdated in ((self.mods_tree, False), (self.unupdated_tree, True)):
            tree.delete(*tree.get_children())
            for status, values in self._mod_rows(only_unupdated=only_unupdated):
                tree.insert("", "end", values=values, tags=(status,))
        if self.plan:
            counts = self.plan.summary()
            self.summary_var.set(
                f"{counts['total_mods']} 模组 · {counts['updates']} 更新 · {counts['configs']} 配置"
            )
            total = len(self.plan.mods)
            shown = len(self.mods_tree.get_children())
            if self.filter_var.get().strip():
                self.summary_var.set(self.summary_var.get() + f" · 筛选显示 {shown}/{total}")

    def _populate_deps(self) -> None:
        self.deps_tree.delete(*self.deps_tree.get_children())
        if not self.plan:
            return
        counts = self.plan.summary()
        self.dep_summary_var.set(
            f"新增前置 {counts['missing_dependencies']} · 可选 {counts['optional_dependencies']} · "
            f"可能可移除 {counts['removable_dependencies']} · 未解析 {counts['unresolved_dependencies']}"
        )
        for dep in self.plan.dependencies:
            self.deps_tree.insert(
                "",
                "end",
                values=(
                    dep.state_label,
                    dep.display_name,
                    dep.relation_label,
                    dep.version_text,
                    "、".join(dep.parents[:4]) + (f" 等{len(dep.parents)}个" if len(dep.parents) > 4 else ""),
                    dep.provider,
                    "；".join(dep.notes),
                ),
                tags=(dep.state,),
            )

    def _populate_configs(self) -> None:
        self.configs_tree.delete(*self.configs_tree.get_children())
        if not self.plan:
            return
        selected = len(self.plan.selected_configs)
        self.config_summary_var.set(f"将迁移 {selected} / {len(self.plan.configs)} 个配置（双击切换）")
        for index, item in enumerate(self.plan.configs):
            self.configs_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    "☑" if item.selected else "☐",
                    item.relative_path,
                    item.owner_text,
                    item.size_text,
                    item.note,
                ),
                tags=("selected" if item.selected else "unselected",),
            )

    def _toggle_config(self, event=None) -> None:
        if not self.plan:
            return
        tree = self.configs_tree
        selected = tree.selection()
        if not selected:
            item_id = tree.identify_row(event.y) if event is not None and hasattr(event, "y") else ""
            if not item_id:
                return
            selected = (item_id,)
        for iid in selected:
            try:
                item = self.plan.configs[int(iid)]
            except (ValueError, IndexError):
                continue
            item.selected = not item.selected
        self._populate_configs()

    def _set_all_configs(self, selected: bool) -> None:
        if not self.plan:
            return
        for item in self.plan.configs:
            item.selected = selected
        self._populate_configs()

    def _select_owned_configs(self) -> None:
        if not self.plan:
            return
        for item in self.plan.configs:
            item.selected = bool(item.owners)
        self._populate_configs()

    def _update_tab_titles(self) -> None:
        if not self.plan:
            return
        counts = self.plan.summary()
        self.notebook.tab(self.mods_tab, text=f"模组总览 ({counts['total_mods']})")
        self.notebook.tab(self.unupdated_tab, text=f"未更新清单 ({counts['total_mods'] - counts['updates']})")
        self.notebook.tab(
            self.deps_tab,
            text=f"前置变化 ({counts['missing_dependencies']}/{counts['unresolved_dependencies']})",
        )
        self.notebook.tab(self.configs_tab, text=f"配置迁移 ({counts['configs']})")

    # ---------- 队列/状态 ----------

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.scan_btn.configure(state=state)
        if not busy:
            self.export_btn.configure(state="normal" if self.plan else "disabled")
            self.apply_btn.configure(state="normal" if self.plan and self.plan.mods else "disabled")
        else:
            self.export_btn.configure(state="disabled")
            self.apply_btn.configure(state="disabled")
        self.root.configure(cursor="watch" if busy else "")
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0, maximum=100)

    def _drain_queue(self) -> None:
        handled = 0
        while handled < 120:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
            handled += 1
            kind = message[0]
            try:
                self._handle_queue_message(kind, message)
            except Exception as exc:
                self._log(f"界面处理消息失败：{type(exc).__name__}: {exc}")
                self._set_busy(False)
                self.status_var.set("界面处理异常，请查看日志")
        self.root.after(120, self._drain_queue)

    def _handle_queue_message(self, kind: str, message: tuple) -> None:
        if kind == "progress":
            _, text, current, total = message
            self.status_var.set(text)
            if total:
                if str(self.progress.cget("mode")) != "determinate":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                self.progress.configure(maximum=max(1, total), value=current)
            if not total or current == total:
                self._log(text)
        elif kind == "log":
            self._log(message[1])
        elif kind == "status":
            self.status_var.set(message[1])
            self._set_busy(False)
        elif kind == "plan":
            self._on_plan(message[1])
            self._set_busy(False)
        elif kind == "applied":
            self._on_applied(message[1])
            self._set_busy(False)
        elif kind == "error":
            self._set_busy(False)
            self.status_var.set("任务失败")
            self._log(f"错误：{message[1]}")
            messagebox.showerror(APP_NAME, str(message[1])[:2000])
        elif kind == "detected":
            self._on_detected(message[1])
        elif kind == "detected_error":
            self.detect_var.set(f"检测失败：{message[1]}")
            self._log(f"源实例检测失败：{message[1]}")
        elif kind == "versions":
            versions = message[1]
            self.version_combo.configure(values=versions or STATIC_VERSIONS)
            self._log(f"已从 Modrinth 获取 {len(versions)} 个游戏版本")
            self.status_var.set("版本列表已更新")

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_NAME, "任务仍在进行，确定退出吗？"):
            return
        try:
            self._settings_from_ui().save()
        except OSError:
            pass
        self.root.destroy()


def main() -> None:
    ensure_app_dirs()
    _set_dpi_awareness()
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
