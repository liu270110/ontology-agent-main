"""统一入口层（锚点 §4：取代根 main.py 的 uvicorn 入口）。

用法：python -m services.main   |   uvicorn services.gateway.app:create_app --factory
"""

from __future__ import annotations

import uvicorn

from services.infra.config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "services.gateway.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        reload=s.deploy_profile == "lite",
    )


if __name__ == "__main__":
    main()
