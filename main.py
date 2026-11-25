# -*- coding: utf-8 -*-
"""main.py — RainnyaImage 插件装配入口。

职责：只做「装配」——注册插件类、继承各职责 Mixin、初始化实例状态与生命周期钩子。
具体能力分散在同包模块中：
    constants.py          纯数据与无状态工具函数
    providers/            生图渠道驱动（Gemini / OpenAI 兼容 / NovelAI 官方+反代）
    services/             领域服务（状态持久化 / 配额 / 画师 / 媒体 / 交互任务）
    handlers/             AstrBot 接口层（群聊指令 / LLM 工具 / WebUI 后端接口）

AstrBot 的装饰器在「方法定义时」即完成注册，且归属判定按模块路径前缀匹配
（astrbot_plugin_rainnya_image.* 均属于本插件），因此 Mixin 拆分不影响指令与工具发现。
"""

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

from .constants import *
from .image_parser import extract_image_bytes_from_response
from .image_workflow import ImageWorkflow
from .storage import load_json, save_json

from .handlers.commands import CommandsMixin
from .handlers.llm_tools import LLMToolsMixin
from .handlers.web_api import WebAPIMixin
from .providers.base import RoutingMixin
from .providers.gemini import GeminiMixin
from .providers.novalai import NovalAIProviderMixin
from .providers.openai import OpenAIMixin
from .services.artist import ArtistMixin
from .services.interactive import InteractiveMixin
from .services.media import MediaMixin
from .services.quota import QuotaMixin
from .services.state import StateMixin


