# FastAPI 入口（任务 13）：扫描/翻译/任务/浏览/术语表路由，串起 M0-M2 全部模块
import asyncio
import json
import os
import re
import shutil
import string
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

import httpx  # /api/test-connection 连接检测用（任务 O1）

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app.archive import archive_fingerprint, dir_fingerprint, extract_cached, is_archive
from app.auto_flow import RUNNING_FLOWS, _flows_lock, run_auto_translation
from app.cfpa import (download_cfpa, get_cfpa_progress, list_bundled_versions,
                      load_cfpa, update_dir)
from app.config import AppConfig
from app.detect import (build_detect_summary, detect_input_type, detect_source_lang,
                        infer_pack_format, unwrap_bare_wrapper)
from app.diff import build_jobs
from app.glossary import load_glossary
from app.hardcode import scan_hardcoded_strings
from app.hardcode_flow import run_hardcode_translation
from app.maps import flow as maps_flow, scan as maps_scan, world as maps_world
from app.models import (AutoRequest, DetectRequest, HardcodeRequest, MapScanRequest,
                        MapTranslateRequest, ScanRequest, TranslateRequest)
from app.scanner import scan_modpack, scan_jar
from app.safeerr import sanitize_error
from app.tasks import TaskStore
from app.translator import run_translation

