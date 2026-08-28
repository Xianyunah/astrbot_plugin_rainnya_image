"""通用 JSON 持久化辅助：供各模块读写数据文件（run_in_executor + try/except 兜底）。"""

import asyncio
import functools
import json
from typing import Any

from astrbot import logger


async def load_json(path) -> Any:
    """读取 JSON 文件。文件不存在或解析失败时返回 None（调用方自行处理默认值）。

    使用 run_in_executor 读文件 + json.loads，避免阻塞事件循环。
    """
    if not path.exists():
        return None
    loop = asyncio.get_running_loop()
    try:
        content = await loop.run_in_executor(None, path.read_text, "utf-8")
        return await loop.run_in_executor(None, json.loads, content)
    except Exception as e:
        logger.error(f"读取 JSON 文件失败 ({path}): {e}", exc_info=True)
        return None


async def save_json(path, data) -> None:
    """写入 JSON 文件（ensure_ascii=False, indent=4），使用 run_in_executor 不阻塞事件循环。"""
    loop = asyncio.get_running_loop()
    try:
        json_data = await loop.run_in_executor(None,
                                               functools.partial(json.dumps, data, ensure_ascii=False, indent=4))
        await loop.run_in_executor(None, path.write_text, json_data, "utf-8")
    except Exception as e:
        logger.error(f"写入 JSON 文件失败 ({path}): {e}", exc_info=True)
