#!/usr/bin/env python3
"""Build the CarryScope carry-check page.

Recomputes BTC/ETH net-of-cost funding carry from public Binance data and renders
index.html from index.template.html (plus carry-data.json). Run weekly; the page
is a dated illustration, not a live feed.

Data sources (all Binance-official, public, no API key; chosen because the trading
API at fapi.binance.com geo-blocks US IPs, where GitHub-hosted runners live):
  - settled funding + intervals: monthly fundingRate dumps on data.binance.vision
  - funding since the last monthly dump: reconstructed from 1m premiumIndexKlines
    daily dumps using Binance's published formula, and validated every build
    against the most recent settled month (build fails hard if it drifts)
  - mark price at settlement: 1h markPriceKlines daily dumps
  - spot closes: data-api.binance.vision (official market-data-only REST mirror)

The method (and a standalone version of this computation) is open source:
https://github.com/ionutcricoveanu/carryscope-methodology

Run: python3 build.py
"""
import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

# ---- method constants -------------------------------------------------------
FEE_PER_FILL_PCT = 0.075         # taker fee per fill
BASIS_PENALTY_BP_PER_LEG = 2.0   # basis half-spread floor, bp/leg
BASIS_CLIP_BP = 50.0             # data-hygiene clip on |basis|
RISK_FREE_APY = 4.5              # risk-free hurdle, %
DAYS_PER_YEAR = 365.0
N_SETTLES = 90

# ---- data-source constants --------------------------------------------------
VISION = "https://data.binance.vision/data"
SPOT_API = "https://data-api.binance.vision"
SYMBOLS = [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")]
PREMIUM_DAYS = 66     # 1m premium-index lookback: covers the 30d window plus a
                      # full prior month so validation survives month boundaries
MARK_DAYS = 36        # 1h mark-price lookback: 30d window + buffer
FUNDING_MONTHS = 3    # monthly funding dumps to try (current + 2 back)
INTEREST_PER_8H = 0.0001   # Binance USDT-perp interest component, 0.01% / 8h
MIN_WINDOW_COVERAGE = 0.9  # min fraction of 1m samples to reconstruct a settle
# Reconstruction acceptance gate (pre-registered 2026-07-04 against June 2026:
# per-settle median 4.1e-6 / max 2.9e-5, aggregate drift -0.066pp on BTC+ETH):
VAL_MIN_SETTLES = 30
VAL_MAX_MEDIAN_ERR = 2e-5
VAL_MAX_ERR = 1e-4
VAL_MAX_DRIFT_PP = 0.15    # |aggregate signed error|, annualized, %-points
# The validation window must be genuinely recent: monthly funding dumps only appear
# after month end, so a naive "last N settled" window silently freezes mid-month and
# re-scores the same stale settles every run. Fail loudly instead.
VAL_MAX_WINDOW_AGE_DAYS = 45
HERE = os.path.dirname(os.path.abspath(__file__))


# ---- fetching ----------------------------------------------------------------
def _fetch(url):
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "carryscope-site/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except urllib.error.URLError as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed after retries: {url}: {last}")


def _get_json(url):
    raw = _fetch(url)
    if raw is None:
        raise RuntimeError(f"unexpected 404: {url}")
    return json.loads(raw)


def _get_zip_csv(url):
    """Rows of the single CSV inside a dump zip, header skipped; None if 404."""
    raw = _fetch(url)
    if raw is None:
        return None
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        text = zf.read(zf.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    return rows[1:] if rows and not rows[0][0].isdigit() else rows


def _utc_days_back(n_days):
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n_days, -1, -1)]


def fetch_settled_funding(sym):
    """[(t_ms, rate, interval_h)] from monthly dumps, oldest first."""
    first = datetime.now(timezone.utc).date().replace(day=1)
    out = []
    for i in range(FUNDING_MONTHS):
        m = first
        for _ in range(i):
            m = (m - timedelta(days=1)).replace(day=1)
        rows = _get_zip_csv(f"{VISION}/futures/um/monthly/fundingRate/{sym}/"
                            f"{sym}-fundingRate-{m.strftime('%Y-%m')}.zip")
        for r in rows or []:  # current month's file 404s until month end
            out.append((int(r[0]), float(r[2]), int(r[1])))
    return sorted(out)


