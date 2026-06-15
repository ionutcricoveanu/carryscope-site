#!/usr/bin/env python3
"""Build the CarryScope carry-check page.

Recomputes BTC/ETH net-of-cost funding carry from public Binance data and renders
index.html from index.template.html (plus carry-data.json). Run weekly; the page
is a dated illustration, not a live feed.

The method (and a standalone version of this computation) is open source:
https://github.com/ionutcricoveanu/carryscope-methodology

Run: python3 build.py
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

# ---- method constants -------------------------------------------------------
FEE_PER_FILL_PCT = 0.075         # taker fee per fill
BASIS_PENALTY_BP_PER_LEG = 2.0   # basis half-spread floor, bp/leg
BASIS_CLIP_BP = 50.0             # data-hygiene clip on |basis|
RISK_FREE_APY = 4.5              # risk-free hurdle, %
DAYS_PER_YEAR = 365.0
N_SETTLES = 90
FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"
SYMBOLS = [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")]
HERE = os.path.dirname(os.path.abspath(__file__))


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "carryscope-site/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_funding(sym, limit):
    rows = _get(f"{FAPI}/fapi/v1/fundingRate?symbol={sym}&limit={limit}")
    return [(int(r["fundingTime"]), float(r["fundingRate"]), float(r["markPrice"]))
            for r in rows if r.get("markPrice") not in (None, "", "0")]


def fetch_spot_1h(sym, start_ms, end_ms):
    out, cur, end = [], start_ms - 2*3600*1000, end_ms + 2*3600*1000
    while cur < end:
        batch = _get(f"{SAPI}/api/v3/klines?symbol={sym}&interval=1h"
                     f"&startTime={cur}&endTime={end}&limit=1000")
        if not batch:
            break
        out.extend((int(k[6]), float(k[4])) for k in batch)  # (closeTime, close)
        cur = int(batch[-1][0]) + 3600*1000
        if len(batch) < 1000:
            break
    return out


def spot_asof(spot, t_ms, tol=90*60*1000):
    best = None
    for ct, close in spot:
        if ct <= t_ms and (best is None or ct > best[0]):
            best = (ct, close)
    return None if best is None or (t_ms - best[0]) > tol else best[1]


def compute(sym):
    fund = fetch_funding(sym, N_SETTLES)
    spot = fetch_spot_1h(sym, fund[0][0], fund[-1][0])
    clip = BASIS_CLIP_BP / 1e4
    rows = []
    for t_ms, rate, mark in fund:
        s = spot_asof(spot, t_ms)
        if s is None:
            continue
        rows.append({"t": t_ms, "mark": mark, "fund": rate,
                     "basis_clipped": max(-clip, min(clip, (s - mark) / mark))})
    med_basis_bp = sorted(abs(r["basis_clipped"]) * 1e4 for r in rows)[len(rows)//2]
    basis_bp_per_leg = max(BASIS_PENALTY_BP_PER_LEG, round(med_basis_bp, 2))
    toggle = 2.0 * (FEE_PER_FILL_PCT/100.0 + basis_bp_per_leg/1e4)

    cum, prev_basis = -toggle, None
    for r in rows:
        step = r["fund"]
        if prev_basis is not None:
            step += (r["basis_clipped"] - prev_basis)
        cum += step
        prev_basis = r["basis_clipped"]
    span_days = max((rows[-1]["t"] - rows[0]["t"]) / 86400_000.0, 1.0)
    gross = sum(r["fund"] for r in rows) / span_days * DAYS_PER_YEAR * 100.0
    net = cum / span_days * DAYS_PER_YEAR * 100.0
    return {"gross_fund_apy": round(gross, 2), "net_apy": round(net, 2),
            "first": datetime.fromtimestamp(rows[0]["t"]/1000, tz=timezone.utc).date().isoformat(),
            "last": datetime.fromtimestamp(rows[-1]["t"]/1000, tz=timezone.utc).date().isoformat()}


def verdict(net):
    if net < RISK_FREE_APY:
        return "Not worth it right now", "bad"
    return "Potentially worth a look", "good"


def main():
    res = {s2: compute(s1) for s1, s2 in SYMBOLS}
    as_of = max(res[k]["last"] for k in res)
    window = f'{min(res[k]["first"] for k in res)} → {as_of}'
    data = {"as_of": as_of, "window": window, "risk_free_apy": RISK_FREE_APY, "symbols": {}}
    for k, v in res.items():
        vt, cls = verdict(v["net_apy"])
        data["symbols"][k] = {**v, "verdict": vt, "verdict_class": cls}
    with open(os.path.join(HERE, "carry-data.json"), "w") as f:
        json.dump(data, f, indent=2)

    tpl = open(os.path.join(HERE, "index.template.html")).read()
    repl = {"{{AS_OF}}": as_of, "{{WINDOW}}": window, "{{RISK_FREE}}": f"{RISK_FREE_APY}"}
    for k in ("BTC", "ETH"):
        s = data["symbols"][k]
        repl[f"{{{{{k}_GROSS}}}}"] = f'{s["gross_fund_apy"]:+.2f}'
        repl[f"{{{{{k}_NET}}}}"] = f'{s["net_apy"]:+.2f}'
        repl[f"{{{{{k}_VERDICT}}}}"] = s["verdict"]
        repl[f"{{{{{k}_CLASS}}}}"] = s["verdict_class"]
    for a, b in repl.items():
        tpl = tpl.replace(a, b)
    with open(os.path.join(HERE, "index.html"), "w") as f:
        f.write(tpl)
    print(f"built index.html + carry-data.json (as_of {as_of})")


if __name__ == "__main__":
    main()
