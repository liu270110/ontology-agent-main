#!/usr/bin/env python3
"""E7 Placement 验证：(a) power-of-k-choices 仿真；(b) edge 本地准入拒绝→apiserver 改选 集成测试。

仿真复刻论文 §7：突发请求 + watcher 视图过期 + 各节点容量有限。
对比 rand / powk(k=2,3,5) / least-loaded-all 的负载均衡（节点利用率标准差）与拒绝率。
"""
import json
import random
import statistics

NODES = 3
CAP = 100.0  # 每节点容量（归一化）
BURST = 240  # 突发作业沙箱数
REQ = 1.0
K_VIEW_STALENESS = 0.05  # 每次选择后视图更新概率（模拟周期快照延迟）
TRIALS = 50


def simulate(mode: str, k: int = 3) -> dict:
    rng = random.Random(42)
    rejected = 0
    loads = []  # 最终每个节点提交的量
    for _ in range(TRIALS):
        node_load = [0.0] * NODES
        # watcher 视图：周期快照，有滞后
        view = [0.0] * NODES
        for _ in range(BURST):
            cands = [i for i in range(NODES)]
            # 过滤：按（可能过期的）视图看容量不足的排除
            avail = [i for i in cands if view[i] + REQ <= CAP + 1e-9]
            if not avail:
                rejected += 1
                continue
            if mode == "rand":
                pick = rng.choice(avail)
            elif mode == "all":
                pick = min(avail, key=lambda i: view[i])
            else:
                pick = min(rng.sample(avail, min(k, len(avail))), key=lambda i: view[i])
            # edge 本地准入：实际负载超了才拒绝（视图过期的真相仲裁）
            if node_load[pick] + REQ > CAP:
                rejected += 1
                continue
            node_load[pick] += REQ
            if rng.random() < K_VIEW_STALENESS:
                view = list(node_load)  # 心跳刷新
        loads.append(node_load)
    util = [sum(t[i] for t in loads) / (TRIALS * CAP) for i in range(NODES)]
    return {
        "mode": mode if mode != "powk" else f"powk(k={k})",
        "rejected_total": rejected,
        "rejection_rate": round(rejected / (TRIALS * BURST), 4),
        "node_utilization": [round(u, 3) for u in util],
        "util_imbalance_std": round(statistics.pstdev(util), 4),
    }


def main() -> None:
    sim = [simulate("rand"), simulate("powk", 2), simulate("powk", 3), simulate("powk", 5), simulate("all")]
    print(json.dumps(sim, indent=2))

    # ---- (b) 集成测试：突发打满最小节点，观察 409 → 改选 ----
    import sys
    import time
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
    from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

    import urllib.request, json as _json

    def set_cap(edge_port: int, cap: int) -> None:
        req = urllib.request.Request(f"http://localhost:{edge_port}/admin/config",
                                     _json.dumps({"max_containers": cap}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)

    c = DSecClient(max_workers=16).open()
    # ---- 确定性 409/503 演示 ----
    # 1) 直接向"已满"edge 创建 → 本地准入 409（edge 保留最终准入权，不信任 placement 视图）
    set_cap(8103, 1)
    time.sleep(3)  # 视图同步
    args = DSecContainerRunArgs(cpu_cores_limit=0.25, memory_limit_mb=64, ttl_running_stop=0)
    first = c.run_container(args)  # 占满 edge-3？placement 决定位置；改为直接对 edge-3 制造已满态
    first.stop()

    import urllib.request as _ur
    def edge_create(port: int, sid: str):
        body = _json.dumps({"sandbox_id": sid, "spec": {
            "project": "root", "subject": "root", "backend": "container",
            "container_image": "dsec-sandbox-base:latest", "cpu_cores_limit": 0.25,
            "memory_limit_mb": 64, "ttl_running_stop": 0}}).encode()
        r = _ur.urlopen(_ur.Request(f"http://localhost:{port}/sandboxes", body,
                                    {"Content-Type": "application/json"}), timeout=30)
        return r.status, r.read()

    # 直接往 edge-3 塞满（绕过 placement），下一个必被 409
    import urllib.error as _ue
    direct_ids = []
    rc409 = None
    for i in range(5):
        try:
            st, _ = edge_create(8103, f"sbx-edge-3-direct7{i:03d}")
            direct_ids.append(i)
        except _ue.HTTPError as e:
            rc409 = e.code
            break
    assert direct_ids, "edge-3 direct create failed"
    for i in range(len(direct_ids)):
        try:
            _ur.urlopen(_ur.Request(f"http://localhost:8103/sandboxes/sbx-edge-3-direct7{i:03d}/stop",
                                    b"{}", {"Content-Type": "application/json"}), timeout=30)
        except Exception:
            pass
    edge_409 = (rc409 == 409)

    # 2) 集群容量穷尽：全部 edge 容量=1，顺序创建 4 个 → 第 4 个 503
    for port in (8101, 8102, 8103):
        set_cap(port, 1)
    time.sleep(3)
    created, exhausted_503 = 0, False
    seq_ids = []
    for i in range(4):
        try:
            sb = c.run_container(args)
            seq_ids.append(sb.id)
            created += 1
        except RuntimeError as e:
            exhausted_503 = "503" in str(e)
            break
    for port in (8101, 8102, 8103):
        set_cap(port, {"8101": 500, "8102": 200, "8103": 40}[str(port)])
    for sid in seq_ids:
        try:
            c._post(f"/v1/sandboxes/{sid}/stop", {})
        except Exception:
            pass

    out = {
        "integration": {
            "note": "确定性流程：绕过 placement 直塞满 edge-3 验证本地准入 409；全部 edge 容量=1 时第 4 个创建 503",
            "edge_local_admission_409": edge_409,
            "exhaustion_503": exhausted_503,
            "sequential_created_before_503": created,
        },
        "simulation": sim,
    }
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E7_placement.json"), "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("integration:", out["integration"])


if __name__ == "__main__":
    main()