def _base() -> Path:
    """定位可写工作目录（backend/）：
    PyInstaller frozen 后 __file__ 指向只读 _MEIPASS，必须改用 exe 同目录（config.json/work 才能落盘）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


app = FastAPI(title="像素译站")
BASE = _base()                                        # backend/（frozen 后为 exe 同目录）
CONFIG_PATH = BASE / "config.json"                    # config 保持 exe 旁（用户可见可改）
CFPA_PATH = BASE / "cfpa_glossary.json"               # CFPA 社区词库：应用目录（清 temp 缓存不删，长期复用）
# 缓存/工作目录：优先用户设置 cache_dir（设置页「缓存目录」，重启生效，可改到其他盘省 C 盘）；
# 未设置 → 默认系统 temp/mc-translator。整合包解压缓存/地图副本/任务/记忆都在这。
def _resolve_work_dir() -> Path:
    try:
        cache_dir = (AppConfig(CONFIG_PATH).get("cache_dir") or "").strip()
        if cache_dir:
            p = Path(cache_dir)
            p.mkdir(parents=True, exist_ok=True)   # 只读/无法创建 → 回退默认（防 import 崩溃）
            return p
    except Exception:
        pass
    return Path(tempfile.gettempdir()) / "mc-translator"


WORK_DIR = _resolve_work_dir()
OUTPUTS_DIR = WORK_DIR / "outputs"                    # 产物也进缓存目录（下载导出用）
STORE = TaskStore(WORK_DIR / "tasks")
_TASKS: dict[str, asyncio.Task] = {}    # 保存后台任务引用，防止被 GC 回收（F2）
_tasks_lock = threading.Lock()          # 修复：_TASKS 跨线程保护（线程池端点读 + 事件循环 done_callback 写）

# 修复：CORS 收窄到本机（origin 正则允许 localhost/127.0.0.1 任意端口）。
# 桌面版页面由 uvicorn 同源 serve（http://127.0.0.1:<随机端口>），vite dev 经 5173 proxy，
# 二者都不需要任意来源；allow_origins=["*"] 会让任意恶意网页通过浏览器 fetch 本地 API
# （浏览文件系统 / 触发翻译 / 下载产物）。
app.add_middleware(CORSMiddleware,
                   allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
                   allow_methods=["*"], allow_headers=["*"])


# 修复（recheck）：本地 API **写请求**校验 Origin——CORS 只防跨源读取，防不了写副作用
#（恶意网页 fetch 本地 /api/key 可覆盖 keyring 凭据等「简单请求」无需预检直接发出）。
# 远程网页请求会带其 Origin（https://evil.com），hostname 非 127.0.0.1/localhost → 403；
# 本地应用同源请求带本地 Origin、curl/非浏览器无 Origin → 放行。
@app.middleware("http")
async def _write_origin_guard(request, call_next):
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            from starlette.responses import JSONResponse
            try:
                host = urlparse(origin).hostname
            except Exception:
                host = None   # 解析失败 fail-closed：无 hostname → 拒绝（防 urlparse 异常绕过守卫）
            if host not in ("127.0.0.1", "localhost"):
                return JSONResponse({"ok": False, "message": "跨源写请求被拒绝"}, status_code=403)
    return await call_next(request)


def _resolve(path_str: str) -> Path:
    """整合包输入：目录或压缩包。压缩包按 zip 指纹缓存解压（extract_cached）——
    detect 与翻译共用同一解压缓存，相同整合包断点重连不重复解压（用户诉求）。
    内部带并发锁 + .done 完整性标记 + 旧缓存清理。"""
    p = Path(path_str)
    if is_archive(p):
        p = extract_cached(p, WORK_DIR / "extracted")
        # 修复：解压目录被单一顶层目录包裹（zip 里 xxxx/主文件夹 嵌套）→ 下钻到项目根，
        # 否则 detect_input_type 只看解压根目录会漏判 unknown
        p = unwrap_bare_wrapper(p)
    return p


@app.get("/api/config")
def get_config():
    # 修复（recheck）：正常流程 api_key 不落盘，但旧版本遗留/用户手改 config.json 写了
    # api_key 时，直接返回 .data 会明文回显 key——统一走 _strip_api_key 剥离
    return _strip_api_key(AppConfig(CONFIG_PATH).data)


# 修复：config 并发读-改-写丢失——每次请求读盘快照 + 无锁，两个并发 POST 会互相覆盖。
# 加锁串行化：POST B 在锁内重新读盘（AppConfig 构造在 acquire 之后），看到 A 的写结果。
_config_lock = threading.Lock()


def _strip_api_key(obj):
    """递归剥离所有 api_key 字段（防嵌套 dict 如 {"llm": {"api_key": ...}} 绕过顶层拦截）。"""
    if isinstance(obj, dict):
        return {k: _strip_api_key(v) for k, v in obj.items() if k != "api_key"}
    if isinstance(obj, list):
        return [_strip_api_key(i) for i in obj]
    return obj


@app.post("/api/config")
def post_config(payload: dict):
    with _config_lock:
        cfg = AppConfig(CONFIG_PATH)
        old_cache = (cfg.get("cache_dir") or "").strip()
        for k, v in _strip_api_key(payload).items():   # 修复：嵌套 api_key 也剥离，绝不落盘 config.json
            cfg.set(k, v)
        cfg.save()
        new_cache = (cfg.get("cache_dir") or "").strip()
    # 缓存目录**即时生效**（用户刚需：不用重启）：检测 cache_dir 变化 → 迁移轻量数据
    # （tasks/memory/progress/outputs）到新目录，全局重算 WORK_DIR/OUTPUTS_DIR/STORE。
    # extracted 大缓存（整合包解压，可达数百 MB）不迁移（指纹缓存断了下次自动重解压）。
    if new_cache != old_cache:
        _switch_work_dir(new_cache)
    # 运行中任务热更新吞吐（用户诉求：翻译中切换吞吐档位立即生效——改属性，
    # LLMClient 下一次 translate_batch 即按新并发/批次跑，无需重启任务）
    _cc = payload.get("concurrency")
    _bs = payload.get("batch_size")
    if _cc or _bs:
        try:
            with _flows_lock:
                for _flow in list(RUNNING_FLOWS.values()):
                    try:
                        _flow.set_throughput(concurrency=_cc, batch_size=_bs)
                    except Exception:
                        pass
        except Exception:
            pass
    return cfg.data


def _switch_work_dir(cache_dir: str) -> None:
    """缓存目录即时切换（用户刚需：改缓存目录不用重启，立即生效）。
    把旧 work 的轻量数据（tasks/memory/progress/outputs/glossary.json/memory.json）
    搬到新目录保留续联/任务历史，重算全局 WORK_DIR/OUTPUTS_DIR/STORE；
    旧 extracted 大缓存不搬（可重解压）。

    **修复（recheck）**：
    - 有运行中任务 → **不切换**（迁移 tasks 会让运行中任务 save 写旧路径崩溃/两端状态
      不一致，暂停/取消失效）；config 已存新 cache_dir，任务完成后重启/下次生效；
    - 迁移失败（文件占用等）→ **不切换全局变量**（否则新 OUTPUTS_DIR 为空，旧产物
      「打开/下载」全 404，用户视角数据丢失且无提示）。"""
    global WORK_DIR, OUTPUTS_DIR, STORE
    new_dir = Path(cache_dir) if (cache_dir or "").strip() \
        else Path(tempfile.gettempdir()) / "mc-translator"
    old_dir = WORK_DIR
    if new_dir == old_dir:
        return
    with _tasks_lock:
        if any(not t.done() for t in _TASKS.values()):
            return   # 有运行中任务：不即时切换（防任务崩/状态不一致），下次启动生效
    try:
        new_dir.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        for sub in ("tasks", "memory", "progress", "outputs",
                    "glossary.json", "memory.json"):
            old_sub = old_dir / sub
            new_sub = new_dir / sub
            if old_sub.exists() and not new_sub.exists():
                try:
                    shutil.move(str(old_sub), str(new_sub))
                    moved.append(sub)
                except OSError:
                    # 修复（recheck）：迁移中途失败 → 回滚已 move 的子目录，保持全局指向旧目录。
                    # 之前直接 return：已搬走的子目录留新目录、全局仍旧目录，新旧数据分裂，
                    # 重启后 WORK_DIR 指向新目录，旧目录里未搬的 outputs 永久丢失（用户视角任务消失）
                    for m in reversed(moved):
                        try:
                            shutil.move(str(new_dir / m), str(old_dir / m))
                        except OSError:
                            pass
                    return
    except Exception:
        return
    WORK_DIR = new_dir
    OUTPUTS_DIR = new_dir / "outputs"
    STORE = TaskStore(new_dir / "tasks")


def _dir_bytes(d: Path) -> int:
    """递归统计目录内所有文件字节数；目录不存在/为空返回 0。"""
    if not d.is_dir():
        return 0
    # 修复：stat 与 is_file 之间文件可能被任务收尾删除 → FileNotFoundError 冒泡 500；
    # 单文件 stat 失败跳过（不中断统计）
    total = 0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


@app.get("/api/cache-size")
def cache_size():
    """缓存占用：返回 WORK_DIR（临时中间产物）+ OUTPUTS_DIR（exe 旁产物）的大小与路径。

    供设置页「缓存占用：xxx MB」显示；目录不存在按 0 计。"""
    # OUTPUTS_DIR 若为 WORK_DIR 子目录（生产），_dir_bytes(WORK_DIR) 已递归包含，不重复计
    work_bytes = _dir_bytes(WORK_DIR)
    outputs_bytes = 0 if OUTPUTS_DIR.is_relative_to(WORK_DIR) else _dir_bytes(OUTPUTS_DIR)
    return {
        "work_bytes": work_bytes,
        "outputs_bytes": outputs_bytes,
        "total_mb": round((work_bytes + outputs_bytes) / (1024 * 1024), 1),
        "work_path": str(WORK_DIR),
        "outputs_path": str(OUTPUTS_DIR),
    }


def _is_managed_work_dir(p: Path) -> bool:
    """校验 WORK_DIR 是应用受控目录（目录名含 mc-translator / mc- 前缀 / translator，
    或在系统 temp 下）。

    修复：cache_dir 由用户设置页可配，若误配到 D:\\、桌面等含重要文件的目录，clear_cache
    直接 rmtree 会不可逆删除用户数据。目录名不含应用标识且不在 temp 下即拒绝清除。"""
    try:
        p = p.resolve()
        name = (p.name or "").lower()
    except OSError:
        return False
    if name.startswith("mc-") or "translator" in name:
        return True
    # 用户设置页配置的 cache_dir（用户明确指定给应用当缓存目录用）→ 视为受控，
    # 即使目录名不含应用标识（如 E:\Temp）也允许清除（用户刚需：改缓存目录后清除生效）。
    # **修复（recheck）**：加护栏——用户可能误配成自己的盘根/桌面/文档/下载等目录，
    # 无条件放行会让 clear_cache 把用户目录整目录不可逆删除（shutil.rmtree 无回收站）。
    try:
        cfg_cache = (AppConfig(CONFIG_PATH).get("cache_dir") or "").strip()
        if cfg_cache and Path(cfg_cache).resolve() == p:
            return _is_clearable_cache_dir(p)
    except (OSError, ValueError):
        pass
    # 系统 temp 下的目录视为受控区（默认 WORK_DIR 就在 temp；测试临时目录也在 temp）
    try:
        return Path(tempfile.gettempdir()).resolve() in p.parents
    except OSError:
        return False


def _is_clearable_cache_dir(p: Path) -> bool:
    """clear_cache 护栏：拒绝盘根与已知用户目录（home/桌面/文档/下载/图片/视频/音乐/
    AppData 等），防用户误配 cache_dir 后清除整目录。"""
    try:
        p = p.resolve()
    except OSError:
        return False
    if p.parent == p:                       # 盘根（D:\）
        return False
    home = Path.home().resolve()
    if p == home or home in p.parents:
        return False
    # 已知用户目录（Windows 中文系统下这些目录名可能是中文，逐项比对）
    for sub in ("Desktop", "桌面", "Documents", "文档", "Downloads", "下载",
                "Pictures", "图片", "Videos", "视频", "Music", "音乐",
                "OneDrive", "AppData", "Application Data"):
        u = home / sub
        if p == u or u in p.parents:
            return False
    return True


@app.post("/api/clear-cache")
def clear_cache():
    """清除缓存：删除 WORK_DIR（中间产物）+ OUTPUTS_DIR（旧产物）内容，重建空目录。

    - 重建目录是必须的：TaskStore（tasks 子目录）与 memory 依赖 WORK_DIR 存在；
    - P2-5：有运行中任务时返回 409（删除运行中任务的中间文件会崩），前端确认弹窗后仍提示先取消；
    - 安全护栏：WORK_DIR 必须是应用专用目录（目录名含 mc-translator），否则拒绝清除。"""
    if not _is_managed_work_dir(WORK_DIR):
        raise HTTPException(400, "缓存目录不是应用专用目录（目录名需含 mc-translator），"
                                 "已拒绝清除。请检查「设置 → 缓存目录」")
    # 修复：持锁读 _TASKS（线程池端点读 + 事件循环 done_callback 写不再互踩）
    with _tasks_lock:
        running = [tid for tid, t in _TASKS.items() if not t.done()]
    if running:
        raise HTTPException(409, f"有 {len(running)} 个任务运行中，请先取消后再清除缓存")
    cleared = _dir_bytes(WORK_DIR) + (0 if OUTPUTS_DIR.is_relative_to(WORK_DIR) else _dir_bytes(OUTPUTS_DIR))
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(OUTPUTS_DIR, ignore_errors=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    STORE.dir.mkdir(parents=True, exist_ok=True)   # tasks 子目录重建，TaskStore.save 不自动建目录
    return {"cleared_bytes": cleared, "cleared_mb": round(cleared / (1024 * 1024), 1)}


@app.get("/api/tasks")
def list_tasks():
    """任务索引：返回全部任务的快照（P2-3），前端重启后可恢复已完成列表/下载。"""
    return [_task_snapshot(s) for s in STORE.list()]


def _legacy_project_name(project_id: str) -> str:
    """断点续联项目名兜底：progress 的 name 为空/哈希（旧版遗留 / 异常中断在取名步骤前）
    时，从关联任务快照（tasks/*.json 里 project_id 匹配且 display_name 非空）找回真实名。
    取 mtime 最新的一个；找不到返回空串（调用方回退哈希）。"""
    tasks_dir = WORK_DIR / "tasks"
    if not tasks_dir.is_dir():
        return ""
    best, best_mtime = "", 0
    for tf in tasks_dir.glob("*.json"):
        try:
            td = json.loads(tf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if td.get("project_id") != project_id:
            continue
        nm = (td.get("display_name") or "").strip()
        if not nm or re.fullmatch(r"[0-9a-f]{12}", nm):
            continue
        mtime = tf.stat().st_mtime
        if mtime >= best_mtime:
            best, best_mtime = nm, mtime
    return best


@app.get("/api/projects")
def list_projects():
    """未完成项目列表（断点续联，用户诉求）：启动扫描 work/progress/*.json，
    正在进行的项目（未翻译完）直接显示在左侧任务列表，不用拖入才对应显示。"""
    projects = []
    pdir = WORK_DIR / "progress"
    if pdir.is_dir():
        # 修复（recheck #2）：排序 key 里 stat 与 delete_project 并发时文件可能已删 →
        # FileNotFoundError 500；包 try 跳过已删文件（同 _dir_bytes 做法）
        def _pf_mtime(f):
            try:
                return -f.stat().st_mtime
            except OSError:
                return float("-inf")
        for pf in sorted(pdir.glob("*.json"), key=_pf_mtime):
            try:
                d = json.loads(pf.read_text(encoding="utf-8"))
                # 未完成判断（修复用户实测）：build 阶段卡住取消时翻译已完成（done==total）
                # 但**产物未生成**——旧逻辑 done<total 误判「已完成」→ 项目列表跳过 → 无续联
                # 按钮。新格式存 status：非 done 状态（cancelled/failed/running）即使 done==total
                # 也算未完成可续联；旧格式（无 status）仍按 done<total 兼容。
                _st = (d.get("status") or "")
                if (d.get("done", 0) > 0 and d.get("total", 0) > 0
                        and (_st not in ("", "done") or d.get("done", 0) < d.get("total", 0))):
                    done, total = d["done"], d["total"]
                    # 修复：name 为空/哈希（旧版遗留 progress 没存名，或中断在取名前）→
                    # 从任务快照找回真实名，不再显示 5a818a7428e7 这类指纹（用户实测）
                    _name = (d.get("name") or "").strip()
                    if not _name or re.fullmatch(r"[0-9a-f]{12}", _name):
                        _name = _legacy_project_name(pf.stem) or pf.stem
                    projects.append({
                        "project_id": pf.stem,
                        "name": _name,
                        # 原始输入路径：前端点「续联」→ autoTranslate(path) 重算指纹续联。
                        # 兼容旧 progress（无 path 字段）→ 空，前端隐藏续联按钮（需拖入）
                        "path": d.get("path") or "",
                        "done": done, "total": total,
                        "failed": d.get("failed", 0), "stage": d.get("stage", ""),
                        "pct": min(100, round(done / total * 100, 1)),
                    })
            except Exception:
                pass
    # 兼容旧版（无 progress 文件）：从 tasks/ 找有记忆的未完成项目
    if not projects:
        mem_dir = WORK_DIR / "memory"
        if mem_dir.is_dir():
            for mf in mem_dir.glob("*.json"):
                pid = mf.stem
                try:
                    cnt = len(json.loads(mf.read_text(encoding="utf-8")))
                except Exception:
                    cnt = 0
                if cnt > 0:
                    # 修复（用户实测）：从 progress 找回真实名 + 原始路径——否则 memory 兜底
                    # 只给 pid（哈希 5a818a7428e7）+ 无 path → 前端显示哈希名、续联按钮被
                    # v-if="p.path" 隐藏（想续联却只能拖入整合包）
                    _name, _path = pid, ""
                    try:
                        _pd = json.loads((WORK_DIR / "progress" / f"{pid}.json")
                                         .read_text(encoding="utf-8"))
                        _name = (_pd.get("name") or "").strip() or _name
                        _path = _pd.get("path") or ""
                    except Exception:
                        pass
                    projects.append({"project_id": pid, "name": _name, "done": 0,
                                     "total": 0, "failed": 0, "stage": "", "pct": 0,
                                     "path": _path})
    return projects


@app.delete("/api/project/{project_id}")
def delete_project(project_id: str):
    """删除项目缓存（用户诉求：任务列表删除任务 → 清理对应临时文件里项目数据，
    **进行中/已取消的任务也要删得掉**——不再一律 409 拒绝）。

    1) 关联该项目运行中的任务 → 取消并等其收尾（否则任务收尾的 _save_progress/
       memory.save 会把刚删的缓存重建）；
    2) 写 progress/<id>.deleted 标记 → _save_progress 检查到即跳过（双保险）；
    3) 删 memory/progress/extracted。"""
    if not re.fullmatch(r"[0-9a-f]{12}", project_id or ""):
        raise HTTPException(404, "项目不存在")
    # 1) 取消关联运行中任务（TaskState.project_id 匹配），等收尾
    with _tasks_lock:
        for tid, t in list(_TASKS.items()):
            if t.done():
                continue
            st = STORE.load(tid)
            if st and st.project_id == project_id:
                st.cancelled = True
                st.paused = False
                STORE.pause_event(tid).set()
                STORE.save(st)
                t.cancel()   # 真正中止协程（对齐 cancel_task 的 P1-3）
    # 等任务真正结束（取消通常秒级；最多 8 秒，超时也继续删——.deleted 标记兜底防重建）
    deadline = time.time() + 8
    while time.time() < deadline:
        with _tasks_lock:
            if not any(not t.done() and (STORE.load(tid) and STORE.load(tid).project_id == project_id)
                       for tid, t in _TASKS.items()):
                break
        time.sleep(0.3)
    # 2) .deleted 标记：任务 finally 的 _save_progress 检查到则跳过保存（防重建）。
    #    修复：**标记不能删**——删掉后任务 finally 检查不到标记会重建 progress，
    #    项目「复活」删不掉（用户实测续联项目点 ✕ 无效果）。标记由任务 finally 消费
    #    （_save_progress 看到即跳过+删除），或下次同项目重新翻译时 run() 清理。
    prog_dir = WORK_DIR / "progress"
    prog_dir.mkdir(parents=True, exist_ok=True)
    try:
        (prog_dir / f"{project_id}.deleted").write_text("", encoding="utf-8")
    except OSError:
        pass
    # 3) 删缓存：memory / progress（**.deleted 标记保留**）/ extracted 解压目录
    for p in (WORK_DIR / "memory" / f"{project_id}.json",
              WORK_DIR / "progress" / f"{project_id}.json"):
        try:
            if p.exists():
                p.unlink(missing_ok=True)
        except OSError:
            pass
    shutil.rmtree(WORK_DIR / "extracted" / project_id, ignore_errors=True)
    return {"ok": True}


@app.post("/api/key")
def set_key(payload: dict):
    """R1：API Key 写入系统 keyring，与 create_engine 读 keyring 打通链路。绝不落盘。"""
    api_key = str(payload.get("api_key", "")).strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")
    try:
        import keyring
        keyring.set_password(AppConfig(CONFIG_PATH).get("api_key_ref", "mc-translator"), "api_key", api_key)
    except Exception as e:
        # 修复：keyring 写失败（Windows 凭据库异常）返回明确错误，不 500
        raise HTTPException(500, f"API Key 写入系统凭据库失败：{e}")
    return {"ok": True}


@app.get("/api/desktop")
def desktop():
    """桌面版标记：desktop.py 起服务时设 MC_DESKTOP=1（下载等前端据此走桌面路径，不依赖 window.pywebview 检测）。"""
    return {"desktop": os.environ.get("MC_DESKTOP") == "1"}


@app.get("/api/key/status")
def key_status():
    """查询 keyring 是否已配置 API Key（仅返回 configured 布尔，绝不返回 key 本身）。

    供桌面版前端启动时判断是否已持久化过 key，避免「每次都要重输」的困惑。
    """
    cfg = AppConfig(CONFIG_PATH)
    return {"configured": bool(_read_api_key(cfg))}


def _read_api_key(cfg: AppConfig) -> str:
    """从系统 keyring 读 api_key（复用 create_engine 前的取 key 逻辑）；读不到返回空串。"""
    try:
        import keyring
        return keyring.get_password(cfg.get("api_key_ref", "mc-translator"), "api_key") or ""
    except Exception:
        return ""


def _extract_api_error(resp) -> str:
    """从平台响应体尽量提取真实错误信息（MiniMax 的 status_msg / status_code、OpenAI 的 error.message 等）。

    修复（v1.2.2）：之前测试连接失败统一甩「地址或接口格式不对」，把模型名错、参数不合规、
    MiniMax 业务错误码等全部误导成「去改地址」。改为透传平台死因，让用户看到真相。"""
    try:
        data = resp.json()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        m = err.get("message") or err.get("status_msg") or err.get("code")
        if m:
            return str(m)
        if err:
            return str(err)
    m = data.get("message") or data.get("status_msg") or data.get("status_message")
    if m:
        return str(m)
    if data.get("status_code") is not None:
        return f"status_code={data.get('status_code')}"
    return ""


@app.post("/api/test-connection")
def test_connection(payload: dict = None):
    """用当前配置验证翻译引擎连通性（任务 O1）。
    - LLM：httpx 发最小 chat 请求（1 token，10s 超时）；
    - machine：deep_translator 翻译一个词验证 Google 通道可达。
    只返回 {"ok": bool, "message": str}，绝不泄露 api_key 与请求细节。"""
    payload = payload or {}
    cfg = AppConfig(CONFIG_PATH)
    engine = payload.get("engine") or cfg.get("engine", "llm")

    # 机翻分支：免费 Google 通道翻译一个词（仅 engine == machine；free 也是 LLM 通道）
    if engine == "machine":
        import deep_translator
        try:
            deep_translator.GoogleTranslator(source="auto", target="zh-CN").translate("test")
        except Exception:
            return {"ok": False, "message": "机翻服务不可用（可能需要网络代理）"}
        return {"ok": True, "message": "连接成功"}

    # LLM 分支（engine in llm/free）：复用 create_engine 的厂商/免费平台智能默认 + config 覆盖 + keyring
    from app.translate.providers import free_defaults, smart_defaults
    if engine == "free":
        provider = payload.get("provider") or cfg.get("provider", "智谱AI")
        d = free_defaults(provider)
    else:
        provider = payload.get("provider") or cfg.get("provider", "DeepSeek")
        d = smart_defaults(provider)
    saved_llm = cfg.get("llm", {}) or {}
    override_llm = payload.get("llm", {}) or {}
    base_url = (override_llm.get("base_url") or saved_llm.get("base_url") or d["base_url"] or "").strip()
    model = (override_llm.get("model") or saved_llm.get("model") or d["model"] or "").strip()
    api_key = (payload.get("api_key") or "").strip() or _read_api_key(cfg)
    if not api_key:
        return {"ok": False, "message": "尚未配置 API Key"}
    if not base_url or not model:
        return {"ok": False, "message": "base_url/model 未配置"}
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            timeout=10,
        )
    except Exception:
        return {"ok": False, "message": "无法连接服务器（网络/地址错误）"}
    if resp.status_code == 200:
        return {"ok": True, "message": "连接成功"}
    if resp.status_code in (401, 403):
        detail = _extract_api_error(resp)
        msg = f"API Key 无效或无权限（HTTP {resp.status_code}）"
        if detail:
            msg += f"：{detail}"
        return {"ok": False, "message": msg}
    if resp.status_code in (400, 404, 422):
        # 修复（v1.2.2）：不再一刀切归为「地址格式不对」。模型名错/参数不合规/MiniMax 等平台
        # 的业务错误码都会返回这些状态——透传真实错误。并修正旧版误导文案：旧版说
        # 「不要带 /v1」纯属错误，Kimi/MiniMax/通义官方 OpenAI 兼容端点都带 /v1。
        detail = _extract_api_error(resp)
        msg = f"接口返回 HTTP {resp.status_code}"
        if detail:
            msg += f"：{detail}"
        msg += ("。请检查：① base_url 填官方 API 端点（如 DeepSeek=https://api.deepseek.com；"
                "Kimi/MiniMax/通义等平台需保留官方 /v1 后缀）；② 模型名拼写与平台一致；"
                "③ 不要填网页控制台地址")
        return {"ok": False, "message": msg}
    detail = _extract_api_error(resp)
    msg = f"HTTP {resp.status_code}"
    if detail:
        msg += f"：{detail}"
    return {"ok": False, "message": msg}


@app.post("/api/test-throughput")
async def test_throughput(payload: dict = None):
    """动态吞吐探测（v1.2.4 **预算闸方程版**，业界标准：请求前 RPM 限速 + Little's Law）。

    不再「爬坡扫窗口找拐点」（反复引入 bug、慢、误杀），改为：
    1. **测基准单批耗时 W**：低并发（2）发 3 批短样本，量平均单批耗时；
    2. **方程推并发（Little's Law）**：在途并发 = 每分钟配额 × 单批耗时 / 60
       `并发 = round(RPM/60 × W)`，封顶 8（保守）；
    3. **批次固定 40**（输出 token 预算预算内保守），扫描并发 = round(并发×0.6)：
    4. **保底验证**：按算出的并发/批次发 2 批确认 failed==0，不稳则降并发重验。
    配合 create_engine 的 RPM 预算闸（请求前限速，API 永不 429），探测结果**就是这个
    API 配额下能稳定跑满的最优组合**——不追求激进上限，只求「稳稳满速不被打」。
    """
    import time as _t
    payload = payload or {}
    cfg = AppConfig(CONFIG_PATH)
    engine_sel = payload.get("engine") or cfg.get("engine", "llm")
    if engine_sel == "machine":
        return {"ok": False, "message": "机翻引擎无吞吐档位概念"}

    from app.translate.providers import free_defaults, smart_defaults
    provider = payload.get("provider") or cfg.get("provider", "智谱AI" if engine_sel == "free" else "DeepSeek")
    d = free_defaults(provider) if engine_sel == "free" else smart_defaults(provider)
    saved_llm = cfg.get("llm", {}) or {}
    override_llm = payload.get("llm", {}) or {}
    base_url = (override_llm.get("base_url") or saved_llm.get("base_url") or d["base_url"] or "").strip()
    model = (override_llm.get("model") or saved_llm.get("model") or d["model"] or "").strip()
    api_key = (payload.get("api_key") or "").strip() or _read_api_key(cfg)
    if not api_key:
        return {"ok": False, "message": "尚未配置 API Key"}
    if not base_url or not model:
        return {"ok": False, "message": "base_url/model 未配置"}
    # RPM：<=0 = 自动校准（推荐，动态测试会估出该 API 建议值）；>0 = 用户固定配额
    _rpm_cfg = int(payload.get("rpm") if payload.get("rpm") is not None else cfg.get("rpm") or 0)
    rpm_auto = _rpm_cfg <= 0

    from app.translate.llm import LLMClient
    # 真实长度样本（token 消耗贴近语言文件：短选项/中句/长描述混合）
    _probe_base = [
        "Sprint", "Back", "Enabled", "Silk Touch",
        "Use this item to light up dark areas in your base",
        "Shows the current weather and time on the HUD overlay",
        "This enchanting table allows you to apply powerful enchantments to your gear",
        "Right click on a block to place it, sneak and click to open the configuration screen",
        "Press the key bound in Controls to toggle the minimap visibility while playing",
        "Warning: this option will reset all settings to their default values",
        "Each tool has its own durability and can be repaired on an anvil with the matching material",
        "When both the right and the left wing are balanced, the crafted shield gains additional knockback resistance",
        "Enchantments applied to this bow affect every arrow shot regardless of the arrow's origin",
    ]
    probe = list(_probe_base) * 6
    SAMPLE_ROUNDS = 3      # 测单批耗时发几批
    BASE_CONC, BASE_BATCH = 2, 16
    MAX_CONC = 16          # 并发封顶 = 滑块上限：方程给理论值(Little's Law)，verify 真打降档到稳定
    MAX_BATCH = 40         # 批封顶（输出 token 预算保守）

    async def _measure_w() -> tuple[float, bool]:
        """低并发测平均单批耗时 W（秒）。全失败返回 (30, False)。"""
        req = _probe_base[:8]              # 慢 API（mimo 等）用 8 条短批：单批快、超时风险低
        eng = LLMClient(base_url, api_key, model, concurrency=BASE_CONC,
                        batch_size=8, rpm=rpm_eff)
        times: list[float] = []
        try:
            # 只跑 2 批：慢 API 跑 1 批就能算出 W，别让 3 批拖到像卡死（mimo 慢测不出的根因）
            for _ in range(2):
                _t0 = _t.time()
                _m: dict = {}
                try:
                    await asyncio.wait_for(eng.translate_batch(req, "zh_cn", meta=_m), timeout=60)
                except Exception:
                    continue
                times.append(_t.time() - _t0)
        finally:
            try:
                await eng.aclose()
            except Exception:
                pass
        # v1.2.7+ 顺带读响应头的 TPM 配额（x-ratelimit-limit-tokens），供批大小约束
        _tpm = getattr(eng, "_ratelimit_tpm", 0) or 0
        return (sum(times) / len(times) if times else 30.0), bool(times), int(_tpm)

    async def _find_auto_rpm() -> tuple[int, float, bool]:
        """自动校准（v1.2.6+ **60s 滑动窗口灌满法**）：RPM 是 60 秒滑动窗口配额。

        正确做法 = 让一个 60s 窗口灌满配额再数（业界压测同思路）：
        - **高并发（14）持续发小块短批（批 8）逼 API 显形**——并发足够才有「灌满」的能力：
          慢 + 高配额 API（如 mimo：批 8 条≈10s、配额 100/分）需要 ~17 并发才能在窗口内
          发满 100；并发 6 只发 ~40 个 → 65s 不撞 → 会把「65s 内成功数」当 RPM 严重低估
          并反被闸二次压慢（recheck 确认的 bug）。并发 14 → 65s 能发 ~90，下界逼近真实。
        - **首次 429 前 60s 内成功数 ≈ 该 API 真实 RPM**（滑窗配额刚用完，精确）；
        - **没撞 429（65s 到顶）**：只测得「下限」——真实配额 ≥ 该值，此时**不乘 0.85 再次
          低估**，并返回 hit_limit=False 让上层提示「可填已知配额更精确」。
        返回 (建议RPM, 耗时秒, 是否撞到限流)。
        """
        req = _probe_base[:8]
        stamps_ok: list[float] = []
        stamps_429: list[float] = []
        t0 = _t.time()
        deadline = t0 + 65
        PROBE_CONC = 14   # recheck：灌满并发，让慢+高配额 API 的 65s 下界逼近真实
        eng = LLMClient(base_url, api_key, model, concurrency=PROBE_CONC, batch_size=8, rpm=100000)

        async def _worker() -> None:
            """灌满 worker：PROBE_CONC 个同时发，直到硬顶或 429 现形提前收。"""
            while True:
                if len(stamps_429) >= 5 and (_t.time() - t0) > 8:
                    return                # 配额封顶已现形，提前收
                if _t.time() >= deadline:
                    return
                _tx = _t.time()
                _m: dict = {}
                try:
                    await asyncio.wait_for(eng.translate_batch(req, "zh_cn", meta=_m), timeout=45)
                except Exception:
                    stamps_429.append(_tx)
                else:
                    if _m.get("failed"):
                        stamps_429.append(_tx)
                    else:
                        stamps_ok.append(_tx)

        try:
            await asyncio.gather(*(_worker() for _ in range(PROBE_CONC)))
        finally:
            try:
                await eng.aclose()
            except Exception:
                pass
        # 命中限流 → cnt = 首次 429 前 60s 内成功数 ≈ 真实 RPM；未命中 → 下界（不 ×0.85）
        if stamps_429:
            first_429 = stamps_429[0]
            cnt = len([t for t in stamps_ok if first_429 - 60 < t <= first_429])
            hit = True
            rpm_out = max(4, int(cnt * 0.85)) if cnt else 8
        else:
            cnt = len(stamps_ok)          # 65s 内实际发出的量
            hit = False
            rpm_out = max(4, int(cnt)) if cnt else 8   # 未撞：下界就是建议（别再乘 0.85 低估）
        return rpm_out, round(max(_t.time() - t0, 0.1), 1), hit


    async def _verify(cc: int, bs: int) -> bool:
        """并发 cc 发 2 批短样本确认稳定。

        v1.2.6：用 16 条短批而非满批——慢 API（mimo 批 40 可能要 1 分钟+）会让整组
        动态测试拖到超时。判定「该并发能跑」用轻负载足够。
        v1.2.7+：**429/限流不算不稳**——RPM 校准刚把 60s 窗口配额撞满，紧接 verify
        的请求会被限流拒（误判并发过载 → 一路降到 1）。限流由运行时预算闸处理，不是
        并发问题；只有网络异常/5xx 才算真不稳。
        """
        _vb = min(bs, 16)
        req = (probe * ((_vb // len(probe)) + 1))[:_vb]
        eng = LLMClient(base_url, api_key, model, concurrency=max(1, cc),
                        batch_size=max(1, bs), rpm=rpm_eff)
        ok = True
        try:
            for _ in range(2):
                _m: dict = {}
                try:
                    await asyncio.wait_for(eng.translate_batch(req, "zh_cn", meta=_m), timeout=180)
                except Exception:
                    ok = False
                    break
                if _m.get("failed") and _m.get("kind") != "ratelimit":
                    ok = False     # 非限流失败 → 真不稳；ratelimit 忽略（窗口满，运行时闸处理）
                    break
        finally:
            try:
                await eng.aclose()
            except Exception:
                pass
        return ok

    # 生效 RPM：固定填了用固定的；默认自动 → 动态测试顺便校准出该 API 建议值
    rpm_eff = _rpm_cfg if not rpm_auto else 0
    w, w_ok, probe_tpm = await _measure_w()
    if not w_ok:
        return {"ok": False, "message": "当前 API 请求全部失败（请先测试连接）"}
    rpm_secs = 0.0
    rpm_hit = False
    if rpm_auto:
        rpm_eff, rpm_secs, rpm_hit = await _find_auto_rpm()   # 60s 滑窗灌满校准（10-65s）
        # v1.2.9：校准值存 config.calibrated_rpm，下次 create_engine 用它做 rate_gate
        # auto 初始目标（否则 auto 从 30 爬坡、1000 条 <50 批永不升档，动态测试白测）。
        # v1.3.0（Agent recheck）：加 _config_lock + 重新加载最新 cfg——原用开头快照
        # save 整个 config，测试 10-65s 期间用户改的设置会被旧快照覆盖（lost update）
        try:
            with _config_lock:
                _cfg2 = AppConfig(CONFIG_PATH)
                _cfg2.set("calibrated_rpm", rpm_eff)
                _cfg2.save()
        except Exception:
            pass
    # ===== 方程（Little's Law）：并发 = 每分钟配额 × 单批耗时 / 60，封顶滑块上限 =====
    conc = min(MAX_CONC, max(1, round((max(1, rpm_eff) / 60.0) * w)))
    # ===== 批大小按 TPM 约束（v1.2.7+ 用户方案）：没拿到 TPM → 40；
    # 拿到（响应头 x-ratelimit-limit-tokens 或设置页手填）→ TPM/1000
    #（每条按 ≤1000 token 预留，保证单批 token 不超每分钟配额），封顶 40、下限 4。=====
    _tpm = int(cfg.get("tpm") or 0) or int(probe_tpm or 0)
    batch = max(4, min(MAX_BATCH, (_tpm // 1000) if _tpm > 0 else MAX_BATCH))
    # ===== 保底验证：方程给理论值，verify 确认该并发可跑（v1.2.7+ 仅单级降档——
    # 429/限流不算不稳，只有真网络/5xx 才降一档；不再连降到 1 误伤）=====
    _final_c = conc
    if not await _verify(_final_c, batch):
        _final_c = max(1, _final_c // 2)   # 真不稳 → 降一档
    scan_ok = min(8, max(1, round(_final_c * 0.6)))
    if rpm_auto and rpm_hit:
        _auto_note = f"（实测撞限流：RPM≈{rpm_eff}，×{rpm_secs}s 窗口校准）"
    elif rpm_auto:
        _auto_note = (f"（未撞限流：实际配额 ≥ {rpm_eff}，已按此估算；"
                      f"已知真实配额可直接在设置页填写更精确，如 {rpm_eff}+）")
    else:
        _auto_note = ""
    # v1.2.8：审查/硬编码判断并发也从 RPM 方程推导（共享预算，不各自满配）
    review_conc = _final_c          # 审查共享翻译全局并发池（阶段独占跑满该档）
    judge_conc = min(_final_c, 5)   # 硬编码判断单条轻量，5 封顶不挤占主池
    return {"ok": True, "preset": "auto",
            "concurrency": _final_c, "batch_size": batch,
            "scan_concurrency": scan_ok,
            "review_concurrency": review_conc, "judge_concurrency": judge_conc,
            "rpm": rpm_eff,
            "message": (f"预算闸方案：RPM≈{rpm_eff}{_auto_note} → 并发 {_final_c}（单批 {round(w, 1)}s）"
                        f"· 批 {batch} · 扫描 {scan_ok} · 审查/判断共享该并发，全速不触发限流"),
            "results": {"w_sec": round(w, 1), "rpm": rpm_eff, "rpm_auto": rpm_auto,
                        "rpm_hit_limit": rpm_hit, "verified_conc": _final_c,
                        "review_concurrency": review_conc, "judge_concurrency": judge_conc}}


def _check_resume(path_str: str) -> dict:
    """断点续联检测：该项目（按输入内容指纹）是否有项目记忆/进度。

    识别（detect）后前端据此显示「可断点续联（上次 X%）」——用户诉求：
    拖入识别完成就在任务列表标出能否续联，不用等翻译开始才知道。
    """
    try:
        p = Path(path_str)
        # 修复：与 run() 的 project_id 一致——jar 也按内容指纹（is_file），目录才用目录指纹
        proj = (archive_fingerprint(p) if (is_archive(p) or p.is_file()) else dir_fingerprint(p))
    except Exception:
        proj = ""
    if not proj:
        return {"available": False, "memory_count": 0, "progress_pct": None}
    count = 0
    mem = WORK_DIR / "memory" / f"{proj}.json"
    if mem.exists():
        try:
            count = len(json.loads(mem.read_text(encoding="utf-8")))
        except Exception:
            count = 0
    if count == 0:
        # 兼容旧全局 memory（老版本遗留）
        legacy = WORK_DIR / "memory.json"
        if legacy.exists():
            try:
                count = len(json.loads(legacy.read_text(encoding="utf-8")))
            except Exception:
                count = 0
    # 修复：卡片进度与 run() 续联基准一致——progress 文件 + 旧任务最大 done（同整合包，
    # memory 同源）+ 记忆条数，取较大值；否则卡片显示 progress 的小进度（用户实测 11.6% vs 真实 89%）
    base_done = 0
    total_ref = 0
    prog = WORK_DIR / "progress" / f"{proj}.json"
    if prog.exists():
        try:
            d = json.loads(prog.read_text(encoding="utf-8"))
            base_done = d.get("done", 0)
            total_ref = d.get("total", 0)
        except Exception:
            pass
    # 旧任务最大 done（仅项目有记忆才计入，防跨项目混淆——与 run() 一致）
    if count > 0:
        try:
            _tdir = WORK_DIR / "tasks"
            if _tdir.is_dir():
                for _tf in _tdir.glob("*.json"):
                    try:
                        _td = json.loads(_tf.read_text(encoding="utf-8"))
                        if _td.get("done", 0) > base_done and _td.get("display_name"):
                            base_done = _td["done"]
                            total_ref = _td.get("total", 0)
                    except Exception:
                        pass
        except Exception:
            pass
    base_done = max(base_done, count)   # 记忆词条数兜底（无进度文件/旧任务时）
    pct = None
    if base_done > 0 and total_ref > 0:
        pct = min(100, round(base_done / total_ref * 100, 1))
    # 已完成项目（done>=total，产物已生成）不算可续联——用户诉求：生成产物后关闭重开
    # 不再显示「可断点续联」，重新翻译也重头（不是续联）
    completed = total_ref > 0 and base_done >= total_ref
    return {"available": (not completed) and (base_done > 0 or count > 0),
            "memory_count": count,
            "progress_pct": None if completed else pct}


@app.post("/api/detect")
def detect(req: DetectRequest):
    """自动识别输入类型 + 源语言 + pack_format。识别失败返回 kind=unknown 供前端提示。

    **轻量识别（用户诉求）**：压缩包输入**只读 zip 中央目录**判断类型/统计 jar 数，
    **不解压整包**（拖上去不产生大缓存；真正翻译时才解压）。名称/内容指纹命中的
    项目在翻译时复用已有缓存。目录输入直接识别（无需解压）。
    附 resume：该项目是否有项目记忆/进度（断点续联提示）。
    """
    raw = Path(req.path)
    if is_archive(raw):
        # 压缩包轻量识别：读中央目录（文件名列表），不解压
        resume = _check_resume(req.path)
        try:
            with zipfile.ZipFile(raw) as zf:
                names = zf.namelist()
        except Exception:
            return {"kind": "unknown", "resume": resume}
        if raw.suffix.lower() == ".jar":
            kind = "modjar"
        # 修复：zip 里 xxxx/ 包裹层嵌套（mods/、level.dat、shaders/lang 在包裹层内）——
        # 之前 startswith 只认根目录层级，嵌套结构全部漏判 unknown（用户实测）。改为任意
        # 层级匹配：f"/{n}" 归一化后找段，level.dat/region 任意层收尾。
        # 修复（recheck）：地图强信号（level.dat/region）优先于 mods——含 mods 文件夹的地图
        # zip 不再误判 modpack；mods 匹配收紧为 mods/**/*.jar（config/mods/*.cfg 不误命中）
        elif any(n.endswith("level.dat") for n in names):
            kind = "map"
        elif any("/region/" in f"/{n}" for n in names):
            kind = "map"
        # 修复（recheck）：mods/**/*.jar（整合包）优先于 shaders/lang——整合包内含
        # shaders/lang/ 路径（mod/资源包携带）不应误判光影（用户实测 Better MC [FORGE] 整合包
        # 被识别成光影）。shader 只在「无 mods jar」时成立（光影包无 mods）。
        elif any("/mods/" in f"/{n}" and n.endswith(".jar") for n in names):
            kind = "modpack"
        # 修复（recheck）：纯 Modrinth .mrpack / CurseForge manifest.json / packwiz pack.toml
        # 整合包（zip 内无 mods/ 目录，mods 由 index 网络下载）——之前判 unknown 无法翻译
        elif any(n.endswith(("modrinth.index.json", "manifest.json", "pack.toml"))
                 for n in names):
            kind = "modpack"
        elif any("/shaders/lang/" in f"/{n}" for n in names):
            kind = "shader"
        else:
            kind = "unknown"
        if kind == "unknown":
            return {"kind": "unknown", "resume": resume}
        if kind == "map":
            return {"kind": "map", "source_lang": None, "pack_format": None,
                    "summary": None, "resume": resume}
        jar_count = (1 if kind == "modjar"
                     else sum(1 for n in names if "/mods/" in f"/{n}" and n.endswith(".jar")))
        # 词条/源语言估算需解压 jar——轻量识别不解压，翻译时统计（summary 只给 jar 数）
        return {"kind": kind, "source_lang": None, "pack_format": None,
                "summary": {"jar_count": jar_count, "total_entries": 0,
                            "total_lang_files": 0, "total_hardcoded": None},
                "resume": resume}
    # 目录输入：直接识别（无需解压）。先下钻包裹层——用户可能直接选 zip 解压后的
    # xxxx/ 父目录（项目根在包裹层内），下钻后聚 jar/统计才基于项目根
    p = unwrap_bare_wrapper(raw)
    kind = detect_input_type(p)
    resume = _check_resume(req.path)
    if kind == "unknown":
        return {"kind": "unknown", "resume": resume}
    if kind == "map":
        return {"kind": "map", "source_lang": None, "pack_format": None, "summary": None, "resume": resume}
    # 按类型聚 jar 列表：modpack → mods/**/*.jar；modjar → 该文件本身
    all_jars = [p] if kind == "modjar" else (sorted((p / "mods").rglob("*.jar")) if (p / "mods").is_dir() else [])
    # 修复：大整合包几百个 jar 逐个读语言文件会卡死识别——检测/估算只取前 200 个
    #（jar_count 用真实总数，source_lang/summary 用截断样本估算，足够识别）
    jars = all_jars[:200]
    source_lang = detect_source_lang(jars, req.target_lang)
    pack_format = infer_pack_format(p)
    summary = build_detect_summary(jars, source_lang)
    summary["jar_count"] = len(all_jars)
    return {
        "kind": kind,
        "source_lang": source_lang,
        "pack_format": pack_format,
        "summary": summary,
        "resume": resume,
    }


