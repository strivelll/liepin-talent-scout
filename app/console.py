"""猎聘人才寻访 · 本地网页控制台。

**启动**：
    python -m app.console
    浏览器打开 http://127.0.0.1:8898

**设计取舍**：
不重写业务逻辑，而是把现有 CLI 脚本包成子进程调用。
理由：脚本已经在真实账号上验证过，重写等于重新引入 bug。
子进程的 stdout 逐行读进来推给前端，进度实时可见，等于把终端搬进网页。

**安全**：
- 只 bind 127.0.0.1，不对外
- 命令白名单，参数只做字符串插值到固定模板，不接受任意 shell
- 一次只允许跑一个任务（避免并发抢浏览器）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import talent as talent_lib

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
WEB_DIR = Path(__file__).resolve().parent / "web"


# ---------------------------------------------------------------- 任务模型

@dataclass
class Task:
    id: str
    kind: str
    cmd: list[str]
    status: str = "running"          # running / done / failed / stopped
    logs: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    returncode: int | None = None
    proc: subprocess.Popen | None = None


TASKS: dict[str, Task] = {}
LOCK = threading.Lock()
CURRENT: str | None = None          # 同一时刻只跑一个


def _pump(task: Task) -> None:
    """持续读子进程输出，追加到 task.logs。"""
    assert task.proc is not None
    try:
        assert task.proc.stdout is not None
        for line in task.proc.stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            with LOCK:
                task.logs.append(line)
                # 只留最近 2000 行，防止长任务吃内存
                if len(task.logs) > 2000:
                    del task.logs[:500]
        task.proc.wait()
        task.returncode = task.proc.returncode
        task.status = "done" if task.returncode == 0 else "failed"
    except Exception as exc:                      # noqa: BLE001
        with LOCK:
            task.logs.append(f"[控制台] 读取输出异常：{exc}")
        task.status = "failed"
    finally:
        task.ended_at = time.time()
        global CURRENT
        with LOCK:
            if CURRENT == task.id:
                CURRENT = None


def start_task(kind: str, argv: list[str]) -> Task:
    global CURRENT
    with LOCK:
        if CURRENT and CURRENT in TASKS and TASKS[CURRENT].status == "running":
            raise RuntimeError("已有任务在运行，请等它结束或先停止")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [PY, "-u", "-m", *argv],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    task = Task(id=uuid.uuid4().hex[:12], kind=kind, cmd=[PY, "-m", *argv], proc=proc)
    with LOCK:
        TASKS[task.id] = task
        CURRENT = task.id
    threading.Thread(target=_pump, args=(task,), daemon=True).start()
    return task


# ---------------------------------------------------------------- 命令白名单

def build_argv(action: str, params: dict[str, Any]) -> list[str]:
    """把前端参数映射成固定脚本调用。不拼接 shell 字符串，避免注入。"""
    if action == "probe":
        return ["scripts.probe_liepin", "--inspect"]

    if action == "search":
        q = str(params.get("query") or "").strip()
        if not q:
            raise ValueError("请填写搜索关键词")
        pages = max(1, min(int(params.get("pages") or 3), 10))
        return ["scripts.probe_liepin", "--search", q, "--pages", str(pages)]

    if action == "send":
        limit = max(1, min(int(params.get("limit") or 5), 20))
        dry = bool(params.get("dry_run"))
        args = ["scripts.send_liepin", "--limit", str(limit)]
        if dry:
            args.append("--dry")
        skip = str(params.get("skip") or "").strip()
        if skip:
            args += ["--skip", skip]
        names = str(params.get("names") or "").strip()
        if names:
            args += ["--names", names]
        text = str(params.get("text") or "").strip()
        if text:
            args += ["--text", text]
        posted = str(params.get("posted_job") or "").strip()
        if posted:
            args += ["--posted-job", posted]
        return args

    if action == "score":
        jd = str(params.get("jd") or "").strip()
        if len(jd) < 6:
            raise ValueError("请填写岗位要求（至少 6 个字）")
        args = ["scripts.score_candidates", "--jd", jd]
        limit = int(params.get("limit") or 0)
        if limit > 0:
            args += ["--limit", str(limit)]
        if params.get("rescore"):
            args.append("--rescore")
        return args

    if action == "hunt":
        query = str(params.get("query") or "AI").strip()
        jd = str(params.get("jd") or "").strip()
        text = str(params.get("text") or "").strip()
        posted = str(params.get("posted_job") or "").strip()
        greet = str(params.get("greet") or "").strip()
        what = str(params.get("what") or "phone,wechat")
        args = ["scripts.auto_hunt", "--query", query, "--what", what]
        if posted:
            args += ["--posted-job", posted]
        if jd:
            args += ["--jd", jd]
        if text:
            args += ["--text", text]
        if greet:
            args += ["--greet", greet]
        limit = max(1, min(int(params.get("limit") or 5), 20))
        args += ["--limit", str(limit)]
        pages = max(1, min(int(params.get("pages") or 1), 5))
        args += ["--pages", str(pages)]
        if params.get("dry"):
            args.append("--dry")
        return args

    if action == "collect_all":
        limit = max(1, min(int(params.get("limit") or 8), 20))
        args = ["scripts.collect_all", "--limit", str(limit)]
        if params.get("scan"):
            args.append("--scan")
        if params.get("redo"):
            args.append("--all")
        return args

    if action == "resume":
        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("请填写候选人姓名")
        return ["scripts.collect_resume", "--name", name]

    raise ValueError(f"未知操作：{action}")


ACTIONS = {
    "probe": "检查登录态与页面结构",
    "search": "搜索候选人（只读）",
    "score": "AI 匹配打分",
    "send": "批量沟通",
    "resume": "采集单个简历",
    "collect_all": "批量采集简历",
}


# ---------------------------------------------------------------- 应用

app = FastAPI(title="猎聘寻访控制台", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    p = WEB_DIR / "console.html"
    if not p.exists():
        return "<h1>界面文件缺失</h1><p>app/web/console.html</p>"
    return p.read_text(encoding="utf-8")


@app.get("/api/meta")
def meta() -> JSONResponse:
    """返回可用操作与当前任务，供前端初始化。"""
    cur = None
    with LOCK:
        if CURRENT and CURRENT in TASKS:
            t = TASKS[CURRENT]
            cur = {"id": t.id, "kind": t.kind, "status": t.status}
    return JSONResponse({"actions": ACTIONS, "current": cur})


@app.post("/api/run")
def run(payload: dict[str, Any]) -> JSONResponse:
    action = str(payload.get("action") or "")
    params = payload.get("params") or {}
    try:
        argv = build_argv(action, params)
        task = start_task(action, argv)
    except Exception as exc:                      # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "task_id": task.id, "cmd": " ".join(task.cmd[1:])})


@app.get("/api/task/{task_id}")
def task_state(task_id: str, offset: int = 0) -> JSONResponse:
    """取任务状态与增量日志。offset 为已读行数，前端只拉新增部分。"""
    with LOCK:
        t = TASKS.get(task_id)
        if not t:
            return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
        logs = t.logs[offset:]
        return JSONResponse({
            "ok": True,
            "id": t.id,
            "kind": t.kind,
            "status": t.status,
            "returncode": t.returncode,
            "total_lines": len(t.logs),
            "logs": logs,
            "elapsed": round((t.ended_at or time.time()) - t.started_at, 1),
        })


@app.post("/api/stop/{task_id}")
def stop(task_id: str) -> JSONResponse:
    with LOCK:
        t = TASKS.get(task_id)
    if not t or t.proc is None:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    if t.status != "running":
        return JSONResponse({"ok": True, "note": "任务已结束"})
    try:
        t.proc.terminate()
        t.status = "stopped"
    except Exception as exc:                      # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True})


@app.get("/api/health")
def health() -> JSONResponse:
    """健康检查：确认浏览器调试端口是否在。"""
    import socket

    ok = False
    try:
        with socket.create_connection(("127.0.0.1", 53471), timeout=1.5):
            ok = True
    except Exception:                             # noqa: BLE001
        ok = False
    return JSONResponse({
        "browser_connected": ok,
        "hint": "" if ok else "未检测到已登录的 Chrome（端口 53471）。请先用猎聘账号登录并保持浏览器常驻。",
    })


# ---------------------------------------------------------------- 候选人数据

CAND_DIR = ROOT / "data" / "liepin-probe"
# collected.json 是翻页采集（多页），cards.json 是单页；优先用前者
CAND_FILES = ("collected.json", "cards.json")


def _load_candidates() -> dict[str, Any]:
    """读最近一次搜索的候选人结果。

    搜索结果由 probe_liepin.py 落盘，这里只负责读出来给前端展示。
    """
    best: dict[str, Any] | None = None
    for name in CAND_FILES:
        f = CAND_DIR / name
        if not f.exists():
            continue
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            continue
        if not isinstance(rows, list) or not rows:
            continue
        mtime = f.stat().st_mtime
        if best is None or mtime > best["_mtime"]:
            best = {"_mtime": mtime, "_file": name, "rows": rows}

    if best is None:
        return {"total": 0, "rows": [], "source": None, "updated_at": None}

    rows = best["rows"]

    # 合并 AI 评分（由 scripts/score_candidates.py 生成）
    scores: dict[str, Any] = {}
    sf = CAND_DIR / "scores.json"
    if sf.exists():
        try:
            scores = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            scores = {}

    # 只保留前端要用的字段，避免把 raw 大文本传过去
    slim = []
    for r in rows:
        works = r.get("工作经历") or []
        edus = r.get("教育经历") or []
        slim.append({
            "姓名": r.get("姓名") or "",
            "年龄": r.get("年龄") or "",
            "年限": r.get("年限") or "",
            "学历": r.get("学历") or "",
            "地区": r.get("地区") or "",
            "期望": r.get("期望") or "",
            "技能": r.get("技能") or [],
            "最近单位": (works[0] if works else "")[:60],
            "最近学历": (edus[0] if edus else "")[:60],
            "_score": (scores.get(f"{r.get('姓名','')}|{r.get('年龄','')}") or {}).get("score"),
            "_verdict": (scores.get(f"{r.get('姓名','')}|{r.get('年龄','')}") or {}).get("verdict"),
            "_reason": (scores.get(f"{r.get('姓名','')}|{r.get('年龄','')}") or {}).get("reason"),
        })

    scored = sum(1 for x in slim if x.get("_score") is not None)
    return {
        "total": len(slim),
        "scored": scored,
        "rows": slim,
        "source": best["_file"],
        "updated_at": time.strftime("%H:%M:%S", time.localtime(best["_mtime"])),
    }


@app.get("/api/candidates")
def candidates() -> JSONResponse:
    return JSONResponse(_load_candidates())


# 猎聘对话框里默认选中的那条，点「不选择职位开聊」就直接发这个
ACTUAL_MESSAGE = "你好，看了你的简历，方便聊聊吗？"


def _make_greeting(city: str, idx: int) -> str:
    """和 send_liepin.py 保持一致的四种开场话术。

    刻意不引用对方信息（公司/学校/职位）——数据可能有误，引错了比不引更糟。
    """
    c = f"{city}的" if city else ""
    tail = f"，工作地点{city}" if city else ""
    v = idx % 4
    if v == 0:
        return f"你好，我这边有个{c}Java岗，看你的方向比较合适，方便聊两句吗？"
    if v == 1:
        return f"打扰了，有个Java后端的机会想推荐给你{tail}，有兴趣的话我把JD发你？"
    if v == 2:
        return f"你好，我们有个Java开发的岗位在招{tail}，想先问下你近期考虑新机会吗？"
    return f"这边有个技术岗跟你的方向挺对口，Java相关{tail}，方便简单聊下吗？"


@app.post("/api/preview")
def preview(payload: dict[str, Any]) -> JSONResponse:
    """预览将要发送的名单，不碰浏览器、不消耗额度。

    前端传 names（勾选的人）或 limit（按分数取前 N），返回谁会收到什么话术。
    """
    names = str(payload.get("names") or "").strip()
    limit = int(payload.get("limit") or 0)
    skip = {x.strip() for x in str(payload.get("skip") or "").split(",") if x.strip()}
    # 岗位城市必须由用户指定。用过候选人所在地是错的——
    # 会出现「工作地点韩国」这种离谱话术（候选人人在韩国，岗位不在）。
    job_city = str(payload.get("city") or "").strip()

    data = _load_candidates()
    rows = data.get("rows") or []

    picked: list[dict[str, Any]] = []
    if names:
        want = {x.strip() for x in names.split(",") if x.strip()}
        for r in rows:
            k = f"{r.get('姓名','')}|{r.get('年龄','')}"
            if k in want or r.get("姓名") in want:
                picked.append(r)
    else:
        picked = [r for r in rows if r.get("姓名") not in skip]

    # 勾选的人按分数降序；按 limit 取时也按分数降序
    picked.sort(key=lambda x: -(x.get("_score") if isinstance(x.get("_score"), int) else -1))
    if limit and not names:
        picked = picked[:limit]

    out = []
    for i, r in enumerate(picked, 1):
        out.append({
            "序": i,
            "姓名": r.get("姓名"),
            "年龄": r.get("年龄"),
            "分数": r.get("_score"),
            "判定": r.get("_verdict"),
            "公司": (r.get("最近单位") or "")[:30],
            "地区": r.get("地区"),
            # 预览里写的是"计划话术"，仅供确认语气
            "计划话术": _make_greeting(job_city, i),
        })

    # 实际发送流程（2026-11-05 版）：
    # ① 弹窗打招呼语 radio 选中的那条（默认第一条，可用 --greet 匹配自定义话术）
    # ② 若传了自定义话术（--text），会话建立后紧跟着再发一条
    # ③ 发完自动点「索要手机」「索要微信」
    return JSONResponse({
        "total": len(out),
        "rows": out,
        "mode": "选中的人" if names else f"分数前 {limit or '全部'}",
        "actual_message": "① 打招呼语（弹窗选中的那条，默认「你好，看了你的简历，方便聊聊吗？」）"
                          "② 自定义话术（沟通卡编辑的内容）"
                          "③ 自动索要手机 / 微信",
        "note": "发送顺序：打招呼语 → 自定义话术 → 自动索要手机/微信。对方会看到 2-3 条消息。",
    })


@app.get("/api/status")
def status() -> JSONResponse:
    """一眼看全：浏览器、最近搜索结果、当前任务。"""
    with LOCK:
        cur = None
        if CURRENT and CURRENT in TASKS:
            t = TASKS[CURRENT]
            cur = {"id": t.id, "kind": t.kind, "status": t.status}
    cand = _load_candidates()
    return JSONResponse({
        "current": cur,
        "candidates": {"total": cand["total"], "updated_at": cand["updated_at"]},
    })


# ---------------------------------------------------------------- 人才库

@app.get("/api/talent")
def talent_list(keyword: str = "", status: str = "") -> JSONResponse:
    rows = talent_lib.list_talent(keyword=keyword, status=status)
    return JSONResponse({"total": len(rows), "rows": rows, "stats": talent_lib.stats()})


@app.get("/api/talent/{tid}")
def talent_detail(tid: int) -> JSONResponse:
    row = talent_lib.get_talent(tid)
    if row is None:
        return JSONResponse({"ok": False, "error": "不存在"}, status_code=404)
    return JSONResponse({"ok": True, "row": row})


@app.post("/api/talent/{tid}")
def talent_update(tid: int, payload: dict[str, Any]) -> JSONResponse:
    fields = {}
    if "status" in payload:
        st = str(payload["status"])
        if st not in ("new", "contacted", "replied", "archived"):
            return JSONResponse({"ok": False, "error": "状态非法"}, status_code=400)
        fields["status"] = st
    if "note" in payload:
        fields["note"] = str(payload["note"])[:500]
    talent_lib.update_talent(tid, **fields)
    return JSONResponse({"ok": True})


@app.post("/api/talent/import")
def talent_import() -> JSONResponse:
    """把 data/resumes/ 里新采的简历补进库。采集完点一下就行。"""
    st = talent_lib.import_dirs()
    return JSONResponse({"ok": True, "result": st, "stats": talent_lib.stats()})


# ---------------------------------------------------------------- 设置

ENV_FILE = ROOT / ".env"


def _read_env() -> dict[str, str]:
    """读 .env 成 dict。不存在就返回空。"""
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env(data: dict[str, str]) -> None:
    """写回 .env。保留注释和其他键。"""
    lines: list[str] = []
    seen: set[str] = set()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            st = line.strip()
            if st and not st.startswith("#") and "=" in st:
                k = st.split("=", 1)[0].strip()
                if k in data:
                    lines.append(f"{k}={data[k]}")
                    seen.add(k)
                    continue
            lines.append(line)
    for k, v in data.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


SETTINGS_YAML = ROOT / "config" / "settings.yaml"


def _patch_settings(updates: dict[str, str]) -> list[str]:
    """只改 llm 段里的指定键，保留注释、缩进和其他配置。

    不用 yaml.dump 重写整个文件——那样会丢掉所有注释，
    而 settings.yaml 里的注释是给人看的说明（比如"provider 支持哪些"）。
    """
    if not SETTINGS_YAML.exists():
        return []
    lines = SETTINGS_YAML.read_text(encoding="utf-8").split("\n")
    changed: list[str] = []
    in_llm = False
    for i, ln in enumerate(lines):
        if re.match(r"^llm:\s*$", ln):
            in_llm = True
            continue
        if in_llm and ln and not ln.startswith((" ", "\t")):
            in_llm = False
        if not in_llm:
            continue
        for k, v in updates.items():
            m = re.match(rf"^(\s+){k}:\s*(.*)$", ln)
            if m:
                indent = m.group(1)
                lines[i] = f'{indent}{k}: "{v}"'
                changed.append(k)
    if changed:
        SETTINGS_YAML.write_text("\n".join(lines), encoding="utf-8")
    return changed


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "****"
    return key[:6] + "…" + key[-4:]


@app.get("/api/config")
def get_config() -> JSONResponse:
    """返回模型配置。Key 脱敏，绝不回传明文。"""
    env = _read_env()
    key = os.environ.get("LLM_API_KEY", "") or env.get("LLM_API_KEY", "")
    from .config import config as cfg
    llm_cfg = cfg.settings.get("llm", {}) or {}
    return JSONResponse({
        "has_key": bool(key),
        "key_masked": _mask(key),
        "model": llm_cfg.get("model", ""),
        "base_url": llm_cfg.get("base_url", ""),
        "env_path": str(ENV_FILE),
        "available": bool(key),
    })


@app.post("/api/config")
def set_config(payload: dict[str, Any]) -> JSONResponse:
    """保存配置。Key 写 .env，模型和接口写 settings.yaml，保存后立即重载。"""
    key = str(payload.get("api_key") or "").strip()
    model = str(payload.get("model") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()

    did: list[str] = []

    # 1) Key：留空表示不改
    if key:
        if not key.startswith("sk-"):
            return JSONResponse({"ok": False, "error": "看起来不像模型 Key（通常以 sk- 开头）"}, status_code=400)
        data = _read_env()
        data["LLM_API_KEY"] = key
        _write_env(data)
        os.environ["LLM_API_KEY"] = key
        did.append("Key")

    # 2) 模型与接口：写 settings.yaml
    updates: dict[str, str] = {}
    if model:
        updates["model"] = model
    if base_url:
        if not base_url.startswith(("http://", "https://")):
            return JSONResponse({"ok": False, "error": "接口地址要以 http:// 或 https:// 开头"}, status_code=400)
        updates["base_url"] = base_url
    if updates:
        changed = _patch_settings(updates)
        if not changed:
            return JSONResponse({"ok": False, "error": "没找到 settings.yaml 的 llm 段，无法写入"}, status_code=500)
        did.extend(changed)

    if not did:
        return JSONResponse({"ok": False, "error": "没有任何改动"}, status_code=400)

    # 3) 重载配置 + 重建模型客户端
    #    踩过的坑：只设 os.environ 不够——llm 客户端在进程启动时（Key 为空）就建好了，
    #    不调 reload() 的话 _client 永远是 None，测试连接永远报"没有可用 Key"。
    from .config import config as cfg
    cfg.reload()
    from .llm import llm
    llm.reload()

    env = _read_env()
    k = os.environ.get("LLM_API_KEY", "") or env.get("LLM_API_KEY", "")
    llm_cfg = cfg.settings.get("llm", {}) or {}
    return JSONResponse({
        "ok": True,
        "changed": did,
        "key_masked": _mask(k),
        "model": llm_cfg.get("model", ""),
        "base_url": llm_cfg.get("base_url", ""),
        "env_path": str(ENV_FILE),
    })


@app.post("/api/config/test")
def test_config() -> JSONResponse:
    """真调一次模型，确认 Key 和网络都通。"""
    from .llm import llm
    llm.reload()          # 用最新的 Key 重建客户端，别拿启动时的旧状态判断
    if not llm.available:
        return JSONResponse({"ok": False, "error": "没有可用 Key"}, status_code=400)
    try:
        r = llm._chat("你是助手", "只回复两个字：收到")
        txt = (r.text or "").strip()[:20]
        return JSONResponse({"ok": True, "reply": txt, "model": "ok"})
    except Exception as exc:                      # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)[:150]}, status_code=500)


def _load_env_into_process() -> None:
    """把 .env 里的键注入当前进程的环境变量。

    不这么做的话，界面配了 Key 也白搭——llm 只认 os.environ。
    （踩过：设置页显示"已配置"，但点测试连接报"没有可用 Key"。）
    """
    for k, v in _read_env().items():
        if v and not os.environ.get(k):
            os.environ[k] = v


# ---------------------------------------------------------------- 在发布岗位

JOBS_CACHE = ROOT / "data" / "posted_jobs.json"
URL_SEARCH_PAGE = "https://h.liepin.com/search/getConditionItem"
CDP_PORT = 53471


def _fetch_posted_jobs() -> list[dict[str, str]]:
    """连浏览器抓搜索页「正在发布的职位」全部 chips（先展开折叠区）。"""
    from patchright.sync_api import sync_playwright
    from scripts.send_liepin import wait_job_chips

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = b.contexts[0]
        pg = next((x for x in ctx.pages if "h.liepin.com" in (x.url or "")), ctx.new_page())
        pg.goto(URL_SEARCH_PAGE, wait_until="domcontentloaded", timeout=45000)
        time.sleep(7)
        # 全部 chips 本来就在 DOM 里（折叠只是 CSS overflow:hidden），按文本抓即可
        chips = [str(c.get("text") or "") for c in wait_job_chips(pg)]

    # 完整文本做去重键：旧版 c[:40] 会把 527 字外层容器和首个 chip 误合并
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for c in chips:
        if c in seen:
            continue
        seen.add(c)
        m_city = re.search(r"\|\s*([^-|]+(?:-[^|]+)?)\s*\|", c)
        city = m_city.group(1).strip() if m_city else c[:10]
        m_name = re.match(r"([^（(]{2,20})", c)
        name = m_name.group(1).strip() if m_name else city
        m_salary = re.search(r"(\d+k-\d+k(?:·\d+薪)?)", c)
        out.append({"label": c[:70], "key": c, "name": name, "city": city,
                    "salary": m_salary.group(1) if m_salary else ""})
    return out


def _normalize_jobs(raw: list) -> list[dict[str, str]]:
    """兼容两种缓存格式：纯字符串（旧）/ {label,key,name,city,salary}（新）。

    key 是 chip 的完整文本，作为下拉 option.value —— 同城多个岗位靠它区分。
    """
    out: list[dict[str, str]] = []
    for c in raw:
        if isinstance(c, dict):
            d = dict(c)
            d.setdefault("key", str(d.get("label") or ""))
            d.setdefault("name", "")
            d.setdefault("city", "")
            d.setdefault("salary", "")
            out.append(d)
            continue
        m_city = re.search(r"\|\s*([^-|]+(?:-[^|]+)?)\s*\|", c)
        city = m_city.group(1).strip() if m_city else c[:10]
        m_name = re.match(r"([^（(]{2,20})", c)
        name = m_name.group(1).strip() if m_name else city
        m_salary = re.search(r"(\d+k-\d+k(?:·\d+薪)?)", c)
        out.append({"label": c[:70], "key": c, "name": name, "city": city,
                    "salary": m_salary.group(1) if m_salary else ""})
    return out


@app.get("/api/jobs")
def jobs_list(refresh: int = 0) -> JSONResponse:
    """在发布岗位列表。默认用缓存，refresh=1 连浏览器重抓。"""
    if not refresh and JOBS_CACHE.exists():
        try:
            jobs = _normalize_jobs(json.loads(JOBS_CACHE.read_text(encoding="utf-8")))
            if jobs:
                return JSONResponse({"ok": True, "jobs": jobs, "cached": True})
        except Exception:                     # noqa: BLE001
            pass
    try:
        jobs = _normalize_jobs(_fetch_posted_jobs())
    except Exception as exc:                  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"抓取失败：{str(exc)[:80]}"}, status_code=500)
    JOBS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    JOBS_CACHE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, "jobs": jobs, "cached": False})


@app.get("/api/hunt/progress")
def hunt_progress() -> JSONResponse:
    """一键寻访的实时阶段进度（auto_hunt 每阶段落盘 progress.json）。"""
    pf = ROOT / "data" / "hunt" / "progress.json"
    if not pf.exists():
        return JSONResponse({"ok": False, "error": "没有进行中的寻访"})
    try:
        d = json.loads(pf.read_text(encoding="utf-8"))
        return JSONResponse({"ok": True, **d})
    except Exception as exc:                      # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)[:80]}, status_code=500)


@app.get("/api/hunt/latest")
def hunt_latest() -> JSONResponse:
    """最近一次一键寻访的结果。"""
    hunt_dir = ROOT / "data" / "hunt"
    files = sorted(hunt_dir.glob("hunt_*.json")) if hunt_dir.exists() else []
    if not files:
        return JSONResponse({"ok": False, "error": "还没有寻访记录"})
    try:
        d = json.loads(files[-1].read_text(encoding="utf-8"))
        return JSONResponse({"ok": True, "time": d.get("time"), "dry": d.get("dry"),
                             "query": d.get("query"), "posted_job": d.get("posted_job"),
                             "results": d.get("results", [])})
    except Exception as exc:                      # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)[:80]}, status_code=500)


def main() -> None:
    _load_env_into_process()
    print("=" * 56)
    print("  猎聘人才寻访 · 本地控制台")
    print("  浏览器打开：http://127.0.0.1:8898")
    print("  只监听本机，不对外暴露")
    print("=" * 56)
    uvicorn.run(app, host="127.0.0.1", port=8898, log_level="warning")


if __name__ == "__main__":
    main()
