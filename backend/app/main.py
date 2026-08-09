# FastAPI 入口（任务 13）：扫描/翻译/任务/浏览/术语表路由，串起 M0-M2 全部模块
import asyncio
import os
import re
import string
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

import httpx  # /api/test-connection 连接检测用（任务 O1）

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.archive import is_archive, extract_modpack
from app.auto_flow import run_auto_translation
from app.config import AppConfig
from app.detect import build_detect_summary, detect_input_type, detect_source_lang, infer_pack_format
from app.diff import build_jobs
from app.glossary import load_glossary
from app.hardcode import scan_hardcoded_strings
from app.hardcode_flow import run_hardcode_translation
from app.maps import flow as maps_flow, scan as maps_scan, world as maps_world
from app.models import (AutoRequest, DetectRequest, HardcodeRequest, MapScanRequest,
                        MapTranslateRequest, ScanRequest, TranslateRequest)
from app.scanner import scan_modpack, scan_jar
from app.tasks import TaskStore
from app.translator import run_translation

def _base() -> Path:
    """定位可写工作目录（backend/）：
    PyInstaller frozen 后 __file__ 指向只读 _MEIPASS，必须改用 exe 同目录（config.json/work 才能落盘）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


app = FastAPI(title="MC 自动翻译器")
BASE = _base()                                        # backend/（frozen 后为 exe 同目录）
CONFIG_PATH = BASE / "config.json"                    # config 保持 exe 旁（用户可见可改）
OUTPUTS_DIR = BASE / "outputs"                        # 产物：exe 旁 outputs/（download 从这里读）
WORK_DIR = Path(tempfile.gettempdir()) / "mc-translator"   # 中间产物：系统 temp，任务终态后清理
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


@app.post("/api/test-connection")
def test_connection(payload: dict = None):
    """用当前配置验证翻译引擎连通性（任务 O1）。
    - LLM：httpx 发最小 chat 请求（1 token，10s 超时）；
    - machine：deep_translator 翻译一个词验证 Google 通道可达。
    只返回 {"ok": bool, "message": str}，绝不泄露 api_key 与请求细节。"""
    payload = payload or {}
    cfg = AppConfig(CONFIG_PATH)
    engine = payload.get("engine") or cfg.get("engine", "llm")

    # 机翻分支：免费 Google 通道翻译一个词
    if engine != "llm":
        import deep_translator
        try:
            deep_translator.GoogleTranslator(source="auto", target="zh-CN").translate("test")
        except Exception:
            return {"ok": False, "message": "机翻服务不可用（可能需要网络代理）"}
        return {"ok": True, "message": "连接成功"}

    # LLM 分支：复用 create_engine 的厂商智能默认 + config 覆盖 + keyring 取 key
    from app.translate.providers import smart_defaults
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
        return {"ok": False, "message": "API Key 无效或无权限"}
    if resp.status_code in (400, 404, 422):
        # 用户反馈：填 /anthropic、platform.deepseek.com 等连不上——给明确地址诊断
        return {"ok": False, "message": "地址或接口格式不对：OpenAI 兼容 base_url 应为 https://api.deepseek.com 这类（不要带 /anthropic、/v1 等后缀，也不要填网页平台地址）"}
    return {"ok": False, "message": f"HTTP {resp.status_code}"}


@app.post("/api/detect")
def detect(req: DetectRequest):
    """自动识别输入类型 + 源语言 + pack_format。识别失败返回 kind=unknown 供前端提示。

    压缩包先 _resolve 解压成目录再判断；map 不适用源语言/版本推断（返回 None）。
    """
    p = _resolve(req.path)
    kind = detect_input_type(p)
    if kind == "unknown":
        return {"kind": "unknown"}
    if kind == "map":
        return {"kind": "map", "source_lang": None, "pack_format": None, "summary": None}
    # 按类型聚 jar 列表：modpack → mods/**/*.jar；modjar → 该文件本身
    jars = [p] if kind == "modjar" else (sorted((p / "mods").rglob("*.jar")) if (p / "mods").is_dir() else [])
    source_lang = detect_source_lang(jars, req.target_lang)
    pack_format = infer_pack_format(p)
    return {
        "kind": kind,
        "source_lang": source_lang,
        "pack_format": pack_format,
        "summary": build_detect_summary(jars, source_lang),
    }


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
    task = asyncio.create_task(run_translation(state.id, req, cfg, STORE, WORK_DIR, OUTPUTS_DIR))
    _TASKS[state.id] = task
    task.add_done_callback(lambda t: _TASKS.pop(state.id, None))
    return {"task_id": state.id}


@app.post("/api/auto-translate")
async def auto_translate(req: AutoRequest):
    """统一全自动翻译入口：后台任务，复用 _TASKS 持有引用防 GC。"""
    cfg = AppConfig(CONFIG_PATH)
    state = STORE.new()
    state.status = "running"
    STORE.save(state)
    task = asyncio.create_task(run_auto_translation(state.id, req, cfg, STORE, WORK_DIR, OUTPUTS_DIR))
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
    # A5：产物目录优先（资源包 + hardcoded jar）→ 打包成 {task_id}.zip；否则旧单文件兼容
    # 任务 C：产物移 OUTPUTS_DIR（exe 旁 outputs/），与 temp 中间产物分离；任务后清理不影响下载
    out_dir = OUTPUTS_DIR / task_id
    if out_dir.is_dir():
        # 产物形态分流（A6）：modjar 顶层单个汉化 jar（无资源包 zip）→ 直接返回该 jar 文件
        # （Content-Disposition 用汉化文件名）；modpack 目录（资源包 zip + hardcoded/）→ 打包总 zip
        top_jars = sorted(out_dir.glob("*.jar"))
        if top_jars and not any(out_dir.glob("*_*.zip")):
            return FileResponse(top_jars[0], filename=top_jars[0].name)
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(out_dir.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(out_dir).as_posix())
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{task_id}.zip"'})
    # M4-6：资源包任务导出 .zip，地图任务导出 .mcworld，两者都匹配，避免地图产物 404
    for f in OUTPUTS_DIR.glob(f"{task_id}_*.*"):
        return FileResponse(f, filename=f.name)
    raise HTTPException(404, "尚未生成产物")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """拖放文件上传：流式分块落盘 work/uploads/<uuid>/<原始文件名>，返回路径供 /api/detect 消费。"""
    # 文件名只取 basename（正反斜杠都剥掉），防路径穿越逃逸出 work 目录
    name = Path((file.filename or "upload").replace("\\", "/")).name or "upload"
    # ⚪-4：净化后若落在 {"..", ".", ""} 则回退 "upload"，
    # 否则 dest_dir/".." 落到 uploads 目录 open("wb") 抛 IsADirectoryError → 500
    if name in {"..", ".", ""}:
        name = "upload"
    dest_dir = WORK_DIR / "uploads" / uuid.uuid4().hex
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    with dest.open("wb") as out:          # 流式分块写盘（1MB/块），防大整合包整读进内存
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
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
    task = asyncio.create_task(maps_flow.run_map_translation(state.id, req, cfg, STORE, WORK_DIR, OUTPUTS_DIR))
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
    task = asyncio.create_task(run_hardcode_translation(state.id, req, cfg, STORE, WORK_DIR, OUTPUTS_DIR))
    _TASKS[state.id] = task
    task.add_done_callback(lambda t: _TASKS.pop(state.id, None))
    return {"task_id": state.id}


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
