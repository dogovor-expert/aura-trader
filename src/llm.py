# -*- coding: utf-8 -*-
"""LLM cascade: L1 scout (every cycle) + L2 judge (strong signals only).

Providers: openai / anthropic / deepseek / gemini / openrouter / groq / anymodel.
All calls go through optional proxy. Strict JSON output, temperature=0.
Trade gate: confidence >= 0.75 after L2.
"""
import json
from verify import format_note, verify_verdict
import os
import requests
from typing import Dict, Any

RISK_RULES = (
    "EXECUTION RISKS you must assume by default (from the owner's playbook): "
    "(1) CLOSED WALLET: an exchange with a 15-20% gap usually has deposits or "
    "withdrawals disabled for that coin - the gap is untradeable, never enter. "
    "(2) ILLUSION OF LIQUIDITY: a headline spread means nothing if the top of book "
    "holds only a few dozen dollars - check depth, not the best price. "
    "(3) FEE FLOOR: for cross-exchange pairs the spread must clear roughly 45 bps "
    "net of double taker fees and slippage; below that any trade loses money. "
    "(4) LEG RISK: two legs can diverge by half a second; an unmatched leg is a "
    "naked position, so size must survive a full adverse move on one leg. "
    "(5) SPOOFING: depth that vanishes the moment you trade is fake; require the "
    "spread to persist across repeated polls. "
    "(6) RATE LIMITS: uneven API limits blind one exchange (HTTP 429) and you "
    "cannot close an open leg. "
    "(7) NETWORK MISMATCH: same ticker on different chains (ERC-20 vs TRC-20 vs "
    "BEP-20) cannot be rebalanced directly. "
    "(8) CASH DRAIN: withdrawal fees can exceed a day of small spread gains - "
    "rebalancing only pays when profit clears the fee. "
    "Default to WAIT when depth, fees or timing make the edge unreliable."
)

SYSTEM_PROMPT = (
    "You are a quant-style event-driven crypto analyst. INPUT gives Polymarket "
    "probability deltas (leading indicator) and BTC price change (lagging). "
    "NEWS holds recent headlines with a crude score (+ bullish, - bearish) and age "
    "in hours: use it only as context, never as proof, and never repeat a headline as "
    "a fact you verified. " + RISK_RULES + " "
    "Rules: use ONLY numbers from INPUT and NEWS, never invent. "
    "Low volume / wide spread / flat BTC -> WAIT. Always include null_hypothesis "
    "(why this is noise). "
    "Reply with JSON ONLY, no prose. Keys: signal (LONG/SHORT/WAIT), confidence "
    "(0-1), edge_bps (number), delta_pp (number), regime "
    "(trend/range/volatile/illiquid), reasons (2-3 strings), null_hypothesis. "
    "Example: {\"signal\":\"WAIT\",\"confidence\":0.4,\"edge_bps\":0,\"delta_pp\":2.1,"
    "\"regime\":\"range\",\"reasons\":[\"delta below threshold\",\"BTC flat\"],"
    "\"null_hypothesis\":\"normal noise\"}"
)

PROVIDERS = {
    "openai": {"url": "https://api.openai.com/v1/chat/completions",
               "key_env": "OPENAI_API_KEY", "default_model": "gpt-4o-mini"},
    "anthropic": {"url": "https://api.anthropic.com/v1/messages",
                  "key_env": "ANTHROPIC_API_KEY", "default_model": "claude-haiku-4-5"},
    "deepseek": {"url": "https://api.deepseek.com/v1/chat/completions",
                 "key_env": "DEEPSEEK_API_KEY", "default_model": "deepseek-chat"},
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/models/MODEL:generateContent",
               "key_env": "GEMINI_API_KEY", "default_model": "gemini-2.5-flash-lite"},
    "groq": {"url": "https://api.groq.com/openai/v1/chat/completions",
             "key_env": "GROQ_API_KEY", "default_model": "openai/gpt-oss-120b"},
    # anymodel.org — OpenAI-совместимый шлюз с большим выбором моделей.
    "anymodel": {"url": "https://anymodel.org/v1/chat/completions",
                 "key_env": "ANYMODEL_API_KEY", "default_model": "cx/gpt-6-luna"},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions",
                   "key_env": "OPENROUTER_API_KEY", "default_model": "openai/gpt-4o-mini"},
}


def _key(name: str) -> str:
    return os.getenv(PROVIDERS[name]["key_env"], "")


