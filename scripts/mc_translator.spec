# -*- mode: python ; coding: utf-8 -*-
# scripts/mc_translator.spec —— PyInstaller 打包配置（中文注释）
# 用法：pyinstaller scripts/mc_translator.spec
#
# 产出（相对项目根 dist/）：
#   dist/安装版/MC自动翻译器/        —— onedir（M6-3 Inno Setup 安装版源）
#   dist/便携版/MC自动翻译器.exe      —— onefile 单 exe（构建后手动移动/改名）
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
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent                  # 项目根（SPECPATH = scripts/，其父目录即项目根）
BACKEND = ROOT / "backend"                   # Python 包根（app/ 在其下）
APP = BACKEND / "app"

a = Analysis(
    [str(APP / "desktop.py")],               # 打包入口：pywebview 壳 + 子线程 uvicorn
    pathex=[str(BACKEND)],                   # 让 "app.main" 等可被解析（app 包根）
    datas=[
        (str(ROOT / "frontend" / "dist"), "frontend/dist"),                    # 前端静态资源 → _MEIPASS/frontend/dist
        (str(APP / "maps" / "scan_keys.json"), "app/maps/scan_keys.json"),    # 地图扫描关键词表
        *collect_data_files("jawa"),        # jawa/util/bytecode.{json,yaml}：反编译常量/指令表
        *collect_data_files("opencc"),      # 简繁转换 config/*.json + dictionary/*.txt
        *collect_data_files("anvil"),       # legacy_blocks.json（区块版本回退表）
    ],
    hiddenimports=[
        # 对象导入双保险：入口显式收集 app 包与地图扫描
        "app.main", "app.maps.scan",
        # 重构新增：detect/auto_flow/hardcode_flow/langfile/translate 子包（顶层链已静态分析，双保险）
        "app.detect", "app.auto_flow", "app.hardcode_flow", "app.langfile",
        "app.translate", "app.translate.engine", "app.translate.machine", "app.translate.llm",
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
    name="MC自动翻译器",
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
    name="MC自动翻译器",
)

# —— onefile（便携版）：全部打进单个 exe，冷启动较慢是特性 ——
exe_one = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="MC自动翻译器-portable",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
