"""依赖安装器：多源自动降级。

为什么不用一行 pip 命令写在 bat 里：

1. 国内镜像质量不稳定。实测同一个 venv，清华源返回 "from versions: none"，
   阿里云正常。写死一个源，客户网络环境不同就会卡住。
2. 需要区分"装失败"和"装成功但缺包"，分别给不同提示。
3. 需要把尝试过程打出来，否则用户只看到一片红字不知道发生了什么。

策略：按顺序试镜像，第一个成功的就用它装完。

用法（由 setup.bat 调用）：
    .venv\\Scripts\\python.exe scripts\\install_deps.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 顺序有讲究：阿里云实测最稳，清华作备份，官方源兜底（慢但不挑网络）
MIRRORS: list[tuple[str, str | None]] = [
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple/"),
    ("清华", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("官方 PyPI", None),
]

REQ_FILES = ["requirements-real.txt"]
VERIFY_IMPORTS = ["fastapi", "uvicorn", "yaml", "openai", "patchright"]


def _setup_stdout() -> None:
    """Windows 控制台是 GBK，Python 默认 UTF-8 输出会乱码。"""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="gbk", errors="replace")
            sys.stderr.reconfigure(encoding="gbk", errors="replace")
        except Exception:
            pass


def run_pip(mirror: str | None) -> int:
    cmd = [sys.executable, "-m", "pip", "install", "-r", REQ_FILES[0]]
    if mirror:
        cmd += ["-i", mirror, "--timeout", "60"]
    else:
        cmd += ["--timeout", "60"]
    proc = subprocess.run(cmd, cwd=str(ROOT))
    return proc.returncode


def verify() -> tuple[bool, list[str]]:
    """逐个 import 验证，缺哪个报哪个。"""
    code = (
        "import importlib, sys\n"
        f"names = {VERIFY_IMPORTS!r}\n"
        "missing = []\n"
        "for n in names:\n"
        "    try:\n"
        "        importlib.import_module(n)\n"
        "    except Exception:\n"
        "        missing.append(n)\n"
        "print('|'.join(missing))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT)
    )
    missing = [m for m in (proc.stdout or "").strip().split("|") if m]
    return (not missing), missing


def main() -> int:
    _setup_stdout()
    print(f"使用解释器：{sys.executable}")
    print()

    for name, mirror in MIRRORS:
        label = f"{name}（{mirror}）" if mirror else f"{name}（默认）"
        print(f"→ 尝试 {label} …")
        code = run_pip(mirror)
        if code == 0:
            print(f"\n✓ 从 {name} 安装成功\n")
            break
        print(f"\n✗ {name} 失败（退出码 {code}），换下一个源重试\n")
    else:
        print("✗ 所有源都失败了。")
        print("  排查方向：网络是否可用、是否需要代理、杀毒软件是否拦截。")
        return 1

    ok, missing = verify()
    if not ok:
        print(f"✗ 依赖验证失败，缺少：{', '.join(missing)}")
        print("  可能安装了不完整的包，请重跑一次 setup.bat。")
        return 1

    print(f"✓ 验证通过：{', '.join(VERIFY_IMPORTS)} 全部可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