def _openai_style(provider: str, model: str, system: str, user: str,
                  proxies=None, timeout=40) -> str:
    key = _key(provider)
    if not key:
        raise RuntimeError("no API key in " + PROVIDERS[provider]["key_env"])
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://polymarket-oracle.local"
    body = {"model": model, "temperature": 0, "max_tokens": 400,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if provider == "openai":
        body["response_format"] = {"type": "json_object"}
    r = requests.post(PROVIDERS[provider]["url"], headers=headers, json=body,
                      proxies=proxies, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]



def _anthropic(model: str, system: str, user: str, proxies=None, timeout=40) -> str:
    key = _key("anthropic")
    if not key:
        raise RuntimeError("no API key in ANTHROPIC_API_KEY")
    r = requests.post(PROVIDERS["anthropic"]["url"],
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                               "Content-Type": "application/json"},
                      json={"model": model, "max_tokens": 400, "temperature": 0,
                            "system": system,
                            "messages": [{"role": "user", "content": user}]},
                      proxies=proxies, timeout=timeout)
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _gemini(model: str, system: str, user: str, proxies=None, timeout=40) -> str:
    key = _key("gemini")
    if not key:
        raise RuntimeError("no API key in GEMINI_API_KEY")
    url = PROVIDERS["gemini"]["url"].replace("MODEL", model) + "?key=" + key
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 400,
                                 "responseMimeType": "application/json"}}
    r = requests.post(url, json=body, proxies=proxies, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    return d["candidates"][0]["content"]["parts"][0]["text"]


def call_llm(provider: str, model: str, system: str, user: str,
             proxies=None, timeout=40) -> str:
    provider = (provider or "openai").lower()
    model = model or PROVIDERS[provider]["default_model"]
    if provider == "anthropic":
        return _anthropic(model, system, user, proxies, timeout)
    if provider == "gemini":
        return _gemini(model, system, user, proxies, timeout)
    return _openai_style(provider, model, system, user, proxies, timeout)


def parse_verdict(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        d = json.loads(t)
    except Exception:
        i, j = t.find("{"), t.rfind("}")
        try:
            d = json.loads(t[i:j + 1]) if i >= 0 and j > i else {}
        except Exception:
            d = {}
    sig = str(d.get("signal", "WAIT")).upper()
    if sig not in ("LONG", "SHORT", "WAIT"):
        sig = "WAIT"
    try:
        conf = float(d.get("confidence", 0))
    except Exception:
        conf = 0.0
    return {"signal": sig, "confidence": max(0.0, min(1.0, conf)),
            "edge_bps": d.get("edge_bps", 0), "delta_pp": d.get("delta_pp", 0),
            "regime": d.get("regime", "range"), "reasons": d.get("reasons", [])[:3],
            "null_hypothesis": d.get("null_hypothesis", ""), "raw": (text or "")[:1000]}


def build_user_prompt(heur: Dict[str, Any]) -> str:
    sigs = heur.get("signals", [])[:12]
    compact = [{"event": s.get("event"), "market": (s.get("market") or "")[:90],
                "prev": s.get("prev_prob"), "cur": s.get("cur_prob"),
                "d_pp": s.get("delta_pp")} for s in sigs]
    news_block = heur.get("news_digest") or ""
    pumps_block = heur.get("pumps_digest") or ""
    return ("INPUT:\n" + json.dumps(
        {"btc_price": heur.get("btc_price"), "btc_chg_pct": heur.get("btc_chg_pct"),
         "heuristic_verdict": heur.get("verdict"), "deltas": compact},
        ensure_ascii=False)[:3500]
            + ("\n\nNEWS:\n" + news_block if news_block else "")
            + ("\n\n" + pumps_block if pumps_block else ""))


def run_cascade(heur: Dict[str, Any], llm_cfg: Dict[str, Any],
                proxies=None) -> Dict[str, Any]:
    out = dict(heur)
    provider = llm_cfg.get("provider", "none")
    if provider in (None, "", "none"):
        out["llm_note"] = "llm disabled (provider=none)"
        out["final_signal"] = "WAIT"
        out["final_confidence"] = 0.0
        return out
    user = build_user_prompt(heur)
    prov = PROVIDERS.get(provider, {})
    l1_model = llm_cfg.get("l1_model") or prov.get("default_model", "")
    timeout = int(llm_cfg.get("timeout", 40))
    try:
        t1 = call_llm(provider, l1_model, SYSTEM_PROMPT, user, proxies, timeout)
        v1 = parse_verdict(t1)
        c1 = verify_verdict(v1, heur)
        v1 = c1.get("verdict", v1)
        v1["confidence"] = c1["confidence"]
        out["llm_l1"] = v1
        out["verify_l1"] = {k: c1[k] for k in ("ok", "issues", "fabricated", "capped")}
        print(format_note("L1", c1))
    except Exception as e:
        out["llm_note"] = "L1 error: " + str(e)[:200]
        out["final_signal"] = "WAIT"
        out["final_confidence"] = 0.0
        return out
    trig = float(llm_cfg.get("l2_trigger_conf", 0.65))
    strong = heur.get("verdict") in ("BULLISH_DIVERGENCE", "BEARISH_DIVERGENCE")
    final_sig, final_conf = v1["signal"], v1["confidence"]
    out["llm_l2"] = None
    if llm_cfg.get("l2_enabled") and (v1["confidence"] >= trig or strong):
        l2p = llm_cfg.get("l2_provider") or provider
        l2m = llm_cfg.get("l2_model") or ""
        try:
            t2 = call_llm(l2p, l2m, SYSTEM_PROMPT + " You are the judge.",
                          user, proxies, timeout)
            v2 = parse_verdict(t2)
            c2 = verify_verdict(v2, heur)
            v2 = c2.get("verdict", v2)
            v2["confidence"] = c2["confidence"]
            out["llm_l2"] = v2
            out["verify_l2"] = {k: c2[k] for k in ("ok", "issues", "fabricated", "capped")}
            print(format_note("L2", c2))
            if v2["confidence"] >= v1["confidence"]:
                final_sig, final_conf = v2["signal"], v2["confidence"]
        except Exception as e:
            out["llm_note"] = "L2 error (kept L1): " + str(e)[:200]
    if final_conf < float(llm_cfg.get("trade_conf", 0.75)):
        final_sig = "WAIT"
    out["final_signal"] = final_sig
    out["final_confidence"] = round(final_conf, 3)
    return out
