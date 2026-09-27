"""空闲队列 worker 入口：python -m services.memory.worker_idle（06 篇 §5.5.2 独立进程）。"""

from arq import run_worker

from services.memory.business.tasks import WorkerSettingsIdle

if __name__ == "__main__":
    run_worker(WorkerSettingsIdle)  # type: ignore[arg-type]
