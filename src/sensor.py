"""Polymarket sensor: опрос Gamma API без ключей, съём вероятностей."""
import requests
from typing import Dict, List, Any

GAMMA = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (polymarket-oracle/1.0)"}

def _to_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def fetch_event(slug: str, timeout: int = 25, proxies=None) -> Dict[str, Any]:
    r = requests.get(f"{GAMMA}/events", params={"slug": slug},
                     headers=HEADERS, timeout=timeout, proxies=proxies)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        if not data:
            raise ValueError(f"event not found: {slug}")
        return data[0]
    return data

def parse_markets(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Возвращает список {question, outcomes, prices, lastTradePrice, volume}.

    outcomes бывают в разном порядке: ["Yes","No"] или ["No","Yes"].
    Yes-цена = prices[index("Yes")]. lastTradePrice — якорь для проверки.
    """
    out = []
    for m in event.get("markets", []):
        try:
            outcomes = m.get("outcomes")
            import json as _j
            if isinstance(outcomes, str):
                outcomes = _j.loads(outcomes)
            prices_raw = m.get("outcomePrices")
            if isinstance(prices_raw, str):
                prices_raw = _j.loads(prices_raw)
            prices = [_to_float(p) for p in (prices_raw or [])]
        except Exception:
            outcomes, prices = [], []
        outcomes = outcomes or ["No", "Yes"]
        last = _to_float(m.get("lastTradePrice"))
        # индекс Yes
        try:
            yes_idx = next(i for i, o in enumerate(outcomes)
                           if str(o).lower() == "yes")
        except StopIteration:
            yes_idx = 1 if len(prices) > 1 else 0
        yes = prices[yes_idx] if len(prices) > yes_idx else 0.0
        # sanity: lastTradePrice должен совпадать с yes (±5 п.п.),
        # иначе пробуем другой индекс (защита от кривых данных)
        if len(prices) > 1 and last:
            other = prices[1 - yes_idx] if len(prices) > 1 else None
            if other is not None and abs(last - other) < abs(last - yes) - 0.05:
                yes = other
        # resolved/мертвый рынок: цены ровно 0/1 — не сигнал
        resolved = (len(prices) == 2 and
                    ((prices[0] == 0 and prices[1] == 1) or
                     (prices[0] == 1 and prices[1] == 0)))
        out.append({
            "question": m.get("question", ""),
            "title": m.get("groupItemTitle", "") or m.get("question", ""),
            "outcomes": outcomes,
            "prices": prices,
            "yes_prob": yes,
            "lastTradePrice": last,
            "volume": m.get("volume") or m.get("volumeNum") or "0",
            "slug": m.get("slug", ""),
            "_resolved": resolved,
        })
    return out

def poll_all(slugs_labels, timeout=25, proxies=None) -> Dict[str, Any]:
    """slugs_labels: [(slug,label)] -> {label: {title, markets, ts}}"""
    import datetime
    res = {}
    for slug, label in slugs_labels:
        try:
            ev = fetch_event(slug, timeout, proxies)
            res[label] = {
                "slug": slug,
                "title": ev.get("title", ""),
                "markets": parse_markets(ev),
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "error": None,
            }
        except Exception as e:
            import datetime as _dt
            res[label] = {"slug": slug, "title": "", "markets": [],
                          "ts": _dt.datetime.utcnow().isoformat() + "Z",
                          "error": str(e)}
    return res

if __name__ == "__main__":
    import json
    demo = poll_all([("fed-decision-in-october-20260617190323537", "FED_OCT")])
    print(json.dumps(demo, ensure_ascii=False, indent=2)[:3000])
