"""IAM：认证鉴权 + 多级项目嵌套 + 配额继承（论文 §4.2 IAM）。

论文要点：
- project 定义资源与访问控制范围，支持多级嵌套（区别于云厂商扁平/两级）；
- 授权以父为界——不能授予自己没有的权限，子项目配额不得超父；
- 人与 Agent 用同一套管理 API 与授权模型。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from common import make_app, jlog

app = make_app("iam")


class Subject(BaseModel):
    name: str
    kind: str = "human"  # human | agent | harness


class Project(BaseModel):
    name: str
    parent: Optional[str] = None
    quota_cpu: float = 4.0
    quota_mem_mb: int = 4096


class Grant(BaseModel):
    subject: str
    project: str
    role: str = "admin"  # admin | use
    granted_by: str


class Acquire(BaseModel):
    project: str
    subject: str
    cpu: float
    mem_mb: int


subjects: dict[str, Subject] = {}
projects: dict[str, Project] = {}
grants: list[Grant] = []  # (subject, project, role, granted_by)
usage: dict[str, dict[str, float]] = {}  # project -> {cpu, mem_mb, sandboxes}


def _proj(name: str) -> Project:
    if name not in projects:
        raise HTTPException(404, f"project {name} not found")
    return projects[name]


def _chain(name: str) -> list[str]:
    """从项目到根的祖先链（含自身）。"""
    out, cur = [], name
    while cur:
        out.append(cur)
        cur = _proj(cur).parent
    return out


def _ancestors(name: str) -> list[str]:
    return _chain(name)[1:]


def _has_grant(subject: str, project: str, role: str) -> bool:
    if subject == "root":  # 集群根主体（运维者）
        return True
    for g in grants:
        if g.subject == subject and g.project == project and (g.role == "admin" or g.role == role):
            return True
    return False


@app.post("/subjects")
def create_subject(s: Subject) -> dict:
    subjects[s.name] = s
    jlog("subject", name=s.name, kind=s.kind)
    return {"ok": True}


@app.post("/projects")
def create_project(p: Project, actor: str = "root") -> dict:
    if p.name in projects:
        raise HTTPException(409, "project exists")
    if p.parent:
        parent = _proj(p.parent)
        if not _has_grant(actor, p.parent, "admin"):
            raise HTTPException(403, f"{actor} lacks admin on {p.parent}")
        # 授权以父为界：子配额不得超父配额
        if p.quota_cpu > parent.quota_cpu or p.quota_mem_mb > parent.quota_mem_mb:
            raise HTTPException(403, "child quota exceeds parent (委托以父为界)")
    projects[p.name] = p
    usage[p.name] = {"cpu": 0.0, "mem_mb": 0, "sandboxes": 0}
    jlog("project", name=p.name, parent=p.parent, cpu=p.quota_cpu)
    return {"ok": True}


@app.get("/projects")
def list_projects() -> dict:
    return {"projects": {k: v.model_dump() for k, v in projects.items()}, "usage": usage}


@app.post("/grants")
def create_grant(g: Grant) -> dict:
    _proj(g.project)
    # 委托以父为界：自身或任一祖先持 admin 即可在该项目授权
    if not any(_has_grant(g.granted_by, name, "admin") for name in _chain(g.project)):
        raise HTTPException(403, f"{g.granted_by} cannot grant on {g.project}")
    grants.append(g)
    jlog("grant", subject=g.subject, project=g.project, role=g.role)
    return {"ok": True}


@app.post("/authz")
def authz(project: str, subject: str, role: str = "use") -> dict:
    _proj(project)
    if subject == "root":
        return {"ok": True}
    if not _has_grant(subject, project, role):
        raise HTTPException(403, f"subject {subject} lacks {role} on {project}")
    return {"ok": True}


@app.post("/acquire")
def acquire(a: Acquire) -> dict:
    """创建沙箱时沿祖先链记账：每层 usage+req 不得超过该层配额。"""
    _proj(a.project)
    if a.subject != "root":
        if not _has_grant(a.subject, a.project, "use"):
            raise HTTPException(403, f"{a.subject} lacks use on {a.project}")
    for name in _chain(a.project):
        p = _proj(name)
        u = usage[name]
        if u["cpu"] + a.cpu > p.quota_cpu + 1e-9 or u["mem_mb"] + a.mem_mb > p.quota_mem_mb:
            raise HTTPException(
                429,
                f"quota exceeded at project {name}: cpu {u['cpu']}+{a.cpu}/{p.quota_cpu}, "
                f"mem {u['mem_mb']}+{a.mem_mb}/{p.quota_mem_mb}",
            )
    for name in _chain(a.project):
        usage[name]["cpu"] += a.cpu
        usage[name]["mem_mb"] += a.mem_mb
        usage[name]["sandboxes"] += 1
    return {"ok": True}


@app.post("/release")
def release(a: Acquire) -> dict:
    for name in _chain(a.project):
        u = usage[name]
        u["cpu"] = max(0.0, u["cpu"] - a.cpu)
        u["mem_mb"] = max(0, u["mem_mb"] - a.mem_mb)
        u["sandboxes"] = max(0, u["sandboxes"] - 1)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    # 种子：根项目 root（集群总配额），人与 Agent 共用同一套项目/授权模型
    projects["root"] = Project(name="root", quota_cpu=32.0, quota_mem_mb=14336)  # 超分：申请额上限 1.45× 物理核
    usage["root"] = {"cpu": 0.0, "mem_mb": 0, "sandboxes": 0}
    subjects["root"] = Subject(name="root", kind="human")
    uvicorn.run(app, host="0.0.0.0", port=8003, log_level="warning")
