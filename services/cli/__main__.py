"""CLI 进程入口：``python -m services.cli``（未来 pip 打包入口 onto = cli.app:main，CLI设计 §2）。"""

from __future__ import annotations

import sys

from services.cli.app import main

if __name__ == "__main__":
    sys.exit(main())
