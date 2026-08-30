# 🎨 AstrBot Rainnya Image Plugin

> **AI 生图插件 · 原生支持 NovelAI（官方 + 反代双协议）、Gemini、OpenAI**
> 本项目 `astrbot_plugin_rainnya_image` 基于上游原版 [`astrbot_plugin_nova_newimage`](https://github.com/YumenoSayuri/astrbot_plugin_nova_newimage)（原作者：辉宝，v1.4.1）的 **AGPL-3.0 fork 魔改增强版**，保留原版全部能力并大幅增强。

> 面向普通用户的插件，无需写代码：**添加提供商 → 填 Key → 开画**，其余全在 WebUI 里点一点。

---

## 🌟 为什么选它

- **支持 NovelAI 生图**：官方协议 + 反代模式双路由，文生图 / 图生图都能跑，ZIP 响应自动解压，支持 `nai-diffusion-4-5-full` 等模型
- **三渠道通吃**：Gemini / OpenAI / NovelAI 一个插件全搞定，像 AI coding 平台一样在 WebUI 添加多个提供商
- **WebUI 全量管理**：提供商 / 次数 / 生图日志 / 画师预设 / 基础设置 五个 Tab，配置不再靠手写 JSON
- **画师串预设 + 打标 + 画师推荐**：实现「看图→反推标签→推荐画师→保存预设→按群启用」的完整工作流
- **LLM 自主生图**：主对话 LLM 可直接调 7 个工具画图、查次数、查时间线，聊天即生图
- **异步生成 + 完整日志**：不阻塞对话，生成完自动发图，所有图片落盘可回溯

---

## 🖼️ NovelAI 生图支持（重点）

### 官方 + 反代双协议

- **官方协议**：走 NovelAI 官方接口（POST 多步采样），`BaseURL` 填官方地址
- **反代模式**：WebUI 提供商详情里打开「启用反代模式」开关，生图走 GET 反代格式（如 `std.loliyc.com`），适合自建/中转的反代服务
- 文生图 / 图生图都支持；**ZIP 响应自动解压**；尺寸自动对齐 64 倍数；`seed` 随机生成

### 推荐配置示例（WebUI「提供商管理」添加）

| 项 | 示例 |
|----|------|
| 提供商类型 | **NovalAI** |
| API Key | 你的 NovelAI Key |
| BaseURL（官方） | `https://api.novelai.net` |
| BaseURL（反代） | `https://std.loliyc.com/novelai` |
| User URL（可选） | `https://api.novelai.net`（key 预检用账户端点，留空用 BaseURL） |
| 反代模式 | 用反代服务时打开「启用反代模式」 |

### 尺寸档位（大中小 × 横竖 + 方图）

| 档位 | 尺寸 |
|------|------|
| 竖·小 / 竖·中 / 竖·大 | 640×1536 / 832×1216 / 1024×1536 |
| 横·小 / 横·中 / 横·大 | 1536×640 / 1216×832 / 1536×1024 |
| 方图 | 1024×1024 |

### 模型名示例

- `nai-diffusion-4-5-full`（主力）
- `nai-diffusion-3` / `nai-diffusion-furry` 等按版本填写

---

## 📦 功能清单

### 🏗️ 多提供商架构

- WebUI 添加 **N 个提供商**（Gemini / OpenAI / NovelAI 模板），各自填 **API Key + BaseURL + User URL**
- 每个提供商下挂**自己的模型列表**（模型名 / 配额 / 说明 / 参数 JSON / 分辨率限制），互不干扰
- 已废弃 AstrBot 的 `provider_id` 下拉，连接方式完全由 WebUI 接管

### 🎛️ 三大渠道专属参数

- **OpenAI**：默认分辨率（1K/2K/4K）、temperature、max_tokens
- **Gemini**：安全过滤等级、默认分辨率（1K/2K/4K）、高分辨率白名单
- **NovelAI**：尺寸档位（竖/横 × 小/中/大 + 方图）、采样器、采样步数、引导强度 scale、负面词

### 🖥️ WebUI 管理面板（五个 Tab）

| Tab | 功能 |
|-----|------|
| **提供商管理** | 添加/编辑/删除提供商，管理每个提供商下的模型 |
| **次数管理** | 用户 / 群组次数查询、充值、设置 |
| **生图日志** | 时间线可视化、按范围/时间筛选、删除 |
| **画师预设** | 画师串预设库管理 + 按群启用 |
| **基础设置** | 全部配置项（含 NovelAI 预检 / 自动撤回 / NSFW 过滤 / 打标 / 画师推荐） |

纯 HTML/CSS/JS 实现，跟随 AstrBot 亮/暗主题，无需额外依赖。

### 🎨 画师串预设

- WebUI「画师预设」Tab 维护预设库（预设名 + 画师串 + 负面词）
- **按群启用**：为指定群设置画师串预设，也可在群里用 `画师串 预设名` 指令切换
- 群里用 `画师串保存 <名称>:<画师串>` 保存预设

### 🤖 LLM 工具全家福（7 个）

`generate_image`（生图，图 + 文，支持 **selfie 自拍 mode**）、`query_models`（查模型）、`query_generation_quota`（查次数）、`query_generation_history`（查时间线）、`recall_image`（调历史图）、`usage_stats`（使用统计）、`query_presets`（查预设）

> 主对话 LLM 支持 function calling 时自动可用，用户说「帮我画一张…」即触发，LLM 扩写提示词并自主调用。`selfie=true` 时自动追加自拍人物外貌特征（可在设置页编辑）。

### 🧠 nai-brush 移植五件套

| 功能 | 说明 |
|------|------|
| **打标** | 图片反推标签（`打标` 指令） |
| **画师推荐** | 根据标签自动推荐画师串（`画师推荐` 指令） |
| **token 预检** | 生图前预检 NovelAI key 余额/可用性，点数不足提前提醒 |
| **自动撤回** | 生成完成后自动撤回结果消息（按群白名单生效） |
| **NSFW 过滤** | 检测到 NSFW 内容时注入自定义标签 |

### ⚡ 异步生图 + 生图日志

- LLM 工具调用**不阻塞对话**，后台生成完成后自动发图，受群并发槽位限制
- 全部生图落盘（`data/generations/<群>/<时间戳>.png`）+ 统一时间线（保留最近 500 条）
- `>>生图日志` 指令：查询（本群/全局/分页）/ 单条查询（含图片）/ 删除（单条/时间段/群）

### 🎁 次数 / 签到 / 权限

- 用户/群组次数限制，按模型消耗不同配额；配额不足明确报错
- 每日签到（`喵喵赐福`）+ 固定/随机奖励，管理员加次数不受上限
- 用户/群组黑白名单、管理员豁免、单群并发控制

---

## 📦 安装

### 方式一：AstrBot 插件市场

AstrBot 管理面板 →「插件市场」→ 搜索 **rainnya_image**（或本插件名）安装。

### 方式二：手动安装

1. 下载本仓库（或 release 下的源码包）
2. 将插件文件夹放入 AstrBot 的 `data/plugins/` 目录
3. 重启 AstrBot

---

## ⚙️ 快速上手（三步）

1. **添加提供商**：插件详情页 → Pages →「提供商管理」→「+ 添加提供商」→ 选类型（Gemini / OpenAI / NovelAI）→ 填 API Key / BaseURL → 保存
2. **添加模型**：点进该提供商「管理模型」→「+ 添加模型」→ 填模型名 / 配额 / 说明 / 参数 → 保存
3. **开画**：群里发 `>>文生图 描述`，或直接让 LLM 帮你画

### 模型配置格式（提供商 → 管理模型内）

每行一个模型：`模型名;每次消耗配额;给LLM的说明;参数JSON`

```
nai-diffusion-4-5-full;3;NovelAI 主力模型;
gpt-image-2;1;标准生图，速度快;
gpt-image-2-large;3;高分辨率精修;{"sizes":["4K"],"temperature":0.8}
gemini-flash;1;性价比之选;{"sizes":["1K"]}
```

- 参数 JSON 可选：`size`/`temperature`/`max_tokens`/`steps`/`negative_prompt`，`sizes` 键限制可用分辨率
- 群内用 `>>切换模型 <序号>` 切换（仅管理员）

### 常用配置项（WebUI「基础设置」Tab）

| 配置项 | 说明 |
|--------|------|
| `prefix` / `extra_prefix` | 是否要求指令带前缀 / 自定义前缀（如 `bnn`） |
| `use_proxy` / `proxy_url` | 网络代理开关 / 代理地址 |
| `download_timeout` / `download_retries` / `api_timeout` | 下载超时 / 重试次数 / API 超时 |
| `selfie_prompt` | 自拍人物外貌特征（英文，`selfie=true` 时追加） |
| `safety_level` / `image_size` / `hires_whitelist` | Gemini 安全过滤 / 默认分辨率 / 高分辨率白名单 |
| `novai_precheck_enabled` / `novai_precheck_timeout` / `novai_point_warn_threshold` | NovelAI 预检开关 / 超时 / 点数提醒阈值 |
| `auto_recall_enabled` / `auto_recall_delay` / `auto_recall_allowed_groups` | 自动撤回开关 / 延迟 / 生效群（每行 `platform:chat_id`，如 `aiocqhttp:123456789`） |
| `nsfw_filter_enabled` / `nsfw_filter_tags` | NSFW 过滤开关 / 注入标签 |
| `tagger_provider_id` / `tagger_temperature` / `tagger_max_tokens` | 打标模型 / 温度 / Max Tokens |
| `artist_recommend_enabled` / `artist_recommend_max_candidates` / `artist_recommend_min_match` | 画师推荐开关 / 最大候选数 / 最少匹配标签数 |
| `enable_user_limit` / `enable_group_limit` / `user_count_cap` / `group_task_limit` | 次数限制与并发 |
| `enable_checkin` / `checkin_fixed_reward` / `enable_random_checkin` / `checkin_random_reward_min`/`max` | 签到与随机奖励 |
| `user_whitelist` / `user_blacklist` / `group_whitelist` / `group_blacklist` | 用户/群组黑白名单 |
| `help_menu` / `prompt_list` | 自定义帮助菜单 / 预设 prompt（每行 `触发词:提示词`） |

---

## 📖 命令列表

> 指令前缀 `>>` 与 `/` 等价。

### 🖼️ 图生图预设指令（40+）

| 命令 | 功能 |
|:-----|:-----|
| `/手办化` ~ `/手办化6` | 6 种手办风格 |
| `/Q版化` / `/cos化` / `/cos自拍` | Q 版 / Cosplay |
| `/痛车化` / `/痛屋化` / `/痛屋化2` | 痛车 / 痛屋 |
| `/真人化` / `/真人化2` / `/半真人` / `/半融合` | 真人 / 混搭 |
| `/玩偶化` / `/贴纸化` / `/挂件化` / `/微型化` | 物品化 |
| `/三视图` / `/穿搭拆解` / `/角色界面` / `/角色设定` / `/姿势表` | 设定类 |
| `/高清修复` / `/人物转身` / `/绘画四宫格` | 修复 / 过程 |
| `/发型九宫格` / `/头像九宫格` / `/表情九宫格` / `/多机位` / `/电影分镜` / `/动漫分镜` | 九宫格 / 分镜 |

### ✏️ 文生图 / 图生图 / 打标

| 命令 | 功能 |
|:-----|:-----|
| `>>文生图 <描述> [参数]` | 文生图，可带参数（如 `size:4K temperature:0.9`） |
| `bnn <想法>` | 自定义 Prompt 图生图（支持多图） |
| `打标 <图片>` | 图片反推标签 |

### 🎨 画师串 / 画师推荐

| 命令 | 功能 |
|:-----|:-----|
| `画师串 <预设名>` | 按群启用画师串预设 |
| `画师串保存 <名称>:<画师串>` | 保存画师串预设 |
| `画师推荐` | 根据标签推荐画师串 |

### ⚙️ 模型与日志

| 命令 | 功能 |
|:-----|:-----|
| `>>切换模型 <序号>` | 按群切换生图模型（仅管理员） |
| `>>生图日志 [查询/单条查询/删除]` | 生图时间线记录（仅管理员） |

### 🎁 签到与查询

| 命令 | 功能 |
|:-----|:-----|
| `喵喵赐福` | 每日签到（无需前缀） |
| `>>生图查询次数` | 查询个人/群组剩余次数（管理员查他人） |
| `>>生图帮助 [预设名]` | 帮助菜单 / 查看预设内容 |

### 👑 管理命令（仅管理员）

| 命令 | 功能 |
|:-----|:-----|
| `>>预设添加 <名称>:<提示词>` | 添加/修改预设 |
| `>>生图增加用户次数 @用户 <次数>` / `<QQ号> <次数>` | 增加用户次数（不受上限限制） |
| `>>生图增加群组次数 <群号> <次数>` | 增加群组次数 |

---

## 📝 更新日志

### 魔改增强版（2026-08）

- 🎨 **NovelAI 双路由**：官方协议 + 反代模式，文生图/图生图、ZIP 解压、64 倍数尺寸、seed 随机
- 🏗️ **多提供商架构**：WebUI 添加 Gemini / OpenAI / NovalAI 提供商，各自 API Key + BaseURL + User URL，下挂模型列表
- 🖥️ **WebUI 管理面板**：提供商 / 次数 / 生图日志 / 画师预设 / 基础设置 五个 Tab
- 🎛️ **三大渠道专属参数**：OpenAI / Gemini / NovelAI 尺寸档位（竖横 × 大中小 + 方图）
- 🎨 **画师串预设**：WebUI 管理 + 按群启用 + `画师串` / `画师串保存` 指令
- 🤖 **LLM 工具全家福（7 个）**：生图（图+文 + selfie 自拍 mode）/ 查模型 / 查次数 / 查时间线 / 调历史图 / 使用统计 / 查预设
- 🧠 **nai-brush 五件套**：打标 / 画师推荐 / token 预检 / 自动撤回 / NSFW 过滤
- 📸 **自拍特征**：`selfie_prompt` 配置，生图工具传 `selfie=true` 自动追加外貌描述
- 🚀 **异步生图**：LLM 工具调用不阻塞对话，后台生成自动发图
- 📜 **生图落盘 + 统一时间线** + `生图日志` 指令
- 🔧 管理员加次数无上限、5xx 报错友好化、拆分多模块架构

### v1.4.1（原版，2026-03-22）

- 🐛 修复 @提及文本残留被当补充 prompt 的问题

### v1.4.0（原版，2026-03-21）

- 🚀 Gemini 原生 API 双路由、安全过滤、高分辨率白名单

### 更早版本

- v1.3.x：SSE 流式、次数上限、供应商实时获取等（详见原版仓库）

---

## 🔧 常见问题

### Q: 怎么用 NovelAI 生图？
A: 「提供商管理」→「+ 添加提供商」→ 选 **NovalAI** 模板 → 填 Key 和 BaseURL（官方 `https://api.novelai.net` 或反代地址）→ 保存 → 进提供商加模型（如 `nai-diffusion-4-5-full`）→ 群里发 `>>文生图` 即可。用反代服务时打开「启用反代模式」。

### Q: 模型配置怎么填？
A: 在提供商「管理模型」里每行一个模型：`模型名;每次消耗配额;给LLM的说明;参数JSON`。如 `gpt-image-2;1;标准;`。群内可用 `>>切换模型` 切换。

### Q: 某些模型只支持 1K 怎么限制？
A: 该模型参数 JSON 里写 `{"sizes":["1K"]}` 即可，指令指定更高分辨率会报错，LLM 调用会自动夹取。

### Q: LLM（雨喵）能自主画图吗？
A: 能。主对话 LLM 支持 function calling 时会自动带上 `generate_image` 工具，用户说「帮我画一张…」即可触发，扩写提示词后调用；传 `selfie=true` 会自动追加自拍外貌特征。

### Q: WebUI 管理面板在哪？
A: AstrBot 管理面板 → 插件详情页 → Pages 打开。五个 Tab：提供商管理、次数管理、生图日志、画师预设、基础设置。

### Q: 生成失败显示「服务端繁忙或超时」？
A: 这是 API 服务端 5xx（如 504）的友好提示，可加大 `api_timeout` 或稍后重试/换模型；NovelAI 可先确认 key 预检是否通过。

---

## 🙏 致谢

本插件基于以下项目魔改，感谢原作者的无私贡献！

- **辉宝 / YumenoSayuri** - [astrbot_plugin_nova_newimage](https://github.com/YumenoSayuri/astrbot_plugin_nova_newimage)（本插件的基础，原版 v1.4.1）
- **shskjw** - astrbot_plugin_siliconflow-shouban（核心架构与预设系统）
- **喵喵** - gemini-25-image-openrouter（OpenRouter API 适配与图像响应解析）
- **维拉大佬** - 原始 JavaScript 版本实现

同时感谢 [AstrBot](https://github.com/Soulter/AstrBot) 项目提供的强大插件框架！

---

## 📄 开源协议

本项目 `astrbot_plugin_rainnya_image` 为 [astrbot_plugin_nova_newimage](https://github.com/YumenoSayuri/astrbot_plugin_nova_newimage)（原作者：辉宝）的派生作品，基于原项目魔改增强，采用 **AGPL-3.0** 协议开源。修改内容与原作者无关，如有问题请联系本仓库维护者。

**Made with ❤️ for 喵喵**
