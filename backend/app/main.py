# FastAPI 入口（任务 13）：扫描/翻译/任务/浏览/术语表路由，串起 M0-M2 全部模块
import asyncio
import re
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.archive import is_archive, extract_modpack
from app.config import AppConfig
from app.diff import build_jobs
from app.glossary import load_glossary
from app.hardcode import scan_hardcoded_strings
from app.hardcode_flow import run_hardcode_translation
from app.maps import flow as maps_flow, scan as maps_scan, world as maps_world
from app.models import HardcodeRequest, MapScanRequest, MapTranslateRequest, ScanRequest, TranslateRequest
from app.scanner import scan_modpack, scan_jar
from app.tasks import TaskStore
from app.translator import run_translation

app = FastAPI(title="MC 自动翻译器")
BASE = Path(__file__).resolve().parent.parent          # backend/
CONFIG_PATH = BASE / "config.json"
WORK_DIR = BASE / "work"
STORE = TaskStore(WORK_DIR / "tasks")
_TASKS: dict[str, asyncio.Task] = {}    # 保存后台任务引用，防止被 GC 回收（F2）

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _resolve(path_str: str) -> Path:
    """整合包输入：目录或压缩包。扫描用压缩包解压到 work/extracted/scan，独立于任务解压目录（F4）。"""
    import shutil
    p = Path(path_str)
    if is_archive(p):
        dest = WORK_DIR / "extracted" / "scan"
        shutil.rmtree(dest, ignore_errors=True)   # M3：解压前清残留，防旧文件混入
        p = extract_modpack(p, dest)
    return p


@app.get("/api/config")
def get_config():
    return AppConfig(CONFIG_PATH).data


@app.post("/api/config")
def post_config(payload: dict):
    cfg = AppConfig(CONFIG_PATH)
    for k, v in payload.items():
        if k == "api_key":          # 禁止经 config 接口落盘 key
            continue
        cfg.set(k, v)
    cfg.save()
    return cfg.data


@app.post("/api/key")
def set_key(payload: dict):
    """R1：API Key 写入系统 keyring，与 create_engine 读 keyring 打通链路。绝不落盘。"""
    api_key = str(payload.get("api_key", "")).strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")
    import keyring
    keyring.set_password(AppConfig(CONFIG_PATH).get("api_key_ref", "mc-translator"), "api_key", api_key)
    return {"ok": True}


@app.post("/api/scan")
def scan(req: ScanRequest):
    p = _resolve(req.path)
    scans = (scan_jar(p, req.source_lang, req.target_lang)
             if req.mode == "jar"
             else scan_modpack(p, req.source_lang, req.target_lang, req.scope))
    jobs = build_jobs(scans)
    return {
        "mods": [{"modid": s.modid, "entries": len(s.source_entries), "gaps": len(build_jobs([s]))}
                 for s in scans],
        "total_gaps": len(jobs),
    }


@app.post("/api/translate")
async def translate(req: TranslateRequest):
    # 注意：端点需 async（FastAPI 在事件循环执行），create_task 才有 running loop 可挂载
    cfg = AppConfig(CONFIG_PATH)
    state = STORE.new()
    state.status = "running"
    STORE.save(state)
    task = asyncio.create_task(run_translation(state.id, req, cfg, STORE, WORK_DIR))
    _TASKS[state.id] = task
    task.add_done_callback(lambda t: _TASKS.pop(state.id, None))
    return {"task_id": state.id}


@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    state = STORE.load(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    return {"id": state.id, "status": state.status, "total": state.total,
            "done": state.done, "failed": state.failed,
            "paused": state.paused, "cancelled": state.cancelled,
            "tokens_in": state.tokens_in, "tokens_out": state.tokens_out,
            "progress": state.progress[-200:]}


@app.post("/api/task/{task_id}/cancel")
def cancel_task(task_id: str):
    state = STORE.load(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    state.cancelled = True
    state.paused = False   # Y4：取消立即解除暂停，避免暂停中取消无效
    STORE.save(state)
    return {"ok": True}


@app.post("/api/task/{task_id}/pause")
def pause_task(task_id: str):
    state = STORE.load(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    state.paused = not state.paused
    STORE.save(state)
    return {"paused": state.paused}


@app.get("/api/task/{task_id}/download")
def download(task_id: str):
    # task_id 为 12 位十六进制 uuid 前缀，先校验再拼路径，防路径注入（F6）
    if not re.fullmatch(r"[0-9a-f]{12}", task_id):
        raise HTTPException(404, "任务不存在")
    # M4-6：资源包任务导出 .zip，地图任务导出 .mcworld，两者都匹配，避免地图产物 404
    for f in (WORK_DIR / "outputs").glob(f"{task_id}_*.*"):
        return FileResponse(f, filename=f.name)
    raise HTTPException(404, "尚未生成资源包")


@app.get("/api/browse")
def browse(path: str = ""):
    p = Path(path) if path else Path.home()
    if not p.exists() or not p.is_dir():
        return {"parent": str(p.parent), "dirs": []}
    try:
        dirs = sorted([d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")])
    except (PermissionError, OSError):
        # M3：无权限/读取失败目录返回空列表，不 500
        dirs = []
    return {"parent": str(p.parent) if p.parent != p else "", "dirs": dirs}


@app.post("/api/glossary")
def upload_glossary(payload: dict):
    src = Path(payload.get("path", ""))
    if not src.exists():
        raise HTTPException(404, "术语表文件不存在")
    (WORK_DIR / "glossary.json").write_bytes(src.read_bytes())
    return {"loaded": len(load_glossary(WORK_DIR / "glossary.json"))}


@app.post("/api/map-scan")
def map_scan(req: MapScanRequest):
    """地图汉化：校验世界目录合法性并扫描可翻译词条（仅读原档，不复制）。"""
    world = Path(req.path)
    if not maps_world.validate_world(world):
        raise HTTPException(400, "不是有效的世界存档目录（缺少可加载的 level.dat）")
    entries = maps_scan.scan_world(world)
    # M4-recheck：按后缀区分可写回词条与 .mca 区块词条，前端数字才诚实
    write_supported = {".dat", ".json", ".mcfunction"}
    mca_skipped = sum(1 for e in entries if Path(e["file"]).suffix.lower() == ".mca")
    writable = [e for e in entries if Path(e["file"]).suffix.lower() in write_supported]
    return {"entries": len(writable), "mca_skipped": mca_skipped, "preview": writable[:50]}


@app.post("/api/map-translate")
async def map_translate(req: MapTranslateRequest):
    """地图汉化：调度后台翻译任务（复制→扫描→翻译→写回→mcworld），复用 _TASKS 持有引用防 GC。"""
    cfg = AppConfig(CONFIG_PATH)
    state = STORE.new()
    state.status = "running"
    STORE.save(state)
    task = asyncio.create_task(maps_flow.run_map_translation(state.id, req, cfg, STORE, WORK_DIR))
    _TASKS[state.id] = task
    task.add_done_callback(lambda t: _TASKS.pop(state.id, None))
    return {"task_id": state.id}


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
    cfg = AppConfig(CONFIG_PATH)
    state = STORE.new()
    state.status = "running"
    STORE.save(state)
    task = asyncio.create_task(run_hardcode_translation(state.id, req, cfg, STORE, WORK_DIR))
    _TASKS[state.id] = task
    task.add_done_callback(lambda t: _TASKS.pop(state.id, None))
    return {"task_id": state.id}
