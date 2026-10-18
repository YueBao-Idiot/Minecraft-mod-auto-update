# MC 模组更新助手

**Minecraft Mod Auto Updater** · 一个便携式 Minecraft 模组移植工具。

[![Release](https://img.shields.io/github/v/release/YueBao-Idiot/Minecraft-mod-auto-update?display_name=tag)](https://github.com/YueBao-Idiot/Minecraft-mod-auto-update/releases/latest)
[![Tests](https://github.com/YueBao-Idiot/Minecraft-mod-auto-update/actions/workflows/tests.yml/badge.svg)](https://github.com/YueBao-Idiot/Minecraft-mod-auto-update/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![icon](assets/app.png)

选择源版本文件夹和目标游戏版本后，它读取源 `mods`，按**文件哈希**在 Modrinth / CurseForge 精确匹配项目，查询目标加载器与游戏版本下的新版本，解析前置变化，迁移功能性模组的配置，最后生成**一个包含 mods、config 和全部清单的 zip 移植包**。

- 支持 **Fabric / Forge / NeoForge**，默认沿用源实例加载器，也可以手动切换。
- 支持旧的 `1.x` 和 2026 年起的 `26.x` 游戏版本号。
- 源实例**只读**；所有下载、zip、报告、配置副本和缓存都写在程序目录内。
- 纯 Python 标准库实现，不需要安装第三方运行时依赖。
- 提供中文图形界面、命令行和 PyInstaller 便携版。

---

## 它解决了什么

| 你的需求 | 工具中的实现 |
| --- | --- |
| 自动读取 mod 列表 | 扫描 `mods/**/*.jar`，解析 `fabric.mod.json`、`quilt.mod.json`、`META-INF/mods.toml`、`META-INF/neoforge.mods.toml`、`mcmod.info` |
| 到 Modrinth / CurseForge 查版本 | Modrinth 用 SHA-512 精确匹配；CurseForge 用文件指纹精确匹配；哈希没命中时再按 mod id / 名称搜索 |
| 复制功能性 mod 的 config | 扫描 `config/` 和 `defaultconfigs/`，按文件名、目录名和内容把配置归属到模组，只默认迁移能识别归属的文件 |
| 列出新版本没有更新的 mod | “未更新清单”页签和报告中的“已是最新 / 无兼容版本 / 未找到 / 查询失败”表 |
| 找出新增前置和不再需要的前置 | 递归解析目标版本依赖；新前置自动加入下载；旧版本需要、目标版本不再需要的已安装模组列为“可能可移除” |
| Fabric / Forge / NeoForge | 三个加载器都可选；换加载器会明确警告；默认自动使用源实例检测到的加载器 |
| 选择游戏版本 | 可编辑下拉框，也可从 Modrinth 拉取版本列表 |

> “可能可移除”只是候选。所有已安装模组本身都可能被玩家手动使用，工具无法知道这一点，所以删除前请人工确认。

---

## 快速开始

### 方式 A：下载便携版

从 [GitHub Releases](https://github.com/YueBao-Idiot/Minecraft-mod-auto-update/releases/latest) 下载最新的 Windows 便携版 zip。

解压后保留整个 `MC模组更新助手/` 文件夹，双击里面的 `启动.bat`，或直接运行 `MC模组更新助手.exe`。**不能只把 exe 拖出来**，必须和同目录的 `_internal/` 一起使用。

便携版的程序、设置和产物都在这个文件夹内：`data/`、`instance/`、`output/`、`.cache/`。整个文件夹可以移动到其他位置。

### 方式 B：从源码启动

需要 Python 3.11+（开发环境为 3.12）：

```powershell
git clone https://github.com/YueBao-Idiot/Minecraft-mod-auto-update.git
cd Minecraft-mod-auto-update
python -m mcmodupdater
```

或双击仓库根目录的 `启动.bat`。

### 第一次使用

1. 点“选择文件夹”，选择**要移植的源版本目录**。支持三种形式：
   - 整个 `.minecraft` 实例根目录（里面有 `mods/`）；
   - 版本隔离目录，例如 `.minecraft/versions/1.20.1-forge/`（里面有 `mods/`、`config/`）；
   - 直接选择只含 jar 的 `mods` 文件夹。
   - 也可以把源文件放进本目录的 `instance/mods` 和 `instance/config`。源目录只读，不会被修改。
2. 选择**要移植到的目标游戏版本**。工具会自动检测源加载器/版本，并默认沿用源加载器；确认或修改目标加载器。
3. 选择更新源：
   - Modrinth：默认开启，不需要 Key。
   - CurseForge：建议填 API Key；没有 Key 时可勾选“用公共代理”。
4. 点 **扫描并生成计划**。
5. 检查四个页签：
   - **模组总览**：每个模组的状态、当前版本、目标版本、匹配到的项目。
   - **未更新清单**：已是最新、无兼容版本、未找到、查询失败。
   - **前置变化**：新增前置、未解析前置、可能可移除、冲突、可选前置。
   - **配置迁移**：双击一行可以切换是否复制该配置。
6. 点 **生成移植压缩包**。程序会在 `output/` 下生成一个文件夹，命名类似：
   ```text
   ModPack_1.20.1_to_1.21.1_Fabric_20261018-1530/
     ModPack_1.20.1_to_1.21.1_Fabric_20261018-1530.zip
     SHA256.txt
   ```
   zip 里已包含 `mods/`、`config/`、`reports/`、`README_如何安装.txt` 和 `manifest.json`，清单不再散落在外面。
7. 解压 zip，先看 `reports/更新报告.md`，再按 `README_如何安装.txt` 把 `mods/` 和 `config/` 合并到新版本实例。

> 工具不安装 Minecraft、Java、加载器，也不修改世界存档。更新前请自行备份世界。

---

## CurseForge 配置

CurseForge 官方 API 需要 `x-api-key`：

1. 打开 <https://console.curseforge.com/> 申请 Key。
2. 在界面的“API Key”里填入，点扫描时会保存在 `data/settings.json`（明文，请注意本机安全）。
3. 也可以用环境变量，优先级更高：

```powershell
$env:CURSEFORGE_API_KEY = "你的 Key"
python -m mcmodupdater
```

如果暂时没有 Key，可以勾选“用公共代理”，工具会改用第三方的只读代理 `https://api.curse.tools/v1`。该代理不是 CurseForge 官方服务，稳定性和限流不受本工具控制；有 Key 时始终优先使用官方 API。

---

## 前置解析规则

- 以目标版本 API 返回的依赖为主；如果某个模组没有匹配到在线版本，则退回读取本地 jar 元数据里的依赖。
- **必需前置**：
  - 已安装：标记为“已满足”。
  - 未安装但能找到兼容版本：标记为“新增前置”，自动加入下载队列，并递归解析它自己的前置。
  - 找不到项目或没有兼容版本：标记为“未解析”，在报告里给出手动查找链接。
- **可选前置**：只列出，不自动下载。
- **内置/工具依赖**和平台自身（`minecraft`、`forge`、`neoforge`、`fabricloader` 等）会跳过。
- **不再需要的前置**：旧版本依赖图里存在、目标依赖图里不存在、且已安装的模组，列为“可能可移除”。模组本身是否还有手动用途需要你判断。
- 依赖 API 没有提供项目 id 时，会按 `file_name` / mod id 搜索；仍找不到则标记未解析，不会静默忽略。

---

## 配置迁移

- 默认只迁移能归属到已安装模组的配置：
  - 文件名/目录名匹配 mod id 或项目名，例如 `config/sodium-options.json`、`config/create/...`。
  - 名称识别不到时，读取文件内容前 256 KB 寻找 mod id。
- 也可以选“迁移全部 config 文件”，或手动双击勾选。
- 只复制，不删除源配置；输出目录里保留相同相对路径。
- 复制是**原样**的。模组跨大版本时配置格式可能改变；如果启动报配置错误，删除对应配置文件让它重新生成。
- `options.txt`、按键设置、世界存档、服务器列表不会迁移。

---

## 命令行用法

```powershell
# 只扫描并生成计划，打印摘要
python -m mcmodupdater plan --instance .\instance --loader fabric --game-version 1.20.1

# 导出 Markdown / CSV / JSON 清单，不下载
python -m mcmodupdater plan --instance .\instance --loader fabric --game-version 1.20.1 --export

# 生成 zip 移植包（非交互需要 --yes）
python -m mcmodupdater apply --instance .\instance --loader fabric --game-version 1.20.1 --yes

# CurseForge 无 Key 时走公共代理
python -m mcmodupdater apply --instance .\instance --loader forge --game-version 1.20.1 --curseforge-proxy --yes

# 创建便携实例目录
python -m mcmodupdater init
```

常用参数：

- `--provider modrinth --provider curseforge`：启用更新源，可重复。
- `--prefer modrinth|curseforge|auto`：优先更新源。
- `--allow-prerelease`：允许 beta/alpha。
- `--no-configs`：不迁移配置。
- `--config-scope matched|all|none`：配置范围。
- `--patch-only`：zip 里只放新下载的文件，不复制旧 mod。
- `--json`：输出机器可读 JSON（进度走 stderr）。
- `--quiet`：不打印进度。

`--loader` 只接受 `fabric`、`forge`、`neoforge`。不同加载器之间的迁移会给出警告，但仍会按目标加载器查询；是否可用由模组本身决定。

---

## 目录结构

```text
启动.bat                  根目录启动器
MC模组更新助手/           打包后的便携程序目录（构建后生成）
  MC模组更新助手.exe
  _internal/              PyInstaller 运行时
  启动.bat
  README.md
  assets/
  instance/               默认源实例（mods/config）
  output/
    ModPack_<源>_to_<目标>_<加载器>_<时间>/
      ModPack_...zip      mods/ + config/ + reports/ + 安装说明 + manifest
      SHA256.txt          压缩包校验值
    plans/                只导出清单时的产物（不下载、不打 zip）
  data/settings.json      界面设置与 CurseForge Key（明文）
  .cache/
    report_previews/      从 zip 提取的报告预览，方便界面直接打开
    ...                   哈希缓存和 API 缓存，可安全删除

mcmodupdater/             源码
tests/                    本地模拟 API 端到端测试
tools/                    图标和打包脚本
assets/                   源码和打包用图标
```

zip 内部结构：

```text
mods/                     更新后的完整 mod 集合（或 --patch-only 的新文件）
config/                   迁移的配置
reports/
  更新报告.md
  mods.csv
  dependencies.csv
  configs.csv
  plan.json
  下载清单.txt
README_如何安装.txt
manifest.json
```

所有用户数据写入都限制在程序目录内；源实例只做读取。删除 `.cache/` 后下次会重新计算哈希和查询 API。

---

## 安全与可靠性

- **哈希优先**：Modrinth 用 SHA-512、CurseForge 用 MurmurHash2 指纹精确匹配，优于按名称猜测。
- 名称搜索有置信度门槛；低置信度不会自动当作命中，会进入“未找到/需手动”。
- 下载后校验平台提供的 SHA-1/SHA-512/MD5，校验不通过会删除临时文件并报错。
- 下载失败或作者禁止第三方下载时，完整实例会保留旧 jar；报告里给出项目/文件手动链接。
- 源实例不会写入；`config` 只读复制。
- 同一个输出目录不会覆盖：每次使用新的时间戳目录。
- 输出文件夹只保留 zip 和 `SHA256.txt`，不会留下重复的解压 mods；报告预览缓存在 `.cache/report_previews/`。
- 报告中的“可能可移除”不会自动删除任何文件。

---

## 开发与测试

```powershell
# 编译检查
python -m compileall -q mcmodupdater tests

# 单元测试 + 端到端测试（本地模拟 Modrinth + CurseForge，不访问外网）
python -m unittest -v tests.test_util tests.test_end_to_end
```

测试覆盖：

- Fabric / Forge jar 元数据解析。
- Modrinth SHA-512 精确匹配、名称搜索、目标版本选择。
- 新增前置、已满足前置、可移除候选、配置归属。
- 输出 zip：替换旧 jar、保留未更新 jar、下载新前置、复制配置，并把报告、CSV、plan.json、manifest 全部放进 zip。
- CurseForge 指纹匹配、依赖解析和下载。

### 重新打包 exe

```powershell
python tools/make_icon.py
powershell -ExecutionPolicy Bypass -File tools/build_exe.ps1
```

打包脚本使用 PyInstaller 的 `--onedir` 模式，把 `assets/` 一起放进便携目录 `MC模组更新助手\`，并带上 DPI 感知清单。这样启动更快，也不需要把运行库解压到系统临时目录。重复打包会更新 exe 和 `_internal`，但不会删除该目录里已有的 `instance/`、`output/`、`data/`。

---

## 已知限制

- 这是一个移植规划器，不是启动器，不会安装 Java、Minecraft 或加载器。
- zip 只包含 `mods`、`config/defaultconfigs` 和清单，不包含世界、资源包、光影、启动器配置。
- 只处理 `mods` 和 `config/defaultconfigs`；不处理资源包、光影、世界、`options.txt`、服务器列表。
- 名称搜索可能匹配到同名/相似项目；请检查“项目”列，必要时手动下载。
- CurseForge 的公共代理是第三方服务，可能限流或不可用；重要更新建议申请官方 Key。
- 某些作者在 CurseForge 关闭了第三方 API 下载，此时只能手动下载，工具会在清单中列出。
- 配置跨大版本可能需要手动调整；工具不会猜测配置格式。
- 自动可移除判断只基于“旧依赖图中有、新依赖图中没有”，不能判断玩家是否手动安装了某个库。

---

## 项目地址与反馈

- 仓库：<https://github.com/YueBao-Idiot/Minecraft-mod-auto-update>
- 下载：<https://github.com/YueBao-Idiot/Minecraft-mod-auto-update/releases/latest>
- 问题反馈：<https://github.com/YueBao-Idiot/Minecraft-mod-auto-update/issues>
- Pull Request 欢迎提交；修改后请先跑一遍单元测试和端到端测试。

如果这个工具帮到了你，欢迎点一个 Star ⭐。

---

## License

MIT。可自由修改、分发；请自行承担更新模组和整合包的风险。