@app.post("/api/scan")
def scan(req: ScanRequest):
    p = _resolve(req.path)
    scans = (scan_jar(p, req.source_lang, req.target_lang)
             if req.mode == "jar"
             else scan_modpack(p, req.source_lang, req.target_lang, req.scope))
    jobs = build_jobs(scans, req.target_lang)
    return {
        "mods": [{"modid": s.modid, "entries": len(s.source_entries), "gaps": len(build_jobs([s], req.target_lang))}
                 for s in scans],
        "total_gaps": len(jobs),
    }


async def _spawn_task(coro_factory) -> str:
    """后台任务样板（P1-9）：4 个翻译端点共用，消除 create_task/_TASKS/done_callback 重复。

    coro_factory: async (cfg, state) -> None 的协程工厂（闭包捕获 req 等端点参数）。
    返回新任务 id。注意端点需 async（FastAPI 在事件循环执行），create_task 才有 loop 可挂载。
    """
    cfg = AppConfig(CONFIG_PATH)
    state = STORE.new()
    state.status = "running"
    STORE.save(state)
    task = asyncio.create_task(coro_factory(cfg, state))

    def _on_task_done(t: asyncio.Task) -> None:
        # 修复：done_callback 持锁 pop（线程池端点同刻读 _TASKS 不再互踩）；
        # 并记录后台任务异常——否则 asyncio 打印未回收异常、任务状态停留 running
        with _tasks_lock:
            _TASKS.pop(state.id, None)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                state.status = "failed"
                state.progress.append({"status": "error", "error": f"任务异常终止：{sanitize_error(str(exc))}"})
                try:
                    STORE.save(state)
                except Exception:
                    pass

    _TASKS[state.id] = task                      # 持有引用防 GC（F2）
    task.add_done_callback(_on_task_done)
    return state.id


