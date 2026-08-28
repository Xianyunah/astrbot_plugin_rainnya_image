import asyncio
import base64
import functools
import io
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

import aiohttp
from PIL import Image as PILImage

from astrbot import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Reply, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.provider import Provider


@register(
    "astrbot_plugin_newimage",
    "辉宝",
    "AI生图插件：支持Gemini原生API与OpenAI兼容双路由，图生图/文生图/自定义Prompt，含安全过滤、高分辨率白名单与签到系统",
    "1.4.1",
    "https://github.com/YumenoSayuri/astrbot_plugin_nova_newimage",
)
class FigurineProPlugin(Star):
    class ImageWorkflow:
        def __init__(self, proxy_url: str | None = None, timeout: int = 60, retries: int = 3):
            if proxy_url: logger.info(f"ImageWorkflow 使用代理: {proxy_url}")
            self.session = aiohttp.ClientSession()
            self.proxy = proxy_url
            self.timeout = timeout
            self.retries = retries

        async def _download_image(self, url: str, timeout: Optional[int] = None, max_retries: Optional[int] = None) -> bytes | None:
            """
            下载图片，支持重试机制和更长的超时时间。
            
            Args:
                url: 图片URL
                timeout: 单次请求超时时间（秒），默认使用构造函数传入的值
                max_retries: 最大重试次数，默认使用构造函数传入的值
            """
            timeout = timeout or self.timeout
            max_retries = max_retries if max_retries is not None else self.retries
            
            logger.info(f"正在尝试下载图片: {url}")
            
            last_error = None
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        logger.info(f"第 {attempt + 1} 次重试下载: {url}")
                        # 重试前等待一小段时间
                        await asyncio.sleep(1 * attempt)
                    
                    async with self.session.get(url, proxy=self.proxy, timeout=timeout) as resp:
                        resp.raise_for_status()
                        return await resp.read()
                except aiohttp.ClientResponseError as e:
                    last_error = f"HTTP状态码 {e.status}: {e.message}"
                    logger.warning(f"图片下载失败 (尝试 {attempt + 1}/{max_retries}): {last_error}, URL: {url}")
                    if e.status in (400, 401, 403, 404):
                        # 这些错误重试也没用
                        break
                except asyncio.TimeoutError:
                    last_error = f"请求超时 ({timeout}s)"
                    logger.warning(f"图片下载失败 (尝试 {attempt + 1}/{max_retries}): {last_error}, URL: {url}")
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}"
                    logger.warning(f"图片下载失败 (尝试 {attempt + 1}/{max_retries}): {last_error}, URL: {url}")
            
            logger.error(f"图片下载最终失败: {last_error}, URL: {url}")
            return None

        async def _get_avatar(self, user_id: str) -> bytes | None:
            if not user_id.isdigit(): logger.warning(f"无法获取非 QQ 平台或无效 QQ 号 {user_id} 的头像。"); return None
            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
            return await self._download_image(avatar_url)

        def _extract_first_frame_sync(self, raw: bytes) -> bytes:
            img_io = io.BytesIO(raw)
            try:
                with PILImage.open(img_io) as img:
                    if getattr(img, "is_animated", False):
                        logger.info("检测到动图, 将抽取第一帧进行生成")
                        img.seek(0)
                        first_frame = img.convert("RGBA")
                        out_io = io.BytesIO()
                        first_frame.save(out_io, format="PNG")
                        return out_io.getvalue()
            except Exception as e:
                logger.warning(f"抽取图片帧时发生错误, 将返回原始数据: {e}", exc_info=True)
            return raw

        async def _load_bytes(self, src: str) -> bytes | None:
            raw: bytes | None = None
            loop = asyncio.get_running_loop()
            if Path(src).is_file():
                raw = await loop.run_in_executor(None, Path(src).read_bytes)
            elif src.startswith("http"):
                raw = await self._download_image(src)
            elif src.startswith("base64://"):
                raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
            if not raw: return None
            return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)

        async def _image_component_to_bytes(self, image_comp: Image) -> bytes | None:
            """
            将 Image 组件转换为字节。
            优先使用 AstrBot 提供的 convert_to_base64，兼容 WebChat 等平台的图片来源。
            """
            # 1. 直接尝试 convert_to_base64
            if hasattr(image_comp, "convert_to_base64"):
                try:
                    base64_str = await image_comp.convert_to_base64()
                    if base64_str:
                        if base64_str.startswith("data:image/"):
                            base64_str = base64_str.split(",", 1)[1]
                        return base64.b64decode(base64_str)
                except Exception as e:
                    logger.warning(f"通过 convert_to_base64 获取图片数据失败: {e}", exc_info=True)

            # 2. 回退到原有逻辑
            if image_comp.url:
                return await self._load_bytes(image_comp.url) or None
            if image_comp.file:
                return await self._load_bytes(image_comp.file) or None
            return None

        async def get_images(self, event: AstrMessageEvent) -> List[bytes]:
            img_bytes_list: List[bytes] = []
            at_user_ids: List[str] = []

            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and seg.chain:
                    for s_chain in seg.chain:
                        if isinstance(s_chain, Image):
                            if img := await self._image_component_to_bytes(s_chain):
                                img_bytes_list.append(img)

            for seg in event.message_obj.message:
                if isinstance(seg, Image):
                    if img := await self._image_component_to_bytes(seg):
                        img_bytes_list.append(img)
                elif isinstance(seg, At):
                    at_user_ids.append(str(seg.qq))

            if img_bytes_list:
                return img_bytes_list

            if at_user_ids:
                for user_id in at_user_ids:
                    if avatar := await self._get_avatar(user_id):
                        img_bytes_list.append(avatar)
                return img_bytes_list

            if avatar := await self._get_avatar(event.get_sender_id()):
                img_bytes_list.append(avatar)

            return img_bytes_list

        async def terminate(self):
            if self.session and not self.session.closed: await self.session.close()

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.plugin_data_dir = StarTools.get_data_dir()
        self.user_counts_file = self.plugin_data_dir / "user_counts.json"
        self.user_counts: Dict[str, int] = {}
        self.group_counts_file = self.plugin_data_dir / "group_counts.json"
        self.group_counts: Dict[str, int] = {}
        self.user_checkin_file = self.plugin_data_dir / "user_checkin.json"
        self.user_checkin_data: Dict[str, str] = {}
        self.group_model_file = self.plugin_data_dir / "group_model.json"
        self.group_models: Dict[str, str] = {}
        self.generation_history_file = self.plugin_data_dir / "generation_history.json"
        self.generation_history: List[Dict[str, Any]] = []
        self._history_lock = asyncio.Lock()  # 生图时间线并发追加/落盘锁
        self._log_page_msgid: Dict[Any, Any] = {}  # (会话, 发送者) -> {"msgid": 消息id, "page": 页码, "scope": 范围, "count": 条数}
        self.prompt_map: Dict[str, str] = {}
        self.key_index = 0
        self.key_lock = asyncio.Lock()
        self.iwf: Optional[FigurineProPlugin.ImageWorkflow] = None
        self.group_task_counts: Dict[str, int] = {}
        self.queue_lock = asyncio.Lock()
        self.group_task_limit: int = 0
        self.api_timeout: int = 120
        self.user_count_cap: int = 0  # 用户持有次数上限，0表示不限制
        # 供应商相关
        self.provider_id: str = ""
        self.provider: Optional[Provider] = None
        # Gemini 模式相关
        self.is_gemini_api: bool = False
        self.safety_level: str = ""
        self.default_image_size: str = ""
        self.hires_whitelist: List[str] = []
        # 模型相关
        self.available_models: List[str] = []  # 手动配置的可用模型列表（model 配置按行拆分，去空去重）

    async def initialize(self):
        use_proxy = self.conf.get("use_proxy", False)
        proxy_url = self.conf.get("proxy_url") if use_proxy else None
        
        # 获取下载配置
        dl_timeout = self.conf.get("download_timeout", 60)
        dl_retries = self.conf.get("download_retries", 3)
        
        self.iwf = self.ImageWorkflow(proxy_url, timeout=dl_timeout, retries=dl_retries)
        await self._load_prompt_map()
        await self._load_user_counts()
        await self._load_group_counts()
        await self._load_user_checkin_data()
        # 加载可用模型列表（model 配置支持多行填写，每行一个模型，去空去重）
        raw_models = (self.conf.get("model", "") or "").split("\n")
        self.available_models = list(dict.fromkeys([m.strip() for m in raw_models if m.strip()]))
        if len(self.available_models) > 1:
            logger.info(f"NewImage: 检测到 {len(self.available_models)} 个可用模型，可通过「切换模型」指令在群内切换")
        await self._load_group_models()
        await self._load_generation_history()
        self.api_timeout = max(10, int(self.conf.get("api_timeout", 120)))
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
        
        # 加载供应商配置
        self.provider_id = self.conf.get("provider_id", "")
        if self.provider_id:
            self.provider = self.context.get_provider_by_id(self.provider_id)
            if self.provider:
                logger.info(f"NewImage 插件已加载，使用提供商: {self.provider_id}")
            else:
                logger.warning(f"NewImage: 未找到提供商 '{self.provider_id}'，将使用手动配置")
        else:
            logger.info("NewImage 插件已加载，使用手动API配置")
            if not self.conf.get("api_keys") and not self.conf.get("api_url"):
                logger.warning("NewImage: 未配置提供商，也未配置手动API，插件可能无法工作")

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
        if cmd == bnn_command:
            user_prompt = text.removeprefix(cmd).strip()
            # 清除 @ 文本残留
            user_prompt = re.sub(r'@\S+?\(\d+\)', '', user_prompt).strip()
            user_prompt = re.sub(r'@\S+', '', user_prompt).strip()
            is_bnn = True
            if not user_prompt:
                yield event.plain_result("📝 用法：bnn <你的绘画想法>\n例如：bnn 樱花树下打伞的女孩")
                return
            # 从 bnn 的自定义 prompt 中提取分辨率（仅白名单用户）
            user_prompt, extracted_size = self._extract_image_size(user_prompt, sender_id)
            if not user_prompt:
                yield event.plain_result("📝 用法：bnn <你的绘画想法>\n例如：bnn 樱花树下打伞的女孩")
                return
        elif cmd in self.prompt_map:
            preset_prompt = self.prompt_map.get(cmd)
            extra_text = text.removeprefix(cmd).strip()
            # 清除 @ 文本残留（如 "@昵称(123456)"），避免被当成补充 prompt 传给 API
            extra_text = re.sub(r'@\S+?\(\d+\)', '', extra_text).strip()
            extra_text = re.sub(r'@\S+', '', extra_text).strip()
            if extra_text:
                # 从用户补充文本中提取分辨率（仅白名单用户）
                extra_text, extracted_size = self._extract_image_size(extra_text, sender_id)
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
            MAX_IMAGES = 5
            original_count = len(img_bytes_list)
            if original_count > MAX_IMAGES:
                images_to_process = img_bytes_list[:MAX_IMAGES]
                initial_messages.append(f"🎨 检测到 {original_count} 张图片，已选取前 {MAX_IMAGES} 张…")
            else:
                images_to_process = img_bytes_list
            display_cmd = user_prompt[:20] + '...' if len(user_prompt) > 20 else user_prompt
            initial_messages.append(f"🎨 检测到 {len(images_to_process)} 张图片，正在生成 [{display_cmd}]...")
        else:
            if not img_bytes_list:
                yield event.plain_result("请发送或引用一张图片。");
                return
            images_to_process = [img_bytes_list[0]]
            
            # 处理预设+补充的显示逻辑（清除 @ 文本避免显示中出现 @昵称）
            full_cmd_text = re.sub(r'@\S+?\(\d+\)', '', text).strip()
            full_cmd_text = re.sub(r'@\S+', '', full_cmd_text).strip()
            display_cmd = full_cmd_text[:30] + '...' if len(full_cmd_text) > 30 else full_cmd_text
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
            res, used_model = await self._call_api(images_to_process, user_prompt, image_size=extracted_size, group_id=group_id)
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
        
        # 从文生图 prompt 中提取分辨率（仅白名单用户）
        extracted_size = None
        prompt, extracted_size = self._extract_image_size(prompt, sender_id)
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

        display_prompt = prompt[:20] + '...' if len(prompt) > 20 else prompt
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
            res, used_model = await self._call_api([], prompt, image_size=extracted_size, group_id=group_id)
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

        await self.conf.set("prompt_list", prompt_list)
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
            msg = "📸 【生图插件帮助】\n\n"
            msg += "🎨 图生图预设指令:\n"
            msg += "、".join(self.prompt_map.keys())
            msg += "\n\n✏️ 纯文本生图指令:\n#文生图 <你的描述>"
            msg += "\n\n💡 使用方法:\n发送图片 + 预设指令 或 @用户 + 预设指令 来进行图生图。"
            msg += "\n\n🎁 每日签到:\n发送「辉宝赐福」获取免费次数"
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
        if not self.user_counts_file.exists(): self.user_counts = {}; return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.user_counts_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict): self.user_counts = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载用户次数文件时发生错误: {e}", exc_info=True);
            self.user_counts = {}

    async def _save_user_counts(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None,
                                                   functools.partial(json.dumps, self.user_counts, ensure_ascii=False,
                                                                     indent=4))
            await loop.run_in_executor(None, self.user_counts_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存用户次数文件时发生错误: {e}", exc_info=True)

    def _get_user_count(self, user_id: str) -> int:
        return self.user_counts.get(str(user_id), 0)

    async def _decrease_user_count(self, user_id: str):
        user_id_str = str(user_id)
        count = self._get_user_count(user_id_str)
        if count > 0: self.user_counts[user_id_str] = count - 1; await self._save_user_counts()

    async def _load_group_counts(self):
        if not self.group_counts_file.exists(): self.group_counts = {}; return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.group_counts_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict): self.group_counts = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载群组次数文件时发生错误: {e}", exc_info=True);
            self.group_counts = {}

    async def _save_group_counts(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None,
                                                   functools.partial(json.dumps, self.group_counts, ensure_ascii=False,
                                                                     indent=4))
            await loop.run_in_executor(None, self.group_counts_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存群组次数文件时发生错误: {e}", exc_info=True)

    def _get_group_count(self, group_id: str) -> int:
        return self.group_counts.get(str(group_id), 0)

    async def _decrease_group_count(self, group_id: str):
        group_id_str = str(group_id)
        count = self._get_group_count(group_id_str)
        if count > 0: self.group_counts[group_id_str] = count - 1; await self._save_group_counts()

    def _check_usage_quota(self, sender_id: str, group_id: Optional[str]) -> str | None:
        """检查次数配额，返回拒绝原因文案；None=可用。管理员豁免由调用方保证。"""
        user_count = self._get_user_count(sender_id)
        group_count = self._get_group_count(group_id) if group_id else 0
        user_limit_on = self.conf.get("enable_user_limit", True)
        group_limit_on = self.conf.get("enable_group_limit", False) and group_id

        if group_id:
            # 群聊：两种限制都开启时，需要至少一种有次数
            if user_limit_on and group_limit_on:
                if user_count <= 0 and group_count <= 0:
                    return "❌ 本群次数与您的个人次数均已用尽。\n请使用「辉宝赐福」获取次数。"
            elif user_limit_on and user_count <= 0:
                # 只开启用户限制，用户次数为0
                return "❌ 您的使用次数已用完。\n请使用「辉宝赐福」获取次数。"
            elif group_limit_on and group_count <= 0:
                # 只开启群组限制，群组次数为0
                return "❌ 本群次数已用尽。\n请联系管理员增加群组次数。"
        else:
            # 私聊：只检查用户限制
            if user_limit_on and user_count <= 0:
                return "❌ 您的使用次数已用完。\n请使用「辉宝赐福」获取次数。"
        return None

    async def _consume_quota(self, sender_id: str, group_id: Optional[str]) -> None:
        """生成成功后扣减次数：群组优先于个人（管理员豁免由调用方保证）。"""
        if self.conf.get("enable_group_limit", False) and group_id and self._get_group_count(group_id) > 0:
            await self._decrease_group_count(group_id)
        elif self.conf.get("enable_user_limit", True) and self._get_user_count(sender_id) > 0:
            await self._decrease_user_count(sender_id)

    async def _load_user_checkin_data(self):
        if not self.user_checkin_file.exists(): self.user_checkin_data = {}; return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.user_checkin_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict): self.user_checkin_data = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载用户签到文件时发生错误: {e}", exc_info=True);
            self.user_checkin_data = {}

    async def _save_user_checkin_data(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None, functools.partial(json.dumps, self.user_checkin_data,
                                                                           ensure_ascii=False, indent=4))
            await loop.run_in_executor(None, self.user_checkin_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存用户签到文件时发生错误: {e}", exc_info=True)

    async def _load_group_models(self):
        """加载各群配置的生图模型映射 {群号: 模型名}"""
        if not self.group_model_file.exists(): self.group_models = {}; return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.group_model_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict): self.group_models = {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载群组模型文件时发生错误: {e}", exc_info=True);
            self.group_models = {}

    async def _save_group_models(self):
        """保存各群配置的生图模型映射 {群号: 模型名}"""
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None, functools.partial(json.dumps, self.group_models,
                                                                           ensure_ascii=False, indent=4))
            await loop.run_in_executor(None, self.group_model_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存群组模型文件时发生错误: {e}", exc_info=True)

    async def _load_generation_history(self):
        """加载生图时间线记录列表（每条 {timestamp, group_id, user_id, prompt, model, file_path}）"""
        if not self.generation_history_file.exists(): self.generation_history = []; return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.generation_history_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, list): self.generation_history = data
        except Exception as e:
            logger.error(f"加载生图时间线文件时发生错误: {e}", exc_info=True);
            self.generation_history = []

    async def _save_generation_history(self):
        """保存生图时间线记录列表"""
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None, functools.partial(json.dumps, self.generation_history,
                                                                           ensure_ascii=False, indent=4))
            await loop.run_in_executor(None, self.generation_history_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存生图时间线文件时发生错误: {e}", exc_info=True)

    async def _append_generation_history(self, record: Dict[str, Any]) -> None:
        """向生图时间线追加一条记录，超过 500 条时删除最旧的。"""
        # 加锁防止多群/并发生图时 read-modify-write 丢记录
        async with self._history_lock:
            self.generation_history.append(record)
            if len(self.generation_history) > 500:
                self.generation_history = self.generation_history[-500:]
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
        current_count = self._get_user_count(target_qq)
        new_count = current_count + count
        # 如果设置了上限，截断到上限
        if self.user_count_cap > 0 and new_count > self.user_count_cap:
            new_count = self.user_count_cap
        self.user_counts[str(target_qq)] = new_count
        await self._save_user_counts()
        cap_info = f"（上限 {self.user_count_cap}）" if self.user_count_cap > 0 else ""
        yield event.plain_result(f"✅ 已为用户 {target_qq} 增加次数，TA当前剩余 {new_count}{cap_info} 次。")

    @filter.command("生图增加群组次数", prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        match = re.search(r"(\d+)\s+(\d+)", event.message_str.strip())
        if not match:
            yield event.plain_result('格式错误: #生图增加群组次数 <群号> <次数>')
            return
        target_group, count = match.group(1), int(match.group(2))
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
        await self.conf.set("api_keys", api_keys)
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
            await self.conf.set("api_keys", [])
            yield event.plain_result(f"✅ 已删除全部 {len(api_keys)} 个 Key。")
        elif param.isdigit() and 1 <= int(param) <= len(api_keys):
            removed_key = api_keys.pop(int(param) - 1)
            await self.conf.set("api_keys", api_keys)
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
                f"[{idx}] {m}" + (" ← 当前" if m == current else "")
                for idx, m in enumerate(self.available_models, 1)
            )
            yield event.plain_result(f"📋 本群可用生图模型：\n{lines}\n回复序号或输入 `>>切换模型 <序号>` 切换")
            return
        if param.isdigit():
            idx = int(param)
            if 1 <= idx <= len(self.available_models):
                model = self.available_models[idx - 1]
                self.group_models[group_id] = model
                await self._save_group_models()
                logger.info(f"[NewImage] 群 {group_id} 的模型已切换为: {model}")
                yield event.plain_result(f"✅ 本群生图模型已切换为: {model}")
                return
            yield event.plain_result(f"❌ 序号超出范围（1-{len(self.available_models)}），请重新输入。")
            return
        yield event.plain_result("格式: >>切换模型 <序号>")

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
                before = len(self.generation_history)
                self.generation_history = [r for r in self.generation_history if r.get("timestamp") != ts]
                removed = before - len(self.generation_history)
                if removed:
                    await self._save_generation_history()
                    yield event.plain_result(f"✅ 已删除 {removed} 条时间戳为 {ts} 的生图记录。")
                else:
                    yield event.plain_result(f"❌ 未找到时间戳为 {ts} 的生图记录。")
                return
            if len(parts) == 4:
                scope_key, start_ts, end_ts = parts[1], parts[2], parts[3]
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
                count = max(1, min(int(args[0]), 20))
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
            prompt_short = str(r.get("prompt", ""))[:15]
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

    if hasattr(filter, "llm_tool"):
        @filter.llm_tool(name="generate_image")
        async def generate_image(self, event: AstrMessageEvent, prompt: str):
            """根据用户的绘画需求生成一张图片。调用前，必须先把用户的绘画需求扩写完善为详细、高质量、适合 AI 绘画的完整英文提示词（包含主体、风格、构图、光线、细节等要素），再作为 prompt 参数传入。只有在用户明确要求生成或绘制图片时才调用本工具。

            Args:
                prompt(string): 经扩写完善的完整绘画提示词（英文，包含主体、风格、构图、光线、细节等）
            """
            # 次数限制与手动命令一致：管理员豁免、群组优先、成功才扣减
            # （工具由主对话 LLM 自主调用，不查黑白名单、不占群槽位）
            # 注意：llm_tool 工具是纯协程（函数体内不能有 yield），发消息给用户用 event.send，return 文本回传 LLM
            sender_id = event.get_sender_id()
            group_id = event.get_group_id()
            if not self.is_global_admin(event):
                err = self._check_usage_quota(sender_id, group_id)
                if err:
                    # 先发用户（立刻看到原因），再 return 文本回传 LLM 让它知晓
                    try:
                        await event.send(MessageChain(chain=[Plain(err)]))
                    except Exception as e:
                        logger.error(f"[NewImage] 发送生图次数不足提示失败: {e}", exc_info=True)
                    return f"未执行生图：该用户生图次数已用完（{err}），请勿让用户再请求生图。"
            res, used_model = await self._call_api([], prompt, image_size=None, group_id=group_id)
            if isinstance(res, bytes):
                if not self.is_global_admin(event):
                    await self._consume_quota(sender_id, group_id)
                # 落盘 + 记录时间线
                try:
                    await self._save_generation(group_id, sender_id, prompt, used_model, res)
                except Exception as e:
                    logger.error(f"[NewImage] 保存生图记录失败: {e}", exc_info=True)
                # 剩余次数：优先显示个人剩余；若未开启用户限制则显示群剩余
                if self.conf.get("enable_user_limit", True):
                    remaining = self._get_user_count(sender_id)
                else:
                    remaining = self._get_group_count(group_id) if group_id else 0
                # 先发图给用户，再 return 文本回传 LLM 告知剩余次数
                try:
                    await event.send(MessageChain(chain=[Image.fromBytes(res), Plain(f"✅ 已生成 | 模型: {used_model}")]))
                except Exception as e:
                    logger.error(f"[NewImage] 发送生图结果失败: {e}", exc_info=True)
                return f"图片已生成完毕，且已自动发送到当前会话（工具已自动发送图片，请勿再重复发送图片）。该用户当前剩余次数: {remaining}。"
            else:
                try:
                    await event.send(MessageChain(chain=[Plain(f"❌ 生成失败: {res}")]))
                except Exception as e:
                    logger.error(f"[NewImage] 发送生图失败提示失败: {e}", exc_info=True)
                return f"生图失败：{res}"

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
                limit = max(1, min(int(limit) if limit else 10, 20))
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
                prompt_short = str(r.get("prompt", ""))[:15]
                lines.append(f"{r.get('timestamp','?')} | {r.get('group_id','') or 'private'} | {prompt_short} | "
                             f"{r.get('file_path','?')}")
            return "生图记录如下：\n" + "\n".join(lines)

    async def _get_api_key(self) -> str | None:
        keys = self.conf.get("api_keys", [])
        if not keys: return None
        async with self.key_lock:
            key = keys[self.key_index]
            self.key_index = (self.key_index + 1) % len(keys)
            return key

    def _extract_image_size(self, text: str, sender_id: str) -> tuple:
        """
        从用户输入中提取分辨率参数（仅白名单用户生效）。
        
        返回: (cleaned_text, image_size)
            - cleaned_text: 移除了分辨率关键词后的文本
            - image_size: 提取到的分辨率（如 "2K", "4K"）或 None
        """
        if not self.is_gemini_api:
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


    async def _extract_image_from_markdown(self, text: str) -> bytes | None:
        if not text or not self.iwf:
            return None
        match = re.search(r"!\[[^\]]*\]\((https?://[^\s)]+)\)", text)
        if match:
            url = match.group(1).strip()
            try:
                downloaded = await self.iwf._download_image(url)
                if downloaded:
                    return downloaded
            except Exception as e:
                logger.warning(f"下载 Markdown 图片失败: {e}", exc_info=True)
        return None

    async def _extract_image_bytes_from_response(self, data: Dict[str, Any]) -> bytes | str | None:
        """
        从 OpenAI / OpenRouter 风格的响应中提取图像数据。
        兼容多种可能的返回结构，包括：
            - images 直接列表
            - chat.completions 的 choices[].message.images
            - chat.completions 的 choices[].message.content 内嵌
            - data[].url / data[].b64_json
        
        返回值:
            - bytes: 成功下载的图片数据
            - str: 如果是以 "FALLBACK_URL:" 开头的字符串，表示下载失败但有可用URL
            - None: 完全没有找到图像数据
        """
        found_urls: List[str] = []  # 记录所有发现的URL，用于fallback
        
        async def try_download_or_record(url: str) -> bytes | None:
            """尝试下载URL，如果失败则记录URL用于fallback"""
            if url.startswith("data:image/"):
                try:
                    return base64.b64decode(url.split(",", 1)[1])
                except Exception:
                    return None
            if self.iwf:
                downloaded = await self.iwf._download_image(url)
                if downloaded:
                    return downloaded
                # 下载失败，记录URL
                found_urls.append(url)
            return None
        
        try:
            # 1. OpenAI Images API 风格 {"data": [{"url": "..."}]} 或 {"data": [{"b64_json": "..."}]}
            if isinstance(data.get("data"), list):
                for item in data["data"]:
                    if isinstance(item, dict):
                        if url := item.get("url"):
                            result = await try_download_or_record(url)
                            if result:
                                return result
                        if b64 := item.get("b64_json"):
                            return base64.b64decode(b64)

            # 2. 旧格式 {"images": [{"url": "..."}]}
            if isinstance(data.get("images"), list):
                for image in data["images"]:
                    if not isinstance(image, dict):
                        continue
                    url = image.get("url")
                    if url:
                        result = await try_download_or_record(url)
                        if result:
                            return result

            # 3. Chat Completions 风格
            choices = data.get("choices") or []
            if choices:
                message = choices[0].get("message", {})

                # 3.1 message.images 显式结构
                if isinstance(message.get("images"), list):
                    for image in message["images"]:
                        if not isinstance(image, dict):
                            continue
                        url = image.get("image_url", {}).get("url") or image.get("url")
                        if url:
                            result = await try_download_or_record(url)
                            if result:
                                return result

                content = message.get("content")

                # 3.2 content 为列表（OpenAI 新版多模态结构）
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue

                        item_type = item.get("type")
                        if item_type in {"output_image", "image_url", "image"}:
                            if isinstance(item.get("image_url"), dict):
                                url = item["image_url"].get("url")
                                if url:
                                    result = await try_download_or_record(url)
                                    if result:
                                        return result
                            if url := item.get("url"):
                                result = await try_download_or_record(url)
                                if result:
                                    return result
                            if b64 := item.get("b64_json"):
                                return base64.b64decode(b64)

                        # 兼容部分模型直接返回 base64 文本
                        if item_type in {"text", "output_text"} and isinstance(item.get("text"), str):
                            text_content = item["text"]
                            matches = re.findall(r"data:image/([^;]+);base64,([A-Za-z0-9+/=]+)", text_content)
                            if matches:
                                return base64.b64decode(matches[0][1])
                            markdown_img = await self._extract_image_from_markdown(text_content)
                            if markdown_img:
                                return markdown_img

                # 3.3 content 为字符串，尝试匹配其中的 base64 或 Markdown 图片
                if isinstance(content, str):
                    matches = re.findall(r"data:image/([^;]+);base64,([A-Za-z0-9+/=]+)", content)
                    if matches:
                        return base64.b64decode(matches[0][1])
                    markdown_img = await self._extract_image_from_markdown(content)
                    if markdown_img:
                        return markdown_img
                    # 如果Markdown图片下载失败，尝试提取URL作为fallback
                    md_match = re.search(r"!\[[^\]]*\]\((https?://[^\s)]+)\)", content)
                    if md_match:
                        found_urls.append(md_match.group(1).strip())

            # 如果有发现URL但下载都失败了，返回第一个URL作为fallback
            if found_urls:
                logger.warning(f"图片下载失败，但找到了可用URL: {found_urls[0]}")
                return f"FALLBACK_URL:{found_urls[0]}"
            
            logger.warning(f"未能在响应中提取图像数据，原始响应(截断): {str(data)[:200]}")
            return None
        except Exception as e:
            logger.error(f"解析图像响应时出现错误: {e}", exc_info=True)
            # 即使出错，如果之前记录了URL，也返回它
            if found_urls:
                return f"FALLBACK_URL:{found_urls[0]}"
            return None

    async def _call_api(self, image_bytes_list: List[bytes], prompt: str, image_size: Optional[str] = None,
                        group_id: Optional[str] = None) -> tuple:
        """调用 API 生成图像，支持 Gemini 原生 API 和 OpenAI 兼容格式双路由。

        返回 (result, model_name) 二元组：
            - result: bytes=生图成功 / str=失败原因
            - model_name: 本次实际使用的模型名（错误早退路径可能为 ""）
        """
        
        # ========== 公共：确定 API URL、Key 和 模型 ==========
        api_url: str = ""
        api_key: str = ""
        model_name: str = ""
        
        if self.provider_id:
            self.provider = self.context.get_provider_by_id(self.provider_id)
            if not self.provider:
                logger.warning(f"[NewImage] 未找到提供商 '{self.provider_id}'，将尝试手动配置")
            if self.provider:
                try:
                    config = self.provider.provider_config
                    api_url = config.get("api_base", "")
                    keys = self.provider.get_keys()
                    if keys:
                        api_key = keys[0]
                    model_name = self.provider.get_model()
                    if api_url:
                        logger.info(f"[NewImage] 使用提供商 '{self.provider_id}'")
                        logger.debug(f"  API URL: {api_url[:50]}...")
                        logger.debug(f"  Model: {model_name}")
                except Exception as e:
                    logger.warning(f"从提供商获取配置失败: {e}，将尝试使用手动配置")
                    api_url = api_key = model_name = ""
        
        if not api_url:
            api_url_raw = (self.conf.get("api_url") or "").strip()
            if not api_url_raw:
                return "❌ 未选择提供商，且未配置手动 API URL", ""
            api_url = api_url_raw
            logger.info(f"[NewImage] 使用手动配置 API URL")
        
        if not api_key:
            api_key = await self._get_api_key()
            if not api_key:
                return "❌ 未选择提供商，且未配置手动 API Key", ""
        
        # ========== 模型选择优先级：本群配置模型 > provider 模型 > 手动 available_models[0] 兜底 ==========
        if group_id and self.group_models.get(group_id):
            # 1. 本群通过「切换模型」指令配置的模型（最高优先）
            model_name = self.group_models[group_id]
            logger.info(f"[NewImage] 使用本群 {group_id} 配置的模型: {model_name}")
        elif not model_name:
            # 2. provider 未提供模型（或未使用 provider）时，回退到手动配置
            if self.available_models:
                # 2a. 手动配置的默认模型（model 配置中第一个）
                model_name = self.available_models[0]
                logger.info(f"[NewImage] 使用手动配置的默认模型: {model_name}")
            else:
                # 2b. 兜底 conf.model（兼容初始化未完成等场景）
                model_name = (self.conf.get("model", "") or "").strip()
                if not model_name:
                    return "❌ 未选择提供商，且未配置手动模型名称", ""

        source_info = f"提供商:{self.provider_id}" if (self.provider_id and self.provider) else "手动配置"
        
        # ========== 根据开关选择路由 ==========
        if self.is_gemini_api:
            return await self._call_gemini_api(api_url, api_key, model_name, image_bytes_list, prompt, image_size, source_info), model_name
        else:
            return await self._call_openai_api(api_url, api_key, model_name, image_bytes_list, prompt, source_info), model_name

    async def _call_gemini_api(self, api_url: str, api_key: str, model_name: str,
                                image_bytes_list: List[bytes], prompt: str,
                                image_size: Optional[str], source_info: str) -> bytes | str:
        """Gemini 原生 API 路径（generateContent）"""
        
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
        
        # 确定最终使用的 imageSize：用户指定 > 全局默认
        final_image_size = image_size or self.default_image_size
        if final_image_size:
            image_config: Dict[str, Any] = {"imageSize": final_image_size}
            request_body["generationConfig"]["imageConfig"] = image_config
            logger.info(f"[NewImage/Gemini] 使用分辨率: {final_image_size}")
        
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
                    logger.error(f"Gemini API 请求失败: HTTP {resp.status}, 响应: {error_text}")
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
                                image_bytes_list: List[bytes], prompt: str, source_info: str) -> bytes | str:
        """OpenAI 兼容 API 路径（/v1/chat/completions）"""
        
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
                    logger.error(f"API 请求失败: HTTP {resp.status}, 响应: {error_text}")
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

                result = await self._extract_image_bytes_from_response(data)

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

    async def terminate(self):
        if self.iwf: await self.iwf.terminate()
        logger.info("[FigurinePro] 插件已终止")
