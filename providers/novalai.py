# -*- coding: utf-8 -*-
"""providers/novalai.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class NovalAIProviderMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    async def _check_novai_key(self, base_url: str, api_key: str, insecure_ssl: bool = False) -> dict:
        """NovelAI token 预检：GET {user_url}/user/data 解析订阅信息。

        返回 {"ok": bool, "error": str|None, **subscription_info}。
        有效判定：active 为真 或 purchasedTrainingSteps>0 或 fixedTrainingStepsLeft>0。
        失败/超时不抛异常，返回 {"ok": False, "error": 原因}（软校验，不拦截生图）。
        insecure_ssl: 为 True 时跳过 HTTPS 证书校验（自建反代常用自签证书）。
        """
        try:
            if not self.iwf:
                return {"ok": False, "error": "ImageWorkflow 未初始化"}
            base = (base_url or "").rstrip("/")
            if not base:
                return {"ok": False, "error": "user_url 未配置"}
            endpoint = f"{base}/user/data"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Origin": "https://novelai.net",
                "Referer": "https://novelai.net",
                "User-Agent": NOVALAI_UA,
                "Content-Type": "application/json",
            }
            timeout = max(1, int(self.conf.get("novai_precheck_timeout", 10) or 10))
            async with self.iwf.session.get(
                endpoint, headers=headers, proxy=self.iwf.proxy, timeout=timeout,
                ssl=False if insecure_ssl else None,
            ) as resp:
                if resp.status != 200:
                    body = (await resp.read())[:200].decode("utf-8", errors="replace")
                    return {"ok": False, "error": f"HTTP {resp.status}: {body[:200]}"}
                data = await resp.json()
            subscription = data.get("subscription") if isinstance(data, dict) else None
            if not isinstance(subscription, dict):
                return {"ok": False, "error": "响应缺少 subscription 字段"}
            active = bool(subscription.get("active"))
            purchased = int(subscription.get("purchasedTrainingSteps") or 0)
            fixed_left = int(subscription.get("fixedTrainingStepsLeft") or 0)
            training_left = int(subscription.get("trainingStepsLeft") or 0)
            ok = active or purchased > 0 or fixed_left > 0
            warn_threshold = max(0, int(self.conf.get("novai_point_warn_threshold", 10) or 10))
            warn = ok and training_left >= 0 and 0 < training_left <= warn_threshold
            return {
                "ok": ok,
                "error": None if ok else "订阅无效（非活跃且无训练步数）",
                "active": active,
                "tier": subscription.get("tier"),
                "expires_at": subscription.get("expiresAt"),
                "training_steps_left": training_left,
                "purchased_training_steps": purchased,
                "fixed_training_steps_left": fixed_left,
                "low_points_warning": warn,
            }
        except asyncio.TimeoutError:
            return {"ok": False, "error": "预检请求超时"}
        except Exception as e:
            logger.warning(f"[NewImage] NovelAI token 预检失败: {e}")
            return {"ok": False, "error": str(e)[:200]}


    async def _run_novai_precheck(self, provider: Dict[str, Any], user_url: str, api_key: str, pname: str) -> None:
        """后台 token 预检包装：结果写 provider["_user_status"] 并打日志（不 await 调用方）。"""
        try:
            if not self.conf.get("novai_precheck_enabled", True):
                return
            status = await self._check_novai_key(user_url, api_key,
                                                 insecure_ssl=bool(provider.get("insecure_ssl", False)))
            provider["_user_status"] = status
            if status.get("ok"):
                logger.info(f"[NewImage] NovelAI 提供商「{pname}」token 有效"
                            f"（tier={status.get('tier')}, 训练步数剩 {status.get('training_steps_left')}）")
                if status.get("low_points_warning"):
                    logger.warning(f"[NewImage] NovelAI 提供商「{pname}」剩余训练步数不足 "
                                   f"({status.get('training_steps_left')})")
            else:
                logger.warning(f"[NewImage] NovelAI 提供商「{pname}」token 预检失败: {status.get('error')}")
        except Exception as e:
            logger.warning(f"[NewImage] NovelAI 提供商「{pname}」token 预检异常: {e}")


    def _fit_novalai_dimensions(self, width: int, height: int) -> tuple:
        """把宽高规整为 64 倍数（min 64），面积超付费上限时按比例缩放保持 64 对齐。

        返回 (width, height)。面积上限取付费档（NOVALAI_MAX_PIXELS_PAID）作为 API 允许的绝对上限；
        免费档用户若请求 2K/4K 超限，由 API 返回错误并被 _call_novalai_api 透出。
        """
        width = max(NOVALAI_MIN_DIM, round(width / NOVALAI_MIN_DIM) * NOVALAI_MIN_DIM)
        height = max(NOVALAI_MIN_DIM, round(height / NOVALAI_MIN_DIM) * NOVALAI_MIN_DIM)
        area = width * height
        if area > NOVALAI_MAX_PIXELS_PAID:
            scale = (NOVALAI_MAX_PIXELS_PAID / area) ** 0.5
            width = max(NOVALAI_MIN_DIM, int(width * scale // NOVALAI_MIN_DIM) * NOVALAI_MIN_DIM)
            height = max(NOVALAI_MIN_DIM, int(height * scale // NOVALAI_MIN_DIM) * NOVALAI_MIN_DIM)
        return width, height


    async def _call_novalai_api(self, api_url: str, api_key: str, model_name: str,
                                image_bytes_list: List[bytes], prompt: str,
                                image_size: Optional[str], source_info: str,
                                gen_params: Optional[Dict] = None,
                                insecure_ssl: bool = False) -> bytes | str:
        """NovelAI 独立路由（POST {base_url}/ai/generate-image）。

        NovelAI 结构与 gemini/openai 完全不同：prompt 放在 v4_prompt.caption.base_caption，
        负面词放 negative_prompt + v4_negative_prompt.caption.base_caption 两处；
        图生图 action=img2img 并在 parameters 追加 strength/noise/image/extra_noise_seed，
        width/height 覆盖为原图实际尺寸。响应为 ZIP（image_0.png）。

        insecure_ssl: 为 True 时跳过 HTTPS 证书校验（自签名证书的自建反代）。
        gen_params: 合并后的生成参数（渠道默认 < 模型默认 < 显式），映射规则：
            size        → width/height（1K=832×1216、2K=1152×1664、4K=1664×2432，64 倍数）
            steps       → parameters.steps（默认 23，免费档 clamp ≤28）
            scale       → parameters.scale（CFG，默认 6.0，clamp 0-10）
            negative_prompt → 替换默认负面词模板（默认模板见 NOVALAI_DEFAULT_NEGATIVE_PROMPT）
            sampler     → 非法值回退默认（PARAM_SCHEMA 无此键，仅当传入时生效）
        返回 bytes=生图成功 / str=失败原因。
        """
        # ========== 尺寸映射 ==========
        size_key = (gen_params or {}).get("size") or image_size or NOVALAI_DEFAULT_SIZE
        if size_key not in NOVALAI_SIZE_DIMENSIONS:
            size_key = NOVALAI_DEFAULT_SIZE
        width, height = NOVALAI_SIZE_DIMENSIONS[size_key]

        # ========== steps（免费档 clamp ≤28，默认 23） ==========
        try:
            steps = int((gen_params or {}).get("steps", NOVALAI_DEFAULT_STEPS))
        except (TypeError, ValueError):
            steps = NOVALAI_DEFAULT_STEPS
        steps = max(1, min(steps, NOVALAI_STEPS_MAX_FREE))

        # ========== scale（CFG，clamp 0-10，默认 6.0） ==========
        try:
            scale = float((gen_params or {}).get("scale", 6.0))
        except (TypeError, ValueError):
            scale = 6.0
        scale = max(0.0, min(scale, 10.0))

        # ========== 负面词：默认模板作底，用户/过滤词叠加在前 ==========
        # 语义：默认模板（质量保底词）始终保留；用户/画师串/NSFW 过滤词叠加在其前。
        # final_params["negative_prompt"] 可能是 "filter_tags, 画师串neg" / "filter_tags, 用户neg" 等形式。
        negative_prompt = NOVALAI_DEFAULT_NEGATIVE_PROMPT
        user_neg = (gen_params or {}).get("negative_prompt")
        if isinstance(user_neg, str) and user_neg.strip():
            negative_prompt = f"{user_neg.strip()}, {NOVALAI_DEFAULT_NEGATIVE_PROMPT}"

        # ========== sampler（可选，非法回退默认） ==========
        sampler = NOVALAI_DEFAULT_SAMPLER
        user_sampler = (gen_params or {}).get("sampler")
        if isinstance(user_sampler, str) and user_sampler.strip() in NOVALAI_SAMPLERS:
            sampler = user_sampler.strip()

        # ========== 判断图生图 ==========
        img2img = bool(image_bytes_list)
        parameters: Dict[str, Any] = {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": scale,
            "sampler": sampler,
            "steps": steps,
            "seed": random.randint(0, 2 ** 32 - 1),
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": True,
            "dynamic_thresholding": False,
            "controlnet_strength": 1,
            "legacy": False,
            "add_original_image": True,
            "cfg_rescale": 0,
            "noise_schedule": "karras",
            "legacy_v3_extend": False,
            "skip_cfg_above_sigma": None,
            "use_coords": False,
            "characterPrompts": [],
            "v4_prompt": {
                "caption": {"base_caption": prompt or "", "char_captions": []},
                "use_coords": False,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {"base_caption": negative_prompt, "char_captions": []},
            },
            "negative_prompt": negative_prompt,
            "reference_image_multiple": [],
            "reference_information_extracted_multiple": [],
            "reference_strength_multiple": [],
        }

        if img2img:
            # 图生图：action=img2img，覆盖宽高为原图实际尺寸，追加图生图字段
            try:
                with PILImage.open(io.BytesIO(image_bytes_list[0])) as pil_img:
                    orig_w, orig_h = pil_img.size
            except Exception as e:
                logger.warning(f"[NewImage/NovelAI] 读取原图尺寸失败: {e}，使用目标尺寸 {width}x{height}")
                orig_w, orig_h = width, height
            width, height = self._fit_novalai_dimensions(orig_w, orig_h)
            parameters["width"] = width
            parameters["height"] = height
            parameters["strength"] = NOVALAI_IMG2IMG_STRENGTH
            parameters["noise"] = NOVALAI_IMG2IMG_NOISE
            parameters["image"] = base64.b64encode(image_bytes_list[0]).decode("utf-8")
            parameters["extra_noise_seed"] = random.randint(0, 2 ** 32 - 1)

        request_body: Dict[str, Any] = {
            "input": prompt or "",
            "model": model_name,
            "action": "img2img" if img2img else "generate",
            "parameters": parameters,
        }

        # ========== 端点 & 请求头 ==========
        base = api_url.rstrip("/")
        endpoint = f"{base}/ai/generate-image"
        parsed_authority = urlparse(endpoint).netloc or "image.novelai.net"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Origin": "https://novelai.net",
            "Referer": "https://novelai.net",
            "User-Agent": NOVALAI_UA,
            "authority": parsed_authority,
        }

        logger.info(f"[NewImage/NovelAI] 发送请求 [{source_info}]: Model={model_name}, "
                    f"Action={'img2img' if img2img else 'generate'}, "
                    f"Size={width}x{height}, Steps={steps}")

        # ========== 读取 429 自动重试配置 ==========
        retry_enabled = bool(self.conf.get("novalai_429_retry_enabled", True))
        try:
            max_retries = max(1, min(int(self.conf.get("novalai_429_max_retries", 10)), 50)) if retry_enabled else 0
        except (TypeError, ValueError):
            max_retries = 10 if retry_enabled else 0
        try:
            retry_delay = max(1, min(int(self.conf.get("novalai_429_retry_delay", 5)), 60))
        except (TypeError, ValueError):
            retry_delay = 5

        attempt = 0
        while True:
            try:
                if not self.iwf:
                    return "ImageWorkflow 未初始化"
                async with self.iwf.session.post(
                    endpoint, json=request_body, headers=headers,
                    proxy=self.iwf.proxy, timeout=self.api_timeout,
                    ssl=False if insecure_ssl else None,
                ) as resp:
                    # 遇到 429 且未达到最大重试次数：完全静默重试，不向会话发送任何信息
                    if resp.status == 429 and attempt < max_retries:
                        attempt += 1
                        logger.info(f"[NewImage/NovelAI] 遇到 429 频率限制，后台静默等待 {retry_delay}s 后重试 ({attempt}/{max_retries})...")
                        await asyncio.sleep(retry_delay)
                        continue

                    if resp.status != 200:
                        raw = await resp.read()
                        err_msg = f"NovelAI API请求失败 (HTTP {resp.status})"
                        try:
                            err_json = json.loads(raw.decode("utf-8", errors="replace"))
                            detail = (err_json.get("message") or err_json.get("detail")
                                      or err_json.get("error") or json.dumps(err_json, ensure_ascii=False))
                            err_msg = f"{err_msg}: {str(detail)[:300]}"
                        except Exception:
                            err_msg = f"{err_msg}: {raw[:200].decode('utf-8', errors='replace')}"
                        if resp.status == 429:
                            err_msg = f"⚠️ NovelAI 频率限制 (HTTP 429)，已连续静默重试 {max_retries} 次仍受限，请稍后重试。"
                        elif resp.status >= 500:
                            err_msg = f"⚠️ NovelAI 服务端繁忙或超时（HTTP {resp.status}），请稍后重试。"
                        logger.error(err_msg)
                        return err_msg

                    data = await resp.read()

                    # ========== 解压 ZIP，取第一张图 ==========
                    try:
                        zf = zipfile.ZipFile(io.BytesIO(data))
                        names = zf.namelist()
                        image_name = next((n for n in names
                                           if n.endswith(".png") or n.endswith(".webp") or n.endswith(".jpg")), None)
                        if image_name is None:
                            return f"NovelAI 响应中没有找到图片文件（zip 内含: {names[:5]}）"
                        return zf.read(image_name)
                    except zipfile.BadZipFile:
                        # 非 zip（可能是错误 JSON / 空响应）
                        body = data[:300].decode("utf-8", errors="replace")
                        logger.error(f"NovelAI 响应不是合法 ZIP: {body}")
                        try:
                            err_json = json.loads(body)
                            detail = (err_json.get("message") or err_json.get("detail")
                                      or err_json.get("error") or json.dumps(err_json, ensure_ascii=False))
                            return f"NovelAI 返回错误: {str(detail)[:300]}"
                        except Exception:
                            return f"NovelAI 返回了无法解析的响应（非图片 ZIP）: {body[:200]}"
            except asyncio.TimeoutError:
                logger.error("NovelAI API 请求超时")
                return "请求超时"
            except Exception as e:
                logger.error(f"调用 NovelAI API 时发生未知错误: {e}", exc_info=True)
                return f"发生未知错误: {e}"


    async def _call_novalai_reverse_proxy_api(self, api_url: str, api_key: str, model_name: str,
                                              image_bytes_list: List[bytes], prompt: str,
                                              image_size: Optional[str], source_info: str,
                                              gen_params: Optional[Dict] = None) -> bytes | str:
        """NovelAI 反代模式路由（GET {base_url}/generate）。

        适用场景：用户网络直连 NovelAI 官方不通，但走第三方反代（如 std.loliyc.com）可用。
        全部参数走 query string（tag/model/token/artist/negative/sampler/steps/scale/cfg/
        noise_schedule/nocache/size），响应可能是 JSON（含图片 URL，需再下载）或图片二进制（PNG）。

        反代对图生图支持有限：image_bytes_list 非空时明确报错（本次仅支持文生图）。

        gen_params: 合并后的生成参数（渠道默认 < 模型默认 < 显式），映射规则同官方路由：
            size → width/height；steps → clamp ≤28；scale → CFG clamp 0-10；
            negative_prompt → 负面词；sampler → 非法值回退默认。
        返回 bytes=生图成功 / str=失败原因。
        """
        # ========== 尺寸映射（与官方路由一致） ==========
        size_key = (gen_params or {}).get("size") or image_size or NOVALAI_DEFAULT_SIZE
        if size_key not in NOVALAI_SIZE_DIMENSIONS:
            size_key = NOVALAI_DEFAULT_SIZE
        width, height = NOVALAI_SIZE_DIMENSIONS[size_key]

        # ========== steps（免费档 clamp ≤28，默认 23） ==========
        try:
            steps = int((gen_params or {}).get("steps", NOVALAI_DEFAULT_STEPS))
        except (TypeError, ValueError):
            steps = NOVALAI_DEFAULT_STEPS
        steps = max(1, min(steps, NOVALAI_STEPS_MAX_FREE))

        # ========== scale（CFG，clamp 0-10，默认 6.0） ==========
        try:
            scale = float((gen_params or {}).get("scale", 6.0))
        except (TypeError, ValueError):
            scale = 6.0
        scale = max(0.0, min(scale, 10.0))

        # ========== 负面词：默认模板作底，用户/过滤词叠加在前 ==========
        # 语义：默认模板（质量保底词）始终保留；用户/画师串/NSFW 过滤词叠加在其前。
        # final_params["negative_prompt"] 可能是 "filter_tags, 画师串neg" / "filter_tags, 用户neg" 等形式。
        negative_prompt = NOVALAI_DEFAULT_NEGATIVE_PROMPT
        user_neg = (gen_params or {}).get("negative_prompt")
        if isinstance(user_neg, str) and user_neg.strip():
            negative_prompt = f"{user_neg.strip()}, {NOVALAI_DEFAULT_NEGATIVE_PROMPT}"

        # ========== sampler（可选，非法回退默认） ==========
        sampler = NOVALAI_DEFAULT_SAMPLER
        user_sampler = (gen_params or {}).get("sampler")
        if isinstance(user_sampler, str) and user_sampler.strip() in NOVALAI_SAMPLERS:
            sampler = user_sampler.strip()

        # ========== 图生图：反代模式暂不支持，明确报错 ==========
        if image_bytes_list:
            return "❌ NovelAI 反代模式暂不支持图生图，请仅使用文生图或关闭反代模式"

        # ========== 构造 GET query params ==========
        # token 去 "bearer " 前缀（兼容用户粘贴了完整 Authorization 头）
        token = api_key.strip()
        if token.lower().startswith("bearer "):
            token = token[len("bearer "):].strip()
        params: Dict[str, str] = {
            "tag": prompt or "",
            "model": model_name,
            "token": token,
            "artist": "",
            "negative": negative_prompt,
            "sampler": sampler,
            "steps": str(steps),
            "scale": str(scale),
            "seed": str(random.randint(0, 2 ** 32 - 1)),
            "cfg": "",
            "noise_schedule": "karras",
            "nocache": "",
            "size": f"{width}x{height}",
        }

        # ========== 端点 & 发送 ==========
        base = api_url.rstrip("/")
        endpoint = f"{base}/generate"
        logger.info(f"[NewImage/NovelAI-反代] 发送请求 [{source_info}]: Model={model_name}, "
                    f"Size={width}x{height}, Steps={steps}, Sampler={sampler}")

        # ========== 读取 429 自动重试配置 ==========
        retry_enabled = bool(self.conf.get("novalai_429_retry_enabled", True))
        try:
            max_retries = max(1, min(int(self.conf.get("novalai_429_max_retries", 10)), 50)) if retry_enabled else 0
        except (TypeError, ValueError):
            max_retries = 10 if retry_enabled else 0
        try:
            retry_delay = max(1, min(int(self.conf.get("novalai_429_retry_delay", 5)), 60))
        except (TypeError, ValueError):
            retry_delay = 5

        attempt = 0
        while True:
            try:
                if not self.iwf:
                    return "ImageWorkflow 未初始化"
                async with self.iwf.session.get(
                    endpoint, params=params,
                    ssl=False,  # 反代常为自签/无证书，跳过 TLS 校验
                    proxy=self.iwf.proxy, timeout=self.api_timeout,
                ) as resp:
                    # 遇到 429 且未达到最大重试次数：完全静默重试，不向会话发送任何信息
                    if resp.status == 429 and attempt < max_retries:
                        attempt += 1
                        logger.info(f"[NewImage/NovelAI-反代] 遇到 429 频率限制，后台静默等待 {retry_delay}s 后重试 ({attempt}/{max_retries})...")
                        await asyncio.sleep(retry_delay)
                        continue

                    if resp.status != 200:
                        raw = await resp.read()
                        err_msg = f"NovelAI 反代请求失败 (HTTP {resp.status})"
                        try:
                            err_json = json.loads(raw.decode("utf-8", errors="replace"))
                            detail = (err_json.get("message") or err_json.get("detail")
                                      or err_json.get("error") or json.dumps(err_json, ensure_ascii=False))
                            err_msg = f"{err_msg}: {str(detail)[:300]}"
                        except Exception:
                            err_msg = f"{err_msg}: {raw[:200].decode('utf-8', errors='replace')}"
                        if resp.status == 429:
                            err_msg = f"⚠️ NovelAI 反代频率限制 (HTTP 429)，已连续静默重试 {max_retries} 次仍受限，请稍后重试。"
                        elif resp.status >= 500:
                            err_msg = f"⚠️ NovelAI 反代服务端繁忙或超时（HTTP {resp.status}），请稍后重试。"
                        logger.error(err_msg)
                        return err_msg

                    content_type = resp.headers.get("Content-Type", "")
                    data = await resp.read()

                    # ========== JSON 响应：取图片 URL / data 再下载 ==========
                    if "json" in content_type.lower():
                        try:
                            payload = json.loads(data.decode("utf-8", errors="replace"))
                        except Exception:
                            return f"NovelAI 反代返回了无法解析的 JSON: {data[:200].decode('utf-8', errors='replace')}"
                        # 依次取 url/image_url/image/data 第一个非空字段
                        img_ref = None
                        if isinstance(payload, dict):
                            for k in ("url", "image_url", "image", "data"):
                                v = payload.get(k)
                                if v:
                                    img_ref = v
                                    break
                        if not img_ref:
                            return f"NovelAI 反代响应中未找到图片（JSON: {str(payload)[:300]}）"
                        img_url = str(img_ref)
                        if img_url.startswith("data:"):  # data URL（base64）
                            try:
                                b64_part = img_url.split(",", 1)[1]
                                return base64.b64decode(b64_part)
                            except Exception as e:
                                logger.error(f"[NewImage/NovelAI-反代] data URL 解码失败: {e}", exc_info=True)
                                return f"NovelAI 反代图片 data URL 解码失败: {e}"
                        # http(s) URL → 再下载
                        try:
                            async with self.iwf.session.get(img_url, ssl=False,
                                                            proxy=self.iwf.proxy,
                                                            timeout=self.api_timeout) as img_resp:
                                if img_resp.status != 200:
                                    return f"NovelAI 反代图片下载失败 (HTTP {img_resp.status})"
                                return await img_resp.read()
                        except asyncio.TimeoutError:
                            return "NovelAI 反代图片下载超时"
                        except Exception as e:
                            logger.error(f"[NewImage/NovelAI-反代] 下载图片失败: {e}", exc_info=True)
                            return f"NovelAI 反代图片下载失败: {e}"

                    # ========== 非 JSON：响应体即图片二进制（PNG） ==========
                    if not data:
                        return "NovelAI 反代返回了空响应"
                    return data
            except asyncio.TimeoutError:
                logger.error("NovelAI 反代请求超时")
                return "请求超时"
            except Exception as e:
                logger.error(f"调用 NovelAI 反代 API 时发生未知错误: {e}", exc_info=True)
                return f"发生未知错误: {e}"

