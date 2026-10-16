import json, urllib.request
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for u in ["https://open-api.bingx.com/openApi/spot/v1/tickers",
          "https://open-api.bingx.com/openApi/spot/v1/common/symbols"]:
    try:
        r=op.open(urllib.request.Request(u, headers={"User-Agent":UA}), timeout=10)
        d=json.loads(r.read())
        rows=d.get("data") or []
        print(u.split("/")[-1], "->", type(d.get("code")), "rows:", len(rows))
        if rows: print("  sample:", json.dumps(rows[0])[:200])
    except Exception as e:
        print(u.split("/")[-1], "FAIL", str(e)[:80])
