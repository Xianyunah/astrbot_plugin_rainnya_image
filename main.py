import asyncio
import base64
import functools
import io
import json
import random
import re
import shutil
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

from PIL import Image as PILImage

from astrbot import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .image_parser import extract_image_bytes_from_response
from .image_workflow import ImageWorkflow
from .storage import load_json, save_json


# ========== 模型详细生成参数 Schema（模块级） ==========
# 每项定义：类型 / 范围(min,max) / 可选值(choices) / 长度上限(max_len)
PARAM_SCHEMA = {
    "size":             {"type": "str",   "choices": ["1K", "2K", "4K"]},
    "temperature":      {"type": "float", "min": 0.0, "max": 2.0},
    "max_tokens":       {"type": "int",   "min": 64,  "max": 16384},
    "steps":            {"type": "int",   "min": 1,   "max": 150},
    "negative_prompt":  {"type": "str",   "max_len": 500},
}

# 各参数校验失败时的错误文案模板
_PARAM_ERROR_MSGS = {
    "size": "参数 size 取值无效，可选：1K/2K/4K",
    "temperature": "参数 temperature 超出范围（0.0-2.0）",
    "max_tokens": "参数 max_tokens 超出范围（64-16384）",
    "steps": "参数 steps 超出范围（1-150）",
    "negative_prompt": "参数 negative_prompt 超出长度（≤500）",
}

# 分辨率数值排序（用于 allowed_sizes 夹取到最大可用 / choices 夹取）
_SIZE_RANK = {"1K": 1, "2K": 2, "4K": 3}
_ALLOWED_SIZE_VALUES = tuple(_SIZE_RANK.keys())

# ========== 通用阈值 / 上限常量（替代魔法数字） ==========
GENERATION_HISTORY_MAX = 500      # 生图时间线历史上限（超出删除最旧）
MAX_IMAGES_PER_REQUEST = 5        # bnn 一次最多处理的图片数
WEB_PAGE_SIZE_MAX = 200           # Web 列表分页 page_size 上限
LOG_QUERY_LIMIT_MAX = 20          # 生图日志查询条数上限
PROMPT_SHORT_LEN = 15             # 日志/查询中 prompt 摘要截断长度
DISPLAY_PROMPT_LEN = 20           # 文生图/指令显示 prompt 预览长度
DISPLAY_CMD_LEN = 30              # 预设指令显示预览长度
API_TIMEOUT_MIN_SECONDS = 10      # API 超时下限
LOG_IMAGE_MAX_BYTES = 20 * 1024 * 1024  # 日志图片大小上限（20MB），超过返回 413

# ========== NovelAI（novalai）提供商路由常量 ==========
NOVALAI_DEFAULT_BASE_URL = "https://image.novelai.net"
NOVALAI_DEFAULT_NEGATIVE_PROMPT = ("nsfw, lowres, {bad}, error, fewer, extra, missing, worst quality, "
                                   "jpeg artifacts, bad quality, watermark, unfinished, displeasing, "
                                   "chromatic aberration, signature, extra digits, artistic error, "
                                   "username, scan, [abstract]")
# size 键（1K/2K/4K）→ 宽高映射（需 64 倍数，竖版 2:3 比例）
NOVALAI_SIZE_DIMENSIONS = {
    "1K": (832, 1216),
    "2K": (1152, 1664),
    "4K": (1664, 2432),
}
NOVALAI_DEFAULT_SIZE = "1K"
NOVALAI_DEFAULT_WIDTH, NOVALAI_DEFAULT_HEIGHT = 832, 1216
NOVALAI_DEFAULT_STEPS = 23          # 免费模式 steps 上限 28，付费可更大（保守用 23）
NOVALAI_STEPS_MAX_FREE = 28         # 免费模式 steps 上限
NOVALAI_MAX_PIXELS_FREE = 1048576   # 免费模式面积上限（px²）
NOVALAI_MAX_PIXELS_PAID = 3145728   # 付费模式面积上限（px²）
NOVALAI_MIN_DIM = 64                # 宽高最小单位（64 的倍数）
NOVALAI_SAMPLERS = ("k_euler_ancestral", "k_dpmpp_2s_ancestral", "k_dpmpp_2m_sde",
                    "k_euler", "k_dpmpp_2m", "k_dpmpp_sde")
NOVALAI_DEFAULT_SAMPLER = "k_euler_ancestral"
NOVALAI_IMG2IMG_STRENGTH = 0.7
NOVALAI_IMG2IMG_NOISE = 0.2
NOVALAI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ========== 各提供商类型的默认模型模板（添加提供商时自动填充，可再编辑） ==========
# novalai 列表来自反代 std.loliyc.com 的 /v1/models 实测（2026-08-30）
PROVIDER_DEFAULT_MODELS: Dict[str, List[Dict[str, Any]]] = {
    "gemini": [
        {"name": "google/gemini-2.0-flash-exp:free", "quota": 1, "desc": "Gemini 默认模型", "params": {}, "allowed_sizes": None},
    ],
    "openai": [
        {"name": "gpt-image-1", "quota": 1, "desc": "OpenAI 默认图像模型", "params": {}, "allowed_sizes": None},
    ],
    "novalai": [
        {"name": "nai-diffusion-4-full", "quota": 1, "desc": "V4 全量（默认）", "params": {}, "allowed_sizes": None},
        {"name": "nai-diffusion-4-curated-preview", "quota": 1, "desc": "V4 精选预览", "params": {}, "allowed_sizes": None},
        {"name": "nai-diffusion-4-5-full", "quota": 1, "desc": "V4.5 全量", "params": {}, "allowed_sizes": None},
        {"name": "nai-diffusion-4-5-curated", "quota": 1, "desc": "V4.5 精选", "params": {}, "allowed_sizes": None},
        {"name": "nai-diffusion-5-full", "quota": 1, "desc": "V5 全量（最新）", "params": {}, "allowed_sizes": None},
        {"name": "nai-diffusion-5-curated", "quota": 1, "desc": "V5 精选（最新）", "params": {}, "allowed_sizes": None},
        {"name": "nai-diffusion-3", "quota": 1, "desc": "V3 标准", "params": {}, "allowed_sizes": None},
        {"name": "nai-diffusion-furry-3", "quota": 1, "desc": "V3 Furry", "params": {}, "allowed_sizes": None},
        {"name": "director", "quota": 1, "desc": "构图/导演模式", "params": {}, "allowed_sizes": None},
    ],
}


def _choice_rank(key: str, value: str) -> float:
    """返回 choices 字符串参数的可比数值：size 用 _SIZE_RANK，其余尝试转 float；无法比较返回 -1。"""
    if key == "size":
        return _SIZE_RANK.get(value, -1)
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def _max_allowed_size(allowed_sizes: List[str]) -> Optional[str]:
    """返回 allowed_sizes 中数值最大（4K>2K>1K）的分辨率；列表为空返回 None。"""
    if not allowed_sizes:
        return None
    return max(allowed_sizes, key=lambda s: _SIZE_RANK.get(s, 0))


def _coerce_param_value(key: str, value: Any, clamp: bool) -> tuple:
    """校验/转换单个参数值；返回 (ok: bool, coerced: Any)。

    clamp=True 时数值越界夹取到边界（LLM/合并用）；clamp=False 时越界判非法（指令/配置用）。
    类型不合法、choices 不匹配、超长（clamp 时可截断）→ ok=False 或夹取后返回。
    """
    schema = PARAM_SCHEMA.get(key)
    if not schema:
        return False, None
    ptype = schema["type"]
    if ptype == "str":
        if not isinstance(value, str):
            return False, None
        max_len = schema.get("max_len")
        if max_len is not None and len(value) > max_len:
            if clamp:
                return True, value[:max_len]
            return False, None
        choices = schema.get("choices")
        if choices:
            if value in choices:
                return True, value
            if clamp:
                # choices 越界夹取：取「数值上不超过输入值的最大可选项」；无匹配则取数值最大者
                target = _choice_rank(key, value)
                best = None
                for c in choices:
                    r = _choice_rank(key, c)
                    if r is None:
                        continue
                    if target is not None and r <= target and (best is None or r > _choice_rank(key, best)):
                        best = c
                if best is None:
                    best = max(choices, key=lambda c: _choice_rank(key, c) or -1)
                return True, best
            return False, None
        return True, value
    if ptype == "float":
        if isinstance(value, bool):
            return False, None
        try:
            val = float(value)
        except (TypeError, ValueError):
            return False, None
        mn, mx = schema["min"], schema["max"]
        if val < mn or val > mx:
            if clamp:
                val = min(max(val, mn), mx)
            else:
                return False, None
        return True, val
    if ptype == "int":
        if isinstance(value, bool):
            return False, None
        if isinstance(value, int):
            val = value
        elif isinstance(value, str) and re.fullmatch(r'-?\d+', value.strip()):
            val = int(value.strip())
        else:
            return False, None
        mn, mx = schema["min"], schema["max"]
        if val < mn or val > mx:
            if clamp:
                val = min(max(val, mn), mx)
            else:
                return False, None
        return True, val
    return False, None


def _merge_params(*dicts: Optional[Dict]) -> Dict:
    """合并多份参数（后者覆盖前者），仅保留 PARAM_SCHEMA 已知键。"""
    merged: Dict = {}
    for d in dicts:
        if not d or not isinstance(d, dict):
            continue
        for k, v in d.items():
            if k in PARAM_SCHEMA:
                merged[k] = v
    return merged


