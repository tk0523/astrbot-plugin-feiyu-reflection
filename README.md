# 大肥鱼 · 反思与状态

> AstrBot 角色状态、跨会话画像、群聊语言学习，以及每天凌晨 4 点的「自我反思」。
>
> 这是从 `astrbot_plugin_feiyu_state` 整理出的可迁移版本，插件名为
> `astrbot_plugin_feiyu_reflection`。

## 这是什么

这个插件不是单纯的“人设提示词”，而是在 AstrBot 之外维护一份轻量的角色状态和记忆：

- 记得同一个人在不同会话里的公开画像
- 群聊会学习语气、梗和表达习惯
- 私聊不会把原文搬进群，只保留人格、情绪和表达方式
- 每天 04:00 回顾前一天的聊天，生成
  - 对自己的反思
  - 对朋友的观察（仅私聊）
  - 对群聊的观察（公开安全版）
- 反思会注入后续对话，并作为 `private_companion` 生成每日日程时的内部素材
- 提供一个 AstrBot 插件页「反思与感悟」，可以查看最近 7 天的反思和当前状态

它适合“想让她有连续记忆，但不想把隐私内容到处搬”的部署方式。

## 功能

### 1. 跨会话画像

按 QQ 号维护：

```text
impression  对某个人的印象
traits      性格/说话特点
topics      常聊话题
count       互动次数
notes       /记住 的备注
```

同一个人的私聊和群聊会合并到同一份画像，但私聊原文不会反向进入群聊。

### 2. 群聊语言学习

每个群独立维护：

```text
style.tone       群整体语气
style.phrases    常用梗/口头禅
style.avoid      不建议学的攻击性表达
recent_digest    最近群聊（带发言人和 QQ）
samples          最近群聊文本样本
```

群风格只影响她的表达，不会把群友的私事带进私聊。

### 3. 每日反思

每天 **04:00**：

1. 从 AstrBot 的 `data_v4.db` 读取前一天：
   - 主人私聊
   - 群聊公开内容
2. 调用当前会话模型，生成短 JSON：

```json
{
  "self": "对自己的新认识，80–140 字",
  "self_group": "适合在群里说的自我反思，50–100 字",
  "friend": "对主人的观察，仅私聊，80–140 字",
  "group": "对群聊氛围/群友的观察，50–100 字"
}
```

3. 写入：

```text
data/plugin_data/astrbot_plugin_feiyu_reflection/state.json
  users["<主人QQ>"].daily_reflection
  users["<主人QQ>"].reflection_history   # 最近 7 天
```

### 4. 反思的作用范围

| 内容 | 私聊 | 群聊 |
|---|---|---|
| `self` | 注入 | 不注入 |
| `self_group` | 注入 | 注入 |
| `friend` | 注入 | **永不注入** |
| `group` | 注入 | 注入 |

群聊只使用公开安全版，避免把私聊对象、称呼和秘密带进群。

### 5. 与陪伴插件联动

如果同时启用了 `astrbot_plugin_private_companion`：

- 每日日程生成时，会读取最新反思作为内部背景
- 陪伴面板的「06 观察」页可以显示「大肥鱼的反思」卡片
- 反思不是硬改 energy 数值，而是通过提示词和日程素材影响语气、选择和生活安排

陪伴面板卡片是可选集成，见 `integrations/companion-panel/`。

### 6. 插件页

AstrBot 管理面板：

```text
插件 → astrbot_plugin_feiyu_reflection → 反思与感悟
```

页面包含：

- 最近一次自我反思
- 对朋友的观察（仅私聊）
- 群聊安全版
- 最近 7 天历史
- 当前画像
- 当前私聊/群聊状态
- 行为影响标记

页面是只读的，不会直接修改状态。

## 安装

### 环境

- AstrBot `>= 4.29`
- Python `>= 3.10`
- 推荐使用 AstrBot 自带 venv

### 步骤

1. 把整个目录放到：

```text
<AstrBot>/data/plugins/astrbot_plugin_feiyu_reflection
```

2. 安装依赖：

```bash
# Windows
<AstrBot>\venv\Scripts\python.exe -m pip install -r requirements.txt

# Linux/macOS
<AstrBot>/venv/bin/python -m pip install -r requirements.txt
```

3. 重启 AstrBot。

4. 在管理面板确认插件已启用：

```text
http://127.0.0.1:6185
→ 插件
→ astrbot_plugin_feiyu_reflection
```

5. 打开「反思与感悟」页。

首次启动会在 60 秒后补跑前一天的反思；之后每天 04:00 自动运行。

## 配置

### 必须按你的账号修改的常量

