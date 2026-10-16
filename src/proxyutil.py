# -*- coding: utf-8 -*-
"""Proxy helper: builds requests-compatible proxies dict from config.

Supports SOCKS5 (needs PySocks) and HTTP(S) proxies.
Config section: cfg["proxy"] = {enabled, url} or cfg["price_source"].http_proxy.
"""
from typing import Optional, Dict


def build_proxies(cfg) -> Optional[Dict[str, str]]:
    url = ""
    if isinstance(cfg, dict):
        px = cfg.get("proxy") or {}
        if isinstance(px, dict) and px.get("enabled") and px.get("url"):
            url = px["url"]
        else:
            ps = cfg.get("price_source") or {}
            url = ps.get("https_proxy") or ps.get("http_proxy") or ""
    if not url:
        return None
    return {"http": url, "https": url}


def redact(url: str) -> str:
    """Hide password for logs: socks5h://user:***@host:port"""
    try:
        if "@" in url and "://" in url:
            scheme, rest = url.split("://", 1)
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":")[0]
            return f"{scheme}://{user}:***@{host}"
    except Exception:
        pass
    return "***"