@app.post("/api/translate")
async def translate(req: TranslateRequest):
    return {"task_id": await _spawn_task(
        lambda cfg, state: run_translation(state.id, req, cfg, STORE, WORK_DIR, OUTPUTS_DIR))}


@app.post("/api/auto-translate")
async def auto_translate(req: AutoRequest):
    """统一全自动翻译入口：后台任务，复用 _TASKS 持有引用防 GC。"""
    return {"task_id": await _spawn_task(
        lambda cfg, state: run_auto_translation(state.id, req, cfg, STORE, WORK_DIR,
                                                OUTPUTS_DIR, cfpa_path=CFPA_PATH))}


def _task_snapshot(state) -> dict:
    """任务状态快照（get_task 与 SSE 端点共用，字段完全一致，前端零适配）。"""
    return {"id": state.id, "status": state.status, "total": state.total,
            "done": state.done, "failed": state.failed,
            "paused": state.paused, "cancelled": state.cancelled,
            "tokens_in": state.tokens_in, "tokens_out": state.tokens_out,
            "cost_estimate": state.cost_estimate,
            "stage": state.stage, "stages": state.stages,
            "display_name": state.display_name,               # 输入名（右栏标题区顶替「翻译流程」）
            "display_name_translated": state.display_name_translated,  # 完成态中文名（原名淡化）
            "created_at": state.created_at,   # 前端运行计时器（已运行 mm:ss）
            "reviewing": state.reviewing,     # 审查状态灯：True=审查中（红灯），False=完成（绿灯）
            "progress": state.progress[-500:] if isinstance(state.progress, list) else []}


