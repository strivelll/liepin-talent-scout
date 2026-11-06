"""配置加载与全局访问。

四个配置文件各司其职：
- settings.yaml  运行开关、节奏、上限、模型参数
- selectors.yaml 页面选择器（改版只改这里）
- jobs.yaml      岗位知识与筛选/判定标准
- scripts.yaml   招呼话术库

设计上配置只加载一次并缓存，调用 reload() 可热更新（管理台用）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML，文件缺失时返回空字典而不是抛错（便于首次启动）。"""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class JobConfig:
    """单个岗位的完整配置。"""

    id: str
    name: str
    enabled: bool = True
    headcount: int = 1
    city: str = ""
    hard_filter: dict[str, Any] = field(default_factory=dict)
    soft_filter: dict[str, Any] = field(default_factory=dict)
    deep_read: dict[str, Any] = field(default_factory=dict)
    greeting: dict[str, Any] = field(default_factory=dict)
    chat: dict[str, Any] = field(default_factory=dict)
    judgement: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobConfig":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            enabled=bool(data.get("enabled", True)),
            headcount=int(data.get("headcount", 1)),
            city=str(data.get("city", "")),
            hard_filter=data.get("hard_filter") or {},
            soft_filter=data.get("soft_filter") or {},
            deep_read=data.get("deep_read") or {},
            greeting=data.get("greeting") or {},
            chat=data.get("chat") or {},
            judgement=data.get("judgement") or {},
        )


class Config:
    """配置单例。所有模块通过 config.settings / config.job(...) 取值。"""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        self.selectors: dict[str, Any] = {}
        self.scripts: dict[str, str] = {}
        self.jobs: dict[str, JobConfig] = {}
        self.seeker: dict[str, Any] = {}
        self.defaults: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        """重新读取全部配置文件。"""
        self.settings = _read_yaml(CONFIG_DIR / "settings.yaml")
        self.selectors = _read_yaml(CONFIG_DIR / "selectors.yaml")
        self.scripts = _read_yaml(CONFIG_DIR / "scripts.yaml").get("scripts", {}) or {}
        self.seeker = _read_yaml(CONFIG_DIR / "seeker.yaml")

        jobs_raw = _read_yaml(CONFIG_DIR / "jobs.yaml")
        self.defaults = jobs_raw.get("defaults", {}) or {}
        self.jobs = {}
        for item in jobs_raw.get("jobs", []) or []:
            job = JobConfig.from_dict(item)
            if job.id:
                self.jobs[job.id] = job

    # ---------- 便捷取值 ----------

    @property
    def mode(self) -> str:
        return str(self.settings.get("app", {}).get("mode", "mock")).lower()

    @property
    def pacing(self) -> dict[str, Any]:
        return self.settings.get("pacing", {}) or {}

    @property
    def funnel(self) -> dict[str, Any]:
        return self.settings.get("funnel", {}) or {}

    @property
    def chat_cfg(self) -> dict[str, Any]:
        return self.settings.get("chat", {}) or {}

    @property
    def invite_cfg(self) -> dict[str, Any]:
        return self.settings.get("invite", {}) or {}

    @property
    def llm_cfg(self) -> dict[str, Any]:
        return self.settings.get("llm", {}) or {}

    @property
    def storage(self) -> dict[str, Any]:
        return self.settings.get("storage", {}) or {}

    def job(self, job_id: str) -> JobConfig | None:
        return self.jobs.get(job_id)

    def active_jobs(self) -> list[JobConfig]:
        return [j for j in self.jobs.values() if j.enabled]

    def get_script(self, script_id: str) -> str:
        return self.scripts.get(script_id, self.scripts.get("generic_a", ""))

    def api_key(self) -> str:
        """从环境变量读取模型 Key，绝不落盘到配置文件。"""
        env_name = str(self.llm_cfg.get("api_key_env", "LLM_API_KEY"))
        return os.environ.get(env_name, "")

    def path(self, key: str, default: str) -> Path:
        """把配置里的相对路径解析为绝对路径，并确保父目录存在。"""
        raw = str(self.storage.get(key, default))
        p = ROOT / raw
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


# 全局单例
config = Config()
