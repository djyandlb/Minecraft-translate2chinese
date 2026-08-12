# M6 桌面打包实施计划

> 阶段门禁：M5 ✅ 已放行（115 passed + 前端 build，38 提交）。
> 用户决策：安装版 + 便携版都出；开发期多文件散放不打包（已完成 M0-M5）；打包最后 debug 完再做（现在做）。

## 一、目标与方案

把 FastAPI 后端 + Vue3 前端搓成一个**可安装桌面应用**：

```
pywebview 桌面壳（本地窗口）
   └─ 子线程起 uvicorn（127.0.0.1:动态端口）
        ├─ /api/*  FastAPI 路由（M0-M5 全部功能）
        └─ /       前端 dist 静态服务（SPA）
```

- **安装版**：PyInstaller onedir → Inno Setup 打成安装程序（带卸载、开始菜单）
- **便携版**：PyInstaller onefile 单 exe（解压即用）

**当前环境：** PyInstaller 6.21.0 ✅、keyring ✅；pywebview 安装中（后台）；Inno Setup 未装（M6-3 前需用户安装免费版 Inno Setup 6）。

## 二、关键技术点

### 2.1 打包后路径定位（frozen 判断）

`main.py` 的 `BASE` 在 PyInstaller frozen 后 `__file__` 指向只读的 `_MEIPASS`，必须改为 exe 同目录（可写）：

```python
import sys
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent   # exe 同目录：config.json/work 放这
else:
    BASE = Path(__file__).resolve().parent.parent  # 开发期 backend/
```

config.json 与 work/ 目录随 exe 位置（用户数据可写），前端 dist 从 exe 旁 `frontend/dist` 或 `_MEIPASS` 读取。

### 2.2 前端静态服务

`main.py` 增加 SPA 静态服务（桌面版 uvicorn 直接 serve dist，开发期 vite proxy 不受影响）：

- `FRONT_DIST`：frozen 后从 `sys._MEIPASS/frontend/dist` 或 exe 旁读取；存在才挂载
- `app.mount("/assets", StaticFiles(...))` + `GET /` 返回 index.html（非 /api 路径 SPA fallback）

### 2.3 desktop.py 桌面壳

```python
def _free_port() -> int: ...        # socket 绑 127.0.0.1:0 拿空闲端口
def _run_server(port): ...          # uvicorn.run("app.main:app", host=127.0.0.1, port, log_level="warning")
def main():                          # 子线程起服务 → webview.create_window → webview.start()
```

pywebview 在 Windows 默认用 MSHTML/Edge WebView2，无额外依赖；`webview.start()` 须在主线程。

### 2.4 PyInstaller 坑（实测迭代）

- **hiddenimports**：keyring 后端选择器（`keyring.backends.*`）、nbtlib、anvil-parser（地图）、jawa、opencc-python-reimplemented（纯 py 一般 OK）
- **data 文件**：前端 dist 整个目录、`app/maps/scan_keys.json`、glossary 等资源
- **onefile 冷启动慢**（解压 _MEIPASS），onedir 快——安装版用 onedir

### 2.5 Inno Setup 安装版

`scripts/installer.iss`：AppName「像素译站」、输出 `dist/安装版/像素译站-Setup.exe`、打包 onedir 产物、开始菜单/桌面快捷方式、卸载器。

## 三、任务分解

### M6-1 `desktop.py` + 静态服务 + frozen 路径
- `backend/app/main.py`：BASE frozen 判断 + 前端静态服务（SPA fallback）
- `backend/app/desktop.py`：pywebview 壳（动态端口 + 子线程 uvicorn）
- 测试：静态服务 200 断言（前端 dist 存在时）；desktop 的端口函数单测

### M6-2 PyInstaller 打包（便携版 onedir + onefile）
- `scripts/mc_translator.spec`：Analysis(datas=前端dist/资源, hiddenimports=[keyring/jawa/nbtlib/anvil...])
- 实测构建 → 修正 hiddenimports/data 到 exe 能跑 → 便携版可运行（启动、/api 通）
- 产物：`dist/便携版/像素译站.exe`（onefile）与 `dist/安装版/`（onedir 源）

### M6-3 Inno Setup 安装版 + 一键脚本
- `scripts/installer.iss` + `scripts/build_all.ps1`（前端 build → pyinstaller → inno）
- 产出安装程序 Setup.exe，安装后桌面图标能启动

### M6-recheck 门禁
- 全维度审查 M6；无 Critical/Important 放行 → 项目完结

## 四、全局约束

- 原始 jar/存档只读铁律不变；api_key 走 keyring 不落盘
- 中文注释/UI/提交；不引入破坏性改动（开发期模式照常 `uvicorn` + `vite dev` 可用）
- 打包产物不入 git（`.gitignore` dist/build）

## 五、完成定义

- 桌面版 exe 能启动：窗口加载前端、/api 各功能可用、config.json/work 落在 exe 旁
- 安装版 Setup.exe 能安装并运行；便携版单 exe 能跑
- 已提交；M6-recheck 放行