def fetch_premium_1m(sym):
    """{minute_open_ms: premium_index_close} from daily dumps; missing days skipped."""
    out = {}
    for d in _utc_days_back(PREMIUM_DAYS):
        rows = _get_zip_csv(f"{VISION}/futures/um/daily/premiumIndexKlines/{sym}/1m/"
                            f"{sym}-1m-{d}.zip")
        for r in rows or []:
            out[int(r[0])] = float(r[4])
    return out


def fetch_mark_1h(sym):
    """[(close_time_ms, mark_close)] from daily dumps; missing days skipped."""
    out = []
    for d in _utc_days_back(MARK_DAYS):
        rows = _get_zip_csv(f"{VISION}/futures/um/daily/markPriceKlines/{sym}/1h/"
                            f"{sym}-1h-{d}.zip")
        for r in rows or []:
            out.append((int(r[6]), float(r[4])))
    return sorted(out)


def fetch_spot_1h(sym, start_ms, end_ms):
    out, cur, end = [], start_ms - 2*3600*1000, end_ms + 2*3600*1000
    while cur < end:
        batch = _get_json(f"{SPOT_API}/api/v3/klines?symbol={sym}&interval=1h"
                          f"&startTime={cur}&endTime={end}&limit=1000")
        if not batch:
            break
        out.extend((int(k[6]), float(k[4])) for k in batch)  # (closeTime, close)
        cur = int(batch[-1][0]) + 3600*1000
        if len(batch) < 1000:
            break
    return out


