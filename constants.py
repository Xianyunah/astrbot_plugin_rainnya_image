# -*- coding: utf-8 -*-
"""constants.py — RainnyaImage 插件常量层。

纯数据与无状态工具函数，不依赖插件实例（self），可被任意模块导入与独立单测。
"""

import base64
import io
import json
import os
import re
import struct
import time
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image as PILImage

from astrbot import logger


PARAM_SCHEMA = {
    "size":             {"type": "str",   "choices": ["portrait_small", "portrait_medium", "portrait_large",
                                                      "landscape_small", "landscape_medium", "landscape_large",
                                                      "square", "1K", "2K", "4K"]},
    "temperature":      {"type": "float", "min": 0.0, "max": 2.0},
    "max_tokens":       {"type": "int",   "min": 64,  "max": 16384},
    "steps":            {"type": "int",   "min": 1,   "max": 150},
    "negative_prompt":  {"type": "str",   "max_len": 500},
}


_PARAM_NOT_SET = object()


_PARAM_ERROR_MSGS = {
    "size": "参数 size 取值无效，可选：portrait_small/portrait_medium/portrait_large/landscape_small/landscape_medium/landscape_large/square（及旧 1K/2K/4K）",
    "temperature": "参数 temperature 超出范围（0.0-2.0）",
    "max_tokens": "参数 max_tokens 超出范围（64-16384）",
    "steps": "参数 steps 超出范围（1-150）",
    "negative_prompt": "参数 negative_prompt 超出长度（≤500）",
}


_SIZE_RANK = {"1K": 1, "2K": 2, "4K": 3}


_ALLOWED_SIZE_VALUES = tuple(_SIZE_RANK.keys())


GENERATION_HISTORY_MAX = 500      # 生图时间线历史上限（超出删除最旧）


MAX_IMAGES_PER_REQUEST = 5        # bnn 一次最多处理的图片数


WEB_PAGE_SIZE_MAX = 200           # Web 列表分页 page_size 上限


LOG_QUERY_LIMIT_MAX = 20          # 生图日志查询条数上限


PROMPT_SHORT_LEN = 15             # 日志/查询中 prompt 摘要截断长度


DISPLAY_PROMPT_LEN = 20           # 文生图/指令显示 prompt 预览长度


DISPLAY_CMD_LEN = 30              # 预设指令显示预览长度


API_TIMEOUT_MIN_SECONDS = 10      # API 超时下限


LOG_IMAGE_MAX_BYTES = 20 * 1024 * 1024  # 日志图片大小上限（20MB），超过返回 413


NOVALAI_DEFAULT_BASE_URL = "https://image.novelai.net"


NOVALAI_DEFAULT_NEGATIVE_PROMPT = ("nsfw, lowres, {bad}, error, fewer, extra, missing, worst quality, "
                                   "jpeg artifacts, bad quality, watermark, unfinished, displeasing, "
                                   "chromatic aberration, signature, extra digits, artistic error, "
                                   "username, scan, [abstract]")


NOVALAI_SIZE_DIMENSIONS = {
    # 竖版（portrait）
    "portrait_small":  (640, 1536),
    "portrait_medium": (832, 1216),
    "portrait_large":  (1024, 1536),
    # 横版（landscape）
    "landscape_small":  (1536, 640),
    "landscape_medium": (1216, 832),
    "landscape_large":  (1536, 1024),
    # 方图
    "square": (1024, 1024),
    # 旧值兼容
    "1K": (832, 1216),
    "2K": (1152, 1664),
    "4K": (1664, 2432),
}


NOVALAI_SIZE_LABELS = {
    "portrait_small": "竖·小 640×1536", "portrait_medium": "竖·中 832×1216",
    "portrait_large": "竖·大 1024×1536", "landscape_small": "横·小 1536×640",
    "landscape_medium": "横·中 1216×832", "landscape_large": "横·大 1536×1024",
    "square": "方图 1024×1024", "1K": "1K(旧)", "2K": "2K(旧)", "4K": "4K(旧)",
}


NOVALAI_DEFAULT_SIZE = "portrait_medium"   # 竖中 832×1216 对齐官方默认


NOVALAI_DEFAULT_WIDTH, NOVALAI_DEFAULT_HEIGHT = NOVALAI_SIZE_DIMENSIONS[NOVALAI_DEFAULT_SIZE]


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
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0")


DANBOORU_BASE_URL = "https://danbooru.donmai.us"


DANBOORU_TIMEOUT = 15              # 单次请求超时（秒）


