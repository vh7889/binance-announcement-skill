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


def clean_article_text(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines()]
    cleaned_lines = []
    skip_prefixes = (
        "title:",
        "source:",
        "url source:",
        "published time:",
        "markdown content:",
    )

    in_body = False
    for line in lines:
        if not line:
            if in_body and cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue

        lower = line.lower()
        if lower.startswith("markdown content:"):
            in_body = True
            continue
        if not in_body and lower.startswith(skip_prefixes):
            continue
        if line.startswith("![]("):
            continue
        if re.fullmatch(r"\[[^\]]+\]\([^)]+\)", line):
            continue
        cleaned_lines.append(line)
        in_body = True

    text = "\n".join(cleaned_lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def is_low_priority_notice(line: str) -> bool:
    normalized = normalize_title(line)
    low_priority_patterns = (
        "一般性公告",
        "产品和服务可能在您所在的地区不可用",
        "此处提到的产品和服务可能在您所在的地区不可用",
        "may not be available in your region",
        "not available in your region",
        "where you are located",
        "general announcement",
    )
    return any(pattern in normalized for pattern in low_priority_patterns)


def prioritize_summary_lines(lines: list[str]) -> list[str]:
    primary = []
    secondary = []
    seen = set()

    for raw_line in lines:
        line = raw_line.strip(" #-*")
        if not line:
            continue
        key = normalize_title(line)
        if not key or key in seen:
            continue
        seen.add(key)

        if len(line) < 8:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            continue

        if is_low_priority_notice(line):
            secondary.append(line)
        else:
            primary.append(line)

    return primary + secondary


def infer_analysis_from_text(title_zh: str, body_zh: str) -> str:
    combined = f"{title_zh}\n{body_zh}"
    keyword_rules = [
        (
            ("上线", "listing", "launchpool", "launchpad", "上架"),
            "该公告偏上新或上线活动，短期内更可能带来相关币种关注度和成交量提升，但需防范消息兑现后的回落。",
        ),
        (
            ("下架", "delist", "移除", "停止交易"),
            "该公告偏下架或交易支持收缩，通常对相关币种流动性和短线情绪不利，持仓用户需要重点关注停止交易与提现时间。",
        ),
        (
            ("维护", "maintenance", "暂停", "恢复"),
            "该公告偏平台维护或功能切换，主要影响用户操作时点与资金调度，对币价本身未必构成直接利空或利多。",
        ),
        (
            ("空投", "奖励", "voucher", "奖池", "campaign"),
            "该公告偏运营活动，通常更影响短期关注度和参与热度，实际价格影响取决于奖励规模、解锁条件和项目基本面。",
        ),
        (
            ("合约", "杠杆", "futures", "margin"),
            "该公告偏衍生品或杠杆功能调整，可能提升短线波动和交易热度，需关注风控参数与交易时间生效点。",
        ),
    ]

    lowered = combined.lower()
    for keywords, analysis in keyword_rules:
        if any(keyword in combined or keyword in lowered for keyword in keywords):
            return analysis
    return "该公告更像常规信息披露，建议优先确认生效时间、适用对象和是否影响交易、充提或持仓安排。"


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


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


def local_fallback_summary(body_zh: str, title_zh: str = "") -> Tuple[str, str]:
    lines = [line.strip(" #-*") for line in body_zh.splitlines() if line.strip()]
    filtered = []
    for line in lines:
        lower = line.lower()
        if lower.startswith(("title:", "source:", "published time:", "markdown content:")):
            continue
        filtered.append(line)

    prioritized = prioritize_summary_lines(filtered)
    brief = "；".join(prioritized[:4]) if prioritized else "未提取到有效正文，建议点击原文查看。"
    analysis = infer_analysis_from_text(title_zh, body_zh)
    return truncate(brief, 500), analysis


async def llm_refine(session: aiohttp.ClientSession, title_zh: str, body_zh: str) -> Tuple[str, str]:
    if not OPENAI_API_KEY:
        return local_fallback_summary(body_zh, title_zh)

    prompt = (
        "你是资深加密市场分析师。请基于给定公告正文，输出中文 JSON："
        '{"summary":"不超过220字，提炼关键事实","analysis":"不超过180字，分析潜在市场或用户影响"}。'
        "总结时优先保留真正有动作或影响的信息，不要把“地区不可用”“一般性公告”这类免责声明放在前面；"
        "除非它直接影响用户操作，否则不要写进 summary。"
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
                return local_fallback_summary(body_zh, title_zh)
            data = await resp.json(content_type=None)
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            parsed = extract_json_object(content)
            if not parsed:
                return local_fallback_summary(body_zh, title_zh)
            summary = truncate(str(parsed.get("summary", "")).strip(), 500)
            analysis = truncate(str(parsed.get("analysis", "")).strip(), 300)
            if summary and analysis:
                return summary, analysis
    except Exception:
        pass
    return local_fallback_summary(body_zh, title_zh)


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
    cleaned_article_text = clean_article_text(article_text_en)
    body_zh = await translate_text(session, truncate(cleaned_article_text, 7000))
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