# ---- funding reconstruction (Binance published formula) -----------------------
def reconstruct_rate(premium, t_ms, interval_h):
    """F = weighted-avg premium over [T-interval, T) + clamp(interest - avg, +-0.05%).
    Weights rise linearly toward T (Binance's time-weighted average premium index)."""
    T = (t_ms // 60000) * 60000
    n = interval_h * 60
    num = den = have = 0
    for i in range(n):
        c = premium.get(T - 60000 * (n - i))
        if c is None:
            continue
        num += (i + 1) * c
        den += (i + 1)
        have += 1
    if have < MIN_WINDOW_COVERAGE * n:
        return None
    p = num / den
    return p + max(-0.0005, min(0.0005, INTEREST_PER_8H - p))


def validate_reconstruction(sym, settled, premium):
    """Reconstruct the last <=N_SETTLES settled settles and gate on the error.

    Also gates on the *age* of the validation window: Binance publishes fundingRate
    only as monthly dumps, so from the 1st of a month until the previous month's dump
    lands, `settled` stops advancing. Without this check the build re-validates an
    unchanging window every week and any verdict it reaches (pass or fail) is stale.
    """
    age_days = (datetime.now(timezone.utc)
                - datetime.fromtimestamp(settled[-1][0] / 1000, tz=timezone.utc)).days
    if age_days > VAL_MAX_WINDOW_AGE_DAYS:
        raise RuntimeError(
            f"{sym}: newest settled funding is {age_days}d old "
            f"(>{VAL_MAX_WINDOW_AGE_DAYS}d) — the monthly dump for the intervening "
            f"period has not been published, so the reconstruction cannot be validated "
            f"against fresh data; not publishing")
    errs = []
    for t_ms, rate, ivh in settled[-N_SETTLES:]:
        f = reconstruct_rate(premium, t_ms, ivh)
        if f is not None:
            errs.append(f - rate)
    if len(errs) < VAL_MIN_SETTLES:
        raise RuntimeError(f"{sym}: only {len(errs)} settled settles reconstructable "
                           f"(<{VAL_MIN_SETTLES}) — cannot validate reconstruction")
    ivh = settled[-1][2]
    ae = sorted(abs(e) for e in errs)
    drift_pp = abs(sum(errs)) / (len(errs) * ivh / 24.0) * DAYS_PER_YEAR * 100.0
    print(f"{sym}: reconstruction check on {len(errs)} settled settles: "
          f"median={ae[len(ae)//2]:.1e} max={ae[-1]:.1e} drift={drift_pp:.3f}pp")
    # Report only the breached condition(s): listing all three regardless of which
    # fired makes a single marginal breach read as a total method failure.
    failed = []
    if ae[len(ae)//2] > VAL_MAX_MEDIAN_ERR:
        failed.append(f"median {ae[len(ae)//2]:.2e}>{VAL_MAX_MEDIAN_ERR:.0e}")
    if ae[-1] > VAL_MAX_ERR:
        failed.append(f"max {ae[-1]:.2e}>{VAL_MAX_ERR:.0e}")
    if drift_pp > VAL_MAX_DRIFT_PP:
        failed.append(f"drift {drift_pp:.3f}pp>{VAL_MAX_DRIFT_PP}pp")
    if failed:
        raise RuntimeError(f"{sym}: funding reconstruction failed the acceptance gate "
                           f"({'; '.join(failed)}) — not publishing")


def fetch_funding(sym, limit):
    """Last `limit` settlements [(t_ms, rate, reconstructed?)]: settled from the
    monthly dumps, then reconstructed forward to now from premium-index data."""
    settled = fetch_settled_funding(sym)
    if not settled:
        raise RuntimeError(f"{sym}: no settled funding months available")
    premium = fetch_premium_1m(sym)
    validate_reconstruction(sym, settled, premium)
    ivh = settled[-1][2]
    fund = [(t, r, False) for t, r, _ in settled]
    t = (settled[-1][0] // 60000) * 60000
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    while True:
        t += ivh * 3600 * 1000
        if t > now_ms:
            break
        f = reconstruct_rate(premium, t, ivh)
        if f is None:  # newest daily dump not published yet — window ends here
            break
        fund.append((t, f, True))
    return fund[-limit:]


# ---- carry computation (method unchanged) -------------------------------------
def asof(series, t_ms, tol=90*60*1000):
    """Latest (close_time, value) at or before t_ms, within tolerance."""
    best = None
    for ct, v in series:
        if ct <= t_ms and (best is None or ct > best[0]):
            best = (ct, v)
    return None if best is None or (t_ms - best[0]) > tol else best[1]


def compute(sym):
    fund = fetch_funding(sym, N_SETTLES)
    mark = fetch_mark_1h(sym)
    spot = fetch_spot_1h(sym, fund[0][0], fund[-1][0])
    clip = BASIS_CLIP_BP / 1e4
    rows, n_recon = [], 0
    for t_ms, rate, recon in fund:
        m, s = asof(mark, t_ms), asof(spot, t_ms)
        n_recon += recon
        # basis None on a data hole: funding still counts, the basis roll pauses
        rows.append({"t": t_ms, "fund": rate,
                     "basis_clipped": None if m is None or s is None
                     else max(-clip, min(clip, (s - m) / m))})
    known = sorted(abs(r["basis_clipped"]) * 1e4 for r in rows
                   if r["basis_clipped"] is not None)
    med_basis_bp = known[len(known)//2]
    basis_bp_per_leg = max(BASIS_PENALTY_BP_PER_LEG, round(med_basis_bp, 2))
    toggle = 2.0 * (FEE_PER_FILL_PCT/100.0 + basis_bp_per_leg/1e4)

    cum, prev_basis = -toggle, None
    for r in rows:
        step = r["fund"]
        b = r["basis_clipped"] if r["basis_clipped"] is not None else prev_basis
        if prev_basis is not None and b is not None:
            step += (b - prev_basis)
        cum += step
        prev_basis = b
    span_days = max((rows[-1]["t"] - rows[0]["t"]) / 86400_000.0, 1.0)
    gross = sum(r["fund"] for r in rows) / span_days * DAYS_PER_YEAR * 100.0
    net = cum / span_days * DAYS_PER_YEAR * 100.0
    return {"gross_fund_apy": round(gross, 2), "net_apy": round(net, 2),
            "settles_used": len(rows), "settles_reconstructed": n_recon,
            "settles_missing_basis": sum(1 for r in rows if r["basis_clipped"] is None),
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
    data = {"as_of": as_of, "window": window, "risk_free_apy": RISK_FREE_APY,
            "data_source": "Binance public dumps (data.binance.vision) + spot mirror "
                           "(data-api.binance.vision); funding since the last monthly "
                           "dump reconstructed from premium-index data and validated "
                           "in-build against the most recent settled month",
            "symbols": {}}
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
    print(f"built index.html + carry-data.json (as_of {as_of}; "
          + ", ".join(f'{k} used {v["settles_used"]} settles '
                      f'({v["settles_reconstructed"]} reconstructed)'
                      for k, v in res.items()) + ")")


if __name__ == "__main__":
    main()