DANBOORU_DEFAULT_LIMIT = 10        # 候选画师上限默认


DANBOORU_DEFAULT_MIN_POSTS = 100   # 画师最低帖子数门槛默认


NSFW_ZIP_PASSWORD = "nsfwmiaomiao"   # NSFW 加密 ZIP 解压密码（硬编码，不配置、不暴露在 WebUI）


NSFW_ZIP_ENTRY = "image.png"         # ZIP 内图片条目名


NSFW_ZIP_CACHE_DIR = "nsfw_cache"    # 插件数据目录下的缓存子目录


def _gen_zipcrypto_crctable():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
        table.append(crc & 0xFFFFFFFF)
    return table


_ZIPCRYPTO_CRC_TABLE = _gen_zipcrypto_crctable()


def _zipcrypto_encrypt_data(data: bytes, password: bytes, crc32_value: int) -> bytes:
    """ZipCrypto（传统 PKWARE）加密压缩数据：前置 12 字节加密头（第 12 字节=CRC 高 8 位校验）后接密文。"""
    table = _ZIPCRYPTO_CRC_TABLE

    def _crc32_byte(ch, crc):
        return (crc >> 8) ^ table[(crc ^ ch) & 0xFF]

    keys = [305419896, 591751049, 878082192]  # 0x12345678 / 0x23456789 / 0x34567890

    def _update_keys(c):
        keys[0] = _crc32_byte(c, keys[0])
        keys[1] = (keys[1] + (keys[0] & 0xFF)) & 0xFFFFFFFF
        keys[1] = (keys[1] * 134775813 + 1) & 0xFFFFFFFF
        keys[2] = _crc32_byte((keys[1] >> 24) & 0xFF, keys[2])

    def _decrypt_byte():
        temp = keys[2] | 2
        return ((temp * (temp ^ 1)) >> 8) & 0xFF

    for b in password:
        _update_keys(b)
    # 12 字节加密头：前 11 字节随机，第 12 字节 = CRC 高 8 位（解压器据此校验密码）
    # 注意：与解密方对称，密钥用「明文」字节更新
    check_byte = (crc32_value >> 24) & 0xFF
    header = bytearray(12)
    for i in range(12):
        plain = os.urandom(1)[0] if i < 11 else check_byte
        header[i] = plain ^ _decrypt_byte()
        _update_keys(plain)
    # 加密压缩数据流
    cipher = bytearray(len(data))
    for i, d in enumerate(data):
        cipher[i] = d ^ _decrypt_byte()
        _update_keys(d)
    return bytes(header) + bytes(cipher)


def _apply_zipcrypto_encryption(zip_bytes: bytes, password: bytes) -> bytes:
    """把 stdlib 生成的「单条目、无 data descriptor」ZIP 改为 ZipCrypto 密码加密。

    步骤：置位加密标志(bit 0) → 加密压缩数据(前置 12 字节头) → 修正本地头/中央目录压缩尺寸与中央目录偏移。
    仅适用于单条目小文件 ZIP（NSFW 生图场景）。
    """
    data = bytearray(zip_bytes)
    if data[0:4] != b"PK\x03\x04":
        raise ValueError("非法 ZIP：缺少本地文件头")
    name_len = int.from_bytes(data[26:28], "little")
    extra_len = int.from_bytes(data[28:30], "little")
    data_start = 30 + name_len + extra_len
    comp_size = int.from_bytes(data[18:22], "little")
    crc = int.from_bytes(data[14:18], "little")
    data_end = data_start + comp_size
    if data[data_end:data_end + 4] != b"PK\x01\x02":
        raise ValueError("非法 ZIP：未找到中央目录头（或不是单条目）")
    # 加密压缩数据
    encrypted = _zipcrypto_encrypt_data(bytes(data[data_start:data_end]), password, crc)
    # 组装输出：本地头(改 flag/压缩尺寸) + 加密数据 + 中央目录(改 flag/压缩尺寸) + EOCD(改中央目录偏移)
    out = bytearray()
    out += data[:data_start]
    flags = int.from_bytes(out[6:8], "little")
    out[6:8] = (flags | 0x1).to_bytes(2, "little")                  # 置加密标志位
    out[18:22] = (comp_size + 12).to_bytes(4, "little")             # 本地头压缩尺寸(加密头+12)
    out += encrypted
    cd_off_new = len(out)                                           # 中央目录新偏移
    out += data[data_end:]
    cd_flags_off = cd_off_new + 8
    cd_comp_off = cd_off_new + 20
    cd_flags = int.from_bytes(out[cd_flags_off:cd_flags_off + 2], "little")
    out[cd_flags_off:cd_flags_off + 2] = (cd_flags | 0x1).to_bytes(2, "little")
    out[cd_comp_off:cd_comp_off + 4] = (comp_size + 12).to_bytes(4, "little")
    eocd = out.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ValueError("非法 ZIP：未找到 EOCD")
    out[eocd + 16:eocd + 20] = cd_off_new.to_bytes(4, "little")
    return bytes(out)


