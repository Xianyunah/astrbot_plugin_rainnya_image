# -*- coding: utf-8 -*-
"""services/state.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class StateMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

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
            # user_url 缺省取 base_url；novalai 官方默认用 api.novelai.net 账户端点
            if not str(p.get("user_url", "") or "").strip():
                b_url = str(p.get("base_url", "") or "").strip()
                if "image.novelai.net" in b_url:
                    p["user_url"] = "https://api.novelai.net"
                else:
                    p["user_url"] = b_url or "https://api.novelai.net"
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


    def _validate_and_normalize_models(self, models: List[Any]) -> tuple:
        """在 JSON 对象层严格校验并规范化模型列表，杜绝分号序列化截断与反序列化崩溃隐患。

        返回 (normalized_models, error)：
        成功时 error 为 None；校验失败时 error 为错误描述。
        """
        normalized: List[Dict[str, Any]] = []
        seen: set = set()
        for m in models:
            if not isinstance(m, dict):
                return [], "models 元素必须是对象"
            name = str(m.get("name", "")).strip()
            if not name:
                return [], "模型名不能为空"
            if name in seen:
                continue  # 去重，保留第一项
            seen.add(name)
            try:
                quota = int(m.get("quota", 1))
            except (TypeError, ValueError):
                return [], f"模型 {name} 的配额必须为整数"
            if quota < 1:
                return [], f"模型 {name} 的配额必须 >= 1"
            desc = str(m.get("desc", "") or "").strip()
            raw_params = m.get("params")
            if raw_params is None:
                params = {}
            elif isinstance(raw_params, dict):
                params, _ = self._sanitize_params(raw_params, "WebUI")
            else:
                params = {}
            allowed_sizes = m.get("allowed_sizes")
            if allowed_sizes is not None:
                if not isinstance(allowed_sizes, list) or not all(
                        isinstance(s, str) and s in _ALLOWED_SIZE_VALUES for s in allowed_sizes):
                    return [], f"模型 {name} 的 allowed_sizes 只能包含 1K/2K/4K 字符串数组或 null"
                if not allowed_sizes:
                    allowed_sizes = None
                else:
                    allowed_sizes = list(dict.fromkeys(allowed_sizes))
            normalized.append({
                "name": name,
                "quota": quota,
                "desc": desc,
                "params": params,
                "allowed_sizes": allowed_sizes,
            })
        return normalized, None


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


    def _expected_model_name(self, group_id: Optional[str] = None) -> str:
        """返回手动命令在 _call_api 中会选中的模型名（与 _call_api 选择优先级一致，无 model_override）。

        优先级：本群 group_models > available_model_names[0] > conf.model 兜底。
        用于在生图前（_call_api 尚未返回 used_model）预估配额做次数预检。
        """
        if group_id and self.group_models.get(group_id):
            return self.group_models[group_id]
        if self.available_model_names:
            return self.available_model_names[0]
        return (self.conf.get("model", "") or "").strip()


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
                # 按「首冒号」切分：元素内部可含换行（跨行提示词），value 保留换行原样加载
                if ":" in item:
                    key, value = item.split(":", 1)
                    self.prompt_map[key.strip()] = value.strip()
                else:
                    logger.warning(f"跳过格式错误的 prompt (缺少冒号): {item}")
            except ValueError:
                logger.warning(f"跳过格式错误的 prompt: {item}")
        logger.info(f"加载了 {len(self.prompt_map)} 个 prompts。")


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


    async def _load_group_nsfw_zip(self):
        """加载各群 NSFW 加密开关映射 {群号: True/False}（只存「覆盖」的群）。"""
        data = await load_json(self.group_nsfw_zip_file)
        if isinstance(data, dict):
            self.group_nsfw_zip = {str(k): bool(v) for k, v in data.items()}
        else:
            self.group_nsfw_zip = {}


    async def _save_group_nsfw_zip(self):
        """原子写各群 NSFW 加密开关映射。"""
        await save_json(self.group_nsfw_zip_file, self.group_nsfw_zip)


    def _nsfw_zip_effective(self, group_id: Optional[str]) -> bool:
        """NSFW 加密双层开关生效判定。

        全局开关 nsfw_zip_enabled 关闭 → 一律不加密（直接发原图）；
        开启时：群有覆盖（group_nsfw_zip 中存在该群）用群覆盖值，否则跟随全局开启。
        """
        if not self.conf.get("nsfw_zip_enabled", True):
            return False
        if group_id and str(group_id) in self.group_nsfw_zip:
            return bool(self.group_nsfw_zip[str(group_id)])
        return True


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


    def _delete_generation_files(self, records: List[Dict]) -> None:
        """物理删除生图记录对应的本地图片文件，释放磁盘空间。"""
        for r in records:
            fp = str(r.get("file_path", "") or "").strip()
            if not fp:
                continue
            try:
                p = Path(fp)
                if p.is_file():
                    p.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"[NewImage] 物理删除生图文件失败 ({fp}): {e}")


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

