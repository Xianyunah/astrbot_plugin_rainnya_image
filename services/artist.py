# -*- coding: utf-8 -*-
"""services/artist.py — RainnyaImage 插件模块（Mixin，由主插件类继承）"""

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


class ArtistMixin:
    """Mixin：方法体通过 self 访问插件实例状态，逻辑与原单文件版一致。"""

    async def _llm_generate_text(self, event: AstrMessageEvent, prompt: str, *,
                                  provider_id: str = "", temperature: float = 0.2,
                                  max_tokens: int = 900,
                                  image_bytes_list: Optional[List[bytes]] = None) -> Optional[str]:
        """通用 LLM 文本生成（纯协程，无 yield）。

        依赖 AstrBot context.llm_generate / get_current_chat_provider_id：
            llm_generate(*, chat_provider_id, prompt, image_urls, temperature, max_tokens) -> LLMResponse
            get_current_chat_provider_id(umo) -> provider_id
        provider_id 空时实时取当前会话 provider（勿缓存）；image_bytes_list → base64 data URL 列表传 image_urls。
        失败/无 provider 返回 None。
        """
        try:
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            if not provider_id:
                logger.warning("[NewImage] 获取当前会话 LLM provider 失败")
                return None
            image_urls: Optional[List[str]] = None
            if image_bytes_list:
                image_urls = []
                for img_bytes in image_bytes_list:
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    image_urls.append(f"data:image/png;base64,{b64}")
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=image_urls,
                temperature=float(temperature),
                max_tokens=int(max_tokens),
            )
            text = str(getattr(response, "completion_text", "") or "").strip()
            return text or None
        except Exception as e:
            logger.error(f"[NewImage] LLM 生成失败: {e}", exc_info=True)
            return None


    async def _danbooru_search_artists(self, tags: str, limit: int = 10) -> List[Dict]:
        """Danbooru 动态搜索画师（本地预设库匹配不足时的兜底）。

        流程：
        - 标签中含明确画师名（artist:xxx / @xxx）→ tags.json 精确/模糊查详情 + related_tag 补充相关画师；
        - 其余按风格标签 posts.json 统计出现画师 → 逐个 tags.json 查 post_count/category。
        统一过滤 post_count >= danbooru_min_posts，按帖子数降序截取 limit。
        返回 [{name, post_count, category}]；失败/超时/非 200 返回 []（不抛异常）。
        """
        iwf = self.iwf
        if iwf is None:
            return []
        base = str(self.conf.get("danbooru_base_url", DANBOORU_BASE_URL) or "").strip().rstrip("/")
        base = base or DANBOORU_BASE_URL
        min_posts = max(0, int(self.conf.get("danbooru_min_posts", DANBOORU_DEFAULT_MIN_POSTS)
                               or DANBOORU_DEFAULT_MIN_POSTS))
        limit = max(1, min(int(limit or DANBOORU_DEFAULT_LIMIT), 50))
        headers = {"User-Agent": NOVALAI_UA}

        async def _get_json(path: str, params: Dict) -> Any:
            try:
                async with iwf.session.get(
                    f"{base}{path}", params=params, headers=headers,
                    proxy=iwf.proxy, timeout=DANBOORU_TIMEOUT,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[NewImage] Danbooru 请求失败 {path}: HTTP {resp.status}")
                        return None
                    return await resp.json()
            except Exception as e:
                logger.warning(f"[NewImage] Danbooru 请求异常 {path}: {e}")
                return None

        async def _fetch_artist_info(name: str) -> Optional[Dict]:
            """tags.json 精确/模糊查画师 tag 详情（category=1 优先，即 artist 类）。"""
            payload = await _get_json("/tags.json", {
                "search[category]": 1,
                "search[name_matches]": name,
                "search[hide_empty]": "true",
                "limit": 5,
            })
            if isinstance(payload, list) and payload:
                for item in payload:
                    if str(item.get("name", "")).lower() == name.lower():
                        return item
                if int(payload[0].get("post_count", 0) or 0) > 0:
                    return payload[0]
            return None

        # 1. 明确画师名（artist:xxx / @xxx）
        explicit = list(dict.fromkeys(
            re.findall(r"(?:artist\s*[:@]|\b@)([a-zA-Z0-9_\-()]+)", (tags or "").lower())
        ))
        # 2. 风格标签 → posts.json 统计画师出现次数
        style_tags = [t for t in re.findall(r"[a-zA-Z0-9_]+", (tags or "").lower())
                      if t not in explicit and not t.startswith("artist")]
        artist_counter: Dict[str, int] = {}
        if style_tags:
            posts = await _get_json("/posts.json", {"tags": " ".join(style_tags[:5]), "limit": 100})
            if isinstance(posts, list):
                for post in posts:
                    for a in str(post.get("tag_string_artist") or "").split():
                        artist_counter[a] = artist_counter.get(a, 0) + 1
        # 3. 相关画师补充（related_tag.json，仅对前 2 个明确画师）
        related_names: List[str] = []
        for name in explicit[:2]:
            payload = await _get_json("/related_tag.json", {"query": f"artist:{name}", "category": 1})
            related_items = payload.get("related_tags") if isinstance(payload, dict) else (
                payload if isinstance(payload, list) else [])
            for item in (related_items or [])[:8]:
                rn = str((item or {}).get("tag", {}).get("name") or "").strip()
                if rn and rn != name:
                    related_names.append(rn)
        # 4. 汇总候选名：明确画师优先 → posts 统计（按出现次数降序）→ 相关画师补充
        ordered_by_posts = [n for n, _ in sorted(artist_counter.items(), key=lambda x: -x[1])]
        candidate_names = list(dict.fromkeys(explicit + ordered_by_posts + related_names))
        # 控制详情查询请求数（最多 min(20, limit*2 + explicit 数)）
        detail_names = candidate_names[:max(len(explicit), min(20, limit * 2 + len(explicit)))]
        # 5. 逐个查详情 + 过滤 min_posts
        results: List[Dict] = []
        for name in detail_names:
            info = await _fetch_artist_info(name)
            if not info:
                continue
            post_count = int(info.get("post_count", 0) or 0)
            if post_count < min_posts:
                continue
            results.append({
                "name": str(info.get("name") or name),
                "post_count": post_count,
                "category": int(info.get("category", 0) or 0),
            })
        # 6. 去重 + 降序 + 截取
        seen: set = set()
        out: List[Dict] = []
        for r in sorted(results, key=lambda x: -x["post_count"]):
            if r["name"] in seen:
                continue
            seen.add(r["name"])
            out.append(r)
            if len(out) >= limit:
                break
        return out


    @staticmethod
    def _format_artist_candidate(candidate: Dict) -> str:
        """候选画师池格式化：`- 画师名 (post_count)`（本地预设无 post_count 则省略）。"""
        name = str(candidate.get("name") or "").strip()
        if not name:
            return ""
        post_count = int(candidate.get("post_count") or 0)
        if post_count > 0:
            return f"- {name} ({post_count:,})"
        return f"- {name}"


    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """剥 code fence（```json ... ``` 等）。"""
        stripped = (text or "").strip()
        if not (stripped.startswith("```") and stripped.endswith("```")):
            return stripped
        inner = stripped[3:-3].strip()
        if "\n" not in inner:
            return inner.strip()
        first_line, rest = inner.split("\n", 1)
        if first_line.strip().isalpha() and len(first_line.strip()) < 15:
            return rest.strip()
        return inner.strip()


    @staticmethod
    def _parse_json_object(text: str) -> Optional[Dict]:
        """从 LLM 文本提取 JSON 对象（剥 code fence，截取首尾大括号后解析）。"""
        cleaned = ArtistMixin._strip_code_fence(text).strip()
        if not cleaned:
            return None
        candidates = [cleaned]
        if '"' in cleaned and "{" in cleaned and "}" in cleaned:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidates.append(cleaned[start:end + 1])
        for cand in candidates:
            try:
                payload = json.loads(cand)
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
        return None


    @staticmethod
    def _normalize_tag_output(payload: Optional[Dict]) -> Optional[Dict]:
        """从打标 JSON 归一化为六字段输出（缺省字段补空，字段类型强制 list/str）。"""
        if not isinstance(payload, dict):
            return None

        def _tag_list(value) -> List[str]:
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            if isinstance(value, list):
                return [str(v).strip() for v in value if str(v).strip()]
            return []

        return {
            "CHARACTER_TAG": _tag_list(payload.get("CHARACTER_TAG")),
            "WORK_TAG": _tag_list(payload.get("WORK_TAG")),
            "TAG": _tag_list(payload.get("TAG")),
            "BAD_TAG": _tag_list(payload.get("BAD_TAG")),
            "PROMPT": str(payload.get("PROMPT", "") or "").strip(),
            "NEGATIVE": str(payload.get("NEGATIVE", "") or "").strip(),
        }


    @staticmethod
    def _format_tag_output(normalized: Dict) -> str:
        """打标结果格式化：角色/作品/标签去重后逗号拼接，附 PROMPT 与负面词提示。"""
        char_tags = list(dict.fromkeys(normalized.get("CHARACTER_TAG", [])))
        work_tags = list(dict.fromkeys(normalized.get("WORK_TAG", [])))
        tags = list(dict.fromkeys(normalized.get("TAG", [])))
        bad_tags = list(dict.fromkeys(normalized.get("BAD_TAG", [])))
        prompt = normalized.get("PROMPT", "")
        negative = normalized.get("NEGATIVE", "")
        lines = []
        if char_tags:
            lines.append(f"角色: {', '.join(char_tags)}")
        if work_tags:
            lines.append(f"作品: {', '.join(work_tags)}")
        if tags:
            lines.append(f"标签: {', '.join(tags)}")
        if bad_tags:
            lines.append(f"负面: {', '.join(bad_tags)}")
        out = "\n".join(lines) if lines else ""
        if prompt:
            out += f"\n\nPrompt: {prompt}"
        if negative:
            out += f"\nNegative: {negative}"
        out += "\n\n📌 打标结果可直接用于 `bnn <标签>` 生图"
        return out.strip()


    def _parse_imported_artist_presets(self, data: Any) -> tuple:
        """智能解析多源画师预设：支持 Rainnya 导出包、AstrBot 幻笔插件配置/预设、云崽幻笔 Key-Value 格式。

        返回 (parsed_presets, detected_source_name)：
        成功解析返回 List[Dict]，失败抛出 ValueError。
        """
        parsed: List[Dict[str, str]] = []
        source_name = "未知格式"
        seen: set = set()

        def _add_preset(name: str, artists: str, negative: str = ""):
            name = str(name or "").strip()
            artists = str(artists or "").strip()
            negative = str(negative or "").strip()
            if not name or not artists:
                return
            if name in seen:
                for item in parsed:
                    if item["name"] == name:
                        item["artists"] = artists
                        if negative:
                            item["negative"] = negative
                        break
                return
            seen.add(name)
            parsed.append({"name": name, "artists": artists, "negative": negative})

        # 1. 如果传的是 dict
        if isinstance(data, dict):
            # 模式 1.1：包含 presets 键（Rainnya 格式或类似打包结构）
            if isinstance(data.get("presets"), list):
                source_name = "Rainnya 画师预设分享包"
                for item in data["presets"]:
                    if isinstance(item, dict):
                        _add_preset(item.get("name"), item.get("artists") or item.get("prompt"), item.get("negative") or item.get("negative_prompt"))
            # 模式 1.2：AstrBot 幻笔完整 config.json（包含 model / model_nai3 / model_nai4 等 section）
            elif any(k in data for k in ("model", "model_nai3", "model_nai4", "model_nai4_5")):
                source_name = "AstrBot 幻笔完整配置文件 (config.json)"
                for sec in ("model", "model_nai3", "model_nai4", "model_nai4_5"):
                    sec_obj = data.get(sec)
                    if isinstance(sec_obj, dict) and isinstance(sec_obj.get("artist_presets"), list):
                        for item in sec_obj["artist_presets"]:
                            if isinstance(item, dict):
                                _add_preset(item.get("name"), item.get("prompt") or item.get("artists"), item.get("negative_prompt") or item.get("negative"))
            # 模式 1.3：包含 artist_presets 键的单节字典
            elif isinstance(data.get("artist_presets"), list):
                source_name = "AstrBot 幻笔画师预设列表"
                for item in data["artist_presets"]:
                    if isinstance(item, dict):
                        _add_preset(item.get("name"), item.get("prompt") or item.get("artists"), item.get("negative_prompt") or item.get("negative"))
            # 模式 1.4：云崽老幻笔 Key-Value 格式（{"古风": "artist:xxx, 1girl...", "水彩": "artist:yyy..."}）
            elif all(isinstance(v, (str, dict)) for v in data.values()):
                source_name = "云崽 / 幻笔 Key-Value 字典格式"
                for k, v in data.items():
                    if isinstance(v, str):
                        _add_preset(k, v, "")
                    elif isinstance(v, dict):
                        _add_preset(k, v.get("prompt") or v.get("artists") or str(v), v.get("negative") or v.get("negative_prompt") or "")

        # 2. 如果传的是 list
        elif isinstance(data, list):
            source_name = "画师预设数组"
            for idx, item in enumerate(data, 1):
                if isinstance(item, dict):
                    nm = item.get("name") or f"画师预设 {idx}"
                    art = item.get("artists") or item.get("prompt") or ""
                    neg = item.get("negative") or item.get("negative_prompt") or ""
                    _add_preset(nm, art, neg)
                elif isinstance(item, str):
                    _add_preset(f"画师预设 {idx}", item, "")

        if not parsed:
            raise ValueError("无法从提供的内容中解析出有效画师预设，请检查格式")
        return parsed, source_name

