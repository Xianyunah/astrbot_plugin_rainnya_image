# -*- coding: utf-8 -*-
"""providers/gemini.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

import asyncio
import base64
import copy
import functools
import io
import json
import os
import random
import re
import shutil
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

import aiohttp
from PIL import Image as PILImage

from astrbot import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, File, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from ..constants import *
from ..image_parser import extract_image_bytes_from_response
from ..image_workflow import ImageWorkflow
from ..storage import load_json, save_json


class GeminiMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    async def _call_gemini_api(self, api_url: str, api_key: str, model_name: str,
                                image_bytes_list: List[bytes], prompt: str,
                                image_size: Optional[str], source_info: str,
                                gen_params: Optional[Dict] = None,
                                insecure_ssl: bool = False) -> bytes | str:
        """Gemini 原生 API 路径（generateContent）

        insecure_ssl: 为 True 时跳过 HTTPS 证书校验（自签名证书的自建网关）。
        gen_params: 合并后的生成参数（显式 > 模型默认），映射规则：
            temperature → generationConfig.temperature（float）
            max_tokens  → generationConfig.maxOutputTokens（int）
            size        → generationConfig.imageConfig.imageSize（与指令 image_size 合并取最高优先级）
            negative_prompt / steps / 其他透传键 → generationConfig.imageConfig 扩展字段
        """

        # 构建 Gemini 格式的 endpoint
        api_url = api_url.rstrip("/")
        version = "v1beta"
        endpoint_base = api_url if f"/{version}" in api_url else f"{api_url}/{version}"
        endpoint = f"{endpoint_base}/models/{model_name}:generateContent?key={api_key}"

        # 构建请求体 parts
        parts: List[Dict[str, Any]] = []

        # 添加输入图片
        for img_bytes in image_bytes_list:
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": "image/png",
                    "data": img_b64
                }
            })

        # 添加文本提示
        if prompt:
            parts.append({"text": prompt})

        if not parts:
            return "缺少 prompt 或图片内容"

        request_body: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"]
            }
        }

        # ========== 应用生成参数（gen_params = 模型默认 + 显式覆盖） ==========
        # temperature → generationConfig.temperature（float）
        if gen_params and "temperature" in gen_params:
            request_body["generationConfig"]["temperature"] = float(gen_params["temperature"])
        # max_tokens → generationConfig.maxOutputTokens（int）
        if gen_params and "max_tokens" in gen_params:
            request_body["generationConfig"]["maxOutputTokens"] = int(gen_params["max_tokens"])
        # 确定最终使用的 imageSize：gen_params.size > 指令 image_size > 全局默认
        final_image_size = gen_params.get("size") if (gen_params and gen_params.get("size")) else (image_size or self.default_image_size)
        image_config: Dict[str, Any] = {}
        if final_image_size:
            image_config["imageSize"] = final_image_size
            logger.info(f"[NewImage/Gemini] 使用分辨率: {final_image_size}")
        # negative_prompt / steps / 其他透传键 → imageConfig 扩展字段
        if gen_params:
            for pk, pv in gen_params.items():
                if pk in ("size", "temperature", "max_tokens"):
                    continue  # 已单独映射
                if pk == "negative_prompt":
                    image_config["negativePrompt"] = pv
                else:
                    image_config[pk] = pv  # steps 及透传键原样透传
        if image_config:
            request_body["generationConfig"]["imageConfig"] = image_config

        # 添加安全过滤设置
        if self.safety_level:
            categories = [
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_HARASSMENT"
            ]
            request_body["safetySettings"] = [
                {"category": cat, "threshold": self.safety_level} for cat in categories
            ]

        logger.info(f"[NewImage/Gemini] 发送请求 [{source_info}]: Model={model_name}, HasImage={bool(image_bytes_list)}")

        try:
            if not self.iwf:
                return "ImageWorkflow 未初始化"
            async with self.iwf.session.post(
                endpoint,
                json=request_body,
                headers={"Content-Type": "application/json"},
                proxy=self.iwf.proxy,
                timeout=self.api_timeout,
                ssl=False if insecure_ssl else None,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Gemini API 请求失败: HTTP {resp.status}, 响应: {error_text[:500]}")
                    if resp.status >= 500:
                        return f"⚠️ API 服务端繁忙或超时（HTTP {resp.status}），请稍后重试。"
                    return f"Gemini API请求失败 (HTTP {resp.status}): {error_text[:300]}"

                data = await resp.json()

                # 解析 Gemini 响应
                candidates = data.get("candidates", [])
                for candidate in candidates:
                    resp_parts = candidate.get("content", {}).get("parts", [])
                    for part in resp_parts:
                        # 跳过思考过程
                        if part.get("thought"):
                            continue
                        # 提取内联图片
                        if "inlineData" in part:
                            inline = part["inlineData"]
                            b64_data = inline.get("data", "")
                            if b64_data:
                                return base64.b64decode(b64_data)

                # 检查是否有错误信息
                if "error" in data:
                    err = data["error"]
                    return f"Gemini API 错误: {err.get('message', json.dumps(err))}"

                # 检查安全过滤
                if candidates:
                    finish_reason = candidates[0].get("finishReason", "")
                    if finish_reason == "SAFETY":
                        safety_ratings = candidates[0].get("safetyRatings", [])
                        blocked = [r for r in safety_ratings if r.get("blocked")]
                        if blocked:
                            cats = ", ".join(r.get("category", "?") for r in blocked)
                            return f"⚠️ 内容被安全过滤器拦截: {cats}\n提示：可在配置中调整安全过滤等级。"
                        return "⚠️ 内容被安全过滤器拦截。提示：可在配置中调整安全过滤等级。"

                error_msg = f"Gemini 响应中未找到图像数据: {str(data)[:500]}..."
                logger.error(error_msg)
                return error_msg
        except asyncio.TimeoutError:
            logger.error("Gemini API 请求超时")
            return "请求超时"
        except Exception as e:
            logger.error(f"调用 Gemini API 时发生未知错误: {e}", exc_info=True)
            return f"发生未知错误: {e}"

