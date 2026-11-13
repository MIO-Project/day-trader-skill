#!/usr/bin/env python3
"""Does the opening-range breakout have an edge across many stocks?

Runs the Time Machine's own history check (history_replay) on a list of stocks
over Yahoo's last 60 days of 5-minute bars, pools every trade, and reports the
average result in R, with and without a trading-cost estimate.

Read-only research: it places nothing, sends nothing to a broker and writes
nothing unless you pass --cache or --json.

    python3 scripts/orb_breadth.py
    python3 scripts/orb_breadth.py --symbols AAPL,MSFT,NVDA --now "2026-11-13 04:00Z"
    python3 scripts/orb_breadth.py --cache /tmp/orb-charts --json > breadth.json

The rule matches the README example: a 15-minute opening range, entry at the
first 5-minute close above it, stop at the range's midpoint, 1R target. Two
variants (a 30-minute range and a 2R target) show whether the result depends
on one exact setting. Entries are allowed until 15:30 and every trade is out by
15:55, the pack's default no_entries_after and flat_by.

Yahoo keeps 5-minute bars for 60 days only, so this cannot look further back.
Not financial advice.
"""
import argparse
import datetime
import importlib.util
import json
import math
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location(
    "trader_timemachine", os.path.join(ROOT, "trader-timemachine", "timemachine.py"))
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

DEFAULT_SYMBOLS = ("AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA,AVGO,JPM,V,"
                   "LLY,WMT,XOM,UNH,MA,COST,HD,PG,NFLX,ORCL")
