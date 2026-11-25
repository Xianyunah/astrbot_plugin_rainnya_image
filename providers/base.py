# -*- coding: utf-8 -*-
"""providers/base.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class RoutingMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    async def _call_api(self, image_bytes_list: List[bytes], prompt: str, image_size: Optional[str] = None,
                        group_id: Optional[str] = None, model_override: Optional[str] = None,
                        gen_params: Optional[Dict] = None, size_strict: bool = False,
                        artist_preset_override: Optional[str] = None) -> tuple:
        """调用 API 生成图像，支持 Gemini 原生 API 和 OpenAI 兼容格式双路由。

        参数 model_override: LLM 工具指定模型用（优先级最高，覆盖本群配置/provider/默认模型）。
        参数 gen_params: 生成参数（显式指定），在方法内与最终模型的默认参数合并（显式 > 模型默认）。
        参数 size_strict: 模型 allowed_sizes 限制校验模式——True=指令路径（用户显式指定不支持分辨率
            时报错拒绝）；False=LLM 路径（不支持时夹取到允许的最大分辨率，仅日志记录）。
        参数 artist_preset_override: 显式指定要使用的画师串预设名（LLM 或单次调用指定，优先级高于本群绑定）。
        返回 (result, model_name) 二元组：
            - result: bytes=生图成功 / str=失败原因
            - model_name: 本次实际使用的模型名（错误早退路径可能为 ""）
        """

        # ========== 模型选择优先级：model_override > 本群配置模型 > available_model_names[0] 兜底 ==========
        model_name: str = ""
        if model_override:
            # 0. LLM 工具指定模型（最高优先）
            model_name = model_override
            logger.info(f"[NewImage] 使用指定模型: {model_name}")
        elif group_id and self.group_models.get(group_id):
            # 1. 本群通过「切换模型」指令配置的模型
            model_name = self.group_models[group_id]
            logger.info(f"[NewImage] 使用本群 {group_id} 配置的模型: {model_name}")
        elif self.available_model_names:
            # 2. 启用提供商中的默认模型（available_models 第一个）
            model_name = self.available_model_names[0]
            logger.info(f"[NewImage] 使用默认模型: {model_name}")
        else:
            # 3. 兜底 conf.model（兼容初始化未完成等场景）
            model_name = (self.conf.get("model", "") or "").strip()
            if not model_name:
                return "❌ 未配置任何生图模型，请在 WebUI 提供商管理中配置", ""

        # ========== 按模型定位所属提供商，取 API 配置 ==========
        # 优先选择包含该模型且可用的提供商（启用且 BaseURL 非空），防止被排在前面的停用/空壳提供商阻断
        provider = self._find_usable_provider_for_model(model_name)
        if not provider:
            any_provider = self._find_provider_for_model(model_name)
            if any_provider:
                pname = str(any_provider.get("name", "") or any_provider.get("id", "未命名"))
                if not any_provider.get("enabled", True):
                    return f"❌ 模型 {model_name} 所属提供商「{pname}」已停用，请在 WebUI 提供商管理中启用", model_name
                if not str(any_provider.get("base_url", "") or "").strip():
                    return f"❌ 提供商「{pname}」未配置 BaseURL，请在 WebUI 提供商管理中配置", model_name
            return f"❌ 模型 {model_name} 未关联任何提供商，请在 WebUI 提供商管理中配置", model_name
        api_url = str(provider.get("base_url", "") or "").strip()
        api_key = str(provider.get("api_key", "") or "").strip()
        provider_type = str(provider.get("type", "") or "openai").strip()
        provider_name = str(provider.get("name", "") or provider.get("id", "未命名"))
        # 跳过 SSL 证书校验（自建反代/私有网关常用自签证书；按提供商粒度开关，默认关闭）
        insecure_ssl = bool(provider.get("insecure_ssl", False))
        if insecure_ssl:
            logger.info(f"[NewImage] 提供商「{provider_name}」已启用跳过 SSL 证书校验")
        if not api_url:
            return f"❌ 提供商「{provider_name}」未配置 BaseURL，请在 WebUI 提供商管理中配置", model_name
        if not api_key:
            return f"❌ 提供商「{provider_name}」未配置 API Key，请在 WebUI 提供商管理中配置", model_name
        logger.info(f"[NewImage] 使用提供商: {provider_name}（{provider_type}）")
        logger.debug(f"  API URL: {api_url[:80]}...")

        source_info = f"提供商:{provider_name}"

        # ========== 合并生成参数：渠道默认 < 模型默认参数 < 显式 gen_params ==========
        # 渠道默认参数（openai_*/novalai_* 设置）作为基础层；模型默认覆盖渠道默认，显式最高。
        channel_defaults = self._get_channel_default_params(provider_type)
        final_params = _merge_params(channel_defaults, self._get_model_default_params(model_name), gen_params or {})
        # sampler/scale 不在 PARAM_SCHEMA，_merge_params 会丢弃：novalai 渠道单独补回
        # （二者无用户显式通道——指令/LLM 工具均经 PARAM_SCHEMA 过滤，唯一来源即渠道配置）
        if provider_type == "novalai":
            if channel_defaults.get("sampler") and not final_params.get("sampler"):
                final_params["sampler"] = channel_defaults["sampler"]
            if channel_defaults.get("scale") is not None and not final_params.get("scale"):
                final_params["scale"] = channel_defaults["scale"]

        # ========== 模型允许分辨率限制（allowed_sizes）统一校验 ==========
        # 说明：size 可能来自三条来源——显式 gen_params（指令 键:值 / LLM params）、指令 image_size
        # （2K/4K 白名单后缀，仅 Gemini 生效）、模型默认参数/全局默认。此处只对「最终生效的 size」
        # 做限制检查；size_strict=True（指令路径，用户显式指定了不支持的分辨率）→ 报错拒绝，
        # size_strict=False（LLM 路径）→ 夹取到 allowed_sizes 中数值最大者，并通过日志记录。
        allowed_sizes = self._get_model_allowed_sizes(model_name)
        if allowed_sizes:
            explicit_size = (gen_params or {}).get("size") or image_size
            effective_size = final_params.get("size") or explicit_size
            if effective_size and effective_size not in allowed_sizes:
                max_avail = _max_allowed_size(allowed_sizes)
                if size_strict and explicit_size:
                    # 指令路径：用户显式指定了该模型不支持的分辨率 → 明确报错
                    return (f"❌ 该模型（{model_name}）仅支持分辨率：{'/'.join(allowed_sizes)}"
                            f"（当前指定 {effective_size}）", model_name)
                if max_avail:
                    final_params["size"] = max_avail
                    logger.info(f"[NewImage] 模型 {model_name} 不支持分辨率 {effective_size}，"
                                f"已自动调整为允许的最大分辨率 {max_avail}")

        # ========== 画师串预设合并（优先级：显式指定 artist_preset_override > 按群 group_artists） ==========
        artist_preset = None
        if artist_preset_override:
            override_name = str(artist_preset_override).strip()
            # 优先精确匹配，其次不区分大小写匹配
            target = next((p for p in self.artist_presets if p.get("name") == override_name), None)
            if not target:
                target = next((p for p in self.artist_presets if p.get("name", "").lower() == override_name.lower()), None)
            if target:
                artist_preset = target
                logger.info(f"[NewImage] 显式应用指定的画师串预设「{target.get('name', '')}」")
            else:
                logger.warning(f"[NewImage] 显式指定的画师串预设「{artist_preset_override}」在库中未找到，回退本群默认画师串")

        if not artist_preset:
            artist_preset = self._get_group_artist_preset(group_id)

        if artist_preset:
            artists = str(artist_preset.get("artists", "") or "").strip()
            preset_neg = str(artist_preset.get("negative", "") or "").strip()
            if artists:
                prompt = f"{prompt}, {artists}" if prompt else artists
                logger.info(f"[NewImage] 应用画师串预设「{artist_preset.get('name', '')}」，prompt 追加画师标签")
            if preset_neg:
                existing_neg = str(final_params.get("negative_prompt", "") or "").strip()
                final_params["negative_prompt"] = f"{existing_neg}, {preset_neg}" if existing_neg else preset_neg
                logger.info(f"[NewImage] 画师串预设「{artist_preset.get('name', '')}」并入负面词")

        # ========== NSFW 过滤（统一前置到负面词） ==========
        # nsfw_filter_enabled 开启时，把过滤标签前置到负面词最前（画师串/渠道/模型/显式负面词之后仍会叠加）。
        # 对 Gemini / NovalAI 官方 / NovalAI 反代三条路由统一生效（它们都从 final_params["negative_prompt"] 读取）。
        if self.conf.get("nsfw_filter_enabled", False):
            filter_tags = str(self.conf.get("nsfw_filter_tags", "") or "").strip()
            if not filter_tags:
                filter_tags = "{{{{{nsfw}}}}}"
            existing_neg = str(final_params.get("negative_prompt", "") or "").strip()
            final_params["negative_prompt"] = f"{filter_tags}, {existing_neg}" if existing_neg else filter_tags
            logger.debug("[NewImage] 已应用 NSFW 过滤标签")

        # ========== 按提供商 type 选择路由 ==========
        if provider_type == "gemini":
            return await self._call_gemini_api(api_url, api_key, model_name, image_bytes_list, prompt,
                                               image_size, source_info, final_params,
                                               insecure_ssl=insecure_ssl), model_name
        elif provider_type == "novalai":
            # token 预检结果软校验（_user_status 由 initialize 后台写入；不拦截生图，仅打日志）
            user_status = provider.get("_user_status")
            if isinstance(user_status, dict):
                if not user_status.get("ok"):
                    logger.warning(f"[NewImage] NovelAI token 预检失败: {user_status.get('error')}")
                elif user_status.get("low_points_warning"):
                    logger.warning(f"[NewImage] NovelAI 剩余训练步数不足 "
                                   f"({user_status.get('training_steps_left')})，请及时充值/续费")
            if provider.get("use_reverse_proxy"):
                return await self._call_novalai_reverse_proxy_api(api_url, api_key, model_name, image_bytes_list,
                                                                  prompt, image_size, source_info,
                                                                  final_params), model_name
            return await self._call_novalai_api(api_url, api_key, model_name, image_bytes_list, prompt,
                                                image_size, source_info, final_params,
                                                insecure_ssl=insecure_ssl), model_name
        else:
            # OpenAI 兼容渠道：按提供商配置决定走 chat/completions 还是 images/generations
            # （muse-image 等纯图像模型必须走 images 端点，否则只会返回一句文字描述）
            api_mode = str(provider.get("openai_api_mode", "chat") or "chat").strip().lower()
            if api_mode == "images":
                logger.info(f"[NewImage] 提供商「{provider_name}」使用 Images API 模式"
                            f"（/v1/images/generations）")
                return await self._call_openai_images_api(api_url, api_key, model_name, image_bytes_list,
                                                          prompt, source_info, final_params,
                                                          insecure_ssl=insecure_ssl), model_name
            return await self._call_openai_api(api_url, api_key, model_name, image_bytes_list, prompt,
                                               source_info, final_params,
                                               insecure_ssl=insecure_ssl), model_name

