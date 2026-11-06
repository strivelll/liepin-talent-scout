"""服务入口。

启动方式：
    python -m app.main
或：
    uvicorn app.main:app --host 127.0.0.1 --port 8899

启动后浏览器打开 http://127.0.0.1:8899 即为管理台。
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .api.seeker_routes import router as seeker_router
from .config import config
from .core import pipeline
from .db import init_db, log_event

WEB_DIR = Path(__file__).resolve().parent / "web"
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _setup_console() -> None:
    """Windows 控制台默认 GBK，Python 输出 UTF-8 会乱码。

    这里统一转成 GBK，与 bat 里的中文保持一致；
    重定向到文件或管道时（非 tty）保持原样，避免影响日志采集。
    """
    import sys as _s

    if _s.platform != "win32":
        return
    for stream in (_s.stdout, _s.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(encoding="gbk", errors="replace")
        except Exception:
            pass


def _scheduler_loop() -> None:
    """后台调度循环。

    间隔固定 25 秒，但每轮内部的动作之间有随机停顿（见 pacing 配置）。
    不用 cron 表达式，是因为需要"一轮内连续处理多个动作"的语义。
    """
    interval = 25
    while not _stop_event.is_set():
        try:
            pipeline.tick()
        except Exception as exc:  # 单轮异常不能把调度线程带崩
            log_event("error", "system", f"调度轮次异常：{exc}")
        _stop_event.wait(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _thread
    init_db()
    if not config.path("log_dir", "data/logs").exists():
        config.path("log_dir", "data/logs").mkdir(parents=True, exist_ok=True)
    log_event("info", "system",
              f"服务启动，模式={config.mode}，岗位={len(config.active_jobs())} 个")
    _stop_event.clear()
    _thread = threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True)
    _thread.start()
    yield
    _stop_event.set()
    pipeline.reset_driver()
    log_event("info", "system", "服务停止")


app = FastAPI(title="AI 招聘助手 MVP", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(seeker_router)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    page = WEB_DIR / "index.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"msg": "管理台未找到，请检查 app/web/index.html"})


@app.get("/demo")
def demo_page():
    """演示页：给客户看的，与管理台分开，避免被工程细节干扰。"""
    page = WEB_DIR / "demo.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"msg": "demo.html 未找到"})


@app.get("/seeker")
def seeker_page():
    """求职端界面。"""
    page = WEB_DIR / "seeker.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"msg": "seeker.html 未找到"})


@app.get("/health")
def health():
    return {"ok": True, "mode": config.mode}


def main() -> None:
    _setup_console()
    app_cfg = config.settings.get("app", {}) or {}
    host = str(app_cfg.get("host", "127.0.0.1"))
    port = int(app_cfg.get("port", 8899))
    print(f"\n  管理台地址： http://{host}:{port}\n  运行模式：   {config.mode}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
