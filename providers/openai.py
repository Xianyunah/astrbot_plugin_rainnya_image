# -*- coding: utf-8 -*-
"""providers/openai.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class OpenAIMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    async def _call_openai_api(self, api_url: str, api_key: str, model_name: str,
                                image_bytes_list: List[bytes], prompt: str, source_info: str,
                                gen_params: Optional[Dict] = None,
                                insecure_ssl: bool = False) -> bytes | str:
        """OpenAI 兼容 API 路径（/v1/chat/completions）

        insecure_ssl: 为 True 时跳过 HTTPS 证书校验（自签名证书的自建中转）。
        gen_params: 合并后的生成参数（显式 > 模型默认），映射规则：
            temperature → payload.temperature（float）
            max_tokens  → payload.max_tokens（int）
            size        → payload.size（str）
            negative_prompt / steps / 其他透传键 → payload.extra_body 扩展字段
        """

        # 处理 API URL 格式
        api_url = api_url.rstrip("/")
        if re.search(r"/v\d+/(chat|images)/", api_url):
            pass
        elif re.search(r"/v\d+$", api_url):
            api_url = api_url + "/chat/completions"
        else:
            api_url = api_url + "/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/astrbot",
            "X-Title": "AstrBot NewImage Plugin",
        }

        message_content: List[Dict[str, Any]] = []
        if prompt:
            message_content.append({"type": "text", "text": prompt})

        if image_bytes_list:
            try:
                for img_bytes in image_bytes_list:
                    img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                    message_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    })
            except Exception as e:
                logger.error(f"Base64 编码图片时出错: {e}", exc_info=True)
                return f"图片编码失败: {e}"

        if not message_content:
            return "缺少 prompt 或图片内容"

        if len(message_content) == 1 and message_content[0].get("type") == "text":
            user_content: Any = message_content[0]["text"]
        else:
            user_content = message_content

        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": "user", "content": user_content}],
            "max_tokens": 1024,
            "temperature": 0.7,
        }

        # ========== 应用生成参数（gen_params = 模型默认 + 显式覆盖） ==========
        # temperature / max_tokens / size 映射到标准字段
        if gen_params:
            if "temperature" in gen_params:
                payload["temperature"] = float(gen_params["temperature"])
            if "max_tokens" in gen_params:
                payload["max_tokens"] = int(gen_params["max_tokens"])
            if "size" in gen_params:
                payload["size"] = gen_params["size"]
            # negative_prompt / steps / 其他透传键 → extra_body（中转常见支持，避免污染标准字段）
            extra: Dict[str, Any] = {}
            for pk, pv in gen_params.items():
                if pk in ("size", "temperature", "max_tokens"):
                    continue
                extra[pk] = pv
            if extra:
                payload["extra_body"] = extra

        logger.info(f"[NewImage/OAI] 发送请求 [{source_info}]: Model={model_name}, HasImage={bool(image_bytes_list)}")

        try:
            if not self.iwf:
                return "ImageWorkflow 未初始化"
            async with self.iwf.session.post(
                api_url, json=payload, headers=headers,
                proxy=self.iwf.proxy, timeout=self.api_timeout,
                ssl=False if insecure_ssl else None,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"API 请求失败: HTTP {resp.status}, 响应: {error_text[:500]}")
                    if resp.status >= 500:
                        return f"⚠️ API 服务端繁忙或超时（HTTP {resp.status}），请稍后重试。"
                    return f"API请求失败 (HTTP {resp.status}): {error_text[:200]}"

                content_type = resp.headers.get("Content-Type", "")

                if "text/event-stream" in content_type:
                    logger.info("[NewImage] 检测到 SSE 流式响应，开始拼接 chunks")
                    full_content = ""
                    raw_text = await resp.text()
                    for line in raw_text.split("\n"):
                        line = line.strip()
                        if not line.startswith("data:") or line == "data: [DONE]":
                            continue
                        try:
                            chunk = json.loads(line[5:].strip())
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content_piece = delta.get("content", "")
                                if content_piece:
                                    full_content += content_piece
                        except Exception:
                            pass
                    logger.info(f"[NewImage] SSE 拼接完成，内容长度: {len(full_content)}")
                    data = {"choices": [{"message": {"content": full_content}}]}
                else:
                    data = await resp.json()

                result = await extract_image_bytes_from_response(data, self.iwf)

                if isinstance(result, bytes):
                    return result

                if isinstance(result, str) and result.startswith("FALLBACK_URL:"):
                    fallback_url = result[len("FALLBACK_URL:"):]
                    logger.warning(f"图片下载失败，返回备用URL供用户访问: {fallback_url}")
                    return f"⚠️ 图片已生成但下载失败，请直接访问链接查看:\n{fallback_url}"

                if "error" in data:
                    return data["error"].get("message", json.dumps(data["error"]))

                error_msg = f"API响应中未找到可用的图像数据: {str(data)[:500]}..."
                logger.error(error_msg)
                return error_msg
        except asyncio.TimeoutError:
            logger.error("API 请求超时")
            return "请求超时"
        except Exception as e:
            logger.error(f"调用 API 时发生未知错误: {e}", exc_info=True)
            return f"发生未知错误: {e}"


    async def _call_openai_images_api(self, api_url: str, api_key: str, model_name: str,
                                       image_bytes_list: List[bytes], prompt: str, source_info: str,
                                       gen_params: Optional[Dict] = None,
                                       insecure_ssl: bool = False) -> bytes | str:
        """OpenAI Images API 路径（POST {base}/v1/images/generations）。

        适用于标准图像生成模型（DALL·E / gpt-image-1 / muse-image 等）：一次请求直接返回图像，
        不会像 chat/completions 那样只返回一句话描述。响应解析复用
        extract_image_bytes_from_response（data[].b64_json 与 data[].url 两种格式都支持）。

        与 chat 模式的差异：
            - 请求体是 {model, prompt, n, size, ...}，没有 messages 结构
            - 图生图 / 参考图编辑自动路由到 /v1/images/edits（multipart/form-data）
            - 尺寸需为 "宽x高" 直白格式，由 _openai_image_size 映射
        insecure_ssl: 为 True 时跳过 HTTPS 证书校验（自签名证书的自建中转）。
        """
        # ========== 端点归一化（兼容用户把完整路径粘进 BaseURL）==========
        api_url = api_url.rstrip("/")
        if re.search(r"/v\d+/images/(generations|edits)$", api_url):
            pass  # 已是完整 images 端点
        elif re.search(r"/v\d+/chat/completions$", api_url):
            # 用户把完整 chat 路径粘进了 BaseURL → 自动纠正为 images 端点
            api_url = re.sub(r"/chat/completions$", "/images/generations", api_url)
        elif re.search(r"/v\d+$", api_url):
            api_url = api_url + "/images/generations"
        else:
            api_url = api_url + "/v1/images/generations"

        if image_bytes_list:
            # 图生图 / 参考图编辑：标准 Images API 走 /v1/images/edits（multipart/form-data）
            logger.info(f"[NewImage/OAI-Images] 检测到 {len(image_bytes_list)} 张参考图，"
                        f"自动路由到 /v1/images/edits 端点")
            return await self._call_openai_images_edits_api(
                api_url, api_key, model_name, image_bytes_list, prompt, source_info,
                gen_params=gen_params, insecure_ssl=insecure_ssl,
            )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/astrbot",
            "X-Title": "AstrBot NewImage Plugin",
        }

        # ========== 构造 Images API 请求体 ==========
        payload: Dict[str, Any] = {"model": model_name, "prompt": prompt, "n": 1}
        size_val = (gen_params or {}).get("size")
        payload["size"] = _openai_image_size(size_val) if size_val else _openai_image_size(None)
        # 其余生成参数直接透传（negative_prompt / steps / quality / style / seed 等，中转普遍支持）
        for pk, pv in (gen_params or {}).items():
            if pk == "size":
                continue
            payload[pk] = pv

        logger.info(f"[NewImage/OAI-Images] 发送请求 [{source_info}]: Model={model_name}, "
                    f"Size={payload['size']}")

        try:
            if not self.iwf:
                return "ImageWorkflow 未初始化"
            async with self.iwf.session.post(
                api_url, json=payload, headers=headers,
                proxy=self.iwf.proxy, timeout=max(300, int(self.api_timeout or 120)),
                ssl=False if insecure_ssl else None,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"[NewImage/OAI-Images] 请求失败: HTTP {resp.status}, 响应: {error_text[:500]}")
                    if resp.status >= 500:
                        return f"⚠️ API 服务端繁忙或超时（HTTP {resp.status}），请稍后重试。"
                    return f"Images API 请求失败 (HTTP {resp.status}): {error_text[:300]}"

                data = await resp.json()

                result = await extract_image_bytes_from_response(data, self.iwf)

                if isinstance(result, bytes):
                    return result

                if isinstance(result, str) and result.startswith("FALLBACK_URL:"):
                    fallback_url = result[len("FALLBACK_URL:"):]
                    logger.warning(f"图片下载失败，返回备用URL供用户访问: {fallback_url}")
                    return f"⚠️ 图片已生成但下载失败，请直接访问链接查看:\n{fallback_url}"

                if isinstance(data, dict) and "error" in data:
                    err = data["error"]
                    if isinstance(err, dict):
                        return err.get("message", json.dumps(err, ensure_ascii=False))
                    return str(err)

                error_msg = f"Images API 响应中未找到可用的图像数据: {str(data)[:500]}..."
                logger.error(error_msg)
                return error_msg
        except asyncio.TimeoutError:
            logger.error("[NewImage/OAI-Images] 请求超时")
            return "请求超时"
        except Exception as e:
            logger.error(f"[NewImage/OAI-Images] 调用 API 时发生未知错误: {e}", exc_info=True)
            return f"发生未知错误: {e}"


    async def _call_openai_images_edits_api(self, api_url: str, api_key: str, model_name: str,
                                             image_bytes_list: List[bytes], prompt: str, source_info: str,
                                             gen_params: Optional[Dict] = None,
                                             insecure_ssl: bool = False) -> bytes | str:
        """OpenAI Images 编辑路径（POST {base}/v1/images/edits，multipart/form-data）。

        图生图 / 参考图编辑的正确端点（generations 端点只接受纯文本 prompt）：
            - 请求体为 multipart/form-data：model、prompt、image（可多张）、size、n 及可选参数
            - 支持 gpt-image 系列多图融合；mask（局部重绘遮罩）如上游支持可通过 gen_params 传文件名场景不受支持
            - 尺寸仍走 _openai_image_size 归一化为 "宽x高"
            - 图像 API 为同步接口（无 task id），超时下限放宽到 300s，避免大图被过早掐断
        insecure_ssl: 为 True 时跳过 HTTPS 证书校验（自签名证书的自建中转）。
        """
        # ========== 端点归一化：统一落到 /images/edits ==========
        api_url = api_url.rstrip("/")
        if re.search(r"/v\d+/images/edits$", api_url):
            pass  # 已是完整 edits 端点
        elif re.search(r"/v\d+/images/generations$", api_url):
            api_url = re.sub(r"/generations$", "/edits", api_url)
        elif re.search(r"/v\d+/chat/completions$", api_url):
            api_url = re.sub(r"/chat/completions$", "/images/edits", api_url)
        elif re.search(r"/v\d+$", api_url):
            api_url = api_url + "/images/edits"
        else:
            api_url = api_url + "/v1/images/edits"

        if not image_bytes_list:
            return "❌ 图像编辑需要至少一张参考图。"

        # 仅带鉴权头；Content-Type 由 aiohttp.FormData 自动生成（含 boundary）
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/astrbot",
            "X-Title": "AstrBot NewImage Plugin",
        }

        # ========== 构造 multipart 表单 ==========
        form = aiohttp.FormData()
        form.add_field("model", model_name)
        form.add_field("prompt", prompt or "")
        form.add_field("n", "1")
        size_val = (gen_params or {}).get("size")
        form.add_field("size", _openai_image_size(size_val) if size_val else _openai_image_size(None))

        # 参考图（支持多张，gpt-image 系列支持多图融合）
        for idx, img_bytes in enumerate(image_bytes_list):
            form.add_field("image", img_bytes,
                           filename=f"image_{idx}.png", content_type="image/png")

        # 其余生成参数透传（quality / input_fidelity / background / output_format /
        # output_compression / strength / steps / negative_prompt 等，中转兼容性不一）
        for pk, pv in (gen_params or {}).items():
            if pk in ("size", "prompt", "model", "image", "n"):
                continue
            if pv is None:
                continue
            form.add_field(pk, str(pv))

        timeout = max(300, int(self.api_timeout or 120))
        logger.info(f"[NewImage/OAI-Images-Edit] 发送请求 [{source_info}]: Model={model_name}, "
                    f"Images={len(image_bytes_list)}, Size={_openai_image_size(size_val)}, Timeout={timeout}s")

        try:
            if not self.iwf:
                return "ImageWorkflow 未初始化"
            async with self.iwf.session.post(
                api_url, data=form, headers=headers,
                proxy=self.iwf.proxy, timeout=timeout,
                ssl=False if insecure_ssl else None,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"[NewImage/OAI-Images-Edit] 请求失败: HTTP {resp.status}, "
                                 f"响应: {error_text[:500]}")
                    if resp.status == 404:
                        return (f"❌ 图像编辑端点不存在（HTTP 404）：{api_url}\n"
                                f"该中转可能未开放 /v1/images/edits，请改用 chat 模式或更换渠道。")
                    if resp.status >= 500:
                        return f"⚠️ API 服务端繁忙或超时（HTTP {resp.status}），请稍后重试。"
                    return f"Images Edits API 请求失败 (HTTP {resp.status}): {error_text[:300]}"

                data = await resp.json()

                result = await extract_image_bytes_from_response(data, self.iwf)

                if isinstance(result, bytes):
                    return result

                if isinstance(result, str) and result.startswith("FALLBACK_URL:"):
                    fallback_url = result[len("FALLBACK_URL:"):]
                    logger.warning(f"编辑图下载失败，返回备用URL供用户访问: {fallback_url}")
                    return f"⚠️ 图片已生成但下载失败，请直接访问链接查看:\n{fallback_url}"

                if isinstance(data, dict) and "error" in data:
                    err = data["error"]
                    if isinstance(err, dict):
                        return err.get("message", json.dumps(err, ensure_ascii=False))
                    return str(err)

                error_msg = f"Images Edits API 响应中未找到可用的图像数据: {str(data)[:500]}..."
                logger.error(error_msg)
                return error_msg
        except asyncio.TimeoutError:
            logger.error("[NewImage/OAI-Images-Edit] 请求超时")
            return f"图像编辑请求超时（{timeout}s），该模型出图较慢，请稍后重试。"
        except Exception as e:
            logger.error(f"[NewImage/OAI-Images-Edit] 调用 API 时发生未知错误: {e}", exc_info=True)
            return f"发生未知错误: {e}"
