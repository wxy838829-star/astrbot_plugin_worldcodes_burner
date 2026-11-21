# astrbot_plugin_worldcodes_burner

定时向当前配置的 LLM 提交**没有任何业务意义的短提示**，把 token 空转消耗掉的 AstrBot 插件。

默认关闭。只有配置里 `enabled: true`，或者管理员手动发 `/烧token 开`，才会开始跑。
所有额度都在运行时 clamp 到硬上限，配置文件里写多夸张都越不过去。

## 特性

- **默认关闭**：不启用就完全不动，加载时只写一行日志
- **可配节奏**：轮询间隔、单次输出上限、每日预算、单次运行时长
- **硬上限**：间隔 15–3600s，单次 ≤512 tokens，每日 ≤200000 tokens / ≤500 次，单次运行 ≤360 分钟
- **预算落盘**：`data/plugin_data/astrbot_plugin_worldcodes_burner/budget.json`，重载插件不清零
- **自动刹车**：预算到顶、请求次数到顶、单次运行超时、连续 3 次调不通，都会自己停并通知
- **随时可停**：`/烧token 停` 或 `/停烧`，直接取消后台任务
- **真账优先**：响应里带 usage 就用真值，没有就按字符数粗估，预占额度多退少补

## 指令

| 指令 | 说明 |
|---|---|
| `/烧token` | 看状态：是否在跑、今日用量、剩余预算 |
| `/烧token 开 [间隔秒] [单次tokens]` | 开启；参数可不填，填了会被 clamp 并回显实际值 |
| `/烧token 停` | 立即停止并清掉命令行临时参数 |
| `/烧token 帮助` | 用法与硬上限速查 |
| `/停烧` | 等价于 `/烧token 停` |

别名：`/burn`、`/空转`、`/burnstop`。

第一发在「间隔」之后，不会一开启就立刻打请求，留出后悔的时间。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 加载时是否自动开跑；需要重载插件生效 |
| `interval_seconds` | `60` | 轮询间隔，clamp 到 15–3600 |
| `max_tokens_per_request` | `16` | 单次输出上限，clamp 到 1–512 |
| `daily_budget_tokens` | `20000` | 每日 token 预算，clamp 到 500–200000 |
| `daily_max_requests` | `100` | 每日请求次数，clamp 到 1–500 |
| `max_runtime_minutes` | `30` | 单次开启最长运行，clamp 到 1–360 |
| `prompt` | `嗯` | 空转提示，最多 64 字符 |
| `system_prompt` | 空 | 可选，也会计费 |
| `provider_id` | 空 | 指定 provider，留空用当前会话默认 |
| `request_timeout` | `60` | 单次调用超时，clamp 到 5–300 |
| `permission` | `仅管理员` | 谁能开关 |
| `extra_user_ids` | `[]` | 额外放行名单 |
| `notify_session` | 空 | 自动停止时通知哪个会话，留空只写日志 |

## 安装

1. 把整个 `astrbot_plugin_worldcodes_burner` 目录放进 `data/plugins/`
2. 重载插件，确认日志里有「默认关闭」那一行
3. 需要时用 `/烧token 开 60 16` 手动试一轮，看状态里的用量是否在涨
4. 确认没问题，再考虑把 `enabled` 设成 `true`

## 预算与安全

- 每日预算按**本地日期**分桶，跨天自动清零，历史的每日用量最多保留 30 天
- 请求发出前先按 `单次上限 + 64 tokens` 预估占额度，拿到真实 usage 后回调差额
- 调用失败**不退还**预占额度：请求可能已经到达服务端，宁可少烧不可漏记
- 插件重载、进程重启都不会重置当天用量，别想靠重启绕过限额
- 所有日志只写用量与错误摘要，不写 Key、不写完整回复

## 测试

```bash
python _selftest_burner.py
```

不依赖 AstrBot 运行时：`budget.py`、`llm.py` 纯标准库，插件层用注入的假 `astrbot` 模块与假
provider 跑指令 → 开启 → 单次空转 → 停止 → 预算耗尽自动停的完整流程。

## 注意

这插件的唯一作用就是花钱。开之前先确认 `daily_budget_tokens` 是你愿意每天烧掉的量，
不用的时候记得 `/烧token 停`。