VARIANTS = [
    ("README rule (15-min range, mid stop, 1R)", {"or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0}),
    ("30-min range", {"or_minutes": 30, "stop_rule": "or_mid", "target_r": 1.0}),
    ("2R target", {"or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0}),
]
LAST_BAR = 76                    # the 15:50 bar, closing 15:55: the pack's flat_by
LATEST_ENTRY = 15 * 60 + 30      # the pack's no_entries_after, 15:30


def chart_text(sym, cache):
    """Yahoo's 60-day 5-minute chart for sym, from the cache folder when given and present."""
    path = os.path.join(cache, sym + ".json") if cache else None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read(), False
    text = tm.fetch_chart(sym)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text, True


def full_sessions(sym, text, now_utc):
    """Every full session (all 78 bars) up to and including the day of now."""
    _, bars, _ = tm.parse_bars(tm.chart_result(text, sym), now_utc)
    tomorrow = (tm.to_eastern(now_utc).date() + datetime.timedelta(days=1)).isoformat()
    full, _ = tm.full_sessions(tm.split_sessions(bars), tomorrow)
    return full


def minutes(clock):
    t = datetime.datetime.strptime(clock, "%I:%M %p")
    return t.hour * 60 + t.minute


def summarize(trades):
    rs = [t["R"] for t in trades]
    n = len(rs)
    avg = statistics.mean(rs) if rs else 0.0
    se = statistics.stdev(rs) / math.sqrt(n) if n > 1 else None
    return {"trades": n,
            "target": sum(t["outcome"] == "target" for t in trades),
            "stop": sum(t["outcome"] == "stop" for t in trades),
            "neither": sum(t["outcome"] == "close" for t in trades),
            "avg_R": round(avg, 3),
            "std_err": round(se, 3) if se else None,
            "low95": round(avg - 1.96 * se, 3) if se else None,
            "high95": round(avg + 1.96 * se, 3) if se else None}


def study(sessions, cost_pct):
    """sessions: {symbol: full sessions} -> the per-variant results."""
    out = []
    for name, v in VARIANTS:
        rule = dict(setup="orb", **v)
        pooled, per_stock = [], {}
        for sym, full in sessions.items():
            h = tm.history_replay(full, rule, LAST_BAR)
            trades = [dict(t, symbol=sym) for t in h["trades"] if minutes(t["time"]) <= LATEST_ENTRY]
            per_stock[sym] = summarize(trades)
            pooled += trades
        res = {"rule": name, "pooled": summarize(pooled), "per_stock": per_stock,
               "stocks_positive": sum(1 for s in per_stock.values() if s["trades"] and s["avg_R"] > 0)}
        if pooled:
            # cost_pct each way, as a share of the risk: 2 * pct * entry / (entry - stop)
            costs = [2 * cost_pct / 100.0 * t["entry"] / abs(t["entry"] - t["stop"]) for t in pooled]
            res["cost_R"] = round(statistics.mean(costs), 3)
            res["avg_R_after_cost"] = round(res["pooled"]["avg_R"] - res["cost_R"], 3)
            dates = sorted({t["date"] for t in pooled})
            mid = dates[len(dates) // 2]
            res["first_half"] = summarize([t for t in pooled if t["date"] < mid])
            res["second_half"] = summarize([t for t in pooled if t["date"] >= mid])
        out.append(res)
    return out


def r(x):
    return "n/a" if x is None else "%+.2fR" % x


def report(results, sessions, cost_pct):
    days = sorted({d for full in sessions.values() for d, _ in full})
    print("Opening-range breakout history across %d stocks, %d sessions (%s to %s)."
          % (len(sessions), len(days), days[0], days[-1]))
    print("Entries until 15:30, out by 15:55. Cost estimate %.2f%% each way.\n" % cost_pct)
    for res in results:
        p = res["pooled"]
        print(res["rule"])
        print("  %d trades: %d target, %d stop, %d neither"
              % (p["trades"], p["target"], p["stop"], p["neither"]))
        if not p["trades"]:
            print()
            continue
        print("  average %s per trade (95%% range %s to %s), after costs %s"
              % (r(p["avg_R"]), r(p["low95"]), r(p["high95"]), r(res["avg_R_after_cost"])))
        print("  first half %s over %d trades, second half %s over %d"
              % (r(res["first_half"]["avg_R"]), res["first_half"]["trades"],
                 r(res["second_half"]["avg_R"]), res["second_half"]["trades"]))
        print("  stocks with a positive average: %d of %d\n" % (res["stocks_positive"], len(sessions)))
    first = results[0]
    print("Per stock, %s:" % first["rule"])
    for sym, s in sorted(first["per_stock"].items(), key=lambda kv: -kv[1]["avg_R"]):
        print("  %-6s %3d trades  %2d target %2d stop %2d neither  %s"
              % (sym, s["trades"], s["target"], s["stop"], s["neither"], r(s["avg_R"])))
    print("\nAbout 30 trades per stock is too few to trust one stock's number on its own.")
    print("A simulation of past data, not a prediction. Not financial advice.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS, help="comma-separated tickers")
    ap.add_argument("--now", help='pretend it is this time ("2026-11-13 04:00Z", or New York time)')
    ap.add_argument("--cost-pct", type=float, default=0.02, help="cost each way, %% of price (default 0.02)")
    ap.add_argument("--cache", help="folder to save and reuse the Yahoo charts in")
    ap.add_argument("--json", action="store_true", help="print the full results as JSON")
    a = ap.parse_args(argv)

    try:
        now_utc = tm.parse_now(a.now)
    except tm.TMError as e:
        sys.exit("orb_breadth.py: %s" % e)
    if a.cache:
        os.makedirs(a.cache, exist_ok=True)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    sessions = {}
    for sym in symbols:
        try:
            text, fetched = chart_text(sym, a.cache)
            sessions[sym] = full_sessions(sym, text, now_utc)
        except tm.TMError as e:
            print("skipped %s: %s" % (sym, e), file=sys.stderr)
            continue
        if fetched:
            time.sleep(1.5)  # be polite to Yahoo
    sessions = {s: f for s, f in sessions.items() if f}
    if not sessions:
        sys.exit("orb_breadth.py: no usable charts")
    results = study(sessions, a.cost_pct)
    if a.json:
        print(json.dumps({"symbols": sorted(sessions), "results": results}, indent=1))
    else:
        report(results, sessions, a.cost_pct)


if __name__ == "__main__":
    main()