def _build_nsfw_encrypted_zip(image_bytes: bytes) -> bytes:
    """构建 NSFW 密码加密 ZIP（内存）：stdlib 生成单条目 ZIP 后套用 ZipCrypto 加密。"""
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(NSFW_ZIP_ENTRY, image_bytes)
    return _apply_zipcrypto_encryption(zip_buf.getvalue(), NSFW_ZIP_PASSWORD.encode("utf-8"))


TAGGER_PROMPT_TEMPLATE = """你是图片内容打标器（Danbooru / NovelAI tag 体系）。

要求：
- 只输出 JSON
- 标签使用英文小写下划线
- 必须包含 CHARACTER_TAG、WORK_TAG、TAG、BAD_TAG、PROMPT、NEGATIVE 六个字段
- BAD_TAG 只放 negative prompt，不要写"与图片相反"的否定 tag
- CHARACTER_TAG 最多 5 个，WORK_TAG 最多 5 个，TAG 最多 80 个，BAD_TAG 最多 40 个

JSON 结构：
{
  "CHARACTER_TAG": ["..."],
  "WORK_TAG": ["..."],
  "TAG": ["..."],
  "BAD_TAG": ["..."],
  "PROMPT": "...",
  "NEGATIVE": "..."
}
"""


EXTRACT_TAGS_TEMPLATE = """你是 Danbooru 标签专家。根据用户的画风需求，提取用于搜索画师的英文标签。

用户需求：<<USER_REQUEST>>

规则：
- 如果用户明确提到了具体画师名，直接输出 `@画师名`（Danbooru 格式，下划线连接）
- 如果是风格特征，输出 2-4 个英文标签，用空格分隔
- 不要解释
"""


ARTIST_FROM_POOL_TEMPLATE = """你是 NovelAI 画师串专家。只能使用下方候选池中的画师，不要编造。

用户需求：<<USER_REQUEST>>
模型版本：<<MODEL_VERSION>>

候选画师池：
<<CANDIDATE_ARTISTS>>

规则：
- 所有画师都必须带 `artist:` 前缀
- 使用 5-10 个画师组合
- 可使用 `1.2::artist:name::` 这类数值权重
- 直接输出最终画师串，不要解释
"""


DEFAULT_LLM_TOOL_SYSTEM_PROMPT = """# AI 绘画与生图工具能力指引 (Image Generation Capabilities)
你具备强大的 AI 绘画与图像生成能力，系统为你配备了专业的生图函数工具：

1. 【严格禁止拒绝生图】：
   - 当用户表达「画一张...」、「生成图片」、「画个...」、「来张...」、「画你自己/自拍」等绘画需求时，严禁回复「我只是文本模型，无法生成图片」！
   - 你必须直接调用 `generate_image` 函数工具为用户执行生图！

2. 【提示词自动高质量扩写要求】：
   - 调用 `generate_image(prompt=...)` 时，必须将用户的简短中文需求扩写为详细、高质量、适合 AI 绘画的完整英文提示词（prompt）。
   - 包含要素：画面主体、角色特征、服装发型、环境背景、光影氛围、视角构图、画质标签（如 masterpiece, highly detailed, photorealistic 或 anime aesthetic 等）。

3. 【AI 助手自拍与形象一致性】：
   - 当用户要求画「你（AI 助手自己）」的自拍、形象或生活照时，请将参数 `selfie=true`，系统会自动在提示词前追加你的预设外貌特征。

4. 【图+文生图能力】：
   - 若用户在当前消息中附带了图片并要求以此图画图、手办化或改变画风，`generate_image` 会自动提取图片作为参考进行图生图。

5. 【辅助查询与预设指引】：
   - 用户询问「我还能画几次图」或「生图配额」时，调用 `query_generation_quota` 查询；
   - 用户询问「有哪些生图模型可以用」时，调用 `query_generation_models` 查询；
   - 用户询问「有哪些画风预设」或「有哪些画师串/画师预设」时，调用 `query_presets` 查询（支持按 category="artist" 查画师串、category="style" 查风格预设，或 category="all" 查全部）；
   - 当用户要求使用特定画师风格（或画师串）生图时，可在 `query_presets` 获取画师预设名后，在 `generate_image(artist_preset="预设名")` 中直接传入该画师预设名，系统会自动将该画师串及其负面词并入生成参数！不使用画师串时请传空字符串 `""`。"""


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


