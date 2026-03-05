import asyncio
import difflib
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
import aiohttp


BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

BINANCE_TOPIC = os.getenv("BINANCE_TOPIC", "com_announcement_en")
BINANCE_WS_URL = os.getenv("BINANCE_WS_URL", "wss://api.binance.com/sapi/wss")
BINANCE_ARTICLE_LIST_API = os.getenv(
    "BINANCE_ARTICLE_LIST_API",
    "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
)
ANNOUNCEMENT_PAGE_TEMPLATE = os.getenv(
    "ANNOUNCEMENT_PAGE_TEMPLATE",
    "https://cache.bwe-ws.com/bn-{id}",
)
BINANCE_START_ANN_ID = int(os.getenv("BINANCE_START_ANN_ID", "1034"))
MAX_ID_SCAN = int(os.getenv("MAX_ID_SCAN", "30"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

GOOGLE_TRANSLATE_API = "https://translate.googleapis.com/translate_a/single"
JINA_PREFIX = "https://r.jina.ai/http://"


LAST_PUBLISHED: Optional[int] = None
NEXT_ANN_ID: int = BINANCE_START_ANN_ID


def sign_query(params: str, secret: str) -> str:
    return hmac.new(secret.encode(), params.encode(), hashlib.sha256).hexdigest()


def unix_ms_to_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def find_possible_url(payload: Dict[str, Any]) -> Optional[str]:
    keys = ("webUrl", "url", "articleUrl", "link", "redirectUrl")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value

    for value in payload.values():
        if not isinstance(value, str):
            continue
        match = re.search(r"https?://[^\s\"'>]+", value)
        if match:
            return match.group(0)
    return None


def normalize_title(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def title_match(ws_title: str, page_title: str) -> bool:
    a = normalize_title(ws_title)
    b = normalize_title(page_title)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= 0.72


def extract_page_title(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return ""

    for line in lines[:30]:
        lower = line.lower()
        if lower.startswith("title:"):
            return line.split(":", 1)[1].strip()

    for line in lines[:30]:
        if line.startswith("#"):
            return line.lstrip("#").strip()

    return lines[0]


async def get_json(session: aiohttp.ClientSession, url: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
    try:
        async with session.get(url, timeout=20, **kwargs) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception:
        return None


async def resolve_announcement_url(
    session: aiohttp.ClientSession,
    payload: Dict[str, Any],
    title: str,
    publish_ts: int,
) -> Optional[str]:
    found = find_possible_url(payload)
    if found:
        return found

    params = {
        "type": "1",
        "catalogId": str(payload.get("catalogId", "48")),
        "pageNo": "1",
        "pageSize": "30",
    }
    data = await get_json(session, BINANCE_ARTICLE_LIST_API, params=params)
    if not data:
        return None

    articles = (
        data.get("data", {})
        .get("catalogs", [{}])[0]
        .get("articles", [])
    )
    if not isinstance(articles, list):
        return None

    target = normalize_title(title)
    best_url = None
    best_score = 10**18
    for item in articles:
        if not isinstance(item, dict):
            continue
        item_title = str(item.get("title", ""))
        item_norm = normalize_title(item_title)
        if target and target not in item_norm and item_norm not in target:
            continue
        code = item.get("code")
        release = int(item.get("releaseDate", 0) or 0)
        if code:
            url = f"https://www.binance.com/en/support/announcement/{code}"
        else:
            url = item.get("webUrl")
        if not url:
            continue
        score = abs(release - publish_ts) if release else 0
        if score < best_score:
            best_score = score
            best_url = url
    return best_url


async def translate_text(session: aiohttp.ClientSession, text: str) -> str:
    text = text.strip()
    if not text:
        return ""

    params = {
        "client": "gtx",
        "sl": "en",
        "tl": "zh-CN",
        "dt": "t",
        "q": text,
    }
    try:
        async with session.get(GOOGLE_TRANSLATE_API, params=params, timeout=20) as resp:
            if resp.status != 200:
                return text
            data = await resp.json(content_type=None)
            if isinstance(data, list) and data and isinstance(data[0], list):
                translated = "".join(
                    part[0] for part in data[0] if isinstance(part, list) and part and isinstance(part[0], str)
                )
                return translated or text
    except Exception:
        return text
    return text


async def fetch_article_text(session: aiohttp.ClientSession, article_url: str) -> str:
    cleaned = article_url.replace("https://", "").replace("http://", "")
    jina_url = f"{JINA_PREFIX}{cleaned}"
    try:
        async with session.get(jina_url, timeout=30) as resp:
            if resp.status != 200:
                return ""
            text = await resp.text()
            return text.strip()
    except Exception:
        return ""


async def get_source_status(session: aiohttp.ClientSession, source_url: str) -> int:
    try:
        async with session.get(source_url, timeout=15, allow_redirects=True) as resp:
            return resp.status
    except Exception:
        return 0


async def probe_article_by_incremental_id(
    session: aiohttp.ClientSession,
    ws_title: str,
) -> Tuple[Optional[int], Optional[str], str]:
    global NEXT_ANN_ID

    start_id = NEXT_ANN_ID
    end_id = start_id + max(1, MAX_ID_SCAN)
    for ann_id in range(start_id, end_id):
        source_url = ANNOUNCEMENT_PAGE_TEMPLATE.format(id=ann_id)
        raw_text = await fetch_article_text(session, source_url)
        if not raw_text:
            continue

        page_title = extract_page_title(raw_text)
        if not title_match(ws_title, page_title):
            continue

        next_url = ANNOUNCEMENT_PAGE_TEMPLATE.format(id=ann_id + 1)
        next_status = await get_source_status(session, next_url)
        if next_status == 404:
            NEXT_ANN_ID = ann_id + 1
            return ann_id, source_url, raw_text

        print(
            f"[INFO] 标题命中但未确认尾部ID: ann_id={ann_id}, next_status={next_status}, continue scanning"
        )
    return None, None, ""


def local_fallback_summary(body_zh: str) -> Tuple[str, str]:
    lines = [line.strip() for line in body_zh.splitlines() if line.strip()]
    brief = "；".join(lines[:4]) if lines else "未提取到正文，建议点击原文查看。"
    analysis = "该公告可能对币种波动、交易行为或平台功能产生短期影响，建议结合持仓和风险偏好执行。"
    return truncate(brief, 500), analysis


async def llm_refine(session: aiohttp.ClientSession, title_zh: str, body_zh: str) -> Tuple[str, str]:
    if not OPENAI_API_KEY:
        return local_fallback_summary(body_zh)

    prompt = (
        "你是资深加密市场分析师。请基于给定公告正文，输出中文 JSON："
        '{"summary":"不超过220字，提炼关键事实","analysis":"不超过180字，分析潜在市场或用户影响"}。'
        "只输出 JSON，不要额外文本。"
    )
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "你擅长提炼交易所公告并给出客观影响分析。"},
            {"role": "user", "content": f"{prompt}\n\n标题：{title_zh}\n\n正文：{truncate(body_zh, 6000)}"},
        ],
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=40,
        ) as resp:
            if resp.status != 200:
                return local_fallback_summary(body_zh)
            data = await resp.json(content_type=None)
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            parsed = json.loads(content)
            summary = truncate(str(parsed.get("summary", "")).strip(), 500)
            analysis = truncate(str(parsed.get("analysis", "")).strip(), 300)
            if summary and analysis:
                return summary, analysis
    except Exception:
        pass
    return local_fallback_summary(body_zh)


async def send_feishu_card(
    session: aiohttp.ClientSession,
    title_en: str,
    title_zh: str,
    publish_ts: int,
    article_url: str,
    summary_zh: str,
    analysis_zh: str,
) -> None:
    header = truncate(f"币安公告 | {title_zh or title_en}", 60)
    content = (
        f"**英文标题**: {truncate(title_en, 180)}\n\n"
        f"**中文标题**: {truncate(title_zh, 180)}\n\n"
        f"**发布时间**: {unix_ms_to_str(publish_ts)}\n\n"
        f"**要点总结**: {truncate(summary_zh, 600)}\n\n"
        f"**影响分析**: {truncate(analysis_zh, 300)}"
    )

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": header},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "查看公告原文"},
                            "url": article_url,
                        }
                    ],
                },
            ],
        },
    }

    for _ in range(3):
        try:
            async with session.post(FEISHU_WEBHOOK, json=payload, timeout=15) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        await asyncio.sleep(2)


