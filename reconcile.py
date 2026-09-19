"""Reconcile executions.csv against Polymarket portfolio truth.

Pulls all activities (trades + position resolutions) and current open positions,
joins against executions.csv, and reports:
  - currently open positions (need manual close)
  - resolved positions where we held to expiry (untracked losses)
  - per-arb real P&L vs logged P&L

Usage: python3 reconcile.py [executions.csv]
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()
from polymarket_us import PolymarketUS


def fetch_all_activities(client, max_pages=50):
    acts = []
    cursor = None
    for _ in range(max_pages):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = client.portfolio.activities(params)
        acts.extend(r.get("activities") or [])
        cursor = r.get("nextCursor")
        if r.get("eof") or not cursor:
            break
    return acts


def short_position(slug):
    """Polymarket reports SHORT positions under a separate key (typically the same
    slug); we just dump everything by slug and let the caller interpret."""
    return slug


def main(exec_path):
    client = PolymarketUS(
        key_id=os.environ["POLYMARKET_API_KEY_ID"],
        secret_key=os.environ["POLYMARKET_PRIVATE_KEY"],
    )

    # 1. Currently-open positions
    pos_resp = client.portfolio.positions()
    open_positions = pos_resp.get("positions") or {}

    print("=" * 78)
    print("CURRENTLY OPEN POSITIONS")
    print("=" * 78)
    open_cost_total = 0.0
    if not open_positions:
        print("  (none)")
    for slug, p in open_positions.items():
        net = int(p.get("netPosition", 0))
        if net == 0:
            continue
        title = p.get("marketMetadata", {}).get("title", slug)
        outcome = p.get("marketMetadata", {}).get("outcome", "?")
        cost = float(p.get("cost", {}).get("value", 0))
        cash_value = float(p.get("cashValue", {}).get("value", 0))
        avg_px = float(p.get("avgPx", {}).get("value", 0))
        side = "LONG" if net > 0 else "SHORT"
        unrealized = cash_value - cost if net > 0 else -cash_value - cost
        open_cost_total += cost
        print(f"  {slug}")
        print(f"    {title} → {outcome}  {side} {abs(net)} @ avg ${avg_px:.3f}")
        print(f"    cost=${cost:.3f}  cash_value=${cash_value:.3f}  unrealized=${unrealized:+.3f}")
    print(f"\n  Total cost at risk: ${open_cost_total:.3f}")

    # 2. Activities — trades + resolutions
    acts = fetch_all_activities(client)
    trades = [a["trade"] for a in acts if a.get("type") == "ACTIVITY_TYPE_TRADE"]
    resolutions = [a["positionResolution"] for a in acts if a.get("type") == "ACTIVITY_TYPE_POSITION_RESOLUTION"]

    print(f"\nFetched {len(acts)} activities ({len(trades)} trades, {len(resolutions)} resolutions)")

    # 3. Position resolutions — anything with non-zero net at resolution = held to expiry
    print("\n" + "=" * 78)
    print("RESOLVED POSITIONS (held to game-end)")
    print("=" * 78)
    held_loss_total = 0.0
    held_count = 0
    for r in resolutions:
        bp = r.get("beforePosition", {})
        net = int(bp.get("netPosition", 0))
        if net == 0:
            continue
        held_count += 1
        md = bp.get("marketMetadata", {})
        title = md.get("title", "?")
        outcome = md.get("outcome", "?")
        bought = bp.get("qtyBought")
        sold = bp.get("qtySold")
        cost = float(bp.get("cost", {}).get("value", 0))
        realized = float(bp.get("realized", {}).get("value", 0))
        # Resolution payout
        res_pnl = r.get("resolutionPnL") or r.get("pnl") or {}
        res_value = float(res_pnl.get("value", 0)) if isinstance(res_pnl, dict) else 0
        side = "LONG" if net > 0 else "SHORT"
        slug = md.get("slug", "?")
        # The "loss" from holding = what we paid for the unsold portion
        # Approximation: avg cost of net held shares
        avg_cost_net = cost / max(int(bought), 1)
        held_cost = avg_cost_net * abs(net)
        held_loss_total += -held_cost  # worst case; resolution pays $0 if outcome=losing side
        print(f"  {slug}")
        print(f"    {title} → {outcome}")
        print(f"    bought={bought} sold={sold} netHeld={net} ({side})")
        print(f"    cost=${cost:.3f} realized=${realized:.3f} held_value≈${held_cost:.3f}")

    print(f"\n  Total positions held to expiry: {held_count}")
    print(f"  Approx untracked loss from held positions: ${held_loss_total:.3f}")

    # 4. Per-arb reconciliation: join executions.csv against trades
    if not exec_path or not os.path.exists(exec_path):
        print(f"\n(no executions.csv at {exec_path}, skipping per-arb join)")
        return

    exec_rows = list(csv.DictReader(open(exec_path)))
    exec_orders = {r["order_id"] for r in exec_rows if r["order_id"]}

    # Build trade -> our-order map
    our_trade_oids = set()
    trade_by_oid = {}
    for t in trades:
        agg_o = (t.get("aggressorExecution") or {}).get("order") or {}
        if agg_o.get("id") in exec_orders:
            our_trade_oids.add(agg_o["id"])
            trade_by_oid[agg_o["id"]] = t
        for m in (t.get("makerExecutions") or []):
            mo = m.get("order") or {}
            if mo.get("id") in exec_orders:
                our_trade_oids.add(mo["id"])
                trade_by_oid[mo["id"]] = t

    # Find logged orders that DIDN'T trade (BUY claimed success but no fill)
    logged_buys = [r for r in exec_rows if r["action"] == "BUY"]
    logged_sells = [r for r in exec_rows if r["action"] in ("SELL_TIMEOUT", "SELL_PRICE_DROP", "SELL_CONVERGED")]

    print("\n" + "=" * 78)
    print("BUYs LOGGED AS FILLED — verifying against trades")
    print("=" * 78)
    phantom_buys = []
    for r in logged_buys:
        if r["order_id"] not in our_trade_oids:
            phantom_buys.append(r)
    if not phantom_buys:
        print(f"  All {len(logged_buys)} logged BUYs found in trade history ✓")
    else:
        print(f"  {len(phantom_buys)} BUYs logged as filled but no matching trade:")
        for r in phantom_buys:
            print(f"    {r['timestamp']} {r['game']} buy=${r['buy_price']} oid={r['order_id'][:12]}")

    print("\n" + "=" * 78)
    print("SELLs LOGGED — verifying maker oid OR taker exit actually filled")
    print("=" * 78)
    print("  (Note: SELL_* rows log the maker-sell oid. Taker exit oid is NOT logged.)")
    print("  Per-game position math is the truer signal — see below.")

    # 5. Per-market position math from our log vs Polymarket truth
    # Aggregate: for each market_slug, sum logged BUY qty and SELL qty (assume qty=1)
    print("\n" + "=" * 78)
    print("PER-MARKET POSITION RECONCILIATION (logged vs Polymarket)")
    print("=" * 78)

    # Logged: count BUY actions per slug (these we believe filled)
    logged_long_buys = defaultdict(int)
    logged_short_buys = defaultdict(int)
    logged_long_sells = defaultdict(int)
    logged_short_sells = defaultdict(int)
    for r in exec_rows:
        slug = r["market_slug"]
        if not slug:
            continue
        intent = r["intent"]
        if r["action"] == "BUY":
            if "SHORT" in intent:
                logged_short_buys[slug] += 1
            else:
                logged_long_buys[slug] += 1
        elif r["action"] in ("SELL_TIMEOUT", "SELL_PRICE_DROP", "SELL_CONVERGED"):
            if "SHORT" in intent:
                logged_short_sells[slug] += 1
            else:
                logged_long_sells[slug] += 1

    # Polymarket truth: from trade history, count fills per slug per intent
    poly_long_buys = defaultdict(int)
    poly_short_buys = defaultdict(int)
    poly_long_sells = defaultdict(int)
    poly_short_sells = defaultdict(int)
    for oid in our_trade_oids:
        t = trade_by_oid[oid]
        # Find which side (aggressor or maker) is ours
        agg_o = (t.get("aggressorExecution") or {}).get("order") or {}
        if agg_o.get("id") == oid:
            o = agg_o
            qty = int((t.get("aggressorExecution") or {}).get("quantity") or o.get("cumQuantity") or 0)
        else:
            for m in (t.get("makerExecutions") or []):
                mo = m.get("order") or {}
                if mo.get("id") == oid:
                    o = mo
                    qty = int(m.get("quantity") or 0)
                    break
            else:
                continue
        intent = o.get("intent", "")
        slug = o.get("marketSlug", "")
        if intent == "ORDER_INTENT_BUY_LONG":
            poly_long_buys[slug] += qty
        elif intent == "ORDER_INTENT_BUY_SHORT":
            poly_short_buys[slug] += qty
        elif intent == "ORDER_INTENT_SELL_LONG":
            poly_long_sells[slug] += qty
        elif intent == "ORDER_INTENT_SELL_SHORT":
            poly_short_sells[slug] += qty

    all_slugs = (set(logged_long_buys) | set(logged_short_buys) | set(logged_long_sells)
                 | set(logged_short_sells) | set(poly_long_buys) | set(poly_short_buys)
                 | set(poly_long_sells) | set(poly_short_sells))

    discrepancies = []
    for slug in sorted(all_slugs):
        lb_l, lb_s = logged_long_buys[slug], logged_short_buys[slug]
        ls_l, ls_s = logged_long_sells[slug], logged_short_sells[slug]
        pb_l, pb_s = poly_long_buys[slug], poly_short_buys[slug]
        ps_l, ps_s = poly_long_sells[slug], poly_short_sells[slug]
        log_long_net = lb_l - ls_l
        log_short_net = lb_s - ls_s
        poly_long_net = pb_l - ps_l
        poly_short_net = pb_s - ps_s
        if log_long_net != poly_long_net or log_short_net != poly_short_net:
            discrepancies.append((slug, log_long_net, poly_long_net, log_short_net, poly_short_net,
                                  lb_l, ls_l, pb_l, ps_l, lb_s, ls_s, pb_s, ps_s))

    if not discrepancies:
        print("  All logged positions match Polymarket truth ✓")
    else:
        print(f"  {len(discrepancies)} markets with logged-vs-truth mismatch:")
        print(f"  {'slug':50s}  LONG(log/poly)   SHORT(log/poly)")
        for d in discrepancies:
            slug, lln, pln, lsn, psn, lbl, lsl, pbl, psl, lbs, lss, pbs, pss = d
            print(f"  {slug:50s}  {lln:+d}/{pln:+d}        {lsn:+d}/{psn:+d}")
            print(f"    LONG  log: bought={lbl} sold={lsl}    poly: bought={pbl} sold={psl}")
            print(f"    SHORT log: bought={lbs} sold={lss}    poly: bought={pbs} sold={pss}")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  Currently open positions: {sum(1 for p in open_positions.values() if int(p.get('netPosition',0))!=0)}  (cost ${open_cost_total:.2f})")
    print(f"  Held-to-expiry positions: {held_count}  (~${held_loss_total:.2f})")
    print(f"  Logged BUYs without matching trade: {len(phantom_buys)}")
    print(f"  Markets with logged-vs-truth mismatch: {len(discrepancies)}")


if __name__ == "__main__":
    exec_path = sys.argv[1] if len(sys.argv) > 1 else "executions.csv"
    main(exec_path)
