#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""淘宝 MTop HTTP 抓取器 —— 启动入口。

抓取逻辑已拆分到同名的 taobao_scraper/ 包中，本文件只负责启动，
保留它是为了让 启动淘宝抓取.bat 和既有使用习惯不受影响。

等价调用方式：

    python taobao_scraper.py --interactive
    python taobao_scraper.py --keywords "accent chair" --pages 20
    python -m taobao_scraper --keywords "accent chair" --pages 20
"""

from __future__ import annotations

import sys
from pathlib import Path

# 保证作为脚本直接运行时能定位到同目录下的包
sys.path.insert(0, str(Path(__file__).resolve().parent))

from taobao_scraper import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
