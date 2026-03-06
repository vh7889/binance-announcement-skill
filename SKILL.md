---
name: binance-announcement-monitor
description: 当用户需要监控币安公告、将公告标题与正文翻译成中文、通过 r.jina.ai 获取公告全文、由 agent 提炼重点并分析影响、再推送到飞书时，使用此技能。
---

# 币安公告监控

通过 WebSocket 监听币安公告流，自动解析公告链接，使用 `r.jina.ai` 获取正文，将英文翻译为中文，再调用你的 agent 进行总结与分析，最后推送到飞书机器人 Webhook。

## Agent 目标

当 agent 已经拿到以下输入后：

- 英文标题
- 中文标题
- 英文正文
- 中文正文

agent 需要优先基于“标题 + 正文”生成面向飞书阅读场景的中文结论，而不是简单拼接原文或照搬网页句子。

## 强制规则

- `agent` 必须参与总结。
- 未拿到 `agent` 返回的有效 `summary + analysis` 时，禁止推送飞书。
- 默认开启强制模式：`AGENT_SUMMARY_REQUIRED=1`。
- 默认重试直到成功：`AGENT_RETRY_MAX_ATTEMPTS=0`。

## 适用场景

- 用户要求做币安公告监控。
- 用户要求公告链接自动解析。
- 用户要求“翻译 + 总结分析 + 飞书推送”一体化流程。

## 文件

- 脚本：`scripts/binance_announcement_monitor.py`

## 环境配置

必填：

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `FEISHU_WEBHOOK`

可选：

- `BINANCE_TOPIC`（默认 `com_announcement_en`）
- `BINANCE_WS_URL`（默认 `wss://api.binance.com/sapi/wss`）
- `ANNOUNCEMENT_PAGE_TEMPLATE`（默认 `https://cache.bwe-ws.com/bn-{id}`）
- `BINANCE_START_ANN_ID`（默认 `1034`）
- `MAX_ID_SCAN`（默认 `30`，每次最多向后探测的 ID 数）
- `AGENT_SUMMARY_URL`（agent 总结接口地址，返回 `summary` 和 `analysis`）
- `AGENT_SUMMARY_TOKEN`（可选，agent 接口鉴权 token）
- `AGENT_SUMMARY_REQUIRED`（默认 `1`；`1`=agent 失败则不推送，`0`=agent 失败时允许本地兜底）
- `AGENT_RETRY_INTERVAL_SEC`（默认 `20`，agent 失败后的重试间隔秒数）
- `AGENT_RETRY_MAX_ATTEMPTS`（默认 `0`，`0` 表示无限重试直到成功）

## 运行

```bash
python /Users/gzy/Documents/code/codex/.agents/skills/binance-announcement-monitor/scripts/binance_announcement_monitor.py
```

## 执行流程

1. 连接 Binance WebSocket 并订阅公告主题。
2. 通过 `publishDate` 识别新公告。
3. 从 `NEXT_ANN_ID` 开始，依次探测 `ANNOUNCEMENT_PAGE_TEMPLATE` 对应页面，并通过 `r.jina.ai` 读取页面文本与标题。
4. 若当前 ID 标题不匹配，则继续 `ID+1` 直到匹配。
5. 当且仅当“标题匹配”且“下一个 ID 页面状态为 404”时，认定当前 ID 为正确公告编号，并将下次起点更新为 `正确ID+1`。
6. 只有确认正确 ID 后，才读取正文并翻译成中文。
7. 调用 `AGENT_SUMMARY_URL`，由 agent 基于“中文标题 + 中文正文”生成中文结论。
8. 当 `AGENT_SUMMARY_REQUIRED=1` 时，agent 未返回有效结果则进入重试队列：按 `AGENT_RETRY_INTERVAL_SEC` 间隔继续重试，直到成功（或达到 `AGENT_RETRY_MAX_ATTEMPTS`）。
9. 推送飞书交互卡片，包含：
   - 原始英文标题
   - 中文标题
   - 中文要点总结
   - 中文影响分析
   - 原文链接

## Agent 输出规则

当使用 agent 生成结论时，遵循以下规则：

1. `要点总结` 或 `Ai分析` 必须是 agent 自己提炼后的中文结果，不能直接复制网页原句，也不能把抓取页头、来源、链接说明写进去。
2. 优先提炼“这条公告到底说了什么”，包括：
   - 新功能或新活动是什么
   - 面向哪些用户
   - 用户能做什么
   - 是否有时间点、参与方式、资格条件、奖励信息
3. 若公告是产品介绍或运营活动，摘要重点放在“新增能力、使用方式、用户收益”，不要先写免责声明。
4. 若公告涉及交易、下架、维护、充值提现、合约、杠杆、风控参数，摘要重点放在“生效时间、受影响对象、需要采取的动作”。
5. `地区不可用`、`一般性公告`、法律/地域限制等句子默认视为低优先级信息：
   - 不要放在第一句
   - 不要占据摘要主体
   - 只有当它会直接影响用户是否可参与时，才允许在最后一句简短提及
6. `Ai分析` 应该是 agent 的归纳判断，不是套话。需要结合公告类型给出简短结论，例如：
   - 活动类：更偏流量和参与度提升，对价格影响未必直接
   - 上线类：短期可能提升关注度和成交量
   - 下架类：需重点关注停止交易/提现时间
   - 维护类：重点影响操作时点和资金调度
7. 输出应简洁，适合飞书卡片直接展示：
   - `要点总结` 建议 80-160 字
   - `Ai分析` 建议 50-120 字

## 推荐字段含义

- `要点总结`：客观事实提炼，回答“发生了什么”
- `Ai分析`：简短判断，回答“这意味着什么”

若只能保留一个字段，优先保留 `Ai分析`，但内容里必须包含核心事实，不能只写空泛观点。

## 可靠性说明

- 监听会持续运行并继续接收新公告。
- 每条公告的“agent 总结 + 推送”在后台独立重试，不会阻塞后续公告监听。
