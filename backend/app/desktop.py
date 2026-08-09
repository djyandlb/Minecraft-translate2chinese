"""桌面壳：子线程起 uvicorn，pywebview 窗口加载本地前端。打包入口。

pywebview 延迟 import（放在 main() 内）：未安装时不影响其余代码与测试。
"""
import socket
import threading
from typing import Optional


def _free_port() -> int:
    """找一个空闲端口（绑定 127.0.0.1:0 由系统分配）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(port: int) -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    """启动后台 API 服务 + 桌面窗口。"""
    port = _free_port()
    threading.Thread(target=_run_server, args=(port,), daemon=True).start()
    import webview  # 延迟导入：未装 pywebview 时不影响其余代码
    webview.create_window("MC 自动翻译器", f"http://127.0.0.1:{port}",
                          width=1150, height=780, min_size=(900, 640))
    webview.start()


if __name__ == "__main__":
    main()
