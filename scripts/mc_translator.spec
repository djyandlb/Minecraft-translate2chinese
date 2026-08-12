# -*- mode: python ; coding: utf-8 -*-
# scripts/mc_translator.spec —— PyInstaller 打包配置（中文注释）
# 用法：pyinstaller scripts/mc_translator.spec
#
# 产出（相对项目根 dist/）：
#   dist/安装版/像素译站/        —— onedir（M6-3 Inno Setup 安装版源）
#   dist/便携版/像素译站.exe      —— onefile 单 exe（构建后手动移动/改名）
#
# 注意：
#   - desktop.py 已把 uvicorn 字符串导入改为对象导入（from app.main import app），
#     PyInstaller 静态分析可收集整个 app 包；hiddenimports 里仍补 "app.main" 双保险。
#   - keyring 在 frozen 下需显式收集 Windows 后端（keyring.backends.Windows），
#     否则 set_password 抛 NoKeyringError。
#   - anvil 在 app/maps/scan.py 内是延迟 import，需 hiddenimports 强制收集。
#   - jawa/opencc/anvil 运行时用 pkgutil.get_data 读包内数据文件（bytecode.json、
#     config/*.json、dictionary/*.txt、legacy_blocks.json），PyInstaller 默认不收非 .py，
#     必须用 collect_data_files 显式收，否则 frozen 下 FileNotFoundError 崩启动。
#   - jawa 按需 import_module 动态加载 attributes 子模块，用 collect_submodules 兜底。
#   - collect_submodules("app.maps"/"app.translate") 需要 app 可被 import，故先手动把
#     backend 塞进 sys.path（pathex 只在 Analysis 内部生效，collect_submodules 在其前执行）。
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent                  # 项目根（SPECPATH = scripts/，其父目录即项目根）
BACKEND = ROOT / "backend"                   # Python 包根（app/ 在其下）
APP = BACKEND / "app"
sys.path.insert(0, str(BACKEND))             # 让 collect_submodules 能定位 app 子包

# 修复（recheck）：直接跑 spec 时若 frontend/dist 缺失（跳过 npm run build）→ PyInstaller
# 对不存在的 datas 只警告并跳过，产物是「无前端空壳」。这里硬校验：缺失直接中止并提示先构建。
if not (ROOT / "frontend" / "dist" / "index.html").exists():
    raise SystemExit(
        "打包前置缺失：frontend/dist/index.html 不存在。请先在 frontend/ 下执行 npm run build。")

a = Analysis(
    [str(APP / "desktop.py")],               # 打包入口：pywebview 壳 + 子线程 uvicorn
    pathex=[str(BACKEND)],                   # 让 "app.main" 等可被解析（app 包根）
    datas=[
        (str(ROOT / "frontend" / "dist"), "frontend/dist"),                    # 前端静态资源 → _MEIPASS/frontend/dist
        (str(ROOT / "assets"), "assets"),                                     # 应用/资源包图标 → _MEIPASS/assets
        (str(APP / "maps" / "scan_keys.json"), "app/maps/scan_keys.json"),    # 地图扫描关键词表
        # 内置 CFPA 汉化资源包（i18n/VP 补丁，6 版本离线可用——用户刚需：整合包汉化
        # 优先用现成人工翻译，不依赖在线下载）。frozen 下 → _MEIPASS/app/data/cfpa/
        (str(APP / "data" / "cfpa"), "app/data/cfpa"),
        # 内置 I18nUpdateMod（i18n 汉化下载器 mod，~49KB）——整合包产物 mods/ 用，
        # 进游戏自动下载 CFPA 全量汉化。frozen 下 → _MEIPASS/app/data/i18n/
        (str(APP / "data" / "i18n"), "app/data/i18n"),
        # 内置 Vault Patcher（VP 硬编码汉化 mod，all jar 跨 loader+MC 通用，~200KB）——
        # 整合包硬编码走 VP 补丁生效，产物 mods/ 用，离线可用。frozen → _MEIPASS/app/data/vp/
        (str(APP / "data" / "vp"), "app/data/vp"),
        *collect_data_files("jawa"),        # jawa/util/bytecode.{json,yaml}：反编译常量/指令表
        *collect_data_files("opencc"),      # 简繁转换 config/*.json + dictionary/*.txt
        *collect_data_files("anvil"),       # legacy_blocks.json（区块版本回退表）
    ],
    hiddenimports=[
        # 对象导入双保险：入口显式收集 app 包与地图扫描
        "app.main", "app.maps.scan",
        # M6 综合改造新增：文本源全覆盖扫描 / 任务中间产物清理 / 硬编码汉化核心（ai_judge 依赖）
        "app.text_sources", "app.cleanup", "app.hardcode",
        # 重构新增：detect/auto_flow/hardcode_flow/langfile/translate 子包（顶层链已静态分析，双保险）
        "app.detect", "app.auto_flow", "app.hardcode_flow", "app.langfile",
        # maps/translate 子包全量收集（copy/export/flow/world/write、common/han/providers 等延迟 import）
        "app.maps", "app.translate",
        *collect_submodules("app.maps"),
        *collect_submodules("app.translate"),
        # uvicorn 运行链（有 hook-uvicorn，补全保险）
        "uvicorn.logging", "uvicorn.loops", "uvicorn.protocols", "uvicorn.lifespan",
        "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
        # keyring：frozen 下必须显式收 Windows 后端（依赖 win32ctypes）
        "keyring.backends.Windows", "win32ctypes",
        # 字节码/存档/地图/简繁/机器翻译：nbtlib/deep_translator 顶层 import 已静态收集；
        # anvil 是延迟 import 必须补；opencc 顶层链 + collect_submodules 兜底
        *collect_submodules("jawa"),        # jawa.attributes.* 动态 import_module
        "nbtlib", "anvil", "opencc", "deep_translator",
    ],
    binaries=[],
    excludes=["tkinter"],
)

pyz = PYZ(a.pure)

# —— onedir（M6-3 安装版源）：exe 与 dll/资源分开，启动快 ——
exe = EXE(
    pyz, a.scripts,
    exclude_binaries=True,                    # onedir：dll/资源交给 COLLECT
    name="像素译站",
    icon=str(ROOT / "assets" / "app-icon.ico"),   # 应用图标（圆形化处理）
    debug=False,
    strip=False,
    upx=False,
    console=False,                            # 桌面 GUI 应用，不开控制台窗口
    disable_windowed_traceback=False,         # 出错时弹 traceback 窗口便于排查
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=False,
    name="像素译站",
)

# —— onefile（便携版）：全部打进单个 exe，冷启动较慢是特性。
# 命名：便携版与安装版都叫「像素译站.exe」（用户诉求：不要 -portable 后缀）——
# onedir 在 dist/安装版/像素译站/ 文件夹内、onefile 在 dist/便携版/，不冲突
exe_one = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="像素译站",
    icon=str(ROOT / "assets" / "app-icon.ico"),   # 应用图标（圆形化处理）
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
