# -*- coding: utf-8 -*-
"""handlers/llm_tools.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class LLMToolsMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    if hasattr(filter, "llm_tool"):
        @filter.llm_tool(name="generate_image")
        async def generate_image(self, event: AstrMessageEvent, prompt: str = "", model: Any = _PARAM_NOT_SET,
                                 params: Any = _PARAM_NOT_SET, selfie: Any = _PARAM_NOT_SET,
                                 nsfw: Any = _PARAM_NOT_SET, artist_preset: Any = _PARAM_NOT_SET):
            """根据用户的绘画需求生成一张图片。调用前，必须先把用户的绘画需求扩写完善为详细、高质量、适合 AI 绘画的完整英文提示词（包含主体、风格、构图、光线、细节等要素），再作为 prompt 参数传入。只有在用户明确要求生成或绘制图片时才调用本工具。

            注意：若用户在当前消息（或引用/刚发送的消息）中附带图片，本工具会自动提取该图片作为参考进行「图+文生图」；若纯文本描述则进行「文生图」。
            注意：若用户要求绘制「你（AI 自己）」的自拍或形象（例如「画你自己」「你的自拍」「画你的样子」等），请将 selfie 参数设为 true，本工具会自动在提示词前追加预设的 AI 人物外貌特征（发色/瞳色/气质等），使形象保持一致。
            注意：若用户指定或希望采用某个特定画师风格（例如通过 query_presets 查到的画师串），请将 artist_preset 参数设为对应的画师预设名，系统会自动将该画师串及其负面词并入生成参数。若不使用画师串，请传入空字符串 ""。

            Args:
                prompt(string): 经扩写完善的完整绘画提示词（必须为纯英文Tag，包含主体、风格、构图、光线、细节等）
                model(string): 生图模型名
                params(object): 生图参数对象（包含生成尺寸size、步数steps、CFG引导强度scale/温度、负面词negative_prompt等）
                selfie(boolean): 自拍模式布尔值，是否为画AI助手自身形象
                nsfw(boolean): NSFW加密模式布尔值，是否为成人内容
                artist_preset(string): 指定使用的画师串预设名称（不使用可传空字符串）
            """
            prompt = (prompt or "").strip()
            force_full = bool(self.conf.get("force_full_params", False))
            force_en = bool(self.conf.get("force_english_tags", False))
            reject_errors: List[str] = []

            # 1. 强制英文 Tag 检测
            if force_en:
                if not prompt:
                    reject_errors.append("prompt: 提示词不能为空")
                elif re.search(r'[\u4e00-\u9fa5]', prompt):
                    reject_errors.append("prompt: 检测到中文字符，AI 绘画必须使用纯英文 Tag，请将描述翻译扩写为英文提示词后重新调用")

            # 2. 强制参数完整检测（智力矫正模式）
            if force_full:
                if not prompt and not force_en:
                    reject_errors.append("prompt: 未提供绘画提示词")
                if model is _PARAM_NOT_SET or not isinstance(model, str) or not model.strip():
                    reject_errors.append("model: 未指定生图模型名（必须显式指定模型名，不知道请先调用 query_generation_models 查询）")
                if params is _PARAM_NOT_SET or not isinstance(params, dict) or not params:
                    reject_errors.append("params: 未提供生成参数对象（必须显式指定包含 size、steps、scale 等参数的对象）")
                if selfie is _PARAM_NOT_SET or not isinstance(selfie, bool):
                    reject_errors.append("selfie: 未指定自拍模式布尔值（必须显式传入 true 或 false）")
                if nsfw is _PARAM_NOT_SET or not isinstance(nsfw, bool):
                    reject_errors.append("nsfw: 未指定 NSFW 模式布尔值（必须显式传入 true 或 false）")
                if artist_preset is _PARAM_NOT_SET or not isinstance(artist_preset, str):
                    reject_errors.append("artist_preset: 未指定画师串预设名称（不使用请显式传入空字符串 \"\"，可先调用 query_presets 查询）")

            # 若有参数不合格，按模板严厉打回
            if reject_errors:
                template = str(self.conf.get("correction_reject_template", "") or "").strip()
                if not template:
                    template = "你是傻逼吗，参数都不填全。再不会填别吃token了。\n以下参数错误：{errors}"
                err_text = "；\n- ".join(reject_errors)
                reject_msg = template.replace("{errors}", f"\n- {err_text}")
                logger.warning(f"[NewImage/智力矫正] 打回模型不合格调用:\n{reject_msg}")
                return reject_msg

            # 非强制模式下的缺省值回退
            final_model_param = model if (model is not _PARAM_NOT_SET and isinstance(model, str)) else ""
            final_params_param = params if (params is not _PARAM_NOT_SET and isinstance(params, dict)) else None
            final_selfie_param = bool(selfie) if selfie is not _PARAM_NOT_SET else False
            final_nsfw_param = bool(nsfw) if nsfw is not _PARAM_NOT_SET else False
            final_artist_preset_param = str(artist_preset).strip() if (artist_preset is not _PARAM_NOT_SET and isinstance(artist_preset, str)) else ""

            if not prompt:
                # 容错：如果非强制模式下模型漏传 prompt，尝试从用户原始文本中提取
                raw_text = event.message_str.strip()
                clean_text = re.sub(r'^(帮我|请|给我)?(画|生成|绘制|画一张|画个|来张)', '', raw_text).strip()
                if clean_text:
                    prompt = clean_text
                else:
                    return "生图失败：未提供绘画提示词 (prompt 不能为空)，请补充具体的绘画描述。"

            # 次数限制与手动命令一致：管理员豁免、群组优先、成功才扣减
            # （工具由主对话 LLM 自主调用，不查黑白名单、不占群槽位）
            # 注意：llm_tool 工具是纯协程（函数体内不能有 yield），发消息给用户用 event.send，return 文本回传 LLM
            sender_id = event.get_sender_id()
            group_id = event.get_group_id()
            # 尝试从用户当前消息中提取图片（引用回复链/直接 Image 组件），无图则纯文生图
            image_bytes_list = await self._extract_event_images(event)
            # 确定最终模型：用户指定 > 本群配置模型 > 手动配置默认模型 > conf.model 兜底
            final_model = final_model_param.strip()
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
            # 自拍模式：在提示词前追加可配置的固定人物外貌特征（AI 画自己的自拍用，保证形象一致）
            if final_selfie_param:
                feature = str(self.conf.get("selfie_prompt", "") or "").strip()
                if feature:
                    prompt = f"{feature}, {prompt}"
            # 异步生图：立即返回给 LLM（不阻塞其回复），实际生成放后台任务，完成后自动发送到会话
            # 对 LLM 传入的 params 做温和清洗（夹取/丢弃），再传入后台任务
            cleaned_params, _ = self._sanitize_params(final_params_param, "LLM工具") if final_params_param else ({}, [])
            task = asyncio.create_task(self._async_generate(event, prompt, model=final_model, quota=quota,
                                                            params=cleaned_params,
                                                            image_bytes_list=image_bytes_list, nsfw=final_nsfw_param,
                                                            artist_preset=final_artist_preset_param))
            # 保存后台任务引用（防 GC），结束后自动从集合移除；terminate 时统一回收
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return "已开始生成图片，生成完成后会自动发送到当前会话，请稍候。"

        async def _async_generate(self, event: AstrMessageEvent, prompt: str, model: str = "", quota: int = 1,
                                  params: Optional[Dict] = None,
                                  image_bytes_list: Optional[List[bytes]] = None,
                                  nsfw: bool = False,
                                  artist_preset: str = "") -> None:
            """后台异步生图：完成后主动发送图片/失败提示到会话（不阻塞 LLM 回复）。

            model: 指定生图模型（可空，空则 _call_api 内部走默认选择逻辑）；
            quota: 本次生成消耗的次数（与 _check_usage_quota / _consume_quota 的 quota 语义一致）；
            params: 生成参数（已在 generate_image 清洗），在 _call_api 内与模型默认参数合并；
            image_bytes_list: 用户当前消息中提取的参考图片（图+文生图用），None/空列表则纯文生图。
            nsfw: NSFW 加密模式——为 True 时生图结果打包成密码加密 ZIP 发送（不直接发图），默认 False。
            artist_preset: 指定的画师串预设名称（优先级高于群级配置）。
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
                                                       model_override=model or None, gen_params=params or None,
                                                       artist_preset_override=artist_preset or None)
                if isinstance(res, bytes):
                    # 记录实际扣减来源（群/个人），剩余次数展示与扣减来源一致
                    consumed_from = ""
                    if not self.is_global_admin(event):
                        consumed_from = await self._consume_quota(sender_id, group_id, count=quota)
                    file_path = ""
                    try:
                        file_path = await self._save_generation(group_id, sender_id, prompt, used_model, res)
                    except Exception as e:
                        logger.error(f"[NewImage] 保存生图记录失败: {e}", exc_info=True)
                    # 剩余次数：优先显示个人剩余；若未开启用户限制则显示群剩余
                    if consumed_from == "group":
                        remaining = self._get_group_count(group_id) if group_id else 0
                    else:
                        remaining = self._get_user_count(sender_id)
                    if nsfw and self._nsfw_zip_effective(group_id):
                        # NSFW 加密模式（双层开关生效时）：不直接发图，打包成密码加密 ZIP 发送（原始图仍已落盘时间线）
                        await self._send_nsfw_zip(event, res, used_model, remaining)
                    else:
                        # nsfw 被全局/按群开关关闭，或非 NSFW：走普通 Image 直发路径
                        try:
                            caption = f"✅ 已生成 | 模型: {used_model} | 剩余: {remaining}次"
                            send_res = await self._send_image_with_recall(event, res, caption)
                            if send_res == "USE_CHAIN_RESULT":
                                await event.send(MessageChain(chain=[Image.fromBytes(res), Plain(caption)]))
                            elif send_res:
                                await event.send(MessageChain(chain=[Plain(f"❌ 发送失败: {send_res}")]))
                        except Exception as e:
                            logger.error(f"[NewImage] 发送生图结果失败: {e}", exc_info=True)
                    # 结果回传主 LLM：下一轮对话可看到图片路径并引用（NSFW ZIP 时指落盘的原始图路径）
                    await self._notify_llm_result(event, f"📎 生图完成 | 模型: {used_model} | 图片路径: {file_path}")
                else:
                    try:
                        await event.send(MessageChain(chain=[Plain(f"❌ 生成失败: {res}")]))
                    except Exception as e:
                        logger.error(f"[NewImage] 发送生图失败提示失败: {e}", exc_info=True)
                    # 结果回传主 LLM：下一轮对话可看到失败原因
                    await self._notify_llm_result(event, f"❌ 生图失败: {res}")
            finally:
                if slot_acquired:
                    await self._release_group_slot(group_id)

        @filter.llm_tool(name="query_generation_quota")
        async def query_generation_quota(self, event: AstrMessageEvent, user_id: Any = _PARAM_NOT_SET):
            """查询用户的生图次数配额（个人剩余次数与当前群剩余次数）。注意本工具不执行生图，仅查询。

            Args:
                user_id(string): 要查询的QQ号，查自己可填当前用户QQ或空串
            """
            force_full = bool(self.conf.get("force_full_params", False))
            if force_full and user_id is _PARAM_NOT_SET:
                template = str(self.conf.get("correction_reject_template", "") or "").strip()
                if not template:
                    template = "你是傻逼吗，参数都不填全。再不会填别吃token了。\n以下参数错误：{errors}"
                return template.replace("{errors}", "\n- user_id: 必须显式指定要查询的 QQ 号或当前用户 QQ")
            target_str = str(user_id or "").strip() if (user_id is not _PARAM_NOT_SET and user_id) else event.get_sender_id()
            target = target_str if target_str else event.get_sender_id()
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
        async def query_generation_history(self, event: AstrMessageEvent, scope: Any = _PARAM_NOT_SET,
                                           days: Any = _PARAM_NOT_SET, limit: Any = _PARAM_NOT_SET):
            """查询生图历史记录（每条包含时间戳、群号、提示词摘要、文件路径）。可用于回复用户「之前生过什么图」等。本工具不执行生图，仅查询。

            Args:
                scope(string): 查询范围（本群 或 全局）
                days(number): 最近多少天内的记录（0为不限天数）
                limit(number): 最多返回条数（最大20条）
            """
            force_full = bool(self.conf.get("force_full_params", False))
            if force_full:
                errors = []
                if scope is _PARAM_NOT_SET or not scope:
                    errors.append("scope: 未指定查询范围（必须为 本群 或 全局）")
                if days is _PARAM_NOT_SET:
                    errors.append("days: 未指定查询天数数字（0为不限天数）")
                if limit is _PARAM_NOT_SET:
                    errors.append("limit: 未指定返回条数数字")
                if errors:
                    template = str(self.conf.get("correction_reject_template", "") or "").strip()
                    if not template:
                        template = "你是傻逼吗，参数都不填全。再不会填别吃token了。\n以下参数错误：{errors}"
                    return template.replace("{errors}", "\n- " + "\n- ".join(errors))

            scope_val = str(scope or "本群").strip() if scope is not _PARAM_NOT_SET else "本群"
            try:
                limit_int = int(limit) if (limit is not _PARAM_NOT_SET and limit is not None) else 10
                limit_val = max(1, min(limit_int, LOG_QUERY_LIMIT_MAX))
            except (TypeError, ValueError):
                limit_val = 10
            records = list(self.generation_history)
            records = list(reversed(records))
            if scope_val != "全局":
                group_id = event.get_group_id()
                if group_id:
                    records = [r for r in records if str(r.get("group_id", "")) == str(group_id)]
            days_int = 0
            if days is not _PARAM_NOT_SET and days is not None:
                try:
                    days_int = max(0, int(days))
                except (TypeError, ValueError):
                    days_int = 0
            if days_int:
                cutoff = datetime.now() - timedelta(days=days_int)
                filtered = []
                for r in records:
                    try:
                        if datetime.strptime(str(r.get("timestamp", "")), "%Y%m%d_%H%M%S_%f") >= cutoff:
                            filtered.append(r)
                    except ValueError:
                        continue
                records = filtered
            records = records[:limit_val]
            if not records:
                return "暂无生图记录"
            lines = []
            for r in records:
                prompt_short = str(r.get("prompt", ""))[:PROMPT_SHORT_LEN]
                lines.append(f"{r.get('timestamp','?')} | {r.get('group_id','') or 'private'} | {prompt_short} | "
                             f"{r.get('file_path','?')}")
            return "生图记录如下：\n" + "\n".join(lines)

        @filter.llm_tool(name="query_generation_image")
        async def query_generation_image(self, event: AstrMessageEvent, file_path: Any = _PARAM_NOT_SET):
            """查询生图历史记录对应的原图，并将图片直接发送给用户。用户问「之前生成的那张图」「历史图片」等时可调用。本工具不执行生图，仅查询并发送图片。

            Args:
                file_path(string): 生图记录中的图片文件路径
            """
            force_full = bool(self.conf.get("force_full_params", False))
            if force_full and (file_path is _PARAM_NOT_SET or not file_path or not str(file_path).strip()):
                template = str(self.conf.get("correction_reject_template", "") or "").strip()
                if not template:
                    template = "你是傻逼吗，参数都不填全。再不会填别吃token了。\n以下参数错误：{errors}"
                return template.replace("{errors}", "\n- file_path: 未指定图片文件路径（必须从 query_generation_history 结果中提取）")
            file_path_str = str(file_path or "").strip() if file_path is not _PARAM_NOT_SET else ""
            resolved, _, err_reason = self._resolve_generation_file(file_path_str)
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
        async def query_generation_stats(self, event: AstrMessageEvent, scope: Any = _PARAM_NOT_SET,
                                         days: Any = _PARAM_NOT_SET):
            """统计生图使用情况：总张数、按模型分组、按用户分组。用户问「最近生成了多少图」「使用统计」等时可调用。本工具不执行生图，仅统计。

            Args:
                scope(string): 统计范围（本群 或 全局）
                days(number): 最近多少天
            """
            force_full = bool(self.conf.get("force_full_params", False))
            if force_full:
                errors = []
                if scope is _PARAM_NOT_SET or not scope:
                    errors.append("scope: 未指定统计范围（必须为 本群 或 全局）")
                if days is _PARAM_NOT_SET:
                    errors.append("days: 未指定统计天数数字")
                if errors:
                    template = str(self.conf.get("correction_reject_template", "") or "").strip()
                    if not template:
                        template = "你是傻逼吗，参数都不填全。再不会填别吃token了。\n以下参数错误：{errors}"
                    return template.replace("{errors}", "\n- " + "\n- ".join(errors))

            scope_val = str(scope or "本群").strip() if scope is not _PARAM_NOT_SET else "本群"
            try:
                days_val = max(1, int(days)) if (days is not _PARAM_NOT_SET and days is not None) else 7
            except (TypeError, ValueError):
                days_val = 7
            records = list(self.generation_history)
            if scope_val != "全局":
                group_id = event.get_group_id()
                if group_id:
                    records = [r for r in records if str(r.get("group_id", "")) == str(group_id)]
            cutoff = datetime.now() - timedelta(days=days_val)
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
                model_n = str(r.get("model", "") or "未知")
                by_model[model_n] = by_model.get(model_n, 0) + 1
                user_id_val = str(r.get("user_id", "") or "未知")
                by_user[user_id_val] = by_user.get(user_id_val, 0) + 1
            model_parts = [f"{m} {c}张" for m, c in sorted(by_model.items(), key=lambda x: -x[1])]
            user_parts = [f"{u} {c}张" for u, c in sorted(by_user.items(), key=lambda x: -x[1])[:5]]
            scope_txt = "全局" if scope_val == "全局" else "本群"
            return (f"近 {days_val} 天（{scope_txt}）共生成 {total} 张图片。\n"
                    f"按模型：{'，'.join(model_parts)}\n"
                    f"按用户（Top5）：{'，'.join(user_parts)}")

        @filter.llm_tool(name="query_presets")
        async def query_presets(self, event: AstrMessageEvent, preset_name: Any = _PARAM_NOT_SET,
                                category: Any = _PARAM_NOT_SET):
            """查询生图预设（风格指令与画师串预设）列表或指定预设的详细内容。用户问「有哪些预设/画风」「有哪些画师串/画师预设」「某个画师串的内容」时调用。本工具不执行生图，仅查询。查询结果中获取到的画师串预设名可直接用于 generate_image 的 artist_preset 参数。

            Args:
                preset_name(string): 预设名称或关键词（查全部可填空串或 all）
                category(string): 预设类别过滤，可选值：all（查全部）、artist（仅画师串预设）、style（仅普通风格预设）
            """
            force_full = bool(self.conf.get("force_full_params", False))
            if force_full:
                reject_errors: List[str] = []
                if preset_name is _PARAM_NOT_SET or not isinstance(preset_name, str):
                    reject_errors.append("preset_name: 未指定预设名称参数（查询全部请传空字符串 \"\" 或 \"all\"）")
                if category is _PARAM_NOT_SET or not isinstance(category, str):
                    reject_errors.append("category: 未指定预设类别参数（必须显式传入 \"all\"、\"artist\" 或 \"style\"）")
                if reject_errors:
                    template = str(self.conf.get("correction_reject_template", "") or "").strip()
                    if not template:
                        template = "你是傻逼吗，参数都不填全。再不会填别吃token了。\n以下参数错误：{errors}"
                    err_text = "；\n- ".join(reject_errors)
                    return template.replace("{errors}", f"\n- {err_text}")

            keyword = str(preset_name or "").strip() if preset_name is not _PARAM_NOT_SET else ""
            cat = str(category or "all").strip().lower() if category is not _PARAM_NOT_SET else "all"
            if cat not in ("all", "artist", "style"):
                cat = "all"

            # 1. 查询指定预设详情（keyword 非空且不是 "all"）
            if keyword and keyword.lower() != "all":
                # 优先查画师串预设
                if cat in ("all", "artist"):
                    target_art = next((p for p in self.artist_presets if p.get("name") == keyword), None)
                    if not target_art:
                        target_art = next((p for p in self.artist_presets if keyword.lower() in p.get("name", "").lower()), None)
                    if target_art:
                        art_name = target_art.get("name", "")
                        artists = target_art.get("artists", "")
                        neg = target_art.get("negative", "")
                        return (f"🎨 【画师串预设：{art_name}】\n"
                                f"- 画师标签 (artists)：{artists or '无'}\n"
                                f"- 专属负面词 (negative)：{neg or '无'}\n"
                                f"- 💡 生图指引：在 generate_image 中传入 artist_preset=\"{art_name}\" 即可单次直接套用该画师风格！")

                # 其次查普通风格预设 (prompt_map)
                if cat in ("all", "style"):
                    if keyword in self.prompt_map:
                        return (f"🖌️ 【风格预设：{keyword}】\n"
                                f"提示词内容：\n{self.prompt_map[keyword]}")

                # 未找到具体预设
                avail_artists = [p.get("name", "") for p in self.artist_presets]
                avail_styles = list(self.prompt_map.keys())
                err_parts = [f"未找到预设 [{keyword}]。"]
                if cat in ("all", "artist") and avail_artists:
                    err_parts.append(f"可用画师串预设：{'、'.join(avail_artists)}")
                if cat in ("all", "style") and avail_styles:
                    err_parts.append(f"可用风格预设：{'、'.join(avail_styles)}")
                return "\n".join(err_parts)

            # 2. 查询预设列表（keyword 为空或 "all"）
            group_id = event.get_group_id()
            current_group_artist = self.group_artists.get(str(group_id), "") if group_id else ""

            output_sections = []

            # 画师串预设列表
            if cat in ("all", "artist"):
                if self.artist_presets:
                    art_lines = []
                    for p in self.artist_presets:
                        pname = p.get("name", "")
                        is_cur = " (本群当前激活)" if pname == current_group_artist else ""
                        neg_tip = f" [专属负面]" if p.get("negative") else ""
                        art_lines.append(f"- {pname}{is_cur}{neg_tip}")
                    cur_tip = f"\n当前群激活画师串: {current_group_artist}" if (current_group_artist and current_group_artist.lower() != "off") else "\n当前群未启用固定画师串"
                    output_sections.append(
                        f"🎨 可用画师串预设（{len(self.artist_presets)} 个）：\n"
                        + "\n".join(art_lines)
                        + cur_tip
                        + "\n💡 提示：在 generate_image(artist_preset=\"预设名\") 中传入即可在单次生图中套用该画师风格！"
                    )
                else:
                    output_sections.append("🎨 画师串预设：当前未配置任何画师串预设（可在 WebUI 或通过指令导入配置）。")

            # 普通风格预设列表
            if cat in ("all", "style"):
                if self.prompt_map:
                    styles = list(self.prompt_map.keys())
                    output_sections.append(
                        f"🖌️ 可用风格提示词预设（{len(styles)} 个）：\n"
                        + "、".join(styles)
                    )
                else:
                    output_sections.append("🖌️ 风格提示词预设：当前未配置任何风格预设。")

            return "\n\n".join(output_sections) if output_sections else "当前未配置任何预设。"


    if hasattr(filter, "on_llm_request"):
        @filter.on_llm_request()
        async def on_llm_request_inject_prompt(self, event: AstrMessageEvent, req: Any) -> None:
            """在向主对话 LLM 发起请求时，自动在系统提示词末尾注入生图函数工具的详细说明与扩写指引。"""
            if not bool(self.conf.get("enable_llm_tool_system_prompt", True)):
                return
            # 只有当插件启用时才注入
            instruction = self._get_llm_tool_system_prompt()
            if not instruction:
                return
            current_prompt = getattr(req, "system_prompt", None)
            if current_prompt is None:
                req.system_prompt = instruction
            elif instruction not in current_prompt:
                req.system_prompt = f"{current_prompt}\n\n{instruction}\n"

