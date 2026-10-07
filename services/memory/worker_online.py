"""在线队列 worker 入口：python -m services.memory.worker_online（06 篇 §5.5.2 独立进程）。"""

from arq import run_worker

from services.memory.business.tasks import WorkerSettingsOnline

if __name__ == "__main__":
    run_worker(WorkerSettingsOnline)  # type: ignore[arg-type]
