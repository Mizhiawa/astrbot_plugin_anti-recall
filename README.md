# astrbot_plugin_anti_recall

防撤回查询插件（AstrBot + NapCat / aiocqhttp）。

机器人平时会把收到的消息缓存在内存里；当某条消息被撤回时，插件会用缓存里的内容生成一条撤回记录。
之后在会话里发送查询指令，就能看到最近一段时间内被撤回消息的**发送者**和**内容**。

仓库地址：<https://github.com/Mizhiawa/astrbot_plugin_anti-recall>

## 安装

**方式一：从链接安装（推荐）**

AstrBot 面板 → 插件管理 → 「从链接安装」→ 填入：

```
https://github.com/Mizhiawa/astrbot_plugin_anti-recall
```

安装完成后插件目录为 `data/plugins/astrbot_plugin_anti_recall`，重启或重载插件即可生效。

**方式二：手动安装**

把本目录放到 AstrBot 的 `data/plugins/` 下，然后在插件管理页重载。

其他说明：

- 配置 NapCat 上报通知事件（默认配置即可；若你改过上报类型，请确保 `notice` 事件正常上报）。
- 无需安装额外依赖。
- `metadata.yaml` 中的 `repo` 字段指向本仓库，因此也可以在面板里使用「检查更新 / 更新插件」。

## 使用

| 指令 | 说明 |
| --- | --- |
| `/防撤回` | 输出当前会话最近被撤回的消息 |

- 指令需要 AstrBot 的唤醒前缀（默认 `/`）。如果已把 wake_prefix 设为空，直接发 `防撤回` 即可。
- 查询结果只包含**当前会话**（同一个群 / 同一私聊）的撤回记录，不会把其他群的记录带到本群。
- 输出示例：

```
最近 10 分钟内共有 2 条消息被撤回：
1. [21:05:40] 张三(123456789)：今晚吃什么
2. [21:07:12] 李四(987654321)：[图片]
```

## 配置

在插件配置页可调整（见 `_conf_schema.json`）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `window_minutes` | 10 | 查询时间窗口（分钟） |
| `cache_minutes` | 30 | 消息缓存时长（分钟），需不小于查询窗口 |
| `max_items` | 20 | 单次最多输出条数 |
| `only_admin` | false | 是否仅 AstrBot 管理员可用 |

## 实现说明与限制

- 依赖 OneBot v11 的撤回通知（`notice.group_recall` / `notice.friend_recall`）与 aiocqhttp 适配器；其他平台不适用。
- 只有在**机器人收到该消息之后**才谈得上还原内容。机器人离线期间发送并撤回、或缓存时长之外撤回的消息，只会记录到发送者，内容显示为 `[消息未在缓存中，内容不可见]`。
- 缓存保存在内存中，重启后清空；文字以外的消息以占位符显示（如 `[图片]`、`[At:123456]`）。
- 撤回记录的监听器是全局的，但不会触发默认的 LLM 回复；仅 `防撤回` 指令会产生回复。

## 文件结构

```
astrbot_plugin_anti-recall/
├── main.py            # 插件主体：消息缓存、撤回记录、查询指令
├── metadata.yaml      # 插件元数据（name / desc / version / author / repo）
├── _conf_schema.json  # 插件配置项定义
└── README.md
```

## 版本

- v1.0.0：首个版本，支持群聊/私聊撤回记录查询。