@register(
    "astrbot_plugin_rainnya_image",
    "xianyunah",
    "AI生图插件：支持Gemini原生API与OpenAI兼容双路由，图生图/文生图/自定义Prompt，含安全过滤、高分辨率白名单与签到系统",
    "1.1.8",
    "https://github.com/Xianyunah/astrbot_plugin_rainnya_image",
)
class FigurineProPlugin(Star):

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
        self.generation_history_file = self.plugin_data_dir / "generation_history.json"
        self.generation_history: List[Dict[str, Any]] = []
        self._history_lock = asyncio.Lock()  # 生图时间线并发追加/删除/落盘锁
        self._counts_lock = asyncio.Lock()   # 用户/群组次数 check-modify-write 串行化锁（防并发竞态）
        self._bg_tasks: set = set()          # 后台生图任务引用集合（防 GC + terminate 时回收）
        self._log_page_msgid: Dict[Any, Any] = {}  # (会话, 发送者) -> {"msgid": 消息id, "page": 页码, "scope": 范围, "count": 条数}
        self.prompt_map: Dict[str, str] = {}
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

    def _migrate_legacy_data_dir(self) -> None:
        """旧数据目录自动迁移（改名 astrbot_plugin_nova_newimage → astrbot_plugin_rainnya_image）。

        改名后 StarTools.get_data_dir() 按 metadata.name 返回新目录（data/plugin_data/astrbot_plugin_rainnya_image），
        旧目录显式写死为 parent / "astrbot_plugin_nova_newimage"（不依赖动态解析，杜绝歧义）。

        迁移条件：旧目录存在、且与新目录不同（防自迁移）、且新目录尚未有同名条目。
        方式：整体 shutil.move（新目录不存在时）；新目录已存在（get_data_dir 已 ensure_dir）则逐条 move 子项。
        失败只 warn 不崩溃（数据缺失比插件崩好）。
        """
        old_dir = self.plugin_data_dir.parent / "astrbot_plugin_nova_newimage"
        try:
            if not old_dir.exists():
                return
            if old_dir.resolve() == self.plugin_data_dir.resolve():
                logger.info(f"[NewImage] 数据目录已是新路径 {self.plugin_data_dir}，无需迁移")
                return
            # 已迁移过（旧目录残留空壳）直接跳过
            if self.plugin_data_dir.exists() and any(self.plugin_data_dir.iterdir()):
                logger.info(f"[NewImage] 新数据目录已有内容 {self.plugin_data_dir}，跳过旧目录迁移")
                return
            if not self.plugin_data_dir.exists():
                # 新目录不存在：整体 move（含 generations 图片等所有条目）
                shutil.move(str(old_dir), str(self.plugin_data_dir))
            else:
                # 新目录已存在但为空：逐条 move（文件 + 子目录都搬）
                for item in old_dir.iterdir():
                    target = self.plugin_data_dir / item.name
                    if target.exists():
                        continue  # 冲突跳过，保留新目录内容
                    shutil.move(str(item), str(target))
                # 旧目录搬空后清理空壳（失败可忽略）
                try:
                    old_dir.rmdir()
                except OSError:
                    pass
            logger.info(f"[NewImage] 已从旧数据目录 {old_dir} 迁移数据到 {self.plugin_data_dir}")
        except Exception as e:
            logger.warning(f"[NewImage] 旧数据目录迁移失败（忽略，插件继续运行）: {e}")

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
        if self.available_models:
            logger.info(f"NewImage: 已解析 {len(self.available_models)} 个可用模型（按每次消耗配额计次）")
            for m in self.available_models:
                _desc = f" - {m['desc']}" if m.get("desc") else ""
                logger.info(f"  [{m['name']}] 每次消耗 {m['quota']} 次{_desc}")
        await self._load_group_models()
        await self._load_artist_presets()
        await self._load_group_artists()
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

    async def _load_providers(self) -> None:
        """加载多提供商配置（providers.json）并扁平化 available_models / available_model_names。

        providers.json 不存在或 providers 为空时，从旧版手动配置（conf.model / api_url / api_keys /
        is_gemini_api）迁移构建一个「默认提供商」并写回 providers.json（一次性的，后续以 providers.json 为准）。

        扁平化规则：遍历所有提供商（含禁用），仅启用提供商的模型进入 available_models / available_model_names，
        元素在原有 name/quota/desc/params/allowed_sizes 结构上新增 provider_id 字段，其余逻辑全部兼容。
        模型名去重（保留顺序），重复名取第一个出现的提供商/条目。
        """
        self.providers = []
        data = await load_json(self.providers_file)
        if isinstance(data, dict):
            providers = data.get("providers")
            if isinstance(providers, list):
                self.providers = [p for p in providers if isinstance(p, dict)]
        if not self.providers:
            # 迁移：旧配置不存在 providers.json 或为空 → 构建默认提供商并写回
            self.providers = self._build_legacy_provider()
            try:
                await save_json(self.providers_file, {"providers": self.providers})
                logger.info(f"[NewImage] 已从旧版手动配置迁移生成默认提供商，写入 {self.providers_file}")
            except Exception as e:
                logger.error(f"[NewImage] 写入 providers.json 失败: {e}", exc_info=True)
        # 扁平化
        self.available_models = []
        self.available_model_names = []
        seen: set = set()
        for p in self.providers:
            if not p.get("enabled", True):
                continue  # 禁用的提供商不参与可用模型列表
            if not str(p.get("base_url", "") or "").strip():
                continue  # base_url 为空的提供商不可用，其模型不进可用列表（避免被当作默认模型）
            pid = str(p.get("id", ""))
            for m in (p.get("models") or []):
                if not isinstance(m, dict):
                    continue
                name = str(m.get("name", "") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                self.available_models.append({
                    "provider_id": pid,
                    "name": name,
                    "quota": int(m.get("quota", 1)),
                    "desc": str(m.get("desc", "") or ""),
                    "params": dict(m.get("params") or {}),
                    "allowed_sizes": m.get("allowed_sizes"),
                    "use_reverse_proxy": bool(p.get("use_reverse_proxy", False)),
                })
                self.available_model_names.append(name)

    def _build_legacy_provider(self) -> List[Dict[str, Any]]:
        """从旧版手动配置构建单个「默认提供商」（迁移用）。

        type = gemini（is_gemini_api 开）/ openai；api_key = conf.api_keys[0]；
        base_url = conf.api_url；models = 复用 _parse_models_lines 解析 conf.model。

        重要：conf.api_url 为空时返回空列表（不建空壳提供商）。
        旧版若未配置手动 API，说明用户一直走 AstrBot provider_id 或根本没配过，没有可迁移的数据，
        不应生成一个 base_url="" 的 stub 提供商污染 providers.json（否则会占住 available_models 首位，
        导致后续 WebUI 添加的 novalai/openai 提供商模型永远排在被当作默认模型之后）。
        """
        api_url = str(self.conf.get("api_url", "") or "").strip()
        if not api_url:
            return []
        raw_model = str(self.conf.get("model", "") or "")
        models = self._parse_models_lines(raw_model.split("\n"))
        keys = self.conf.get("api_keys", []) or []
        api_key = str(keys[0]) if keys else ""
        return [{
            "id": self._new_provider_id(),
            "name": "默认提供商",
            "type": "gemini" if bool(self.conf.get("is_gemini_api", False)) else "openai",
            "api_key": api_key,
            "base_url": api_url,
            "enabled": True,
            "models": models,
        }]

    def _new_provider_id(self) -> str:
        """生成唯一提供商 id（p_ + 毫秒时间戳 + 随机数）。"""
        return f"p_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"

    async def _save_providers(self) -> None:
        """原子写 providers.json（self.providers 当前内存态）。"""
        try:
            await save_json(self.providers_file, {"providers": self.providers})
        except Exception as e:
            logger.error(f"[NewImage] 写入 providers.json 失败: {e}", exc_info=True)

    def _find_provider_for_model(self, name: str) -> Optional[Dict[str, Any]]:
        """在 self.providers 中查找包含指定模型的提供商（按模型名匹配，返回首个）。

        找不到返回 None。注意：本方法不区分启用/禁用、不区分 base_url 是否为空，
        由调用方（_call_api）判定并给出针对性错误。
        """
        if not name:
            return None
        for p in self.providers:
            for m in (p.get("models") or []):
                if isinstance(m, dict) and str(m.get("name", "") or "").strip() == name:
                    return p
        return None

    def _find_usable_provider_for_model(self, name: str) -> Optional[Dict[str, Any]]:
        """在 self.providers 中查找「包含该模型 且 可用」的提供商。

        可用 = 启用（enabled 缺省视为 True）且 base_url 非空。
        用于 _call_api 的兜底：当 _find_provider_for_model 命中一个 base_url 为空的
        stub 提供商（旧迁移残留）时，优先改用真正可用的提供商，避免误报「未配置 BaseURL」。
        找不到返回 None。
        """
        if not name:
            return None
        for p in self.providers:
            if not p.get("enabled", True):
                continue
            if not str(p.get("base_url", "") or "").strip():
                continue
            for m in (p.get("models") or []):
                if isinstance(m, dict) and str(m.get("name", "") or "").strip() == name:
                    return p
        return None

    def _mask_api_key(self, key: str) -> str:
        """API Key 掩码：前4位***后4位；空串返回空串。"""
        key = str(key or "")
        if not key:
            return ""
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}***{key[-4:]}"

    def _parse_model_line(self, line: str) -> Optional[Dict[str, Any]]:
        """解析单行模型配置（格式：模型名;每次消耗配额;说明;{参数JSON}，分号分隔）。

        兼容旧格式：只有模型名 → 配额 1、说明空；2 段 → 配额（非法则 1）；3 段 → 含说明。
        第 4 段为参数 JSON（默认生成参数，可缺省）：只保留 PARAM_SCHEMA 中的键，
        对已知键做类型/范围校验，非法或越界的丢弃（log.warning，配置数据宁可丢弃不报错）。
        参数 JSON 里可含 `sizes` 键（模型允许分辨率限制，如 {"sizes":["1K"]}）：
        识别后从 params 中移除并写入条目 allowed_sizes（None=不限制走全局；空数组视为不限制；
        值非法则忽略该键 log.warning）。解析失败返回 None（空行/无模型名）。
        """
        line = line.strip()
        if not line:
            return None
        parts = line.split(";")
        name = parts[0].strip()
        if not name:
            return None
        quota = 1
        desc = ""
        params: Dict = {}
        allowed_sizes: Optional[List[str]] = None
        if len(parts) >= 2:
            q_raw = parts[1].strip()
            try:
                quota = max(1, int(q_raw))
            except (TypeError, ValueError):
                quota = 1
        if len(parts) >= 3:
            # desc 半角分号已由 _serialize_models_lines 转义为全角，此处还原（round-trip 对称）
            desc = parts[2].strip().replace("；", ";")
        if len(parts) >= 4:
            raw_params = parts[3].strip()
            if raw_params:
                try:
                    parsed = json.loads(raw_params)
                except (ValueError, TypeError):
                    # 容错回退：JSON 解析失败时保留 name/quota/desc，params 置空，不丢弃整个模型
                    logger.warning(f"[NewImage] 模型 {name} 的参数JSON解析失败，已忽略: {raw_params}")
                    parsed = None
                if isinstance(parsed, dict):
                    # 先识别 sizes 键（模型允许分辨率限制），取出并从 params 中移除
                    if "sizes" in parsed:
                        sizes_raw = parsed.pop("sizes")
                        if isinstance(sizes_raw, list) and sizes_raw == []:
                            allowed_sizes = None  # 空数组视为不限制
                        elif (isinstance(sizes_raw, list) and sizes_raw
                              and all(isinstance(s, str) and s in _ALLOWED_SIZE_VALUES for s in sizes_raw)):
                            allowed_sizes = list(dict.fromkeys(sizes_raw))  # 去重保序
                        else:
                            logger.warning(f"[NewImage] 模型 {name} 的 sizes 键非法，已忽略: {sizes_raw}")
                    for pk, pv in parsed.items():
                        if pk not in PARAM_SCHEMA:
                            logger.warning(f"[NewImage] 模型 {name} 存在未知参数 {pk}，已忽略")
                            continue
                        ok, coerced = _coerce_param_value(pk, pv, clamp=False)
                        if not ok:
                            logger.warning(f"[NewImage] 模型 {name} 参数 {pk} 非法或越界，已丢弃")
                            continue
                        params[pk] = coerced
        return {"name": name, "quota": quota, "desc": desc, "params": params,
                "allowed_sizes": allowed_sizes}

    def _parse_models_lines(self, lines: List[str]) -> List[Dict[str, Any]]:
        """把模型行数组解析为模型 dict 列表（复用 _parse_model_line，去重保留顺序，重复名取第一条）。"""
        result: List[Dict[str, Any]] = []
        seen: set = set()
        for line in lines:
            entry = self._parse_model_line(line)
            if not entry:
                continue
            if entry["name"] in seen:
                continue
            seen.add(entry["name"])
            result.append(entry)
        return result

    def _serialize_models_lines(self, models: List[Any]) -> tuple:
        """校验并序列化模型 dict 列表为行数组（格式：模型名;配额;说明;{参数JSON}）。

        返回 (lines, error)：成功时 error 为 None；校验失败时 lines 为空列表、error 为错误消息。
        校验规则与旧 _web_models_post 一致：name 非空、quota 整数 >=1、desc 半角分号转义全角、
        params 经 _sanitize_params 温和清洗、allowed_sizes 仅 1K/2K/4K 或 null。
        """
        lines: List[str] = []
        for m in models:
            if not isinstance(m, dict):
                return [], "models 元素必须是对象"
            name = str(m.get("name", "")).strip()
            if not name:
                return [], "模型名不能为空"
            try:
                quota = int(m.get("quota", 1))
            except (TypeError, ValueError):
                return [], f"模型 {name} 的配额必须为整数"
            if quota < 1:
                return [], f"模型 {name} 的配额必须 >= 1"
            # desc 中的半角分号转义为全角（防与模型行分隔符冲突），解析时还原（见 _parse_model_line）
            desc = str(m.get("desc", "")).strip().replace(";", "；")
            # 接受 params（dict），用 _sanitize_params 温和清洗（夹取/丢弃）后序列化为第 4 段 JSON
            raw_params = m.get("params")
            if raw_params is None:
                params, _ = {}, []
            else:
                params, _ = self._sanitize_params(raw_params, "WebUI")
            # 接受 allowed_sizes（List[str] 或 null；null/空数组 = 不限制，不写 sizes 键）
            allowed_sizes = m.get("allowed_sizes")
            if allowed_sizes is not None:
                if not isinstance(allowed_sizes, list) or not all(
                        isinstance(s, str) and s in _ALLOWED_SIZE_VALUES for s in allowed_sizes):
                    return [], f"模型 {name} 的 allowed_sizes 只能包含 1K/2K/4K 字符串数组或 null"
                if allowed_sizes:
                    params["sizes"] = list(dict.fromkeys(allowed_sizes))
            if params:
                params_json = json.dumps(params, ensure_ascii=False)
                lines.append(f"{name};{quota};{desc};{params_json}")
            elif desc:
                lines.append(f"{name};{quota};{desc}")
            else:
                lines.append(f"{name};{quota}")
        return lines, None

    def _get_model_entry(self, name: str) -> Optional[Dict]:
        """按模型名查 available_models（含 params），找不到返回 None。"""
        if not name:
            return None
        for m in self.available_models:
            if m.get("name") == name:
                return m
        return None

    def _get_model_quota(self, name: str) -> int:
        """返回该模型每次生成消耗的配额（找不到返回 1）。"""
        entry = self._get_model_entry(name)
        return int(entry.get("quota", 1)) if entry else 1

    def _get_model_default_params(self, name: str) -> Dict:
        """返回该模型默认生成参数（params 的副本；找不到或无参数返回空 dict）。"""
        entry = self._get_model_entry(name)
        if not entry:
            return {}
        return dict(entry.get("params") or {})

    def _get_model_allowed_sizes(self, name: str) -> Optional[List[str]]:
        """返回该模型允许的分辨率列表（None=不限制走全局 1K/2K/4K）。"""
        entry = self._get_model_entry(name)
        if not entry:
            return None
        return entry.get("allowed_sizes")

    def _get_channel_default_params(self, provider_type: str) -> Dict:
        """按渠道（provider_type）从 conf 读取渠道默认生成参数。

        优先级：渠道默认 < 模型默认 params < 显式 gen_params（合并处见 _call_api）。
        - openai：size / temperature / max_tokens
        - novalai：size / sampler / steps / scale / negative_prompt
        - gemini：无（safety_level / image_size / hires_whitelist 有各自独立用途，不并入 gen_params）
        size 空串不写入（避免覆盖模型默认）；数值字段带默认值兜底。
        """
        defaults: Dict[str, Any] = {}
        if provider_type == "openai":
            size = str(self.conf.get("openai_size", "") or "").strip()
            if size:
                defaults["size"] = size
            try:
                defaults["temperature"] = float(self.conf.get("openai_temperature", 1.0))
            except (TypeError, ValueError):
                defaults["temperature"] = 1.0
            try:
                defaults["max_tokens"] = int(self.conf.get("openai_max_tokens", 2048))
            except (TypeError, ValueError):
                defaults["max_tokens"] = 2048
        elif provider_type == "novalai":
            size = str(self.conf.get("novalai_size", "") or "").strip()
            if size:
                defaults["size"] = size
            sampler = str(self.conf.get("novalai_sampler", "") or "").strip()
            if sampler:
                defaults["sampler"] = sampler
            try:
                defaults["steps"] = int(self.conf.get("novalai_steps", 23))
            except (TypeError, ValueError):
                defaults["steps"] = 23
            try:
                defaults["scale"] = float(self.conf.get("novalai_scale", 6.0))
            except (TypeError, ValueError):
                defaults["scale"] = 6.0
            neg = str(self.conf.get("novalai_negative_prompt", "") or "").strip()
            if neg:
                defaults["negative_prompt"] = neg
        return defaults

    def _parse_and_validate_params(self, text: str, allowed_extra: Dict[str, str]) -> tuple:
        """从指令文本**开头**提取连续 `键:值` 生成参数（只取前缀段，避免误伤 prompt 正文）。

        返回 (params_dict, cleaned_text)：
            - params_dict: 提取且校验通过的参数（仅 PARAM_SCHEMA 已知键 + allowed_extra 透传键）
            - cleaned_text: 移除已提取参数前缀后的 prompt 文本（原样保留剩余内容，不删任何字）
        校验失败返回 (None, 错误消息)。未知键不是参数，立即停止提取并把剩余文本原样返回。
        """
        if not text:
            return {}, text
        params: Dict = {}
        pos = 0
        n = len(text)
        # 只匹配开头的连续参数段：`键:值`（值支持 "..." 引号或非空白非冒号串）
        while pos < n:
            m = re.match(r'\s*([a-z_]+):("(?:[^"\\]|\\.)*"|[^\s:]+)', text[pos:])
            if not m:
                break  # 开头不是参数，停止提取
            key, raw_val = m.group(1), m.group(2)
            if key not in PARAM_SCHEMA and key not in allowed_extra:
                break  # 未知键不是参数，停止并原样保留（含本次匹配前的空白）
            # 去掉引号（支持 "..." 包裹的值，允许空格/冒号）
            if raw_val.startswith('"') and raw_val.endswith('"'):
                try:
                    val = json.loads(raw_val)
                except (ValueError, TypeError):
                    val = raw_val[1:-1]
            else:
                val = raw_val
            if key in PARAM_SCHEMA:
                ok, coerced = _coerce_param_value(key, val, clamp=False)
                if not ok:
                    return None, _PARAM_ERROR_MSGS.get(key, f"参数 {key} 无效")
                params[key] = coerced
            else:
                # allowed_extra 透传键：原样保留
                params[key] = val
            pos += m.end()
        return params, text[pos:].lstrip()

    def _sanitize_params(self, raw: Dict, source: str) -> tuple:
        """对传入参数 dict（LLM 工具/Web API 等）做温和清洗。

        逐键校验：数值越界夹取到边界；类型错/未知键丢弃。
        返回 (清洗后 params, 丢弃键列表)。
        """
        if not isinstance(raw, dict):
            logger.warning(f"[NewImage] {source} 传入参数必须是对象，已忽略")
            return {}, []
        cleaned: Dict = {}
        dropped: List[str] = []
        for k, v in raw.items():
            if k not in PARAM_SCHEMA:
                dropped.append(k)
                continue
            ok, coerced = _coerce_param_value(k, v, clamp=True)
            if not ok:
                dropped.append(k)
                continue
            cleaned[k] = coerced
        if dropped:
            logger.warning(f"[NewImage] {source} 参数被丢弃: {', '.join(map(str, dropped))}")
        return cleaned, dropped

    async def _load_prompt_map(self):
        logger.info("正在加载 prompts...")
        self.prompt_map.clear()
        prompt_list = self.conf.get("prompt_list", [])
        for item in prompt_list:
            try:
                if ":" in item:
                    key, value = item.split(":", 1)
                    self.prompt_map[key.strip()] = value.strip()
                else:
                    logger.warning(f"跳过格式错误的 prompt (缺少冒号): {item}")
            except ValueError:
                logger.warning(f"跳过格式错误的 prompt: {item}")
        logger.info(f"加载了 {len(self.prompt_map)} 个 prompts。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_figurine_request(self, event: AstrMessageEvent):
        if self.conf.get("prefix", True) and not event.is_at_or_wake_command:
            return
        text = event.message_str.strip()
        if not text: return
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        cmd = text.split()[0].strip()
        bnn_command = self.conf.get("extra_prefix", "bnn")
        user_prompt = ""
        is_bnn = False
        extracted_size = None  # 用户指定的分辨率参数
        gen_params: Dict = {}  # 用户通过键:值显式指定的生成参数（显式 > 模型默认）
        if cmd == bnn_command:
            user_prompt = text.removeprefix(cmd).strip()
            # 清除 @ 文本残留
            user_prompt = re.sub(r'@\S+?\(\d+\)', '', user_prompt).strip()
            user_prompt = re.sub(r'@\S+', '', user_prompt).strip()
            is_bnn = True
            if not user_prompt:
                yield event.plain_result("📝 用法：bnn <你的绘画想法>\n例如：bnn 樱花树下打伞的女孩")
                return
            # 从 bnn 的自定义 prompt 中提取分辨率（仅白名单用户 + Gemini 提供商）
            user_prompt, extracted_size = self._extract_image_size(user_prompt, sender_id, group_id=group_id)
            if not user_prompt:
                yield event.plain_result("📝 用法：bnn <你的绘画想法>\n例如：bnn 樱花树下打伞的女孩")
                return
            # 提取生成参数（键:值），校验失败明确报错不生成
            params, cleaned = self._parse_and_validate_params(user_prompt, {})
            if params is None:
                yield event.plain_result(f"❌ {cleaned}")
                return
            gen_params = params
            user_prompt = cleaned
            if not user_prompt:
                yield event.plain_result("📝 用法：bnn <你的绘画想法> size:4K temperature:0.9")
                return
        elif cmd in self.prompt_map:
            preset_prompt = self.prompt_map.get(cmd)
            extra_text = text.removeprefix(cmd).strip()
            # 清除 @ 文本残留（如 "@昵称(123456)"），避免被当成补充 prompt 传给 API
            extra_text = re.sub(r'@\S+?\(\d+\)', '', extra_text).strip()
            extra_text = re.sub(r'@\S+', '', extra_text).strip()
            if extra_text:
                # 从用户补充文本中提取分辨率（仅白名单用户 + Gemini 提供商）
                extra_text, extracted_size = self._extract_image_size(extra_text, sender_id, group_id=group_id)
                # 提取生成参数（键:值），校验失败明确报错不生成
                params, cleaned_extra = self._parse_and_validate_params(extra_text, {})
                if params is None:
                    yield event.plain_result(f"❌ {cleaned_extra}")
                    return
                gen_params = params
                extra_text = cleaned_extra
                if extra_text:
                    user_prompt = f"{preset_prompt}\n\n[User Additional Instruction]: {extra_text}"
                else:
                    user_prompt = preset_prompt
            else:
                user_prompt = preset_prompt
        else:
            return
        is_master = self.is_global_admin(event)
        if not is_master:
            if sender_id in self.conf.get("user_blacklist", []): return
            if group_id and group_id in self.conf.get("group_blacklist", []): return
            if self.conf.get("user_whitelist", []) and sender_id not in self.conf.get("user_whitelist", []): return
            if group_id and self.conf.get("group_whitelist", []) and group_id not in self.conf.get("group_whitelist",
                                                                                                   []): return
            # 次数配额检查（与文生图 / LLM 工具共用 _check_usage_quota）
            err = self._check_usage_quota(sender_id, group_id)
            if err:
                yield event.plain_result(err)
                return
        if not self.iwf or not (img_bytes_list := await self.iwf.get_images(event)):
            if not is_bnn:
                yield event.plain_result("请发送或引用一张图片。");
                return
        images_to_process: List[bytes] = []
        initial_messages: List[str] = []
        display_cmd = cmd
        if is_bnn:
            MAX_IMAGES = MAX_IMAGES_PER_REQUEST
            original_count = len(img_bytes_list)
            if original_count > MAX_IMAGES:
                images_to_process = img_bytes_list[:MAX_IMAGES]
                initial_messages.append(f"🎨 检测到 {original_count} 张图片，已选取前 {MAX_IMAGES} 张…")
            else:
                images_to_process = img_bytes_list
            display_cmd = user_prompt[:DISPLAY_PROMPT_LEN] + '...' if len(user_prompt) > DISPLAY_PROMPT_LEN else user_prompt
            initial_messages.append(f"🎨 检测到 {len(images_to_process)} 张图片，正在生成 [{display_cmd}]...")
        else:
            if not img_bytes_list:
                yield event.plain_result("请发送或引用一张图片。");
                return
            images_to_process = [img_bytes_list[0]]
            
            # 处理预设+补充的显示逻辑（清除 @ 文本避免显示中出现 @昵称）
            full_cmd_text = re.sub(r'@\S+?\(\d+\)', '', text).strip()
            full_cmd_text = re.sub(r'@\S+', '', full_cmd_text).strip()
            display_cmd = full_cmd_text[:DISPLAY_CMD_LEN] + '...' if len(full_cmd_text) > DISPLAY_CMD_LEN else full_cmd_text
            initial_messages.append(f"🎨 收到请求，正在生成 [{display_cmd}]...")

        slot_acquired = False
        try:
            if not await self._acquire_group_slot(group_id):
                if self.group_task_limit > 0:
                    yield event.plain_result(f"⚠️ 当前本群已有 {self.group_task_limit} 个生成任务正在处理，请稍后再试。")
                else:
                    yield event.plain_result("⚠️ 当前生成任务过多，请稍后再试。")
                return
            slot_acquired = True

            for msg in initial_messages:
                yield event.plain_result(msg)

            start_time = datetime.now()
            res, used_model = await self._call_api(images_to_process, user_prompt, image_size=extracted_size,
                                                   group_id=group_id, gen_params=gen_params or None,
                                                   size_strict=True)
            elapsed = (datetime.now() - start_time).total_seconds()
            if isinstance(res, bytes):
                if not is_master:
                    # 扣除次数（群组优先于个人，管理员豁免）
                    await self._consume_quota(sender_id, group_id)
                # 落盘 + 记录时间线
                try:
                    await self._save_generation(group_id, sender_id, user_prompt, used_model, res)
                except Exception as e:
                    logger.error(f"[NewImage] 保存生图记录失败: {e}", exc_info=True)
                caption_parts = [f"✅ 生成成功 ({elapsed:.2f}s)", f"预设: {display_cmd}", f"模型: {used_model}"]
                if is_master:
                    caption_parts.append("剩余次数: ∞")
                else:
                    if self.conf.get("enable_user_limit", True): caption_parts.append(
                        f"个人剩余: {self._get_user_count(sender_id)}")
                    if self.conf.get("enable_group_limit", False) and group_id: caption_parts.append(
                        f"本群剩余: {self._get_group_count(group_id)}")
                yield event.chain_result([Image.fromBytes(res), Plain(" | ".join(caption_parts))])
            else:
                yield event.plain_result(f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {res}")
        finally:
            if slot_acquired:
                await self._release_group_slot(group_id)
        event.stop_event()

    @filter.command("文生图", prefix_optional=True)
    async def on_text_to_image_request(self, event: AstrMessageEvent):
        prompt = event.message_str.strip()
        prompt = prompt.removeprefix("文生图").strip()  # 剥指令名，完整保留多词描述，不依赖 parsed_params
        if not prompt:
            yield event.plain_result("📝 用法：文生图 <画面描述>\n例如：>>文生图 一个人走在樱花林中漫步")
            return

        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_master = self.is_global_admin(event)
        
        # 从文生图 prompt 中提取分辨率（仅白名单用户 + Gemini 提供商）
        extracted_size = None
        prompt, extracted_size = self._extract_image_size(prompt, sender_id, group_id=group_id)
        # 提取生成参数（键:值），校验失败明确报错不生成
        gen_params, prompt = self._parse_and_validate_params(prompt, {})
        if gen_params is None:
            yield event.plain_result(f"❌ {prompt}")
            return
        if not prompt:
            yield event.plain_result("请提供文生图的描述（分辨率参数不能作为唯一输入）。")
            return

        # --- 权限和次数检查 ---
        if not is_master:
            if sender_id in self.conf.get("user_blacklist", []): return
            if group_id and group_id in self.conf.get("group_blacklist", []): return
            if self.conf.get("user_whitelist", []) and sender_id not in self.conf.get("user_whitelist", []): return
            if group_id and self.conf.get("group_whitelist", []) and group_id not in self.conf.get("group_whitelist",
                                                                                                   []): return
            # 次数配额检查（与图生图 / LLM 工具共用 _check_usage_quota）
            err = self._check_usage_quota(sender_id, group_id)
            if err:
                yield event.plain_result(err)
                return

        display_prompt = prompt[:DISPLAY_PROMPT_LEN] + '...' if len(prompt) > DISPLAY_PROMPT_LEN else prompt
        slot_acquired = False
        try:
            if not await self._acquire_group_slot(group_id):
                if self.group_task_limit > 0:
                    yield event.plain_result(f"⚠️ 当前本群已有 {self.group_task_limit} 个生成任务正在处理，请稍后再试。")
                else:
                    yield event.plain_result("⚠️ 当前生成任务过多，请稍后再试。")
                return
            slot_acquired = True

            yield event.plain_result(f"🎨 收到文生图请求，正在生成 [{display_prompt}]...")

            start_time = datetime.now()
            # 调用通用API，但传入空的图片列表
            res, used_model = await self._call_api([], prompt, image_size=extracted_size, group_id=group_id,
                                                   gen_params=gen_params or None, size_strict=True)
            elapsed = (datetime.now() - start_time).total_seconds()

            if isinstance(res, bytes):
                if not is_master:
                    # 扣除次数（群组优先于个人，管理员豁免）
                    await self._consume_quota(sender_id, group_id)
                # 落盘 + 记录时间线
                try:
                    await self._save_generation(group_id, sender_id, prompt, used_model, res)
                except Exception as e:
                    logger.error(f"[NewImage] 保存生图记录失败: {e}", exc_info=True)

                caption_parts = [f"✅ 生成成功 ({elapsed:.2f}s)", f"模型: {used_model}"]
                if is_master:
                    caption_parts.append("剩余次数: ∞")
                else:
                    if self.conf.get("enable_user_limit", True): caption_parts.append(
                        f"个人剩余: {self._get_user_count(sender_id)}")
                    if self.conf.get("enable_group_limit", False) and group_id: caption_parts.append(
                        f"本群剩余: {self._get_group_count(group_id)}")
                yield event.chain_result([Image.fromBytes(res), Plain(" | ".join(caption_parts))])
            else:
                yield event.plain_result(f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {res}")
        finally:
            if slot_acquired:
                await self._release_group_slot(group_id)
        event.stop_event()

    @filter.command("预设添加", aliases={"lm添加", "lma"}, prefix_optional=True)
    async def add_lm_prompt(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        raw = event.message_str.strip()
        if ":" not in raw:
            yield event.plain_result('格式错误, 正确示例:\n#预设添加 姿势表:为这幅图创建一个姿势表, 摆出各种姿势')
            return

        key, new_value = map(str.strip, raw.split(":", 1))
        prompt_list = self.conf.get("prompt_list", [])
        found = False
        for idx, item in enumerate(prompt_list):
            if item.strip().startswith(key + ":"):
                prompt_list[idx] = f"{key}:{new_value}"
                found = True
                break
        if not found: prompt_list.append(f"{key}:{new_value}")

        self.conf["prompt_list"] = prompt_list
        await self.conf.save_config_async()
        await self._load_prompt_map()
        yield event.plain_result(f"✅ 已保存生图预设:\n{key}:{new_value}")

    @filter.command("生图帮助", aliases={"lm帮助", "lmh"}, prefix_optional=True)
    async def on_prompt_help(self, event: AstrMessageEvent):
        raw_keyword = event.message_str.strip()

        # 兼容直接发送"生图帮助"而没有附加参数的情况
        keyword = raw_keyword
        for prefix_symbol in ("#", "/", "！", "!"):
            if keyword.startswith(prefix_symbol):
                keyword = keyword[len(prefix_symbol):].strip()
        if keyword in {"", "lm帮助", "lmh", "生图帮助"}:
            # 优先使用配置面板自定义的帮助菜单内容（help_menu），留空则用默认
            custom = (self.conf.get("help_menu") or "").strip()
            if custom:
                yield event.plain_result(custom)
                return
            msg = "📸 【NewImage 生图帮助】\n\n"
            msg += "🎨 图生图：发送图片 + 预设指令\n"
            msg += ">>手办化 / >>Q版化 / >>cos化 / >>真人化\n"
            msg += ">>痛车化 / >>痛屋化 / >>玩偶化 / >>贴纸化\n"
            msg += ">>三视图 / >>角色设定 / >>高清修复 / >>鬼图 等 40+ 预设\n"
            msg += "（>>生图帮助 <预设名> 可查看该预设的提示词）\n\n"
            msg += "✏️ 文生图：\n"
            msg += ">>文生图 <画面描述>\n\n"
            msg += "🎨 自定义图生图：\n"
            msg += "bnn <你的绘画想法>（可一次带多张图）\n\n"
            msg += "🎁 次数与签到：\n"
            msg += "发送「辉宝赐福」—— 每日签到领次数\n"
            msg += ">>生图查询次数 —— 查询剩余次数\n\n"
            msg += "👑 管理员：\n"
            msg += ">>切换模型 <序号> —— 按群切换生图模型\n"
            msg += ">>生图日志 —— 生图时间线记录（查询/删除）\n"
            msg += ">>预设添加 <名称>:<提示词> —— 增改预设\n"
            msg += ">>生图增加用户次数 / >>生图增加群组次数"
            yield event.plain_result(msg)
            return

        prompt = self.prompt_map.get(keyword)
        if not prompt:
            yield event.plain_result("❌ 未找到此预设指令")
            return
        yield event.plain_result(f"📋 预设 [{keyword}] 的内容:\n{prompt}")

    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = self.context.get_config().get("admins_id", [])
        return event.get_sender_id() in admin_ids

    def _is_admin_id(self, uid: str) -> bool:
        """判断某 QQ 号是否为全局管理员（用于查询时显示豁免）。"""
        admin_ids = self.context.get_config().get("admins_id", [])
        return str(uid) in [str(x) for x in admin_ids]

    async def _load_user_counts(self):
        data = await load_json(self.user_counts_file)
        if isinstance(data, dict):
            self.user_counts = {str(k): v for k, v in data.items()}
        else:
            self.user_counts = {}

    async def _save_user_counts(self):
        # 说明：调用方须已持 self._counts_lock（保证 check-modify-write 原子），此处不再重复加锁
        await save_json(self.user_counts_file, self.user_counts)

    def _get_user_count(self, user_id: str) -> int:
        return self.user_counts.get(str(user_id), 0)

    async def _decrease_user_count(self, user_id: str, amount: int = 1):
        # 说明：由持 self._counts_lock 的调用方（_consume_quota）调用，避免嵌套死锁
        user_id_str = str(user_id)
        amount = max(1, int(amount))
        count = self._get_user_count(user_id_str)
        if count > 0:
            self.user_counts[user_id_str] = max(0, count - amount)
            await self._save_user_counts()

    async def _load_group_counts(self):
        data = await load_json(self.group_counts_file)
        if isinstance(data, dict):
            self.group_counts = {str(k): v for k, v in data.items()}
        else:
            self.group_counts = {}

    async def _save_group_counts(self):
        # 说明：调用方须已持 self._counts_lock（保证 check-modify-write 原子），此处不再重复加锁
        await save_json(self.group_counts_file, self.group_counts)

    def _get_group_count(self, group_id: str) -> int:
        return self.group_counts.get(str(group_id), 0)

    async def _decrease_group_count(self, group_id: str, amount: int = 1):
        # 说明：由持 self._counts_lock 的调用方（_consume_quota）调用，避免嵌套死锁
        group_id_str = str(group_id)
        amount = max(1, int(amount))
        count = self._get_group_count(group_id_str)
        if count > 0:
            self.group_counts[group_id_str] = max(0, count - amount)
            await self._save_group_counts()

    def _check_usage_quota(self, sender_id: str, group_id: Optional[str], quota: int = 1) -> str | None:
        """检查次数配额，返回拒绝原因文案；None=可用。管理员豁免由调用方保证。

        quota: 本次生成需消耗的次数（默认 1，多配额模型可传入更大值）。
        语义：只要「个人次数 >= quota」或「群次数 >= quota」任一满足即放行（不叠加扣减）。
        """
        quota = max(1, int(quota))
        user_count = self._get_user_count(sender_id)
        group_count = self._get_group_count(group_id) if group_id else 0
        user_limit_on = self.conf.get("enable_user_limit", True)
        group_limit_on = self.conf.get("enable_group_limit", False) and group_id

        if group_id:
            # 群聊：两种限制都开启时，需要至少一种有次数
            if user_limit_on and group_limit_on:
                if user_count < quota and group_count < quota:
                    return (f"❌ 本群次数与您的个人次数均不足以生成（需 {quota} 次，"
                            f"个人剩 {user_count}，群剩 {group_count}）。\n请使用「辉宝赐福」获取次数。")
            elif user_limit_on and user_count < quota:
                # 只开启用户限制，用户次数不足
                return f"❌ 您的次数不足（需 {quota} 次，当前剩余 {user_count}）。\n请使用「辉宝赐福」获取次数。"
            elif group_limit_on and group_count < quota:
                # 只开启群组限制，群组次数不足
                return f"❌ 本群次数不足（需 {quota} 次，当前剩余 {group_count}）。\n请联系管理员增加群组次数。"
        else:
            # 私聊：只检查用户限制
            if user_limit_on and user_count < quota:
                return f"❌ 您的次数不足（需 {quota} 次，当前剩余 {user_count}）。\n请使用「辉宝赐福」获取次数。"
        return None

    async def _consume_quota(self, sender_id: str, group_id: Optional[str], count: int = 1) -> str:
        """生成成功后扣减次数：群组优先于个人，单一来源扣减（管理员豁免由调用方保证）。

        count: 本次生成实际消耗的次数（默认 1）。
        语义：群次数 >= count 则扣群 count；否则若用户次数 >= count 则扣用户 count（与检查一致，不叠加）。
        用 self._counts_lock 包裹「检查-修改-写盘」，保证并发下原子（decrease_* 内部不加锁避免死锁）。
        返回实际扣减来源：'group' / 'user'；未扣到返回 ''（供调用方展示对应来源的剩余次数）。
        """
        count = max(1, int(count))
        async with self._counts_lock:
            if self.conf.get("enable_group_limit", False) and group_id and self._get_group_count(group_id) >= count:
                await self._decrease_group_count(group_id, amount=count)
                return "group"
            elif self.conf.get("enable_user_limit", True) and self._get_user_count(sender_id) >= count:
                await self._decrease_user_count(sender_id, amount=count)
                return "user"
        return ""

    async def _load_user_checkin_data(self):
        data = await load_json(self.user_checkin_file)
        if isinstance(data, dict):
            self.user_checkin_data = {str(k): v for k, v in data.items()}
        else:
            self.user_checkin_data = {}

    async def _save_user_checkin_data(self):
        await save_json(self.user_checkin_file, self.user_checkin_data)

    async def _load_group_models(self):
        """加载各群配置的生图模型映射 {群号: 模型名}"""
        data = await load_json(self.group_model_file)
        if isinstance(data, dict):
            self.group_models = {str(k): str(v) for k, v in data.items()}
        else:
            self.group_models = {}

    async def _save_group_models(self):
        """保存各群配置的生图模型映射 {群号: 模型名}"""
        await save_json(self.group_model_file, self.group_models)

    async def _load_artist_presets(self):
        """加载画师串预设库（[{name, artists, negative}]，name 去重保序）。"""
        data = await load_json(self.artist_presets_file)
        presets: List[Dict[str, Any]] = []
        seen: set = set()
        if isinstance(data, dict) and isinstance(data.get("presets"), list):
            for p in data["presets"]:
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name", "") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                presets.append({
                    "name": name,
                    "artists": str(p.get("artists", "") or "").strip(),
                    "negative": str(p.get("negative", "") or "").strip(),
                })
        self.artist_presets = presets

    async def _save_artist_presets(self):
        """原子写画师串预设库。"""
        await save_json(self.artist_presets_file, {"presets": self.artist_presets})

    async def _load_group_artists(self):
        """加载各群画师串预设映射 {群号: 预设名}（空/"off" 视为关闭）。"""
        data = await load_json(self.group_artist_file)
        if isinstance(data, dict):
            self.group_artists = {str(k): str(v) for k, v in data.items()}
        else:
            self.group_artists = {}

    async def _save_group_artists(self):
        """原子写各群画师串预设映射。"""
        await save_json(self.group_artist_file, self.group_artists)

    def _get_group_artist_preset(self, group_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """返回当前群激活的画师串预设（未配置/关闭/不存在 → None）。

        group_artists[群号] = 预设名；空串或 "off" 视为关闭；预设名在库中不存在则忽略。
        """
        if not group_id:
            return None
        preset_name = self.group_artists.get(str(group_id), "")
        preset_name = (preset_name or "").strip()
        if not preset_name or preset_name.lower() == "off":
            return None
        for p in self.artist_presets:
            if p.get("name") == preset_name:
                return p
        return None

    async def _load_generation_history(self):
        """加载生图时间线记录列表（每条 {timestamp, group_id, user_id, prompt, model, file_path}）"""
        data = await load_json(self.generation_history_file)
        if isinstance(data, list):
            self.generation_history = data
        else:
            self.generation_history = []

    async def _save_generation_history(self):
        """保存生图时间线记录列表"""
        await save_json(self.generation_history_file, self.generation_history)

    async def _append_generation_history(self, record: Dict[str, Any]) -> None:
        """向生图时间线追加一条记录，超过 500 条时删除最旧的。"""
        # 加锁防止多群/并发生图时 read-modify-write 丢记录
        async with self._history_lock:
            self.generation_history.append(record)
            if len(self.generation_history) > GENERATION_HISTORY_MAX:
                self.generation_history = self.generation_history[-GENERATION_HISTORY_MAX:]
            await self._save_generation_history()

    async def _save_generation(self, group_id: Optional[str], user_id: str, prompt: str, model: str,
                               image_bytes: bytes) -> str:
        """保存生图结果到磁盘并记录时间线，返回文件绝对路径。

        目录: StarTools.get_data_dir()/generations/<group_id|private>/<时间戳>.png
        时间戳: %Y%m%d_%H%M%S_%f（含微秒，基本不会重复）；仍保留存在性检查随机后缀兜底；
        group_id 为空/私聊用 private
        """
        loop = asyncio.get_running_loop()
        dir_name = str(group_id) if group_id else "private"
        gen_dir = self.plugin_data_dir / "generations" / dir_name
        try:
            await loop.run_in_executor(None, functools.partial(gen_dir.mkdir, parents=True, exist_ok=True))
        except Exception as e:
            logger.error(f"[NewImage] 创建生图目录失败: {e}", exc_info=True)
            return ""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        file_path = gen_dir / f"{ts}.png"
        if file_path.exists():
            # 极端情况下（同一微秒）仍追加随机后缀兜底
            file_path = gen_dir / f"{ts}_{random.randint(1000, 9999)}.png"
        try:
            await loop.run_in_executor(None, file_path.write_bytes, image_bytes)
        except Exception as e:
            logger.error(f"[NewImage] 保存生图文件失败: {e}", exc_info=True)
            return ""
        logger.info(f"[NewImage] 生图已保存: {file_path}")
        # 记录时间线
        await self._append_generation_history({
            "timestamp": ts,
            "group_id": str(group_id) if group_id else "",
            "user_id": str(user_id),
            "prompt": prompt,
            "model": model,
            "file_path": str(file_path),
        })
        return str(file_path)

    async def _recall_log_message(self, event: AstrMessageEvent, message_id):
        """撤回之前发送的日志消息（仅支持 aiocqhttp 平台；拿不到消息 id 则跳过）"""
        if not message_id:
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        try:
            # 兼容 bot.call_action 与 bot.api.call_action 两种形态
            call = getattr(bot, "call_action", None) or getattr(getattr(bot, "api", None), "call_action", None)
            if call:
                await call("delete_msg", message_id=message_id)
        except Exception as e:
            logger.error(f"[NewImage] 撤回日志消息失败: {e}", exc_info=True)

    @filter.regex(r"^[#/!！]?辉宝赐福\s*$")
    async def on_checkin(self, event: AstrMessageEvent):
        """每日签到获取生图次数 - 支持直接发送"辉宝赐福"触发"""
        if not self.conf.get("enable_checkin", False):
            yield event.plain_result("📅 本机器人未开启辉宝赐福功能。")
            return
        user_id = event.get_sender_id()
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.user_checkin_data.get(user_id) == today_str:
            yield event.plain_result(f"您今天已经领取过辉宝赐福。\n剩余次数: {self._get_user_count(user_id)}")
            return
        # 检查是否已达到持有上限
        # 用 _counts_lock 包裹「读-判-写-盘」，保证签到并发下 user_counts 修改原子
        async with self._counts_lock:
            current_count = self._get_user_count(user_id)
            if self.user_count_cap > 0 and current_count >= self.user_count_cap:
                yield event.plain_result(f"📦 您的生图次数已存满（{current_count}/{self.user_count_cap}），无法继续累积。\n请先使用后再来签到哦~")
                return
            reward = 0
            if str(self.conf.get("enable_random_checkin", False)).lower() == 'true':
                min_reward = max(1, int(self.conf.get("checkin_random_reward_min", 1)))
                max_reward = max(min_reward, int(self.conf.get("checkin_random_reward_max", 5)))
                reward = random.randint(min_reward, max_reward)
            else:
                reward = int(self.conf.get("checkin_fixed_reward", 3))
            new_count = current_count + reward
            # 如果设置了上限，截断到上限
            if self.user_count_cap > 0 and new_count > self.user_count_cap:
                new_count = self.user_count_cap
                actual_reward = new_count - current_count
                self.user_counts[user_id] = new_count
                await self._save_user_counts()
                self.user_checkin_data[user_id] = today_str
                await self._save_user_checkin_data()
                yield event.plain_result(f"🎉 辉宝赐福成功！获得大香蕉生图 {actual_reward} 次（已达上限 {self.user_count_cap}），当前生图剩余: {new_count} 次。")
            else:
                self.user_counts[user_id] = new_count
                await self._save_user_counts()
                self.user_checkin_data[user_id] = today_str
                await self._save_user_checkin_data()
                cap_info = f"（上限 {self.user_count_cap}）" if self.user_count_cap > 0 else ""
                yield event.plain_result(f"🎉 辉宝赐福成功！获得大香蕉生图 {reward} 次，当前生图剩余: {new_count}{cap_info} 次。")

    @filter.command("生图增加用户次数", prefix_optional=True)
    async def on_add_user_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        cmd_text = event.message_str.strip()
        at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
        target_qq, count = None, 0
        if at_seg:
            target_qq = str(at_seg.qq)
            match = re.search(r"(\d+)\s*$", cmd_text)
            if match: count = int(match.group(1))
        else:
            match = re.search(r"(\d+)\s+(\d+)", cmd_text)
            if match: target_qq, count = match.group(1), int(match.group(2))
        if not target_qq or count <= 0:
            yield event.plain_result(
                '格式错误:\n#生图增加用户次数 @用户 <次数>\n或 #生图增加用户次数 <QQ号> <次数>')
            return
        async with self._counts_lock:
            current_count = self._get_user_count(target_qq)
            new_count = current_count + count
            # 管理员手动增加不受次数上限（user_count_cap）限制
            self.user_counts[str(target_qq)] = new_count
            await self._save_user_counts()
        yield event.plain_result(f"✅ 已为用户 {target_qq} 增加次数，TA当前剩余 {new_count} 次（管理员增加不受上限限制）。")

    @filter.command("生图增加群组次数", prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        match = re.search(r"(\d+)\s+(\d+)", event.message_str.strip())
        if not match:
            yield event.plain_result('格式错误: #生图增加群组次数 <群号> <次数>')
            return
        target_group, count = match.group(1), int(match.group(2))
        async with self._counts_lock:
            current_count = self._get_group_count(target_group)
            self.group_counts[str(target_group)] = current_count + count
            await self._save_group_counts()
        yield event.plain_result(f"✅ 已为群组 {target_group} 增加 {count} 次，该群当前剩余 {current_count + count} 次。")

    @filter.command("生图查询次数", prefix_optional=True)
    async def on_query_counts(self, event: AstrMessageEvent):
        user_id_to_query = event.get_sender_id()
        if self.is_global_admin(event):
            at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
            if at_seg:
                user_id_to_query = str(at_seg.qq)
            else:
                match = re.search(r"(\d+)", event.message_str)
                if match: user_id_to_query = match.group(1)
        if self._is_admin_id(user_id_to_query):
            personal_display = "∞（管理员豁免）"
        else:
            personal_display = str(self._get_user_count(user_id_to_query))
        reply_msg = f"用户 {user_id_to_query} 个人剩余次数为: {personal_display}"
        if user_id_to_query == event.get_sender_id(): reply_msg = f"您好，您当前个人剩余次数为: {personal_display}"
        if group_id := event.get_group_id(): reply_msg += f"\n本群共享剩余次数为: {self._get_group_count(group_id)}"
        yield event.plain_result(reply_msg)

    @filter.command("生图添加key", prefix_optional=True)
    async def on_add_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        new_keys = event.message_str.strip().split()
        if not new_keys: yield event.plain_result("格式错误，请提供要添加的Key。"); return
        api_keys = self.conf.get("api_keys", [])
        added_keys = [key for key in new_keys if key not in api_keys]
        api_keys.extend(added_keys)
        self.conf["api_keys"] = api_keys
        await self.conf.save_config_async()
        yield event.plain_result(f"✅ 操作完成，新增 {len(added_keys)} 个Key，当前共 {len(api_keys)} 个。")

    @filter.command("生图key列表", prefix_optional=True)
    async def on_list_keys(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        api_keys = self.conf.get("api_keys", [])
        if not api_keys: yield event.plain_result("📝 暂未配置任何 API Key。"); return
        key_list_str = "\n".join(f"{i + 1}. {key[:8]}...{key[-4:]}" for i, key in enumerate(api_keys))
        yield event.plain_result(f"🔑 API Key 列表:\n{key_list_str}")

    @filter.command("生图删除key", prefix_optional=True)
    async def on_delete_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        param = event.message_str.strip()
        api_keys = self.conf.get("api_keys", [])
        if param.lower() == "all":
            self.conf["api_keys"] = []
            await self.conf.save_config_async()
            yield event.plain_result(f"✅ 已删除全部 {len(api_keys)} 个 Key。")
        elif param.isdigit() and 1 <= int(param) <= len(api_keys):
            removed_key = api_keys.pop(int(param) - 1)
            self.conf["api_keys"] = api_keys
            await self.conf.save_config_async()
            yield event.plain_result(f"✅ 已删除 Key: {removed_key[:8]}...")
        else:
            yield event.plain_result("格式错误，请使用 #生图删除key <序号|all>")

    @filter.command("切换模型", prefix_optional=True)
    async def on_switch_model(self, event: AstrMessageEvent):
        """按群切换生图模型（仅管理员）"""
        if not self.is_global_admin(event): return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用本指令")
            return
        if not self.available_models:
            yield event.plain_result("📝 尚未配置可用生图模型，请在插件配置的「model」项中每行填写一个模型。")
            return
        param = event.message_str.strip()
        param = param.removeprefix("切换模型").strip()  # 兼容 message_str 带/不带指令名
        if not param:
            # 无参数：列出模型列表
            current = self.group_models.get(group_id, "")
            lines = "\n".join(
                f"[{idx}] {m['name']}（消耗{m['quota']}次）" + (f" - {m['desc']}" if m.get("desc") else "") + (" ← 当前" if m["name"] == current else "")
                for idx, m in enumerate(self.available_models, 1)
            )
            yield event.plain_result(f"📋 本群可用生图模型：\n{lines}\n回复序号或输入 `>>切换模型 <序号>` 切换")
            return
        if param.isdigit():
            idx = int(param)
            if 1 <= idx <= len(self.available_models):
                model = self.available_models[idx - 1]["name"]
                self.group_models[group_id] = model
                await self._save_group_models()
                logger.info(f"[NewImage] 群 {group_id} 的模型已切换为: {model}")
                yield event.plain_result(f"✅ 本群生图模型已切换为: {model}")
                return
            yield event.plain_result(f"❌ 序号超出范围（1-{len(self.available_models)}），请重新输入。")
            return
        yield event.plain_result("格式: >>切换模型 <序号>")

    @filter.command("画师串", prefix_optional=True)
    async def on_artist_preset(self, event: AstrMessageEvent):
        """画师串预设：列出 / 按群切换 / 关闭。

        - `画师串` → 列出所有预设 + 当前群激活的预设
        - `画师串 <预设名>` → 当前群切换到该预设（写入 group_artist.json）
        - `画师串 off` → 当前群关闭画师串
        切换/关闭仅管理员可用（同「切换模型」）；私聊无法按群配置。
        """
        group_id = event.get_group_id()
        param = event.message_str.strip()
        param = param.removeprefix("画师串").strip()  # 兼容 message_str 带/不带指令名
        if not group_id:
            # 列出仍可用（公共信息），切换需群聊
            if not param:
                current = self.group_artists.get("", "")
                lines = "\n".join(
                    f"- {p['name']}" + (f"（负面: {p['negative']}）" if p.get("negative") else "")
                    for p in self.artist_presets
                ) if self.artist_presets else "（暂无画师串预设，请在 WebUI 画师串管理中配置）"
                yield event.plain_result(f"🎨 可用画师串预设：\n{lines}")
                return
            yield event.plain_result("画师串按群切换仅支持在群聊中使用")
            return
        if not param:
            # 列出所有预设 + 当前群激活
            current = self.group_artists.get(str(group_id), "")
            if self.artist_presets:
                lines = "\n".join(
                    f"- {p['name']}" + (" ← 当前" if p["name"] == current else "")
                    + (f"（负面: {p['negative']}）" if p.get("negative") else "")
                    for p in self.artist_presets
                )
            else:
                lines = "（暂无画师串预设，请在 WebUI 画师串管理中配置）"
            cur_tip = f"\n当前群激活: {current}" if current and current.lower() != "off" else "\n当前群未启用画师串"
            yield event.plain_result(f"🎨 可用画师串预设：\n{lines}{cur_tip}\n输入 `>>画师串 <预设名>` 切换，`>>画师串 off` 关闭")
            return
        if not self.is_global_admin(event):
            yield event.plain_result("仅管理员可切换画师串预设")
            return
        if param.lower() == "off":
            self.group_artists.pop(str(group_id), None)
            await self._save_group_artists()
            logger.info(f"[NewImage] 群 {group_id} 的画师串已关闭")
            yield event.plain_result("✅ 本群画师串已关闭")
            return
        target = next((p for p in self.artist_presets if p.get("name") == param), None)
        if not target:
            names = "、".join(p.get("name", "") for p in self.artist_presets) or "（暂无预设）"
            yield event.plain_result(f"❌ 未找到画师串预设 [{param}]。可用预设：{names}")
            return
        self.group_artists[str(group_id)] = target["name"]
        await self._save_group_artists()
        logger.info(f"[NewImage] 群 {group_id} 的画师串已切换为: {target['name']}")
        yield event.plain_result(f"✅ 本群画师串已切换为: {target['name']}")

    @filter.command("生图日志", prefix_optional=True)
    async def on_generation_log(self, event: AstrMessageEvent):
        """查询/删除生图日志（仅管理员）"""
        if not self.is_global_admin(event): return
        usage = ("📋 生图日志用法：\n"
                 "查询 [条数] [页码]\n"
                 "查询 全局 [条数] [页码]\n"
                 "单条查询 <时间戳>\n"
                 "删除 <时间戳>\n"
                 "删除 <群号|全局> <开始时间戳> <结束时间戳>")
        raw = event.message_str.strip()
        raw = raw.removeprefix("生图日志").strip()  # AstrBot 4.27.2 message_str 带指令名，需手动剥名
        if not raw:
            yield event.plain_result(usage)
            return
        parts = raw.split()
        action = parts[0]
        if action == "查询":
            await self._handle_log_query(event, parts[1:])
            return
        if action == "单条查询":
            if len(parts) < 2:
                yield event.plain_result("格式: 生图日志 单条查询 <时间戳>")
                return
            record = next((r for r in self.generation_history if r.get("timestamp") == parts[1]), None)
            if not record:
                yield event.plain_result(f"❌ 未找到时间戳为 {parts[1]} 的生图记录。")
                return
            info = (f"🖼 生图记录详情\n"
                    f"时间戳: {record.get('timestamp')}\n"
                    f"群号: {record.get('group_id') or 'private'}\n"
                    f"请求者: {record.get('user_id')}\n"
                    f"模型: {record.get('model')}\n"
                    f"文件: {record.get('file_path')}\n"
                    f"完整提示词: {record.get('prompt')}")
            chain = [Plain(info)]
            fp = record.get("file_path") or ""
            if fp and Path(fp).is_file():
                chain.append(Image.fromFileSystem(fp))
            try:
                await event.send(MessageChain(chain=chain))
            except Exception as e:
                logger.error(f"[NewImage] 发送单条生图日志失败: {e}", exc_info=True)
            return
        if action == "删除":
            if len(parts) == 2:
                ts = parts[1]
                async with self._history_lock:
                    before = len(self.generation_history)
                    self.generation_history = [r for r in self.generation_history if r.get("timestamp") != ts]
                    removed = before - len(self.generation_history)
                    if removed:
                        await self._save_generation_history()
                if removed:
                    yield event.plain_result(f"✅ 已删除 {removed} 条时间戳为 {ts} 的生图记录。")
                else:
                    yield event.plain_result(f"❌ 未找到时间戳为 {ts} 的生图记录。")
                return
            if len(parts) == 4:
                scope_key, start_ts, end_ts = parts[1], parts[2], parts[3]
                async with self._history_lock:
                    remaining = []
                    for r in self.generation_history:
                        r_ts = r.get("timestamp", "")
                        in_range = start_ts <= r_ts <= end_ts  # %Y%m%d_%H%M%S_%f 时间戳按字符串比较即时间序（含微秒基本唯一）
                        in_scope = (scope_key == "全局") or (str(r.get("group_id", "")) == str(scope_key))
                        if not (in_range and in_scope):
                            remaining.append(r)
                    removed = len(self.generation_history) - len(remaining)
                    self.generation_history = remaining
                    if removed:
                        await self._save_generation_history()
                if removed:
                    yield event.plain_result(f"✅ 已删除 {removed} 条生图记录（范围: {scope_key}, {start_ts} ~ {end_ts}）。")
                else:
                    yield event.plain_result(f"❌ 该范围内没有生图记录。")
                return
            yield event.plain_result("格式: 生图日志 删除 <时间戳> 或 删除 <群号|全局> <开始时间戳> <结束时间戳>")
            return
        yield event.plain_result(usage)

    async def _handle_log_query(self, event: AstrMessageEvent, args: List[str]) -> None:
        """处理生图日志「查询」子命令：本群/全局、条数(≤20)、每页10条翻页，翻页时先撤回上一条日志消息再发新页。"""
        async def _send(msg: str):
            try:
                await event.send(MessageChain(chain=[Plain(msg)]))
            except Exception as e:
                logger.error(f"[NewImage] 发送生图日志失败: {e}", exc_info=True)

        scope = "group"
        count = 10
        page = 1
        if args and args[0] == "全局":
            scope = "global"
            args = args[1:]
        if args:
            try:
                count = max(1, min(int(args[0]), LOG_QUERY_LIMIT_MAX))
            except ValueError:
                await _send("❌ 条数格式错误（应为数字，最大 20）。")
                return
        if len(args) > 1:
            try:
                page = max(1, int(args[1]))
            except ValueError:
                await _send("❌ 页码格式错误（应为数字）。")
                return
        group_id = event.get_group_id()
        # 时间倒序（最新在前）：generation_history 为 append 顺序，最新在末尾
        records = list(reversed(self.generation_history))
        if scope == "group" and group_id:
            records = [r for r in records if str(r.get("group_id", "")) == str(group_id)]
        records = records[:count]
        total_pages = max(1, (len(records) + 9) // 10)
        if page > total_pages:
            page = total_pages
        start = (page - 1) * 10
        page_records = records[start:start + 10]
        if not page_records:
            await _send("📭 暂无生图日志。")
            return
        lines = []
        for r in page_records:
            prompt_short = str(r.get("prompt", ""))[:PROMPT_SHORT_LEN]
            lines.append(f"{r.get('timestamp','?')} | {r.get('file_path','?')} | {prompt_short} | "
                         f"{r.get('group_id','') or 'private'} | {r.get('user_id','?')}")
        msg = (f"📋 生图日志（第 {page}/{total_pages} 页，本页 {len(page_records)} 条，"
               f"范围: {'全局' if scope == 'global' else '本群'}）\n" + "\n".join(lines))
        session_id = event.get_session_id()
        key = (session_id, event.get_sender_id())  # (会话, 发送者) 作键，避免群内跨用户误撤回
        prev = self._log_page_msgid.get(key)
        if page > 1 and prev and prev.get("scope") == scope and prev.get("count") == count:
            # 翻页：先撤回上一条日志消息，再发新页
            # 注：AstrBot 4.27.2 的 event.send 不返回消息 id（返回 None），msgid 恒为 None，
            # _recall_log_message 会因 message_id 为空而自然跳过，实际降级为「直接发新页」；
            # 保留调用与存储结构，供未来平台 API 返回消息 id 时自动启用撤回
            await self._recall_log_message(event, prev.get("msgid"))
        try:
            sent_id = await event.send(MessageChain(chain=[Plain(msg)]))
        except Exception as e:
            logger.error(f"[NewImage] 发送生图日志失败: {e}", exc_info=True)
            return
        # 记录当前日志消息 id 供后续翻页撤回（event.send 返回 None 时即不可撤回）
        self._log_page_msgid[key] = {"msgid": sent_id, "page": page, "scope": scope, "count": count}

    async def _extract_event_images(self, event: AstrMessageEvent) -> List[bytes]:
        """从用户当前消息中提取图片字节（引用回复链内的图 + 直接 Image 组件）。

        LLM 工具（generate_image）用：拿到用户附带的参考图做图+文生图；无图返回空列表（纯文生图）。
        注意：不走 self.iwf.get_images 的头像兜底逻辑（LLM 场景不该自动用头像当参考图）。
        """
        if not self.iwf or not event.message_obj or not getattr(event.message_obj, "message", None):
            return []
        img_bytes_list: List[bytes] = []
        try:
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and getattr(seg, "chain", None):
                    for s_chain in seg.chain:
                        if isinstance(s_chain, Image):
                            if img := await self.iwf._image_component_to_bytes(s_chain):
                                img_bytes_list.append(img)
                elif isinstance(seg, Image):
                    if img := await self.iwf._image_component_to_bytes(seg):
                        img_bytes_list.append(img)
        except Exception as e:
            logger.warning(f"[NewImage] 提取当前消息图片失败: {e}", exc_info=True)
        return img_bytes_list

    if hasattr(filter, "llm_tool"):
        @filter.llm_tool(name="generate_image")
        async def generate_image(self, event: AstrMessageEvent, prompt: str, model: str = "",
                                 params: Optional[Dict] = None):
            """根据用户的绘画需求生成一张图片。调用前，必须先把用户的绘画需求扩写完善为详细、高质量、适合 AI 绘画的完整英文提示词（包含主体、风格、构图、光线、细节等要素），再作为 prompt 参数传入。只有在用户明确要求生成或绘制图片时才调用本工具。

            注意：若用户在当前消息（或引用/刚发送的消息）中附带图片，本工具会自动提取该图片作为参考进行「图+文生图」；若纯文本描述则进行「文生图」。

            Args:
                prompt(string): 经扩写完善的完整绘画提示词（英文，包含主体、风格、构图、光线、细节等）
                model(string): 生图模型名（可留空，留空则用当前会话所在群的默认模型；若不确定可用模型请先调用 query_generation_models 工具查询）
                params(object): 生图参数（可选，JSON对象，如 {"size":"4K","temperature":0.9}；可用键：size/temperature/max_tokens/steps/negative_prompt）
            """
            # 次数限制与手动命令一致：管理员豁免、群组优先、成功才扣减
            # （工具由主对话 LLM 自主调用，不查黑白名单、不占群槽位）
            # 注意：llm_tool 工具是纯协程（函数体内不能有 yield），发消息给用户用 event.send，return 文本回传 LLM
            sender_id = event.get_sender_id()
            group_id = event.get_group_id()
            # 尝试从用户当前消息中提取图片（引用回复链/直接 Image 组件），无图则纯文生图
            image_bytes_list = await self._extract_event_images(event)
            # 确定最终模型：用户指定 > 本群配置模型 > 手动配置默认模型 > conf.model 兜底
            final_model = (model or "").strip()
            if not final_model:
                final_model = self.group_models.get(group_id, "") if group_id else ""
            if not final_model and self.available_model_names:
                final_model = self.available_model_names[0]
            if not final_model:
                final_model = (self.conf.get("model", "") or "").strip()
            quota = self._get_model_quota(final_model)
            if not self.is_global_admin(event):
                err = self._check_usage_quota(sender_id, group_id, quota)
                if err:
                    # 先发用户（立刻看到原因），再 return 文本回传 LLM 让它知晓
                    try:
                        await event.send(MessageChain(chain=[Plain(err)]))
                    except Exception as e:
                        logger.error(f"[NewImage] 发送生图次数不足提示失败: {e}", exc_info=True)
                    return f"未执行生图：该用户生图次数已用完（{err}），请勿让用户再请求生图。"
            # 异步生图：立即返回给 LLM（不阻塞其回复），实际生成放后台任务，完成后自动发送到会话
            # 对 LLM 传入的 params 做温和清洗（夹取/丢弃），再传入后台任务
            cleaned_params, _ = self._sanitize_params(params, "LLM工具") if params else ({}, [])
            task = asyncio.create_task(self._async_generate(event, prompt, model=final_model, quota=quota,
                                                            params=cleaned_params,
                                                            image_bytes_list=image_bytes_list))
            # 保存后台任务引用（防 GC），结束后自动从集合移除；terminate 时统一回收
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return "已开始生成图片，生成完成后会自动发送到当前会话，请稍候。"

        async def _async_generate(self, event: AstrMessageEvent, prompt: str, model: str = "", quota: int = 1,
                                  params: Optional[Dict] = None,
                                  image_bytes_list: Optional[List[bytes]] = None) -> None:
            """后台异步生图：完成后主动发送图片/失败提示到会话（不阻塞 LLM 回复）。

            model: 指定生图模型（可空，空则 _call_api 内部走默认选择逻辑）；
            quota: 本次生成消耗的次数（与 _check_usage_quota / _consume_quota 的 quota 语义一致）；
            params: 生成参数（已在 generate_image 清洗），在 _call_api 内与模型默认参数合并；
            image_bytes_list: 用户当前消息中提取的参考图片（图+文生图用），None/空列表则纯文生图。
            """
            sender_id = event.get_sender_id()
            group_id = event.get_group_id()
            slot_acquired = False
            try:
                if not await self._acquire_group_slot(group_id):
                    try:
                        await event.send(MessageChain(chain=[Plain("⚠️ 当前生图任务较多，请稍后再试。")]))
                    except Exception as e:
                        logger.error(f"[NewImage] 发送队列提示失败: {e}", exc_info=True)
                    return
                slot_acquired = True
                # 二次校验配额（后台任务启动时可能已被并发任务扣减），失败则直接返回不发图
                if not self.is_global_admin(event):
                    err = self._check_usage_quota(sender_id, group_id, quota)
                    if err:
                        try:
                            await event.send(MessageChain(chain=[Plain(err)]))
                        except Exception as e:
                            logger.error(f"[NewImage] 发送生图次数不足提示失败: {e}", exc_info=True)
                        return
                res, used_model = await self._call_api(image_bytes_list or [], prompt, image_size=None, group_id=group_id,
                                                       model_override=model or None, gen_params=params or None)
                if isinstance(res, bytes):
                    # 记录实际扣减来源（群/个人），剩余次数展示与扣减来源一致
                    consumed_from = ""
                    if not self.is_global_admin(event):
                        consumed_from = await self._consume_quota(sender_id, group_id, count=quota)
                    try:
                        await self._save_generation(group_id, sender_id, prompt, used_model, res)
                    except Exception as e:
                        logger.error(f"[NewImage] 保存生图记录失败: {e}", exc_info=True)
                    # 剩余次数：优先显示个人剩余；若未开启用户限制则显示群剩余
                    if consumed_from == "group":
                        remaining = self._get_group_count(group_id) if group_id else 0
                    else:
                        remaining = self._get_user_count(sender_id)
                    try:
                        await event.send(MessageChain(chain=[
                            Image.fromBytes(res),
                            Plain(f"✅ 已生成 | 模型: {used_model} | 剩余: {remaining}次"),
                        ]))
                    except Exception as e:
                        logger.error(f"[NewImage] 发送生图结果失败: {e}", exc_info=True)
                else:
                    try:
                        await event.send(MessageChain(chain=[Plain(f"❌ 生成失败: {res}")]))
                    except Exception as e:
                        logger.error(f"[NewImage] 发送生图失败提示失败: {e}", exc_info=True)
            finally:
                if slot_acquired:
                    await self._release_group_slot(group_id)

        @filter.llm_tool(name="query_generation_quota")
        async def query_generation_quota(self, event: AstrMessageEvent, user_id: str = ""):
            """查询用户的生图次数配额（个人剩余次数与当前群剩余次数）。注意本工具不执行生图，仅查询。

            Args:
                user_id(string): 要查询的QQ号，留空则查当前对话用户
            """
            target = user_id.strip() if (user_id and user_id.strip()) else event.get_sender_id()
            if self._is_admin_id(target):
                personal_display = "∞（管理员豁免）"
            else:
                personal_display = str(self._get_user_count(target))
            group_id = event.get_group_id()
            group_remaining = self._get_group_count(group_id) if group_id else 0
            user_limit_on = self.conf.get("enable_user_limit", True)
            group_limit_on = self.conf.get("enable_group_limit", False)
            return (f"用户 {target} 生图次数：个人剩余 {personal_display} 次（用户限制{'开' if user_limit_on else '关'}），"
                    f"当前群剩余 {group_remaining} 次（群限制{'开' if group_limit_on else '关'}）。")

        @filter.llm_tool(name="query_generation_models")
        async def query_generation_models(self, event: AstrMessageEvent):
            """查询当前可用的生图模型列表（含每次消耗配额、说明、允许分辨率）。在不确定应使用哪个生图模型、或用户询问有哪些生图模型时可调用。本工具不执行生图，仅查询。

            Args:
            """
            group_id = event.get_group_id()
            current = self.group_models.get(group_id, "") if group_id else ""
            if not self.available_models:
                return "当前未配置任何生图模型。"
            lines = []
            for idx, m in enumerate(self.available_models, 1):
                sizes = "/".join(m.get("allowed_sizes") or ["1K", "2K", "4K"])
                desc = m.get("desc") or "（无说明）"
                mark = "（当前群默认）" if m.get("name") == current else ""
                lines.append(f"{idx}. {m['name']} 消耗{m.get('quota', 1)}次/张 · 允许{sizes} · {desc}{mark}")
            return "可用生图模型：\n" + "\n".join(lines)

        @filter.llm_tool(name="query_generation_history")
        async def query_generation_history(self, event: AstrMessageEvent, scope: str = "本群", days: int = 0,
                                           limit: int = 10):
            """查询生图历史记录（每条包含时间戳、群号、提示词摘要、文件路径）。可用于回复用户「之前生过什么图」等。本工具不执行生图，仅查询。

            Args:
                scope(string): 查询范围，本群 或 全局，默认本群
                days(number): 最近多少天内的记录，可选，默认不限
                limit(number): 最多返回条数，默认 10，最大 20
            """
            scope_val = (scope or "").strip() or "本群"
            try:
                limit = max(1, min(int(limit) if limit else 10, LOG_QUERY_LIMIT_MAX))
            except (TypeError, ValueError):
                limit = 10
            records = list(self.generation_history)
            # 时间倒序（最新在前）：generation_history 为 append 顺序，最新在末尾
            records = list(reversed(records))
            # 范围过滤：默认本群（event.get_group_id() 为空则退化为全局）
            if scope_val != "全局":
                group_id = event.get_group_id()
                if group_id:
                    records = [r for r in records if str(r.get("group_id", "")) == str(group_id)]
            # days 过滤：可选，按时间戳字符串解析比较
            if days:
                try:
                    days = max(1, int(days))
                except (TypeError, ValueError):
                    days = 0
                if days:
                    cutoff = datetime.now() - timedelta(days=days)
                    filtered = []
                    for r in records:
                        try:
                            if datetime.strptime(str(r.get("timestamp", "")), "%Y%m%d_%H%M%S_%f") >= cutoff:
                                filtered.append(r)
                        except ValueError:
                            continue
                    records = filtered
            records = records[:limit]
            if not records:
                return "暂无生图记录"
            lines = []
            for r in records:
                prompt_short = str(r.get("prompt", ""))[:PROMPT_SHORT_LEN]
                lines.append(f"{r.get('timestamp','?')} | {r.get('group_id','') or 'private'} | {prompt_short} | "
                             f"{r.get('file_path','?')}")
            return "生图记录如下：\n" + "\n".join(lines)

        @filter.llm_tool(name="query_generation_image")
        async def query_generation_image(self, event: AstrMessageEvent, file_path: str = ""):
            """查询生图历史记录对应的原图，并将图片直接发送给用户。用户问「之前生成的那张图」「历史图片」等时可调用。本工具不执行生图，仅查询并发送图片。

            Args:
                file_path(string): 生图记录中的图片文件路径（来自 query_generation_history 返回的 file_path 字段）
            """
            # 校验 file_path 在 generations 目录内（防路径穿越），读取并转 base64 data URL 发送
            resolved, _, err_reason = self._resolve_generation_file(file_path)
            if not resolved:
                try:
                    await event.send(MessageChain(chain=[Plain(f"未找到该历史图片：{err_reason}")]))
                except Exception as e:
                    logger.error(f"[NewImage] 发送历史图片失败提示出错: {e}", exc_info=True)
                return f"未找到历史图片：{err_reason}"
            loop = asyncio.get_running_loop()
            try:
                data = await loop.run_in_executor(None, resolved.read_bytes)
            except Exception as e:
                logger.error(f"[NewImage] 读取历史图片失败: {e}", exc_info=True)
                try:
                    await event.send(MessageChain(chain=[Plain("读取历史图片失败，请稍后重试。")]))
                except Exception as e2:
                    logger.error(f"[NewImage] 发送读取失败提示出错: {e2}", exc_info=True)
                return "读取历史图片失败，请稍后重试。"
            try:
                await event.send(MessageChain(chain=[Image.fromBytes(data), Plain("历史图片")]))
            except Exception as e:
                logger.error(f"[NewImage] 发送历史图片失败: {e}", exc_info=True)
                return "发送历史图片失败，请稍后重试。"
            return "已将历史图片发送给用户。"

        @filter.llm_tool(name="query_generation_stats")
        async def query_generation_stats(self, event: AstrMessageEvent, scope: str = "本群", days: int = 7):
            """统计生图使用情况：总张数、按模型分组、按用户分组（Top）。用户问「最近生成了多少图」「使用统计」等时可调用。本工具不执行生图，仅统计。

            Args:
                scope(string): 统计范围，本群 或 全局，默认本群
                days(number): 最近多少天，默认 7
            """
            scope_val = (scope or "").strip() or "本群"
            try:
                days = max(1, int(days))
            except (TypeError, ValueError):
                days = 7
            records = list(self.generation_history)
            # 范围过滤：本群按当前会话群匹配；全局全部（本群且无群上下文时退化为全局）
            if scope_val != "全局":
                group_id = event.get_group_id()
                if group_id:
                    records = [r for r in records if str(r.get("group_id", "")) == str(group_id)]
            # days 过滤：按时间戳 %Y%m%d_%H%M%S_%f 解析比较
            cutoff = datetime.now() - timedelta(days=days)
            filtered = []
            for r in records:
                try:
                    if datetime.strptime(str(r.get("timestamp", "")), "%Y%m%d_%H%M%S_%f") >= cutoff:
                        filtered.append(r)
                except ValueError:
                    continue
            if not filtered:
                return "暂无生图记录"
            total = len(filtered)
            by_model: Dict[str, int] = {}
            by_user: Dict[str, int] = {}
            for r in filtered:
                model = str(r.get("model", "") or "未知")
                by_model[model] = by_model.get(model, 0) + 1
                user = str(r.get("user_id", "") or "未知")
                by_user[user] = by_user.get(user, 0) + 1
            # 按数量降序
            model_parts = [f"{m} {c}张" for m, c in sorted(by_model.items(), key=lambda x: -x[1])]
            user_parts = [f"{u} {c}张" for u, c in sorted(by_user.items(), key=lambda x: -x[1])[:5]]
            scope_txt = "全局" if scope_val == "全局" else "本群"
            return (f"近 {days} 天（{scope_txt}）共生成 {total} 张图片。\n"
                    f"按模型：{'，'.join(model_parts)}\n"
                    f"按用户（Top5）：{'，'.join(user_parts)}")

        @filter.llm_tool(name="query_presets")
        async def query_presets(self, event: AstrMessageEvent, preset_name: str = ""):
            """查询生图预设（风格指令）列表或指定预设的提示词内容。用户问「有哪些预设/风格指令」「某个预设的提示词」时可调用。本工具不执行生图，仅查询。

            Args:
                preset_name(string): 预设触发词（如 手办化），留空则列出全部预设触发词
            """
            if not self.prompt_map:
                return "当前未配置任何生图预设。"
            keyword = (preset_name or "").strip()
            if keyword:
                prompt = self.prompt_map.get(keyword)
                if not prompt:
                    return f"未找到预设 [{keyword}]。可用预设：{'、'.join(list(self.prompt_map.keys()))}"
                return f"预设 [{keyword}] 的提示词内容：\n{prompt}"
            presets = list(self.prompt_map.keys())
            return f"可用生图预设（{len(presets)} 个）：\n{'、'.join(presets)}"

    def _effective_model_is_gemini(self, group_id: Optional[str] = None,
                                   model_override: Optional[str] = None) -> bool:
        """判断「最终生效模型」是否属于 type=gemini 的提供商。

        模型优先级与 _call_api 一致：model_override > 本群 group_models > available_model_names[0]。
        无法确定模型时回退：存在任一启用且 type=gemini 的提供商即视为可用。
        （is_gemini_api 已废弃为历史配置，不再作为唯一闸门，以 providers.json 的 provider.type 为准。）
        """
        model_name = (model_override or "").strip()
        if not model_name and group_id:
            model_name = self.group_models.get(group_id, "")
        if not model_name and self.available_model_names:
            model_name = self.available_model_names[0]
        if not model_name:
            return any(p.get("type") == "gemini" and p.get("enabled", True)
                       for p in self.providers)
        provider = self._find_provider_for_model(model_name)
        return bool(provider and provider.get("type") == "gemini" and provider.get("enabled", True))

    def _extract_image_size(self, text: str, sender_id: str,
                            group_id: Optional[str] = None,
                            model_override: Optional[str] = None) -> tuple:
        """
        从用户输入中提取分辨率参数（仅白名单用户 + 该模型所属提供商为 Gemini 时生效）。

        返回: (cleaned_text, image_size)
            - cleaned_text: 移除了分辨率关键词后的文本
            - image_size: 提取到的分辨率（如 "2K", "4K"）或 None
        """
        # 闸门：最终生效模型所属提供商 type 必须为 gemini 才启用 2K/4K 白名单提取
        # （openai/novalai 提供商模型不启用；is_gemini_api 历史配置不再参与判断）
        if not self._effective_model_is_gemini(group_id=group_id, model_override=model_override):
            return text, None
        
        # 检查用户是否在高分辨率白名单中
        if sender_id not in self.hires_whitelist:
            return text, None
        
        # 匹配 2K/4K（不区分大小写，支持独立出现或在开头/结尾）
        match = re.search(r'\b([24][kK])\b', text)
        if match:
            size_str = match.group(1).upper()
            # 从文本中移除匹配到的分辨率关键词
            cleaned = text[:match.start()] + text[match.end():]
            cleaned = cleaned.strip()
            # 清理多余的空格
            cleaned = re.sub(r'\s{2,}', ' ', cleaned)
            logger.info(f"[NewImage] 白名单用户 {sender_id} 提取分辨率参数: {size_str}")
            return cleaned, size_str
        
        return text, None

    async def _acquire_group_slot(self, group_id: Optional[str]) -> bool:
        if not group_id or self.group_task_limit <= 0:
            return True
        async with self.queue_lock:
            current = self.group_task_counts.get(group_id, 0)
            if current >= self.group_task_limit:
                return False
            self.group_task_counts[group_id] = current + 1
            logger.debug(f"[FigurinePro] 群 {group_id} 任务占用 {self.group_task_counts[group_id]}/{self.group_task_limit}")
            return True

    async def _release_group_slot(self, group_id: Optional[str]):
        if not group_id or self.group_task_limit <= 0:
            return
        async with self.queue_lock:
            current = self.group_task_counts.get(group_id, 0)
            if current <= 1:
                self.group_task_counts.pop(group_id, None)
            else:
                self.group_task_counts[group_id] = current - 1
            logger.debug(f"[FigurinePro] 群 {group_id} 任务释放，当前 {self.group_task_counts.get(group_id, 0)}")


    async def _call_api(self, image_bytes_list: List[bytes], prompt: str, image_size: Optional[str] = None,
                        group_id: Optional[str] = None, model_override: Optional[str] = None,
                        gen_params: Optional[Dict] = None, size_strict: bool = False) -> tuple:
        """调用 API 生成图像，支持 Gemini 原生 API 和 OpenAI 兼容格式双路由。

        参数 model_override: LLM 工具指定模型用（优先级最高，覆盖本群配置/provider/默认模型）。
        参数 gen_params: 生成参数（显式指定），在方法内与最终模型的默认参数合并（显式 > 模型默认）。
        参数 size_strict: 模型 allowed_sizes 限制校验模式——True=指令路径（用户显式指定不支持分辨率
            时报错拒绝）；False=LLM 路径（不支持时夹取到允许的最大分辨率，仅日志记录）。
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
        provider = self._find_provider_for_model(model_name)
        if not provider:
            return f"❌ 模型 {model_name} 未关联任何提供商，请在 WebUI 提供商管理中配置", model_name
        # 若命中的提供商 base_url 为空（旧迁移残留的 stub 提供商），优先改用真正可用的提供商
        if not str(provider.get("base_url", "") or "").strip():
            usable = self._find_usable_provider_for_model(model_name)
            if usable:
                old_name = str(provider.get("name", "") or provider.get("id", "未命名"))
                provider = usable
                logger.info(f"[NewImage] 提供商「{old_name}」无 BaseURL，已改用可用提供商 "
                            f"「{provider.get('name', '') or provider.get('id', '未命名')}」")
        if not provider.get("enabled", True):
            provider_name_dis = str(provider.get("name", "") or provider.get("id", "未命名"))
            return f"❌ 模型 {model_name} 所属提供商「{provider_name_dis}」已停用，请在 WebUI 提供商管理中启用", model_name
        api_url = str(provider.get("base_url", "") or "").strip()
        api_key = str(provider.get("api_key", "") or "").strip()
        provider_type = str(provider.get("type", "") or "openai").strip()
        provider_name = str(provider.get("name", "") or provider.get("id", "未命名"))
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

        # ========== 画师串预设合并（按群） ==========
        # 当前群激活的画师串预设：prompt 追加 artists 串控画风；预设 negative 并入负面词。
        artist_preset = self._get_group_artist_preset(group_id)
        if artist_preset:
            artists = str(artist_preset.get("artists", "") or "").strip()
            preset_neg = str(artist_preset.get("negative", "") or "").strip()
            if artists:
                prompt = f"{prompt}, {artists}" if prompt else artists
                logger.info(f"[NewImage] 群 {group_id} 应用画师串预设「{artist_preset.get('name', '')}」，prompt 追加画师标签")
            if preset_neg:
                existing_neg = str(final_params.get("negative_prompt", "") or "").strip()
                final_params["negative_prompt"] = f"{existing_neg}, {preset_neg}" if existing_neg else preset_neg
                logger.info(f"[NewImage] 群 {group_id} 画师串预设「{artist_preset.get('name', '')}」并入负面词")

        # ========== 按提供商 type 选择路由 ==========
        if provider_type == "gemini":
            return await self._call_gemini_api(api_url, api_key, model_name, image_bytes_list, prompt,
                                               image_size, source_info, final_params), model_name
        elif provider_type == "novalai":
            if provider.get("use_reverse_proxy"):
                return await self._call_novalai_reverse_proxy_api(api_url, api_key, model_name, image_bytes_list,
                                                                  prompt, image_size, source_info,
                                                                  final_params), model_name
            return await self._call_novalai_api(api_url, api_key, model_name, image_bytes_list, prompt,
                                                image_size, source_info, final_params), model_name
        else:
            return await self._call_openai_api(api_url, api_key, model_name, image_bytes_list, prompt,
                                               source_info, final_params), model_name

    async def _call_gemini_api(self, api_url: str, api_key: str, model_name: str,
                                image_bytes_list: List[bytes], prompt: str,
                                image_size: Optional[str], source_info: str,
                                gen_params: Optional[Dict] = None) -> bytes | str:
        """Gemini 原生 API 路径（generateContent）

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

    async def _call_openai_api(self, api_url: str, api_key: str, model_name: str,
                                image_bytes_list: List[bytes], prompt: str, source_info: str,
                                gen_params: Optional[Dict] = None) -> bytes | str:
        """OpenAI 兼容 API 路径（/v1/chat/completions）

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
                                gen_params: Optional[Dict] = None) -> bytes | str:
        """NovelAI 独立路由（POST {base_url}/ai/generate-image）。

        NovelAI 结构与 gemini/openai 完全不同：prompt 放在 v4_prompt.caption.base_caption，
        负面词放 negative_prompt + v4_negative_prompt.caption.base_caption 两处；
        图生图 action=img2img 并在 parameters 追加 strength/noise/image/extra_noise_seed，
        width/height 覆盖为原图实际尺寸。响应为 ZIP（image_0.png）。

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

        # ========== 负面词：默认模板作底，用户传入则替换 ==========
        negative_prompt = NOVALAI_DEFAULT_NEGATIVE_PROMPT
        user_neg = (gen_params or {}).get("negative_prompt")
        if isinstance(user_neg, str) and user_neg.strip():
            negative_prompt = user_neg.strip()

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
            "input": "",
            "model": model_name,
            "action": "img2img" if img2img else "generate",
            "parameters": parameters,
        }

        # ========== 端点 & 请求头 ==========
        base = api_url.rstrip("/")
        endpoint = f"{base}/ai/generate-image"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Origin": "https://novelai.net",
            "Referer": "https://novelai.net",
            "User-Agent": NOVALAI_UA,
        }

        logger.info(f"[NewImage/NovelAI] 发送请求 [{source_info}]: Model={model_name}, "
                    f"Action={'img2img' if img2img else 'generate'}, "
                    f"Size={width}x{height}, Steps={steps}")

        try:
            if not self.iwf:
                return "ImageWorkflow 未初始化"
            async with self.iwf.session.post(
                endpoint, json=request_body, headers=headers,
                proxy=self.iwf.proxy, timeout=self.api_timeout,
            ) as resp:
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
                    if resp.status >= 500:
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

        # ========== 负面词：默认模板作底，用户传入则替换 ==========
        negative_prompt = NOVALAI_DEFAULT_NEGATIVE_PROMPT
        user_neg = (gen_params or {}).get("negative_prompt")
        if isinstance(user_neg, str) and user_neg.strip():
            negative_prompt = user_neg.strip()

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

        try:
            if not self.iwf:
                return "ImageWorkflow 未初始化"
            async with self.iwf.session.get(
                endpoint, params=params,
                ssl=False,  # 反代常为自签/无证书，跳过 TLS 校验
                proxy=self.iwf.proxy, timeout=self.api_timeout,
            ) as resp:
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
                    if resp.status >= 500:
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

    # ========== 插件 WebUI 后端 API（context.register_web_api） ==========
    WEB_PLUGIN_NAME = "astrbot_plugin_rainnya_image"
    # POST /config 白名单字段（键名 = conf 键名；api_url/api_keys/is_gemini_api/model/provider_id 已迁移到提供商，不再暴露）
    _WEB_CONFIG_FIELD_TYPES = {
        # 基础
        "prefix": bool,              # 是否要求指令必须带前缀
        "extra_prefix": str,         # 自定义提示词模式的前缀
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
        "novalai_size": str,         # 1K/2K/4K（空=不指定）
        "novalai_sampler": str,      # 采样器
        "novalai_steps": int,        # 1-50
        "novalai_scale": float,      # CFG 0-10
        "novalai_negative_prompt": str,  # 负面词（空=用默认模板）
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
    }

    def _register_web_apis(self) -> None:
        """注册插件 WebUI 后端 API（仅注册一次，重载插件时幂等）。"""
        if self._web_registered:
            return
        self._web_registered = True
        base = f"/{self.WEB_PLUGIN_NAME}"
        endpoints = [
            (f"{base}/models", self._web_models_get, ["GET"], "获取可用模型列表（含配额/说明，兼容旧前端：返回第一个启用提供商）"),
            (f"{base}/models", self._web_models_post, ["POST"], "保存可用模型列表（已迁移到提供商管理，返回引导错误）"),
            (f"{base}/providers", self._web_providers_get, ["GET"], "获取提供商列表（API Key 掩码）"),
            (f"{base}/providers", self._web_providers_post, ["POST"], "新增提供商"),
            (f"{base}/providers/delete", self._web_providers_delete, ["POST"], "删除提供商（含其模型）"),
            (f"{base}/providers/save", self._web_providers_save, ["POST"], "更新提供商配置（api_key 传空串表示不修改）"),
            (f"{base}/providers/models", self._web_providers_models_get, ["GET"], "获取指定提供商的模型列表"),
            (f"{base}/providers/models", self._web_providers_models_post, ["POST"], "保存指定提供商的模型列表"),
            (f"{base}/users", self._web_users_get, ["GET"], "获取用户次数列表（分页/搜索）"),
            (f"{base}/users/add", self._web_users_add, ["POST"], "增加用户次数（不受上限限制）"),
            (f"{base}/users/set", self._web_users_set, ["POST"], "设置用户次数为指定值（0 或任意非负整数）"),
            (f"{base}/groups", self._web_groups_get, ["GET"], "获取群组次数列表（分页/搜索）"),
            (f"{base}/groups/add", self._web_groups_add, ["POST"], "增加群组次数"),
            (f"{base}/groups/set", self._web_groups_set, ["POST"], "设置群组次数为指定值（0 或任意非负整数）"),
            (f"{base}/logs", self._web_logs_get, ["GET"], "获取生图时间线（范围/天数/分页）"),
            (f"{base}/logs/image", self._web_logs_image, ["GET"], "获取生图图片（路径校验 + base64）"),
            (f"{base}/logs/delete", self._web_logs_delete, ["POST"], "删除生图时间线记录（按时间戳或范围）"),
            (f"{base}/config", self._web_config_get, ["GET"], "获取全部设置（API 配置已迁移到提供商管理）"),
            (f"{base}/config", self._web_config_post, ["POST"], "保存全部设置（白名单字段，一次性原子落盘）"),
            (f"{base}/artist-presets", self._web_artist_presets_get, ["GET"], "获取画师串预设库"),
            (f"{base}/artist-presets", self._web_artist_presets_post, ["POST"], "保存画师串预设库"),
            (f"{base}/artist-presets/group", self._web_artist_groups_get, ["GET"], "获取各群画师串激活映射"),
            (f"{base}/artist-presets/group", self._web_artist_groups_post, ["POST"], "设置某群画师串预设"),
        ]
        for route, handler, methods, desc in endpoints:
            self.context.register_web_api(route, handler, methods, desc)

    def _web_require_login(self):
        """校验请求来自 Dashboard 登录用户（request.username 存在）。返回 None 表示放行，否则返回错误响应。"""
        try:
            username = request.username
        except Exception:
            username = None
        if not username:
            return error_response("未登录，禁止访问（仅 Dashboard 登录用户可调用）", status_code=403)
        return None

    async def _web_models_get(self):
        """兼容旧前端：返回第一个启用提供商的模型列表（未迁移的旧面板仍可读）。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            target = next((p for p in self.providers if p.get("enabled", True)), None)
            if not target:
                return json_response({"models": []})
            models = [
                {"name": m["name"], "quota": int(m.get("quota", 1)), "desc": str(m.get("desc", "") or ""),
                 "params": dict(m.get("params") or {}),
                 "allowed_sizes": m.get("allowed_sizes")}
                for m in (target.get("models") or [])
                if isinstance(m, dict) and m.get("name")
            ]
            return json_response({"models": models})
        except Exception as e:
            return error_response(str(e))

    async def _web_models_post(self):
        """模型管理已迁移到提供商管理：旧 POST /models 直接返回引导错误，避免误写旧 conf.model。"""
        if (resp := self._web_require_login()):
            return resp
        return error_response("模型管理已迁移到提供商管理，请使用 /providers/models 接口")

    # ---------- 多提供商管理 Web API ----------

    async def _web_providers_get(self):
        """GET {base}/providers：返回提供商列表。

        返回完整 api_key（WebUI 管理面板可查看/回填编辑框）+ api_key_masked（兼容旧前端）。
        use_reverse_proxy：novalai 是否走反代模式（GET /generate）。
        本端点经 _web_require_login 保护，仅 Dashboard 登录用户可访问，完整 key 不对外泄露。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            providers = []
            for p in self.providers:
                providers.append({
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "type": p.get("type", ""),
                    "api_key": str(p.get("api_key", "") or ""),
                    "api_key_masked": self._mask_api_key(p.get("api_key", "")),
                    "base_url": p.get("base_url", ""),
                    "use_reverse_proxy": bool(p.get("use_reverse_proxy", False)),
                    "enabled": bool(p.get("enabled", True)),
                    "model_count": len(p.get("models") or []),
                })
            return json_response({"providers": providers})
        except Exception as e:
            return error_response(str(e))

    async def _web_providers_post(self):
        """POST {base}/providers：新增提供商。body {name,type,api_key,base_url,use_reverse_proxy?}。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            name = str(payload.get("name", "") or "").strip()
            if not name:
                return error_response("提供商名称不能为空")
            ptype = str(payload.get("type", "") or "").strip()
            if ptype not in ("gemini", "openai", "novalai"):
                return error_response("type 必须是 gemini/openai/novalai 之一")
            api_key = str(payload.get("api_key", "") or "").strip()
            if not api_key:
                return error_response("API Key 不能为空")
            base_url = str(payload.get("base_url", "") or "").strip()
            if not base_url:
                return error_response("BaseURL 不能为空")
            provider = {
                "id": self._new_provider_id(),
                "name": name,
                "type": ptype,
                "api_key": api_key,
                "base_url": base_url,
                "use_reverse_proxy": bool(payload.get("use_reverse_proxy", False)),
                "enabled": True,
                "models": list(PROVIDER_DEFAULT_MODELS.get(ptype, [])),
            }
            self.providers.append(provider)
            await self._save_providers()
            await self._load_providers()
            return json_response({"ok": True, "id": provider["id"]})
        except Exception as e:
            return error_response(str(e))

    async def _web_providers_delete(self):
        """POST {base}/providers/delete：删除提供商（其下模型一并删除）。body {id}。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            pid = str(payload.get("id", "") or "").strip()
            if not pid:
                return error_response("id 不能为空")
            before = len(self.providers)
            self.providers = [p for p in self.providers if p.get("id") != pid]
            if len(self.providers) == before:
                return error_response("未找到该提供商")
            await self._save_providers()
            await self._load_providers()
            return json_response({"ok": True})
        except Exception as e:
            return error_response(str(e))

    async def _web_providers_save(self):
        """POST {base}/providers/save：更新提供商配置。body {id,name,api_key,base_url,use_reverse_proxy,enabled}。

        api_key 传空串表示不修改（保留原值）；name/base_url/enabled/use_reverse_proxy 可选。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            pid = str(payload.get("id", "") or "").strip()
            if not pid:
                return error_response("id 不能为空")
            target = next((p for p in self.providers if p.get("id") == pid), None)
            if not target:
                return error_response("未找到该提供商")
            if "name" in payload:
                name = str(payload.get("name", "") or "").strip()
                if not name:
                    return error_response("提供商名称不能为空")
                target["name"] = name
            if "type" in payload:
                ptype = str(payload.get("type", "") or "").strip()
                if ptype not in ("gemini", "openai", "novalai"):
                    return error_response("type 必须是 gemini/openai/novalai 之一")
                target["type"] = ptype
            if "base_url" in payload:
                base_url = str(payload.get("base_url", "") or "").strip()
                if not base_url:
                    return error_response("BaseURL 不能为空")
                target["base_url"] = base_url
            if "api_key" in payload:
                api_key = str(payload.get("api_key", "") or "").strip()
                if api_key:  # 传空串表示不修改，保留原值
                    target["api_key"] = api_key
            if "use_reverse_proxy" in payload:
                target["use_reverse_proxy"] = bool(payload.get("use_reverse_proxy"))
            if "enabled" in payload:
                target["enabled"] = bool(payload.get("enabled"))
            await self._save_providers()
            await self._load_providers()
            return json_response({"ok": True})
        except Exception as e:
            return error_response(str(e))

    async def _web_providers_models_get(self):
        """GET {base}/providers/models?provider={id}：返回该提供商模型数组。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            pid = (request.query.get("provider", "") or "").strip()
            target = next((p for p in self.providers if p.get("id") == pid), None)
            if not target:
                return error_response("未找到该提供商")
            models = [
                {"name": m["name"], "quota": int(m.get("quota", 1)), "desc": str(m.get("desc", "") or ""),
                 "params": dict(m.get("params") or {}),
                 "allowed_sizes": m.get("allowed_sizes")}
                for m in (target.get("models") or [])
                if isinstance(m, dict) and m.get("name")
            ]
            return json_response({"models": models})
        except Exception as e:
            return error_response(str(e))

    async def _web_providers_models_post(self):
        """POST {base}/providers/models：保存该提供商模型。body {provider:id, models:[...]}。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            pid = str(payload.get("provider", "") or "").strip()
            if not pid:
                return error_response("provider 不能为空")
            target = next((p for p in self.providers if p.get("id") == pid), None)
            if not target:
                return error_response("未找到该提供商")
            models = payload.get("models")
            if not isinstance(models, list):
                return error_response("models 必须是数组")
            # 复用旧 /models 的校验/序列化逻辑（抽成公共方法），经行格式 round-trip 后落盘 dict
            lines, err = self._serialize_models_lines(models)
            if err:
                return error_response(err)
            target["models"] = self._parse_models_lines(lines)
            await self._save_providers()
            await self._load_providers()
            return json_response({"ok": True, "count": len(target["models"])})
        except Exception as e:
            return error_response(str(e))

    async def _web_users_get(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            search = (request.query.get("search", "") or "").strip()
            page = max(1, request.query.get("page", 1, type=int))
            page_size = max(1, min(request.query.get("page_size", 20, type=int), WEB_PAGE_SIZE_MAX))
            items = [{"qq": uid, "count": cnt} for uid, cnt in self.user_counts.items()]
            if search:
                items = [it for it in items if search in it["qq"]]
            items.sort(key=lambda x: x["count"], reverse=True)
            total = len(items)
            start = (page - 1) * page_size
            return json_response({"users": items[start:start + page_size], "total": total})
        except Exception as e:
            return error_response(str(e))

    async def _web_users_add(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            qq = str(payload.get("qq", "")).strip()
            if not qq:
                return error_response("qq 不能为空")
            try:
                amount = int(payload.get("amount", 0))
            except (TypeError, ValueError):
                return error_response("amount 必须为整数")
            if amount < 1:
                return error_response("amount 必须 >= 1")
            async with self._counts_lock:
                current_count = self._get_user_count(qq)
                new_count = current_count + amount
                # 管理员豁免 cap——不受 user_count_cap 截断
                self.user_counts[str(qq)] = new_count
                await self._save_user_counts()
            return json_response({"ok": True, "count": new_count})
        except Exception as e:
            return error_response(str(e))

    async def _web_groups_get(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            search = (request.query.get("search", "") or "").strip()
            page = max(1, request.query.get("page", 1, type=int))
            page_size = max(1, min(request.query.get("page_size", 20, type=int), WEB_PAGE_SIZE_MAX))
            items = [{"group_id": gid, "count": cnt} for gid, cnt in self.group_counts.items()]
            if search:
                items = [it for it in items if search in it["group_id"]]
            items.sort(key=lambda x: x["count"], reverse=True)
            total = len(items)
            start = (page - 1) * page_size
            return json_response({"groups": items[start:start + page_size], "total": total})
        except Exception as e:
            return error_response(str(e))

    async def _web_groups_add(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            group_id = str(payload.get("group_id", "")).strip()
            if not group_id:
                return error_response("group_id 不能为空")
            try:
                amount = int(payload.get("amount", 0))
            except (TypeError, ValueError):
                return error_response("amount 必须为整数")
            if amount < 1:
                return error_response("amount 必须 >= 1")
            async with self._counts_lock:
                current_count = self._get_group_count(group_id)
                new_count = current_count + amount
                self.group_counts[str(group_id)] = new_count
                await self._save_group_counts()
            return json_response({"ok": True, "count": new_count})
        except Exception as e:
            return error_response(str(e))

    async def _web_users_set(self):
        """设置用户次数为指定值（0 或任意非负整数，不受 user_count_cap 上限限制）。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            qq = str(payload.get("qq", "")).strip()
            if not qq:
                return error_response("qq 不能为空")
            try:
                count = int(payload.get("count", 0))
            except (TypeError, ValueError):
                return error_response("count 必须为整数")
            if count < 0:
                return error_response("count 必须 >= 0")
            async with self._counts_lock:
                self.user_counts[str(qq)] = count
                await self._save_user_counts()
            return json_response({"ok": True, "count": count})
        except Exception as e:
            return error_response(str(e))

    async def _web_groups_set(self):
        """设置群组次数为指定值（0 或任意非负整数）。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            group_id = str(payload.get("group_id", "")).strip()
            if not group_id:
                return error_response("group_id 不能为空")
            try:
                count = int(payload.get("count", 0))
            except (TypeError, ValueError):
                return error_response("count 必须为整数")
            if count < 0:
                return error_response("count 必须 >= 0")
            async with self._counts_lock:
                self.group_counts[str(group_id)] = count
                await self._save_group_counts()
            return json_response({"ok": True, "count": count})
        except Exception as e:
            return error_response(str(e))

    async def _web_logs_get(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            scope = (request.query.get("scope", "") or "").strip() or "本群"
            days = request.query.get("days", 0, type=int)
            limit = max(1, min(request.query.get("limit", 20, type=int), 200))
            page = max(1, request.query.get("page", 1, type=int))
            records = list(reversed(self.generation_history))  # 时间倒序（最新在前）
            if scope != "全局":
                # 本群：WebUI 无群上下文，需显式 group_id 查询参数
                gid = (request.query.get("group_id", "") or "").strip()
                records = [r for r in records if str(r.get("group_id", "")) == str(gid)] if gid else []
            if days > 0:
                cutoff = datetime.now() - timedelta(days=days)
                filtered = []
                for r in records:
                    try:
                        if datetime.strptime(str(r.get("timestamp", "")), "%Y%m%d_%H%M%S_%f") >= cutoff:
                            filtered.append(r)
                    except ValueError:
                        continue
                records = filtered
            total = len(records)
            start = (page - 1) * limit
            logs = [{
                "timestamp": r.get("timestamp", ""),
                "group_id": r.get("group_id", ""),
                "user_id": r.get("user_id", ""),
                "prompt": r.get("prompt", ""),
                "model": r.get("model", ""),
                "file_path": r.get("file_path", ""),
            } for r in records[start:start + limit]]
            return json_response({"logs": logs, "total": total})
        except Exception as e:
            return error_response(str(e))

    async def _web_logs_delete(self):
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            # 用 _history_lock 包裹「读-筛选-改-盘」，与 _append_generation_history 一致，
            # 防止删除期间并发生成追加导致 read-modify-write 丢记录
            async with self._history_lock:
                remaining = list(self.generation_history)
                if "timestamps" in payload:
                    ts_set = set(str(t) for t in payload.get("timestamps", []) if str(t).strip())
                    if not ts_set:
                        return error_response("timestamps 不能为空")
                    remaining = [r for r in remaining if str(r.get("timestamp", "")) not in ts_set]
                elif "start" in payload and "end" in payload:
                    start_ts = str(payload.get("start", "")).strip()
                    end_ts = str(payload.get("end", "")).strip()
                    if not start_ts or not end_ts:
                        return error_response("范围删除需要 start 与 end 时间戳")
                    scope_key = str(payload.get("scope", "全局") or "全局")
                    kept = []
                    for r in self.generation_history:
                        r_ts = str(r.get("timestamp", ""))
                        in_range = start_ts <= r_ts <= end_ts
                        in_scope = (scope_key == "全局") or (str(r.get("group_id", "")) == str(scope_key))
                        if not (in_range and in_scope):
                            kept.append(r)
                    remaining = kept
                else:
                    return error_response("需要提供 timestamps 或 scope+start+end")
                removed = len(self.generation_history) - len(remaining)
                if removed:
                    self.generation_history = remaining
                    await self._save_generation_history()
            return json_response({"ok": True, "deleted": removed})
        except Exception as e:
            return error_response(str(e))

    def _resolve_generation_file(self, raw_path: str):
        """校验并解析生图文件路径（供 _web_logs_image / query_generation_image 复用）。

        返回 (path, content_type, err_reason)：
            - 成功：path 为 resolved Path、content_type 为 MIME、err_reason 为 None
            - 失败：path 为 None、err_reason 为原因字符串（缺参/非法路径/不存在/过大）
        """
        if not raw_path or not raw_path.strip():
            return None, None, "缺少 path 参数"
        gen_root = (self.plugin_data_dir / "generations").resolve()
        try:
            resolved = Path(raw_path).resolve()
        except Exception:
            return None, None, "非法路径"
        # 防路径穿越：必须在 generations 目录之内
        if not resolved.is_relative_to(gen_root):
            return None, None, "非法路径"
        if not resolved.is_file():
            return None, None, "文件不存在"
        try:
            if resolved.stat().st_size > LOG_IMAGE_MAX_BYTES:
                return None, None, "图片过大（超过 20MB）"
        except OSError:
            return None, None, "文件读取失败"
        ext = resolved.suffix.lower()
        content_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(ext, "image/png")
        return resolved, content_type, None

    async def _web_logs_image(self):
        """返回生图记录对应的图片（base64 data URL）。

        参数 path: 日志里的 file_path（服务器绝对路径）。
        安全校验：resolve() 后必须位于 StarTools.get_data_dir()/generations 目录之内，防路径穿越。
        说明：父页面 bridge api:get 走 axios 默认 JSON 解析，二进制 file_response 无法直接透传，
        故改为返回 JSON {"image": "data:<type>;base64,..."}，前端以 <img src=dataURL> 展示。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            raw_path = (request.query.get("path", "") or "").strip()
            resolved, content_type, err_reason = self._resolve_generation_file(raw_path)
            if not resolved:
                if err_reason == "文件不存在" or err_reason == "文件读取失败":
                    status = 404
                elif err_reason and "过大" in err_reason:
                    status = 413
                else:
                    status = 400
                return error_response(err_reason, status)
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, resolved.read_bytes)
            b64 = base64.b64encode(data).decode("ascii")
            return json_response({"image": f"data:{content_type};base64,{b64}"})
        except Exception as e:
            logger.error(f"[NewImage] 读取生图图片失败: {e}", exc_info=True)
            return error_response(str(e))

    async def _web_artist_presets_get(self):
        """GET {base}/artist-presets：返回画师串预设库。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            return json_response({"presets": self.artist_presets})
        except Exception as e:
            return error_response(str(e))

    async def _web_artist_presets_post(self):
        """POST {base}/artist-presets：保存画师串预设库。body {presets:[{name,artists,negative}]}。

        name/artists 非空（negative 可空）；name 去重保序（重复取最后一个）。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            presets = payload.get("presets")
            if not isinstance(presets, list):
                return error_response("presets 必须是数组")
            cleaned: List[Dict[str, Any]] = []
            seen: set = set()
            for item in presets:
                if not isinstance(item, dict):
                    return error_response("presets 元素必须是对象")
                name = str(item.get("name", "") or "").strip()
                if not name:
                    return error_response("预设名不能为空")
                artists = str(item.get("artists", "") or "").strip()
                if not artists:
                    return error_response(f"预设 [{name}] 的画师串不能为空")
                negative = str(item.get("negative", "") or "").strip()
                if name in seen:
                    # 重名：覆盖同名条目（保序）
                    for existing in cleaned:
                        if existing["name"] == name:
                            existing["artists"] = artists
                            existing["negative"] = negative
                            break
                    continue
                seen.add(name)
                cleaned.append({"name": name, "artists": artists, "negative": negative})
            self.artist_presets = cleaned
            await self._save_artist_presets()
            await self._load_artist_presets()
            return json_response({"ok": True, "count": len(self.artist_presets)})
        except Exception as e:
            return error_response(str(e))

    async def _web_artist_groups_get(self):
        """GET {base}/artist-presets/group：返回各群画师串激活映射。"""
        if (resp := self._web_require_login()):
            return resp
        try:
            return json_response({"groups": self.group_artists})
        except Exception as e:
            return error_response(str(e))

    async def _web_artist_groups_post(self):
        """POST {base}/artist-presets/group：设置某群画师串预设。body {group_id, preset}。

        preset 为空串或 "off" 表示关闭（移除该群配置）；否则须在预设库中存在。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            group_id = str(payload.get("group_id", "") or "").strip()
            if not group_id:
                return error_response("group_id 不能为空")
            preset = str(payload.get("preset", "") or "").strip()
            if not preset or preset.lower() == "off":
                self.group_artists.pop(group_id, None)
                await self._save_group_artists()
                return json_response({"ok": True, "preset": ""})
            if not any(p.get("name") == preset for p in self.artist_presets):
                return error_response(f"预设 [{preset}] 不存在")
            self.group_artists[group_id] = preset
            await self._save_group_artists()
            return json_response({"ok": True, "preset": preset})
        except Exception as e:
            return error_response(str(e))

    async def _web_config_get(self):
        """GET {base}/config：返回全部设置（默认值对齐 _conf_schema.json）。

        注意：api_url/api_keys/is_gemini_api/model/provider_id 已迁移到提供商管理（providers.json），
        不再从此接口返回；WebUI 基础设置页暴露全部非 API 配置字段（含各渠道默认参数）。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            return json_response({
                # 基础
                "prefix": bool(self.conf.get("prefix", True)),
                "extra_prefix": str(self.conf.get("extra_prefix", "bnn") or ""),
                "use_proxy": bool(self.conf.get("use_proxy", False)),
                "proxy_url": str(self.conf.get("proxy_url", "") or ""),
                "download_timeout": self.conf.get("download_timeout", 60),
                "download_retries": self.conf.get("download_retries", 3),
                "api_timeout": self.conf.get("api_timeout", 120),
                # Gemini
                "safety_level": str(self.conf.get("safety_level", "") or ""),
                "image_size": str(self.conf.get("image_size", "") or ""),
                "hires_whitelist": self.conf.get("hires_whitelist", []),
                # OpenAI 渠道默认参数
                "openai_size": str(self.conf.get("openai_size", "") or ""),
                "openai_temperature": self.conf.get("openai_temperature", 1.0),
                "openai_max_tokens": self.conf.get("openai_max_tokens", 2048),
                # NovalAI 渠道默认参数
                "novalai_size": str(self.conf.get("novalai_size", "") or ""),
                "novalai_sampler": str(self.conf.get("novalai_sampler", "k_euler_ancestral") or ""),
                "novalai_steps": self.conf.get("novalai_steps", 23),
                "novalai_scale": self.conf.get("novalai_scale", 6.0),
                "novalai_negative_prompt": str(self.conf.get("novalai_negative_prompt", "") or ""),
                # 权限
                "user_whitelist": self.conf.get("user_whitelist", []),
                "user_blacklist": self.conf.get("user_blacklist", []),
                "group_whitelist": self.conf.get("group_whitelist", []),
                "group_blacklist": self.conf.get("group_blacklist", []),
                # 次数
                "enable_user_limit": bool(self.conf.get("enable_user_limit", True)),
                "enable_group_limit": bool(self.conf.get("enable_group_limit", False)),
                "user_count_cap": self.conf.get("user_count_cap", 0),
                "group_task_limit": self.conf.get("group_task_limit", 2),
                # 签到
                "enable_checkin": bool(self.conf.get("enable_checkin", False)),
                "checkin_fixed_reward": self.conf.get("checkin_fixed_reward", 3),
                "enable_random_checkin": bool(self.conf.get("enable_random_checkin", False)),
                "checkin_random_reward_min": self.conf.get("checkin_random_reward_min", 1),
                "checkin_random_reward_max": self.conf.get("checkin_random_reward_max", 5),
                # 帮助/预设
                "help_menu": str(self.conf.get("help_menu", "") or ""),
                "prompt_list": self.conf.get("prompt_list", []),
            })
        except Exception as e:
            return error_response(str(e))

    async def _web_config_post(self):
        """POST {base}/config：保存全部设置（白名单 _WEB_CONFIG_FIELD_TYPES，一次性原子落盘）。

        兼容旧前端：`blacklist` 别名仍接受（映射到 user_blacklist）。
        prompt_list 元素必须是字符串且含 ":"（触发词:提示词），非法元素直接报错不落盘。
        """
        if (resp := self._web_require_login()):
            return resp
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            # blacklist 别名 → conf 键 user_blacklist
            if "blacklist" in payload and "user_blacklist" not in payload:
                payload["user_blacklist"] = payload.pop("blacklist")
            saved = []
            # AstrBotConfig(dict) 无 set 方法：先收集更新项，最后一次性原子落盘（避免多次写盘）
            config_updates: Dict[str, Any] = {}
            for key, expected in self._WEB_CONFIG_FIELD_TYPES.items():
                if key not in payload:
                    continue
                value = payload[key]
                if expected is bool:
                    if not isinstance(value, bool):
                        return error_response(f"字段 {key} 必须为布尔值")
                elif expected is float:
                    # float 字段：openai_temperature(0-2) / novalai_scale(0-10)，越界夹取（同 _coerce clamp 语义）
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        return error_response(f"字段 {key} 必须为数字")
                    if key == "openai_temperature":
                        value = max(0.0, min(value, 2.0))
                    elif key == "novalai_scale":
                        value = max(0.0, min(value, 10.0))
                elif expected is int:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        return error_response(f"字段 {key} 必须为整数")
                    # 非负校验：全部 int 字段均不允许负数（含超时/重试/签到奖励/上限等）
                    if value < 0:
                        return error_response(f"字段 {key} 必须 >= 0")
                    # 渠道字段范围夹取
                    if key == "openai_max_tokens":
                        value = max(64, min(value, 16384))
                    elif key == "novalai_steps":
                        value = max(1, min(value, 50))
                elif expected is list:
                    if not isinstance(value, list):
                        return error_response(f"字段 {key} 必须为数组")
                    if key == "prompt_list":
                        # 预设列表：元素必须是字符串且含 ":"（触发词:提示词）
                        for item in value:
                            if not isinstance(item, str) or ":" not in item:
                                return error_response("prompt_list 元素格式应为 触发词:提示词")
                    value = [str(v) for v in value]
                elif expected is str:
                    if not isinstance(value, str):
                        return error_response(f"字段 {key} 必须为字符串")
                    # 渠道 size 字段：仅允许 1K/2K/4K 或空串
                    if key in ("openai_size", "novalai_size") and value.strip() not in ("", "1K", "2K", "4K"):
                        return error_response(f"字段 {key} 只能为 1K/2K/4K 或空")
                config_updates[key] = value
                saved.append(key)
            if config_updates:
                await self.conf.save_config_async(config_updates)
            # 重载受影响的运行时状态
            if "user_count_cap" in saved:
                try:
                    self.user_count_cap = max(0, int(self.conf.get("user_count_cap", 0)))
                except (TypeError, ValueError):
                    self.user_count_cap = 0
            if "group_task_limit" in saved:
                try:
                    self.group_task_limit = max(0, int(self.conf.get("group_task_limit", 2)))
                except (TypeError, ValueError):
                    self.group_task_limit = 0
                # 并发任务计数在限制变化后不再准确，清空让各群重新计数
                self.group_task_counts.clear()
            if "api_timeout" in saved:
                try:
                    self.api_timeout = max(API_TIMEOUT_MIN_SECONDS, int(self.conf.get("api_timeout", 120)))
                except (TypeError, ValueError):
                    self.api_timeout = 120
            if "safety_level" in saved:
                self.safety_level = (self.conf.get("safety_level", "") or "").strip()
            if "image_size" in saved:
                self.default_image_size = (self.conf.get("image_size", "") or "").strip().upper()
            if "hires_whitelist" in saved:
                self.hires_whitelist = [str(x) for x in self.conf.get("hires_whitelist", [])]
            return json_response({"ok": True, "saved": saved})
        except Exception as e:
            return error_response(str(e))

    async def terminate(self):
        # 回收在途后台生图任务（防止插件卸载时任务悬挂）；未完成的任务被 gather 等待/吞异常
        if self._bg_tasks:
            pending = list(self._bg_tasks)
            self._bg_tasks.clear()
            await asyncio.gather(*pending, return_exceptions=True)
        if self.iwf:
            await self.iwf.terminate()
        logger.info("[FigurinePro] 插件已终止")
