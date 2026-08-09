# FastAPI 入口（任务 13）：扫描/翻译/任务/浏览/术语表路由，串起 M0-M2 全部模块
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.archive import is_archive, extract_modpack
from app.config import AppConfig
from app.diff import build_jobs
from app.glossary import load_glossary
from app.models import ScanRequest, TranslateRequest
from app.scanner import scan_modpack, scan_jar
from app.tasks import TaskStore
from app.translator import run_translation

app = FastAPI(title="MC 自动翻译器")
BASE = Path(__file__).resolve().parent.parent          # backend/
CONFIG_PATH = BASE / "config.json"
WORK_DIR = BASE / "work"
STORE = TaskStore(WORK_DIR / "tasks")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _resolve(path_str: str) -> Path:
    """整合包输入：目录或压缩包。压缩包解压到 work/extracted。"""
    p = Path(path_str)
    if is_archive(p):
        p = extract_modpack(p, WORK_DIR / "extracted")
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
    asyncio.create_task(run_translation(state.id, req, cfg, STORE, WORK_DIR))
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
    for f in (WORK_DIR / "outputs").glob(f"{task_id}_*.zip"):
        return FileResponse(f, filename=f.name)
    raise HTTPException(404, "尚未生成资源包")


@app.get("/api/browse")
def browse(path: str = ""):
    p = Path(path) if path else Path("C:/")
    if not p.exists() or not p.is_dir():
        return {"parent": str(p.parent), "dirs": []}
    dirs = sorted([d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")])
    return {"parent": str(p.parent) if p.parent != p else "", "dirs": dirs}


@app.post("/api/glossary")
def upload_glossary(payload: dict):
    src = Path(payload.get("path", ""))
    if not src.exists():
        raise HTTPException(404, "术语表文件不存在")
    (WORK_DIR / "glossary.json").write_bytes(src.read_bytes())
    return {"loaded": len(load_glossary(WORK_DIR / "glossary.json"))}
