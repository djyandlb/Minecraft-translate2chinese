"""桌面壳：子线程起 uvicorn，pywebview 窗口加载本地前端。打包入口。

pywebview 延迟 import（放在 main() 内）：未安装时不影响其余代码与测试。
"""
import socket
import threading
import time


def _free_port() -> int:
    """找一个空闲端口（绑定 127.0.0.1:0 由系统分配）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(port: int) -> None:
    import uvicorn
    from app.main import app   # 对象导入：PyInstaller 静态分析可收集整个 app 包
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _wait_port(port: int, timeout: float = 10.0) -> None:
    """M6-1 Important-3：阻塞等待 uvicorn 就绪，防 webview 抢先加载白屏。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.2)


# 占位页：onefile 冷启动需解压运行环境（可达 30~60 秒），
# 窗口先显示启动提示，服务器就绪后再切真实前端地址，根治「拒绝连接」
PLACEHOLDER_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body { font-family: "Microsoft YaHei", sans-serif; margin: 0; height: 100vh;
       display: flex; align-items: center; justify-content: center;
       background: #0f1720; color: #cfe3d8; }
.box { text-align: center; }
h2 { color: #58e6a0; }
p { color: #7d95a8; font-size: 14px; }
.spin { display: inline-block; width: 16px; height: 16px; margin-right: 8px;
        border: 3px solid #2a3a45; border-top-color: #58e6a0; border-radius: 50%;
        animation: r 1s linear infinite; vertical-align: middle; }
@keyframes r { to { transform: rotate(360deg); } }
</style></head><body><div class="box">
<h2><span class="spin"></span>MC 自动翻译器 正在启动…</h2>
<p>首次启动需解压运行环境（约 30~60 秒），请稍候</p>
</div></body></html>"""


def _wait_and_load(window, port: int, timeout: float = 120.0) -> None:
    """阻塞等服务器就绪（onefile 冷启动可达 40s+），就绪后把窗口切到真实前端地址。"""
    _wait_port(port, timeout=timeout)
    try:
        window.load_url(f"http://127.0.0.1:{port}")
    except Exception:
        # 极端超时：占位页已给出提示，前端 api 封装也会兜底报「无法连接后端」
        pass


def main() -> None:
    """启动后台 API 服务 + 桌面窗口。"""
    port = _free_port()
    threading.Thread(target=_run_server, args=(port,), daemon=True).start()
    import webview  # 延迟导入：未装 pywebview 时不影响其余代码
    # 先加载占位页（防服务器未就绪时前端「拒绝连接」），就绪后切真实 URL
    window = webview.create_window("MC 自动翻译器", html=PLACEHOLDER_HTML,
                                   width=1150, height=780, min_size=(900, 640))
    threading.Thread(target=_wait_and_load, args=(window, port), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
