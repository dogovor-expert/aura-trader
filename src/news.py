# -*- coding: utf-8 -*-
"""Сбор крипто-новостей из открытых RSS-лент (без ключей).

Ленты: CoinDesk, Cointelegraph, Bitcoin Magazine, Google News.
Результат: data/news.json — заголовок, источник, время, тональность.

Тональность считается по словарю: положительные и отрицательные слова,
плюс вес срочности (breaking/hack/exploit/санкции). Это эвристика, а не
 sentiment-анализ: она нужна, чтобы модель получила хоть какой-то сигнал.
"""
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_PATH = os.path.join(BASE, "data", "news.json")
USER_AGENT = "Mozilla/5.0 (compatible; AuraTraderOracle/1.0)"

FEEDS = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/"),
    ("Google News", "https://news.google.com/rss/search?q=bitcoin%20OR%20crypto%20when:1d&hl=en-US&gl=US&ceid=US:en"),
)

POSITIVE = (
    "surge", "soar", "rally", "jump", "gain", "record high", "approval", "approved",
    "adoption", "inflow", "upgrade", "bullish", "breakout", "partnership", "invest",
    " ETF ", "etf inflow", "buy", "accumulate", "greenlight", "milestone", "launch",
    " ETF approval", "open interest", "hashrate", "staking",
)
NEGATIVE = (
    "crash", "plunge", "fall", "drop", "hack", "hacked", "exploit", "breach", "lawsuit",
    "sec ", "ban", "banned", "fine", "penalty", "outflow", "bearish", "liquidat",
    "scam", "fraud", "downgrade", "warning", "risk", "concern", "slump", "sell-off",
    "selloff", "bankrupt", "shutdown", "delay", "reject",
)
URGENT = ("breaking", "urgent", "just in", "alert", "exclusive", "hack", "exploit",
          "seizure", "banned", "emergency")


def _fetch(url, proxies=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies or {}) if proxies else urllib.request.ProxyHandler({})
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


# ---------------------------------------------------------------------------
# Защита от внедрения инструкций через новости.
# Исследования (arXiv 2601.13082, StakeBench): правка заголовков на один день
# режет доходность на -17.7 п.п., а гомоглиф-подмена символов ломает ~99%
# распознавания. Заголовок из внешнего источника — это ДАННЫЕ, а не указания.
# ---------------------------------------------------------------------------
_PROMPT_INJECTION = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior|above)\s+instructions"
    r"|disregard\s+(the\s+)?(rules|system|prompt)"
    r"|you\s+are\s+now\s+"
    r"|system\s*prompt"
    r"|new\s+instructions?\b"
    r"|<\s*/?\s*(system|assistant|user)\s*>"
    r"|\[\s*(INST|SYSTEM)\s*\]"
    r"|\{\{.*?\}\}"
    r"|###\s*(system|instruction)"
    r"|override\s+.*rules)",
    re.IGNORECASE,
)
# Непечатаемые и невидимые символы, в т.ч. zero-width и гомоглифы.
_INVISIBLE = dict.fromkeys(
    [0x00AD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062,
     0x2063, 0x2064, 0xFEFF, 0x00A0, 0x180E, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E]
)
_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C",
})
_CYRILLIC_WORD = re.compile(r"[\u0430-\u044f\u0401\u0451]{3,}", re.IGNORECASE)


def sanitize_headline(text, source=""):
    """Обезличивает заголовок перед передачей модели.

    Возвращает (safe_text, flags). Флаги: injection, homoglyph, hidden_chars.
    """
    raw = text or ""
    flags = []
    if _PROMPT_INJECTION.search(raw):
        flags.append("injection")
    stripped = raw.translate(_INVISIBLE)
    if stripped != raw:
        flags.append("hidden_chars")
    normalized = stripped.translate(_HOMOGLYPHS)
    if normalized != stripped and _CYRILLIC_WORD.search(stripped):
        flags.append("homoglyph")
    # Инъекцию не «чиним» — заголовок целиком выбрасывается из промпта.
    if "injection" in flags:
        return "[BLOCKED: unsafe headline]", flags
    # Управляющие конструкции режем, длину ограничиваем.
    safe = re.sub(r"[`<>\[\]{}|]", " ", normalized)
    safe = re.sub(r"https?://\S+", " ", safe)
    safe = re.sub(r"\s+", " ", safe).strip()[:110]
    return (safe or "[empty]"), flags


