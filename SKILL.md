---
name: binance-announcement-monitor
description: 当用户需要监控币安公告、将公告标题与正文翻译成中文、通过 r.jina.ai 获取公告全文、由 agent 提炼重点并分析影响、再推送到飞书时，使用此技能。
---

# 币安公告监控

通过 WebSocket 监听币安公告流，自动解析公告链接，使用 `r.jina.ai` 获取正文，将英文翻译为中文，使用 LLM 提炼总结与影响分析，最后推送到飞书机器人 Webhook。

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

- `OPENAI_API_KEY`（用于 LLM 总结/分析）
- `OPENAI_BASE_URL`（默认 `https://api.openai.com/v1`）
- `OPENAI_MODEL`（默认 `gpt-4o-mini`）
- `BINANCE_TOPIC`（默认 `com_announcement_en`）
- `BINANCE_WS_URL`（默认 `wss://api.binance.com/sapi/wss`）
- `ANNOUNCEMENT_PAGE_TEMPLATE`（默认 `https://cache.bwe-ws.com/bn-{id}`）
- `BINANCE_START_ANN_ID`（默认 `1034`）
- `MAX_ID_SCAN`（默认 `30`，每次最多向后探测的 ID 数）

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
7. 对中文正文做要点提炼与影响分析（配置了 LLM 时走 LLM，否则走本地兜底摘要）。
8. 推送飞书交互卡片，包含：
   - 原始英文标题
   - 中文标题
   - 中文要点总结
   - 中文影响分析
   - 原文链接