register('astrbot_plugin_rainnya_image', 'xianyunah', 'AI生图插件：支持Gemini原生API与OpenAI兼容双路由，图生图/文生图/自定义Prompt，含安全过滤、高分辨率白名单与签到系统', '1.2.4', 'https://github.com/Xianyunah/astrbot_plugin_rainnya_image')
class FigurineProPlugin(
    Star,
    # —— AstrBot 接口层（承载指令 / LLM 工具 / Web API 装饰器）——
    CommandsMixin,
    LLMToolsMixin,
    WebAPIMixin,
    # —— 生图渠道驱动层 ——
    RoutingMixin,
    GeminiMixin,
    OpenAIMixin,
    NovalAIProviderMixin,
    # —— 领域服务层 ——
    StateMixin,
    QuotaMixin,
    ArtistMixin,
    MediaMixin,
    InteractiveMixin,
):
    """RainnyaImage 全 OpenAI 兼容画图插件（装配入口，能力由 Mixin 提供）。"""

    WEB_PLUGIN_NAME = "astrbot_plugin_rainnya_image"


    _WEB_CONFIG_FIELD_TYPES = {
        # 基础
        "prefix": bool,              # 是否要求指令必须带前缀
        "extra_prefix": str,         # 自定义提示词模式的前缀
        "selfie_prompt": str,        # 自拍模式的人物外貌特征描述（generate_image selfie=true 时前置到提示词）
        "use_proxy": bool,           # 是否启用网络代理
        "proxy_url": str,            # 网络代理地址
        "download_timeout": int,     # 图片下载超时时间（秒）
        "download_retries": int,     # 图片下载重试次数
        "api_timeout": int,          # API请求等待超时时间（秒）
        # Gemini
        "safety_level": str,         # 安全过滤等级
        "image_size": str,           # 全局默认生成图片分辨率
        "hires_whitelist": list,     # 高分辨率白名单
        # OpenAI 渠道默认参数
        "openai_size": str,          # 默认尺寸 1K/2K/4K（空=不指定）
        "openai_temperature": float, # 0-2
        "openai_max_tokens": int,    # 64-16384
        # NovalAI 渠道默认参数
        "novalai_size": str,         # 竖/横×小中大档位键（空=不指定；旧 1K/2K/4K 兼容）
        "novalai_sampler": str,      # 采样器
        "novalai_steps": int,        # 1-50
        "novalai_scale": float,      # CFG 0-10
        "novalai_negative_prompt": str,  # 负面词（空=用默认模板）
        "novalai_429_retry_enabled": bool,  # 429自动重试开关
        "novalai_429_max_retries": int,     # 429最大重试次数
        "novalai_429_retry_delay": int,     # 429重试等待间隔(秒)
        # 权限
        "user_whitelist": list,
        "user_blacklist": list,
        "group_whitelist": list,
        "group_blacklist": list,
        # 次数
        "enable_user_limit": bool,
        "enable_group_limit": bool,
        "user_count_cap": int,
        "group_task_limit": int,
        # 签到
        "enable_checkin": bool,
        "checkin_fixed_reward": int,
        "enable_random_checkin": bool,
        "checkin_random_reward_min": int,
        "checkin_random_reward_max": int,
        # 帮助/预设
        "help_menu": str,
        "prompt_list": list,         # 生图触发词与提示词（每元素 "触发词:提示词"）
        # 打标（图片反推）
        "tagger_provider_id": str,   # 打标专用 LLM provider（空=用当前会话 provider）
        "tagger_temperature": float, # 打标 temperature
        "tagger_max_tokens": int,    # 打标 max_tokens
        # 画师推荐
        "artist_recommend_enabled": bool,
        "artist_recommend_max_candidates": int,
        "artist_recommend_min_match": int,
        # Danbooru 动态搜索（画师推荐兜底）
        "danbooru_enabled": bool,
        "danbooru_base_url": str,
        "danbooru_limit": int,
        "danbooru_min_posts": int,
        # NovelAI token 预检
        "novai_precheck_enabled": bool,
        "novai_precheck_timeout": int,
        "novai_point_warn_threshold": int,
        # 自动撤回
        "auto_recall_enabled": bool,
        "auto_recall_delay": int,
        "auto_recall_allowed_groups": list,
        # NSFW 过滤
        "nsfw_filter_enabled": bool,
        "nsfw_filter_tags": str,
        # NSFW 加密（LLM 工具 nsfw=true 走加密 ZIP 发送的双层开关）
        "nsfw_zip_enabled": bool,
        # LLM 工具系统提示词注入
        "enable_llm_tool_system_prompt": bool,
        "custom_llm_tool_system_prompt": str,
        # 智力矫正
        "force_full_params": bool,
        "force_english_tags": bool,
        "correction_reject_template": str,
    }


    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.plugin_data_dir = StarTools.get_data_dir()
        # 旧数据目录自动迁移：改名后 get_data_dir() 返回新目录，旧目录数据搬入
        self._migrate_legacy_data_dir()
        self.user_counts_file = self.plugin_data_dir / "user_counts.json"
        self.user_counts: Dict[str, int] = {}
        self.group_counts_file = self.plugin_data_dir / "group_counts.json"
        self.group_counts: Dict[str, int] = {}
        self.user_checkin_file = self.plugin_data_dir / "user_checkin.json"
        self.user_checkin_data: Dict[str, str] = {}
        self.group_model_file = self.plugin_data_dir / "group_model.json"
        self.group_models: Dict[str, str] = {}
        # 画师串预设（artist_presets.json：全局预设库；group_artist.json：按群开关）
        self.artist_presets_file = self.plugin_data_dir / "artist_presets.json"
        self.group_artist_file = self.plugin_data_dir / "group_artist.json"
        self.artist_presets: List[Dict[str, Any]] = []
        self.group_artists: Dict[str, str] = {}
        # NSFW 加密按群开关（group_nsfw_zip.json：{群ID: True/False}，只存「覆盖」的群）
        self.group_nsfw_zip_file = self.plugin_data_dir / "group_nsfw_zip.json"
        self.group_nsfw_zip: Dict[str, bool] = {}
        self.generation_history_file = self.plugin_data_dir / "generation_history.json"
        self.generation_history: List[Dict[str, Any]] = []
        self._history_lock = asyncio.Lock()  # 生图时间线并发追加/删除/落盘锁
        self._counts_lock = asyncio.Lock()   # 用户/群组次数 check-modify-write 串行化锁（防并发竞态）
        self._bg_tasks: set = set()          # 后台生图任务引用集合（防 GC + terminate 时回收）
        self._log_page_msgid: Dict[Any, Any] = {}  # (会话, 发送者) -> {"msgid": 消息id, "page": 页码, "scope": 范围, "count": 条数}
        self._pending_text_to_image: Dict[str, Dict[str, Any]] = {}  # 兼容旧版待确认文生图
        self._pending_generation_tasks: Dict[str, Dict[str, Any]] = {}  # 8位校验码绑定的交互式调参与确认生图任务池
        self.prompt_map: Dict[str, str] = {}
        self._last_artist_recommendation: str = ""  # 最近一次画师推荐结果（供 画师串保存 落库）
        self.iwf: Optional[ImageWorkflow] = None
        self.group_task_counts: Dict[str, int] = {}
        self.queue_lock = asyncio.Lock()
        self.group_task_limit: int = 0
        self.api_timeout: int = 120
        self.user_count_cap: int = 0  # 用户持有次数上限，0表示不限制
        # 多提供商架构（providers.json 持久化，见 _load_providers）
        self.providers_file = self.plugin_data_dir / "providers.json"
        self.providers: List[Dict[str, Any]] = []
        # Gemini 模式相关
        self.is_gemini_api: bool = False
        self.safety_level: str = ""
        self.default_image_size: str = ""
        self.hires_whitelist: List[str] = []
        # 模型相关
        # available_models: 手动配置的可用模型列表，元素为 {"name": 模型名, "quota": 每次消耗配额, "desc": 说明}
        self.available_models: List[Dict[str, Any]] = []
        # available_model_names: 仅模型名列表（供群配置/切换/取用，去重保持顺序）
        self.available_model_names: List[str] = []
        # Web API 相关（供插件 WebUI 调用）
        self._web_registered = False
        self._register_web_apis()


    async def initialize(self):
        use_proxy = self.conf.get("use_proxy", False)
        proxy_url = self.conf.get("proxy_url") if use_proxy else None

        # 获取下载配置
        dl_timeout = self.conf.get("download_timeout", 60)
        dl_retries = self.conf.get("download_retries", 3)

        self.iwf = ImageWorkflow(proxy_url, timeout=dl_timeout, retries=dl_retries)
        await self._load_prompt_map()
        await self._load_user_counts()
        await self._load_group_counts()
        await self._load_user_checkin_data()
        # 加载多提供商配置（providers.json）：构建 self.providers + 扁平化 available_models/available_model_names
        await self._load_providers()
        # NovelAI token 预检：对启用 novalai 提供商后台预检（不 await，结果写 provider["_user_status"]）
        if self.conf.get("novai_precheck_enabled", True):
            for p in self.providers:
                if p.get("type") == "novalai" and p.get("enabled", True):
                    api_key_p = str(p.get("api_key", "") or "").strip()
                    user_url_p = str(p.get("user_url", "") or "").strip() or str(p.get("base_url", "") or "").strip()
                    if not api_key_p or not user_url_p:
                        continue
                    pname = str(p.get("name", "") or p.get("id", "未命名"))
                    task = asyncio.create_task(self._run_novai_precheck(p, user_url_p, api_key_p, pname))
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
        if self.available_models:
            logger.info(f"NewImage: 已解析 {len(self.available_models)} 个可用模型（按每次消耗配额计次）")
            for m in self.available_models:
                _desc = f" - {m['desc']}" if m.get("desc") else ""
                logger.info(f"  [{m['name']}] 每次消耗 {m['quota']} 次{_desc}")
        await self._load_group_models()
        await self._load_artist_presets()
        await self._load_group_artists()
        await self._load_group_nsfw_zip()
        await self._load_generation_history()
        self.api_timeout = max(API_TIMEOUT_MIN_SECONDS, int(self.conf.get("api_timeout", 120)))
        logger.info(f"NewImage: API请求超时时间设置为 {self.api_timeout}s")
        # 加载用户持有次数上限
        try:
            self.user_count_cap = max(0, int(self.conf.get("user_count_cap", 0)))
        except (TypeError, ValueError):
            self.user_count_cap = 0
        if self.user_count_cap > 0:
            logger.info(f"NewImage: 用户持有次数上限设置为 {self.user_count_cap}")
        limit_raw = self.conf.get("group_task_limit", 2)
        try:
            self.group_task_limit = max(0, int(limit_raw))
        except (TypeError, ValueError):
            self.group_task_limit = 0
            logger.warning(f"NewImage: group_task_limit 配置无效 ({limit_raw})，已按 0 处理")
        self.group_task_counts.clear()

        # 加载 Gemini 模式配置
        self.is_gemini_api = bool(self.conf.get("is_gemini_api", False))
        self.safety_level = (self.conf.get("safety_level", "") or "").strip()
        self.default_image_size = (self.conf.get("image_size", "") or "").strip().upper()
        self.hires_whitelist = [str(x) for x in self.conf.get("hires_whitelist", [])]
        if self.is_gemini_api:
            logger.info(f"NewImage: Gemini 原生 API 模式已开启")
            if self.safety_level:
                logger.info(f"  安全过滤等级: {self.safety_level}")
            if self.default_image_size:
                logger.info(f"  默认图片分辨率: {self.default_image_size}")
            if self.hires_whitelist:
                logger.info(f"  高分辨率白名单: {len(self.hires_whitelist)} 人")

        # 多提供商已由 _load_providers 加载（providers.json），此处仅输出状态
        if not self.providers:
            logger.warning("NewImage: 未配置任何提供商，插件可能无法工作")
        else:
            _enabled = sum(1 for p in self.providers if p.get("enabled", True))
            logger.info(f"NewImage 插件已加载，共 {len(self.providers)} 个提供商（{_enabled} 个启用），"
                        f"{len(self.available_models)} 个可用模型")


    def _get_llm_tool_system_prompt(self) -> str:
        """获取注入到系统提示词中的生图函数工具说明。支持用户自定义覆盖与智力矫正模式适配。"""
        custom = str(self.conf.get("custom_llm_tool_system_prompt", "") or "").strip()
        if custom:
            return custom
        prompt_text = DEFAULT_LLM_TOOL_SYSTEM_PROMPT.strip()
        force_full = bool(self.conf.get("force_full_params", False))
        force_en = bool(self.conf.get("force_english_tags", False))
        if force_full or force_en:
            correction_rules = ["\n\n【⚠️ 智力矫正模式 - 强制调用规范】："]
            if force_full:
                correction_rules.append(
                    "- 必须【完整填写所有参数】：严禁漏填任何参数！调用 generate_image 时必须显式携带 prompt、model、params、selfie、nsfw、artist_preset 所有字段（不使用画师串时 artist_preset 传空字符串 \"\"；params 必须为包含 size/steps 等配置的对象，不可为空对象）。严禁偷懒省略参数！调用 query_presets 时必须显式携带 preset_name 与 category 两个字段（查全部画师串可传 preset_name=\"all\", category=\"artist\"）。"
                )
            if force_en:
                correction_rules.append(
                    "- 必须【纯英文 Tag】：prompt 参数中绝对严禁出现任何中文字符！必须将用户的所有需求完整扩写并翻译成纯英文 Tag 后再传入！"
                )
            correction_rules.append(
                "- 若违反上述规范，系统将直接严厉拒绝并打回，请务必一次性准确完整地填写所有参数！"
            )
            prompt_text += "\n".join(correction_rules)
        return prompt_text


    async def terminate(self):
        # 回收在途后台任务（防止插件卸载时任务悬挂）；优先 cancel 未完成的任务，再 gather，避免休眠任务阻断热重载
        if self._bg_tasks:
            pending = list(self._bg_tasks)
            self._bg_tasks.clear()
            for t in pending:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self.iwf:
            await self.iwf.terminate()
        logger.info("[FigurinePro] 插件已终止")