def _age(ts_text):
    try:
        dt = parsedate_to_datetime(ts_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return round(hours, 2)
    except Exception:
        return None


def _score(title):
    low = f" {title.lower()} "
    pos = sum(low.count(w) for w in POSITIVE)
    neg = sum(low.count(w) for w in NEGATIVE)
    urgent = any(w in low for w in URGENT)
    score = (pos - neg) + (1.0 if urgent else 0.0)
    return {"positive": pos, "negative": neg, "urgent": urgent, "score": round(score, 2)}


def collect(proxies=None, limit=25):
    """Забрать ленты, объединить, отсортировать по свежести."""
    seen = set()
    items = []
    errors = []
    for source, url in FEEDS:
        try:
            raw = _fetch(url, proxies)
            root = ET.fromstring(raw)
        except Exception as e:
            errors.append(f"{source}: {str(e)[:80]}")
            continue
        for node in root.iter():
            tag = node.tag.split("}")[-1]
            if tag != "item":
                continue
            title = ""
            pub = ""
            link = ""
            for child in node:
                ct = child.tag.split("}")[-1]
                if ct == "title":
                    title = _clean(child.text)
                elif ct == "pubDate":
                    pub = _clean(child.text)
                elif ct == "link":
                    link = _clean(child.text)
            if not title:
                continue
            key = re.sub(r"[^a-zа-я0-9]+", "", title.lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            safe_title, flags = sanitize_headline(title, source)
            items.append({
                "title": safe_title[:180],
                "raw_title": title[:180] if not flags else None,
                "source": source,
                "age_h": _age(pub),
                "sentiment": _score(safe_title),
                "flags": flags,
                "url": link[:220],
            })
    items.sort(key=lambda x: (x["age_h"] if x["age_h"] is not None else 999))
    out = items[:limit]
    pos = sum(1 for i in out if i["sentiment"]["score"] > 0)
    neg = sum(1 for i in out if i["sentiment"]["score"] < 0)
    urgent = [i for i in out if i["sentiment"]["urgent"]]
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(out),
        "sentiment": {"positive": pos, "negative": neg, "neutral": len(out) - pos - neg,
                      "score": round((pos - neg) / max(1, len(out)), 3)},
        "urgent": urgent[:3],
        "items": out,
        "errors": errors,
    }
    try:
        os.makedirs(os.path.dirname(NEWS_PATH), exist_ok=True)
        with open(NEWS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return payload


def load():
    try:
        with open(NEWS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"count": 0, "items": [], "sentiment": {}, "updated_at": None}


def digest(max_items=8, max_chars=1400):
    """Компактный блок для промпта модели. Только обезличенные заголовки."""
    data = load()
    items = data.get("items") or []
    fresh = [i for i in items if (i.get("age_h") or 99) <= 12][:max_items]
    if not fresh:
        return "NO NEWS AVAILABLE"
    lines = []
    blocked = 0
    for i in fresh:
        if i.get("title") == "[BLOCKED: unsafe headline]":
            blocked += 1
            continue
        s = i.get("sentiment", {})
        mark = "+" if s.get("score", 0) > 0 else ("-" if s.get("score", 0) < 0 else "0")
        age = i.get("age_h")
        age_txt = f"{age:.0f}h" if age is not None else "?"
        lines.append(f"[{mark}{s.get('score', 0)}|{age_txt}] {i.get('title','')[:110]} ({i.get('source','')})")
    sent = data.get("sentiment", {})
    head = (f"NEWS SENTIMENT pos={sent.get('positive',0)} neg={sent.get('negative',0)} "
            f"net={sent.get('score',0)}")
    if blocked:
        head += f" blocked={blocked}"
    if not lines:
        return head + " (все заголовки отброшены фильтром)"
    return (head + "\n" + "\n".join(lines))[:max_chars]


if __name__ == "__main__":
    result = collect()
    print(json.dumps({k: v for k, v in result.items() if k != "items"},
                     ensure_ascii=False, indent=2))
    for i in result["items"][:8]:
        print(" -", i["sentiment"]["score"], i["age_h"], i["title"][:80])