def _openai_image_size(size: Any) -> str:
    """把插件内部尺寸档位映射为 OpenAI Images API 的 "宽x高" 格式。

    - 已符合 "1024x1024" 格式 → 原样使用（用户/中转自定义尺寸优先）
    - landscape_* → 1536x1024；portrait_* → 1024x1536
    - square / 1K / 2K / 4K / 未知 → 1024x1024（Images API 最通用尺寸）
    """
    s = str(size or "").strip().lower()
    if re.fullmatch(r"\d{2,5}x\d{2,5}", s):
        return s
    if s.startswith("landscape"):
        return "1536x1024"
    if s.startswith("portrait"):
        return "1024x1536"
    return "1024x1024"


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
                    if r is None or r < 0:
                        # 不可比项（如 size 新档位键 portrait_*/landscape_*/square）不参与数值夹取：
                        # 有效的新档位键已在上方 `value in choices` 直接命中，这里仅保留旧 1K/2K/4K 夹取语义
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


def _rejoin_prompt_list(items: List[Any]) -> List[str]:
    """容错重组 prompt_list：前端按换行切分会把跨行预设提示词切成多个数组元素。

    规则：不含 ":" 的元素并入前一个元素（用 \\n 连接），还原成 触发词:提示词 对——
    因为跨行提示词只有首行含触发词冒号，后续行是提示词正文的续行。
    合并后仍无 ":" 的孤立元素（如最开头的残留片段）原样保留，由调用方校验报错。
    """
    rejoined: List[str] = []
    for item in items:
        s = str(item)
        if ":" in s:
            rejoined.append(s)
        elif rejoined:
            rejoined[-1] = f"{rejoined[-1]}\n{s}"
        else:
            rejoined.append(s)
    return rejoined



__all__ = [
  "PARAM_SCHEMA", "_PARAM_NOT_SET", "_PARAM_ERROR_MSGS", "_SIZE_RANK", "_ALLOWED_SIZE_VALUES", "GENERATION_HISTORY_MAX", "MAX_IMAGES_PER_REQUEST", "WEB_PAGE_SIZE_MAX", "LOG_QUERY_LIMIT_MAX", "PROMPT_SHORT_LEN", "DISPLAY_PROMPT_LEN", "DISPLAY_CMD_LEN", "API_TIMEOUT_MIN_SECONDS", "LOG_IMAGE_MAX_BYTES", "NOVALAI_DEFAULT_BASE_URL", "NOVALAI_DEFAULT_NEGATIVE_PROMPT", "NOVALAI_SIZE_DIMENSIONS", "NOVALAI_SIZE_LABELS", "NOVALAI_DEFAULT_SIZE", "NOVALAI_DEFAULT_WIDTH", "NOVALAI_DEFAULT_HEIGHT", "NOVALAI_DEFAULT_STEPS", "NOVALAI_STEPS_MAX_FREE", "NOVALAI_MAX_PIXELS_FREE", "NOVALAI_MAX_PIXELS_PAID", "NOVALAI_MIN_DIM", "NOVALAI_SAMPLERS", "NOVALAI_DEFAULT_SAMPLER", "NOVALAI_IMG2IMG_STRENGTH", "NOVALAI_IMG2IMG_NOISE", "NOVALAI_UA", "DANBOORU_BASE_URL", "DANBOORU_TIMEOUT", "DANBOORU_DEFAULT_LIMIT", "DANBOORU_DEFAULT_MIN_POSTS", "NSFW_ZIP_PASSWORD", "NSFW_ZIP_ENTRY", "NSFW_ZIP_CACHE_DIR", "_gen_zipcrypto_crctable", "_ZIPCRYPTO_CRC_TABLE", "_zipcrypto_encrypt_data", "_apply_zipcrypto_encryption", "_build_nsfw_encrypted_zip", "TAGGER_PROMPT_TEMPLATE", "EXTRACT_TAGS_TEMPLATE", "ARTIST_FROM_POOL_TEMPLATE", "DEFAULT_LLM_TOOL_SYSTEM_PROMPT", "PROVIDER_DEFAULT_MODELS", "_choice_rank", "_max_allowed_size", "_openai_image_size", "_coerce_param_value", "_merge_params", "_rejoin_prompt_list",
]