# 任务 id 校验：12 位十六进制（uuid 前缀）。防路径注入（对齐 download 的 F6 校验）——
# get_task/task_stream/cancel/pause 之前都漏了，task_id 含 ../ 会越界拼路径。
_TASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _check_task_id(task_id: str) -> None:
    if not _TASK_ID_RE.match(task_id):
        raise HTTPException(404, "任务不存在")


@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    _check_task_id(task_id)
    state = STORE.load(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    return _task_snapshot(state)


@app.get("/api/task/{task_id}/stream")
async def task_stream(task_id: str):
    """SSE 推送任务状态（前后端联动更及时）：状态变更即时推送，替代前端 1s 轮询。

    连接即发当前快照（断线重连/首次订阅立即有数据）；save 时广播；15s 心跳保活；
    终态（done/failed/cancelled）后关闭流。降级：EventSource 断线自动重连。
    """
    _check_task_id(task_id)
    state = STORE.load(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    q = STORE.subscribe(task_id)

    async def gen():
        try:
            # 连接即发当前快照
            snap0 = _task_snapshot(state)
            yield f"event: state\ndata: {json.dumps(snap0, ensure_ascii=False)}\n\n"
            if snap0["status"] in ("done", "failed", "cancelled"):
                return   # 修复：订阅已终态任务（断线重连/回看历史）立即关闭，防流永久挂起+订阅泄漏
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # 心跳注释帧，防代理/WebView2 空闲断流
                    continue
                snap = _task_snapshot(evt)
                yield f"event: state\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
                if snap["status"] in ("done", "failed", "cancelled"):
                    break   # 终态后关闭流
        finally:
            STORE.unsubscribe(task_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    _check_task_id(task_id)
    state = STORE.load(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    if state.status in ("done", "failed", "cancelled"):
        return {"ok": True}      # 终态任务幂等返回，不重复标记
    state.cancelled = True
    state.paused = False   # Y4：取消立即解除暂停，避免暂停中取消无效
    STORE.pause_event(task_id).set()   # P1-4：唤醒暂停等待中的协程（即时取消不再等 0.5s）
    STORE.save(state)
    # P1-3：真正中止后台任务（协程捕获 CancelledError 置状态），否则正在飞的批请求
    # 无法中断，取消后进度仍走一整批（用户感觉「取消没反应」）
    t = _TASKS.get(task_id)
    if t and not t.done():
        t.cancel()
    return {"ok": True}


@app.post("/api/task/{task_id}/pause")
async def pause_task(task_id: str):
    _check_task_id(task_id)
    state = STORE.load(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    if state.status in ("done", "failed", "cancelled"):
        raise HTTPException(409, "任务已结束，无法暂停")
    state.paused = not state.paused
    # P1-4：暂停态清空事件（翻译协程 wait 阻塞）、继续 set 立即唤醒（即时响应不再等 0.5s 轮询）
    ev = STORE.pause_event(task_id)
    if state.paused:
        ev.clear()
    else:
        ev.set()
    STORE.save(state)
    return {"paused": state.paused}


@app.get("/api/task/{task_id}/download")
def download(task_id: str):
    # task_id 为 12 位十六进制 uuid 前缀，先校验再拼路径，防路径注入（F6）
    if not re.fullmatch(r"[0-9a-f]{12}", task_id):
        raise HTTPException(404, "任务不存在")
    # A5：产物目录优先（资源包 + hardcoded jar）→ 打包成 {task_id}.zip；否则旧单文件兼容
    # 任务 C：产物移 OUTPUTS_DIR（exe 旁 outputs/），与 temp 中间产物分离；任务后清理不影响下载
    out_dir = OUTPUTS_DIR / task_id
    if out_dir.is_dir():
        from urllib.parse import quote
        # 产物形态分流：顶层单个 jar（modjar / hardcoded）→ 直接返回该 jar（产物名一一对应）；
        # 顶层成品 zip（modpack：整合包汉化.zip）→ 直接返回（产物文件夹重构后只留成品 +
        # report.json，不再 rglob 把 zip 再包一层）；旧数据无顶层 zip 才 rglob 兜底重打包
        top_jars = sorted(out_dir.glob("*.jar"))
        if top_jars:
            return FileResponse(top_jars[0], filename=top_jars[0].name)
        top_zips = sorted(out_dir.glob("*.zip"))
        if top_zips:
            _name = top_zips[0].name
            return FileResponse(top_zips[0], media_type="application/zip",
                                headers={"Content-Disposition":
                                         f"attachment; filename=\"{quote(_name)}\"; "
                                         f"filename*=UTF-8''{quote(_name)}"})
        if any(out_dir.rglob("*.zip")):
            # 修复：产物可能数百 MB，全量进内存 BytesIO 会 OOM → 写临时文件再 FileResponse
            # 修复：文件名带 uuid（并发下载同一任务各自独立临时文件，同路径并发写会损坏 zip）
            tmp_zip = WORK_DIR / "downloads" / f"{task_id}-{uuid.uuid4().hex}.zip"
            tmp_zip.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(out_dir.rglob("*")):
                    if f.is_file():
                        zf.write(f, f.relative_to(out_dir).as_posix())
            fname = "整合包汉化.zip"
            # 修复（recheck）：临时 zip 响应完成后删除——之前每次命中此分支留一个
            # WORK_DIR/downloads/*.zip 永不清理，下载多了目录无限膨胀
            return FileResponse(tmp_zip, media_type="application/zip",
                                background=BackgroundTask(tmp_zip.unlink, missing_ok=True),
                                headers={"Content-Disposition":
                                         f"attachment; filename=\"{quote(fname)}\"; "
                                         f"filename*=UTF-8''{quote(fname)}"})
        # 空产物（审查硬性失败等）→ 明确报错，不打包空 zip
        raise HTTPException(404, "该任务审查未通过或无产物，请重新汉化")
    # M4-6：资源包任务导出 .zip，地图任务导出 .mcworld；限定两种后缀，
    # 不匹配任意 *.*（修复：同前缀非产物文件不会被误当产物返回）
    for f in sorted(list(OUTPUTS_DIR.glob(f"{task_id}_*.mcworld"))
                    + list(OUTPUTS_DIR.glob(f"{task_id}_*.zip"))):
        return FileResponse(f, filename=f.name)
    raise HTTPException(404, "尚未生成产物")


@app.post("/api/task/{task_id}/open-output")
def open_output(task_id: str):
    """打开产物文件夹（用户诉求：完成态直接打开 temp 产物目录让用户自己看，而不是选地方下载）——
    系统资源管理器弹出 outputs/<task_id>/，用户直接取用。桌面版有效。"""
    if not re.fullmatch(r"[0-9a-f]{12}", task_id):
        raise HTTPException(404, "任务不存在")
    out_dir = OUTPUTS_DIR / task_id
    if not out_dir.is_dir():
        raise HTTPException(404, "产物不存在")
    try:
        os.startfile(str(out_dir))
    except (OSError, AttributeError):
        raise HTTPException(500, "打开产物文件夹失败")
    return {"ok": True}


@app.get("/api/task/{task_id}/report")
def task_report(task_id: str):
    """翻译报告：任务完成后点「阅读翻译报告」弹窗阅读（通用所有模式：整合包/mod/地图/光影）。

    报告在任务收尾生成 outputs/<task_id>/report.json（含**全部**未翻译条目，不只前端 60 条）。
    返回结构化 JSON，前端弹窗渲染（不下载）。
    """
    _check_task_id(task_id)
    p = OUTPUTS_DIR / task_id / "report.json"
    if not p.exists():
        raise HTTPException(404, "翻译报告尚未生成")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raise HTTPException(404, "翻译报告不可用")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """拖放文件上传：流式分块落盘 work/uploads/<uuid>/<原始文件名>，返回路径供 /api/detect 消费。"""
    # 文件名只取 basename（正反斜杠都剥掉），防路径穿越逃逸出 work 目录
    name = Path((file.filename or "upload").replace("\\", "/")).name or "upload"
    # ⚪-4：净化后若落在 {"..", ".", ""} 则回退 "upload"，
    # 否则 dest_dir/".." 落到 uploads 目录 open("wb") 抛 IsADirectoryError → 500
    if name in {"..", ".", ""}:
        name = "upload"
    # 修复（recheck）：Windows 非法字符（< > : " | ? * 控制字符）与保留设备名（CON/NUL/COM1
    # 等）会导致 dest.open 抛 OSError 500——统一清洗为下划线，清洗后仍无效回退 "upload"
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip().strip(".")
    if not name or name.upper() in {"CON", "PRN", "AUX", "NUL",
                                    "COM1", "COM2", "COM3", "COM4", "COM5",
                                    "COM6", "COM7", "COM8", "COM9",
                                    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
                                    "LPT6", "LPT7", "LPT8", "LPT9"}:
        name = "upload"
    dest_dir = WORK_DIR / "uploads" / uuid.uuid4().hex
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    # 修复：磁盘写走 to_thread，避免几百 MB 上传写盘阻塞事件循环（其他请求/SSE 卡顿）；
    # 加 2GB 硬上限（超限清理已写分块，防填满磁盘）
    _MAX_UPLOAD = 2 * 1024 * 1024 * 1024
    written = 0
    with dest.open("wb") as out:          # 流式分块写盘（1MB/块），防大整合包整读进内存
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > _MAX_UPLOAD:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "文件过大（上限 2GB）")
            await asyncio.to_thread(out.write, chunk)
    return {"path": str(dest), "name": name, "size": dest.stat().st_size}


def _windows_drives() -> list[str]:
    """探测存在的盘符，如 ['C:\\', 'D:\\', ...]，供跨盘导航。"""
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(root)
    return drives


@app.get("/api/browse")
def browse(path: str = ""):
    p = Path(path) if path else Path.home()
    # 🟡-2：路径不存在/非目录时统一返回空，不回吐垃圾 parent（如 C:\/D:\ 规范化的 C:\D:）
    if not p.exists() or not p.is_dir():
        return {"parent": "", "dirs": []}
    # 🟡-3 盘根：仅起始盘根（第一个存在盘符）列出全部盘符供跨盘；
    # 其余盘根（如 D:\）直接列该盘子目录，否则用户永远进不去非起始盘
    if os.name == "nt" and p.parent == p:
        drives = _windows_drives()
        if drives and str(p) == drives[0]:
            return {"parent": "", "dirs": drives}
    try:
        dirs = sorted([d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")])
    except (PermissionError, OSError):
        # M3：无权限/读取失败目录返回空列表，不 500
        dirs = []
    return {"parent": str(p.parent) if p.parent != p else "", "dirs": dirs}


@app.post("/api/glossary")
def upload_glossary(payload: dict):
    # 修复：空 path（=当前目录）或目录路径会 read_bytes 抛 IsADirectoryError → 500；
    # 校验必须是「存在的文件」
    src = Path(payload.get("path", ""))
    if not src.is_file():
        raise HTTPException(400, "无效的术语表文件路径（需是存在的文件）")
    (WORK_DIR / "glossary.json").write_bytes(src.read_bytes())
    return {"loaded": len(load_glossary(WORK_DIR / "glossary.json"))}


@app.get("/api/cfpa/status")
def cfpa_status():
    """CFPA 社区词库状态：应用内置版本（离线可用，用户刚需）+ 在线下载的词库状态。

    内置 6 版本汉化包（1.12.2~1.21）随应用分发，翻译时**离线加载**对应版本词库，
    不再依赖在线下载（用户反馈「下载页面很久才跳出来」的根因——在线源慢/不稳）。"""
    bundled = list_bundled_versions()
    g = load_cfpa(CFPA_PATH)
    # 内置词库也算「已就绪」：应用内置 6 版本 CFPA 汉化包（离线可用），翻译自动加载，
    # 不再显示「未下载」（用户反馈：内置了却显示未下载困惑）
    return {"downloaded": g["count"] > 0 or len(bundled) > 0,
            "mc_version": g.get("mc_version", ""),
            "count": g["count"], "size_mb": g.get("size_mb", 0.0),
            "bundled": bundled, "bundled_count": len(bundled),
            "download_progress": get_cfpa_progress()}


@app.post("/api/cfpa/download")
async def cfpa_download(payload: dict):
    """下载匹配 MC 版本的 CFPA 社区词库（异步，可能耗时数十秒）。

    on_progress 写全局进度（模块级单例），前端轮询 /status 展示进度条；
    下载完成/失败后 active=False（前端自动隐藏进度条）。
    """
    mc_version = str(payload.get("mc_version", "") or "").strip()
    if not mc_version:
        raise HTTPException(400, "缺少 MC 版本号（如 1.20.1）")
    g = await download_cfpa(mc_version, CFPA_PATH, on_progress=lambda _p: None)
    if g is None:
        raise HTTPException(502, "词库下载失败（网络不可用或版本无匹配，请重试）")
    return {"downloaded": True, "mc_version": g["mc_version"],
            "count": g["count"], "size_mb": g["size_mb"]}


# GitHub 镜像代理前缀（国内可访问，官方直连失败时并发尝试，总能下到）。
# 代理的是 github.com / api.github.com / raw 路径；download URL 形如
# https://github.com/<owner>/<repo>/releases/download/<tag>/<asset> 同样可代理。
# 修复（recheck）：不同镜像对 api.github.com 的支持不一，多发几个总有能通的。
_GITHUB_PROXIES = [
    "",                              # 官方直连
    "https://ghfast.top/",           # ghfast 镜像
    "https://gh-proxy.com/",         # gh-proxy 镜像
    "https://ghproxy.net/",          # ghproxy.net 镜像
    "https://mirror.ghproxy.com/",   # mirror.ghproxy 镜像
    "https://ghps.cc/",              # ghps 镜像
    "https://gh.ddlc.top/",          # ddlc 镜像
    "https://ghproxy.cc/",           # ghproxy.cc 镜像
]


async def _github_get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    """带多镜像代理的 GET：**官方 + 全部镜像并发尝试**（gather 取第一个 200，比串行快得多），
    全失败返回 None。api 小请求并发无压力；下载大文件也并发试，取先到者。"""
    async def _one(prefix: str):
        try:
            resp = await client.get(f"{prefix}{url}", timeout=10)
            if resp.status_code == 200:
                return resp
        except Exception:
            pass
        return None

    results = await asyncio.gather(*(_one(p) for p in _GITHUB_PROXIES))
    for r in results:
        if r is not None:
            return r
    return None


async def _github_latest_jar(repo: str, asset_filter) -> tuple[str, bytes] | None:
    """GitHub latest release：拿 tag + 匹配 asset 的 jar bytes（PK 头校验防反代污染）。
    **多源**：官方直连失败自动走 ghfast/gh-proxy/ghproxy.net 镜像（修复 recheck：国内
    直连 GitHub 超时「无法连接更新源」）。全失败返回 None（网络/无匹配）。"""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await _github_get(client, f"https://api.github.com/repos/{repo}/releases/latest")
            if resp is None:
                return None
            data = resp.json()
            tag = str(data.get("tag_name", ""))
            for a in data.get("assets", []):
                name = str(a.get("name", ""))
                if name.endswith(".jar") and asset_filter(name):
                    url = a.get("browser_download_url", "")
                    if not url:
                        continue
                    dl = await _github_get(client, url)
                    if dl is not None and dl.content[:2] == b"PK":
                        return tag, dl.content
    except Exception:
        pass
    return None


def _internal_data_dir() -> Path:
    """内置资源目录（源码 backend/app/data；frozen _MEIPASS/app/data）。
    修复（recheck）：原 frozen 返回 _MEIPASS/data，而 spec 打的是 _MEIPASS/app/data/
    {cfpa,i18n,vp}——漏 app/ 段导致内置资源版本检测失效（每次「检查更新」重复下载内置 jar）。"""
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "app" / "data"
    return Path(__file__).resolve().parent / "data"


def _bundled_resource_version(subdir: str) -> str:
    """读内置 / 更新版 jar 的版本号（i18n/vp：fabric.mod.json 或 mods.toml 的 version）。
    优先应用目录更新版，否则内置。无版本元数据返回空串。"""
    import zipfile as _zf
    for base in (update_dir() / subdir, _internal_data_dir() / subdir):
        if not base.is_dir():
            continue
        for jar in sorted(base.glob("*.jar")):
            try:
                with _zf.ZipFile(jar) as zf:
                    names = zf.namelist()
                    if "fabric.mod.json" in names:
                        d = json.loads(zf.read("fabric.mod.json"))
                        return str(d.get("version") or "")
                    for t in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                        if t in names:
                            raw = zf.read(t).decode("utf-8", "replace")
                            for line in raw.splitlines():
                                if line.strip().startswith("version="):
                                    return line.split("=", 1)[1].strip()
            except Exception:
                continue
    return ""


def _normalize_version(v: str) -> str:
    """版本号归一化：去 v 前缀 / 非版本字符，取 x.y(.z) 形式；无则小写原文。"""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", str(v or ""))
    return m.group(1) if m else str(v or "").strip().lower()


async def _check_update_github(repo: str, asset_filter, subdir: str, prefix: str) -> dict:
    """GitHub release 检查更新（版本对比）：源最新 tag vs **内置版本** + 应用目录已下载版本，
    一样就不下载（用户诉求：内置就是当前版没必要重复下）。有更新下载到应用目录
    data/{subdir}/（持久，清 temp 缓存不删），没更新/没连上给明确状态可重试。"""
    found = await _github_latest_jar(repo, asset_filter)
    if not found:
        return {"ok": False, "status": "unreachable",
                "message": "无法连接更新源（网络/代理），请重试"}
    tag, data = found
    src_ver = _normalize_version(tag)
    bundled_ver = _normalize_version(_bundled_resource_version(subdir))
    d = update_dir() / subdir
    ver_file = d / "version.txt"
    local_ver = _normalize_version(
        ver_file.read_text(encoding="utf-8").strip()) if ver_file.exists() else ""
    # 版本对比：源版本 == 内置版本 或 应用目录已下载版本 → 已最新不下载
    if src_ver and (src_ver == bundled_ver or (local_ver and src_ver == local_ver)):
        display = f"内置 {bundled_ver}" if src_ver == bundled_ver else src_ver
        return {"ok": True, "status": "up_to_date", "version": tag,
                "message": f"已是最新（{display}），无需更新"}
    d.mkdir(parents=True, exist_ok=True)
    try:
        for old in d.glob("*.jar"):
            try:
                old.unlink()
            except OSError:
                pass
        (d / f"{prefix}-{tag}.jar").write_bytes(data)
        ver_file.write_text(tag, encoding="utf-8")
    except OSError:
        # 修复（recheck）：应用目录只读（便携版装在 Program Files 等）→ 写失败给明确提示，
        # 不抛 500；内置版仍可用
        return {"ok": False, "status": "error",
                "message": "应用目录不可写，无法保存更新。请将程序放到可写位置（如 D 盘）后重试"}
    return {"ok": True, "status": "updated", "version": tag,
            "message": f"已更新到 {tag}（{len(data) / 1048576:.1f}MB），存于应用目录"}


async def _check_update_cfpa(payload: dict) -> dict:
    """CFPA 词库检查更新（版本检测）：联网拉取对应 MC 版本最新词库（下载 + 建索引），
    下载后对比本地已下载版本——同版本提示「已最新」，不同版本提示「已更新」。"""
    mc = str(payload.get("mc_version", "") or "1.20.1").strip()
    try:
        # 本地已下载版本（词库索引 mc_version）
        local_ver = ""
        if CFPA_PATH.exists():
            try:
                _g = json.loads(CFPA_PATH.read_text(encoding="utf-8"))
                local_ver = str(_g.get("mc_version") or _g.get("version") or "")
            except Exception:
                pass
        g = await download_cfpa(mc, CFPA_PATH, on_progress=lambda _p: None)
        if g is None:
            return {"ok": False, "status": "unreachable", "message": "无法连接 CFPA 更新源，请重试"}
        new_ver = str(g.get("mc_version", mc))
        if local_ver == new_ver and local_ver:
            return {"ok": True, "status": "up_to_date", "version": new_ver,
                    "message": f"词库已是最新（{new_ver}）"}
        return {"ok": True, "status": "updated", "version": new_ver,
                "message": f"词库已更新（{new_ver} · {g.get('count', 0)} 词条）"}
    except Exception as e:
        return {"ok": False, "status": "unreachable",
                "message": f"无法连接 CFPA 更新源：{str(e)[:60]}"}


@app.post("/api/check-update")
async def check_update(payload: dict = None):
    """检查并更新内置资源（cfpa 词库 / i18n 汉化 mod / vp 硬编码 mod）：有更新下载到
    应用目录（持久，清 temp 缓存不删）；没更新 / 没连上给明确状态可重试。
    返回 {ok, status, message, version}，status ∈ updated/up_to_date/unreachable/error。"""
    kind = (payload or {}).get("kind", "")
    if kind == "i18n":
        return await _check_update_github("CFPAOrg/I18nUpdateMod3",
                                          lambda n: "i18n" in n.lower() or "I18nUpdateMod" in n,
                                          "i18n", "I18nUpdateMod")
    if kind == "vp":
        return await _check_update_github("3093FengMing/VaultPatcher",
                                          lambda n: "all" in n.lower(),
                                          "vp", "vault-patcher")
    if kind == "cfpa":
        return await _check_update_cfpa(payload or {})
    return {"ok": False, "message": "未知更新类型"}


@app.post("/api/map-scan")
def map_scan(req: MapScanRequest):
    """地图汉化：校验世界目录合法性并扫描可翻译词条（仅读原档，不复制）。"""
    world = Path(req.path)
    if not maps_world.validate_world(world):
        raise HTTPException(400, "不是有效的世界存档目录（缺少可加载的 level.dat）")
    entries = maps_scan.scan_world(world, req.target_lang)   # 目标中文系时不跳纯中文（简繁转换）
    # M4-mca：.mca 已支持写回（整 region 重写），全部扫描词条可写，mca_skipped 恒 0
    write_supported = {".dat", ".mca", ".json", ".mcfunction"}
    writable = [e for e in entries if Path(e["file"]).suffix.lower() in write_supported]
    return {"entries": len(writable), "mca_skipped": 0, "preview": writable[:50]}


@app.post("/api/map-translate")
async def map_translate(req: MapTranslateRequest):
    """地图汉化：调度后台翻译任务（复制→扫描→翻译→写回→mcworld），复用 _TASKS 持有引用防 GC。"""
    return {"task_id": await _spawn_task(
        lambda cfg, state: maps_flow.run_map_translation(state.id, req, cfg, STORE,
                                                         WORK_DIR, OUTPUTS_DIR))}


@app.post("/api/hardcode-scan")
def hardcode_scan(req: HardcodeRequest):
    """硬编码汉化：扫描 jar 内可翻译的字节码字符串（复制到 work 副本再扫，原 jar 只读）。"""
    import shutil
    src = Path(req.path)
    if not src.is_file() or src.suffix.lower() != ".jar":
        raise HTTPException(400, "请选择 .jar 文件")
    # M5-recheck：副本用 uuid 子目录隔离，避免同名 jar 并发互踩 + 扫描后清理防 work 目录膨胀
    dest = WORK_DIR / "hardcode" / "scan" / uuid.uuid4().hex / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    try:
        strings = scan_hardcoded_strings(dest)
    except zipfile.BadZipFile:
        # M5-recheck：假 jar（非有效 zip）报 400 而不是 500
        raise HTTPException(400, "不是有效的 jar/zip 文件")
    finally:
        shutil.rmtree(dest.parent, ignore_errors=True)
    return {"strings": strings, "count": len(strings)}


@app.post("/api/hardcode-translate")
async def hardcode_translate(req: HardcodeRequest):
    """硬编码汉化：调度后台翻译任务（复制→扫描→翻译→替换校验→输出新 jar），复用 _TASKS 持有引用防 GC。"""
    # M5-recheck：与 scan 端点对称校验，避免误选目录白白启动必失败任务
    src = Path(req.path)
    if not src.is_file() or src.suffix.lower() != ".jar":
        raise HTTPException(400, "请选择 .jar 文件")
    return {"task_id": await _spawn_task(
        lambda cfg, state: run_hardcode_translation(state.id, req, cfg, STORE,
                                                    WORK_DIR, OUTPUTS_DIR))}


# —— 前端静态服务（桌面版 uvicorn 直接 serve dist；开发期 vite dev proxy 不受影响）——
def _front_dist() -> Path:
    """定位前端 dist：frozen 后在 _MEIPASS/frontend/dist，否则项目根 frontend/dist。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", str(BASE))) / "frontend" / "dist"
    return BASE.parent / "frontend" / "dist"


FRONT_DIST = _front_dist()
if FRONT_DIST.exists() and (FRONT_DIST / "index.html").exists():
    # Minor：assets 目录缺失时跳过 mount，避免 StaticFiles 构造抛 RuntimeError 崩导入
    _assets = FRONT_DIST / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/")
    def _index():
        return FileResponse(FRONT_DIST / "index.html")

    @app.get("/{full_path:path}")
    def _spa(full_path: str):
        # vue SPA fallback：非 /api 路径回 index.html；已存在的静态文件直接返回
        # Important-2：/api（无斜杠）也必须 404，不被 fallback 吞成 200 HTML
        if full_path.split("/", 1)[0] == "api":
            raise HTTPException(404, "接口不存在")
        # Critical-1：resolve 后校验仍在 dist 内才返回文件，防 ../ 与盘符路径穿越读任意文件
        root = FRONT_DIST.resolve()
        p = (FRONT_DIST / full_path).resolve()
        if p.is_relative_to(root) and p.is_file():
            return FileResponse(p)
        return FileResponse(FRONT_DIST / "index.html")