async def handle_announcement(
    session: aiohttp.ClientSession,
    inner: Dict[str, Any],
) -> None:
    title_en = str(inner.get("title", "Untitled")).strip()
    publish_ts = int(inner.get("publishDate", 0) or 0)
    if not title_en or not publish_ts:
        return

    matched_id, article_url, article_text_en = await probe_article_by_incremental_id(session, title_en)
    if not article_url:
        print(f"[WARN] 未确认正确公告ID（需命中标题且下一个ID=404）: {title_en}")
        return

    print(f"[INFO] 公告ID匹配成功: {matched_id} -> {article_url}")

    title_zh = await translate_text(session, title_en)
    body_zh = await translate_text(session, truncate(article_text_en, 7000))
    summary_zh, analysis_zh = await llm_refine(session, title_zh, body_zh)

    await send_feishu_card(
        session=session,
        title_en=title_en,
        title_zh=title_zh,
        publish_ts=publish_ts,
        article_url=article_url,
        summary_zh=summary_zh,
        analysis_zh=analysis_zh,
    )
    print(f"[OK] 已推送飞书: {title_en}")


async def connect_binance() -> None:
    global LAST_PUBLISHED

    if not BINANCE_API_KEY or not BINANCE_API_SECRET or not FEISHU_WEBHOOK:
        raise RuntimeError(
            "Missing required env: BINANCE_API_KEY, BINANCE_API_SECRET, FEISHU_WEBHOOK"
        )

    while True:
        try:
            timestamp = int(time.time() * 1000)
            random_str = uuid.uuid4().hex
            recv_window = 30000

            params = f"random={random_str}&recvWindow={recv_window}&timestamp={timestamp}&topic={BINANCE_TOPIC}"
            signature = sign_query(params, BINANCE_API_SECRET)
            ws_url = f"{BINANCE_WS_URL}?{params}&signature={signature}"

            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    ws_url,
                    headers={"X-MBX-APIKEY": BINANCE_API_KEY},
                    heartbeat=30,
                ) as ws:
                    print("[INFO] Binance WS connected")
                    await ws.send_json({"command": "SUBSCRIBE", "value": BINANCE_TOPIC})

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue

                            if data.get("type") != "DATA":
                                continue
                            data_str = data.get("data")
                            if not data_str:
                                continue
                            try:
                                inner = json.loads(data_str)
                            except Exception:
                                continue

                            ts = int(inner.get("publishDate", 0) or 0)
                            if not ts:
                                continue
                            if LAST_PUBLISHED is not None and ts == LAST_PUBLISHED:
                                continue

                            LAST_PUBLISHED = ts
                            await handle_announcement(session, inner)

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
        except Exception as e:
            print(f"[ERROR] {e}, reconnect in 5s")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(connect_binance())