这个包默认按“001 / 大肥鱼”的部署整理，里面有个人 ID。迁移到你自己的 Bot 时，请修改 `main.py` 顶部：

```python
MASTER_IDS = {"3282993487"}          # 主人 QQ
PROACTIVE_GROUP_IDS = {831955733}    # 主动发言/反思读取的群
SEMANTIC_GROUP_IDS = {831955733}     # 语义唤醒群
BILI_GROUP_IDS = {831955733}         # 群 B 站分享群
BOT_ALIASES = ("大肥鱼", "肥鱼", "鱼鱼", "深海在逃女仆")
```

以及 `web_api.py`：

```python
MASTER_ID = "3282993487"
```

如果只改插件名、不改这些 ID，插件仍然会按原作者的主人/群运行。

### 人格提示词

建议在 AstrBot 人格中加入：

```text
你会在每天凌晨回顾前一天，形成对自己的反思和对朋友的观察。
这些反思是你自己的笔记，不是用户告诉你的事实。
群聊里只使用公开安全版，不提私聊对象、秘密和原话。
```

### 依赖插件

- 必需：AstrBot 本体
- 可选：
  - `astrbot_plugin_private_companion`：日程/状态/陪伴面板联动
  - `astrbot_plugin_bili_scroll`：独立 B 站私信推送（本插件也有自己的 B 站分享逻辑）
  - 表情包/图片插件：按需

## 数据与迁移

### 数据位置

```text
<AstrBot>/data/plugin_data/astrbot_plugin_feiyu_reflection/state.json
```

插件不会把 `state.json` 打包进仓库，避免泄露聊天和用户画像。

### 从旧插件迁移

如果之前使用的是 `astrbot_plugin_feiyu_state`：

1. 停止 AstrBot
2. 把旧状态复制到新目录：

```bash
mkdir -p data/plugin_data/astrbot_plugin_feiyu_reflection
cp data/plugin_data/astrbot_plugin_feiyu_state/state.json \
   data/plugin_data/astrbot_plugin_feiyu_reflection/state.json
```

3. 禁用/移走旧插件目录，避免两个插件同时注册事件
4. 启动 AstrBot，确认反思页和画像正常

旧日志标签仍然叫 `feiyu_state`，只是日志名，不影响运行。

### 重置

删除：

```text
data/plugin_data/astrbot_plugin_feiyu_reflection/state.json
```

重启后会从空白状态开始。请先备份。

## 目录结构

```text
astrbot_plugin_feiyu_reflection/
├─ main.py                         # 状态、画像、群聊学习、反思任务、提示注入
├─ web_api.py                      # 反思页只读 API
├─ metadata.yaml                   # AstrBot 插件元数据
├─ requirements.txt
├─ README.md
├─ LICENSE
├─ assets/stickers/                # 内置表情素材
├─ views/反思/index.html           # AstrBot 插件页
├─ .astrbot-plugin/i18n/zh-CN.json
└─ integrations/companion-panel/   # 可选的陪伴面板卡片片段
```

## 反思页 API

只读接口：

```text
GET /api/v1/plugins/extensions/astrbot_plugin_feiyu_reflection/reflection/overview
```

由 AstrBot 登录鉴权保护。返回：

```json
{
  "ok": true,
  "reflection": {},
  "history": [],
  "profile": {},
  "sessions": [],
  "effect": {
    "private_prompt": true,
    "group_prompt": true,
    "friend_private_only": true
  }
}
```

## 隐私边界

- 私聊原文只用于该私聊的上下文与反思生成
- `friend` 观察不会进入群聊
- 群聊只使用 `self_group + group`
- `/记住` 的备注只在私聊注入
- 反思页需要 AstrBot 登录，不是公开网页
- 不要把 `state.json` 提交到公开仓库

## 常见问题

### 反思页显示“还没有生成反思”

- 确认当天/前一天有聊天记录
- 确认 `data_v4.db` 路径正确
- 查看日志：

```text
[feiyu_state] daily reflection ...
```

- 首次启动会等 60 秒再补跑

### 群聊没有出现反思

- 检查 `self_group` 是否生成
- 群聊只注入 `self_group + group`
- 如果没有 `self_group`，不会拿 `friend` 顶替

### 改名后画像为空

旧状态还在旧插件目录。按“从旧插件迁移”复制 `state.json`。

### 两个插件同时回复

不要同时启用旧 `astrbot_plugin_feiyu_state` 和新 `astrbot_plugin_feiyu_reflection`，它们会重复注册事件。

## 开发

```bash
python -m py_compile main.py web_api.py
```

页面调试：

```text
http://127.0.0.1:6185
```

## License

MIT。内置表情素材仅用于角色表达，请确认你拥有相应素材的使用权。
