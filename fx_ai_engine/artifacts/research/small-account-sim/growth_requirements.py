"""What a strategy must deliver to grow a $10 cent account (analysis script, not engine code).

Synthetic trades with a fixed 2.2R target bracket: a win pays +2.2R minus cost, a loss costs -1R minus cost
(cost_r = round-trip cost in pips / stop pips). Account rules are the core SRS rules: percent risk with loss
throttle {1: 0.75, 2: 0.5}, 3-loss halt (cleared at the next ISO week), daily 8%, weekly 15%, drawdown 20% halt.
"""
import numpy as np
RNG = np.random.default_rng(11)
RR = 2.2
THR = np.array([1.0, 0.75, 0.5, 0.0])

def expectancy(p, c): return p * (RR - c) - (1 - p) * (1 + c)

def sim(p, cost_r, f, trades_per_day, days=250, paths=20000, start=10.0, target=20.0):
    eq = np.full(paths, start); peak = eq.copy(); streak = np.zeros(paths, int)
    halted = np.zeros(paths, bool); hit = np.full(paths, np.nan)
    wk_start = eq.copy(); wk_stop = np.zeros(paths, bool)
    tpd = np.resize(np.array(trades_per_day), days)
    for d in range(days):
        if d % 5 == 0:
            wk_start = eq.copy(); wk_stop[:] = False; streak = np.where(streak >= 3, 0, streak)
        day_start = eq.copy(); day_stop = np.zeros(paths, bool)
        for _ in range(int(tpd[d])):
            act = ~halted & ~wk_stop & ~day_stop & (streak < 3)
            if not act.any(): break
            win = RNG.random(paths) < p
            r = np.where(win, RR - cost_r, -1 - cost_r)
            risk = f * eq * THR[np.minimum(streak, 3)]
            eq = np.where(act, eq + risk * r, eq)
            streak = np.where(act, np.where(r < 0, streak + 1, 0), streak)
            peak = np.maximum(peak, eq)
            halted |= act & ((peak - eq) / peak >= 0.20)
            day_stop |= act & ((day_start - eq) / day_start >= 0.08)
            wk_stop |= act & ((wk_start - eq) / wk_start >= 0.15)
            newhit = act & np.isnan(hit) & (eq >= target)
            hit[newhit] = d + 1
    return hit, eq, halted

print("Break-even win rate at R:R 2.2 = (1 + c) / (3.2 - ... ) -> p* = (1 + c) / (RR + 1)")
print("round-trip cost 2.5 pips: cost in R by stop size and the win rate needed to break even")
for stop in (8, 12, 20, 25, 30, 40, 60):
    c = 2.5 / stop
    print(f"  stop {stop:>2} pips: cost {c:5.3f}R  break-even win rate {(1 + c) / (RR + 1) * 100:5.1f}%   (zero-cost: {1 / (RR + 1) * 100:.1f}%)")

print("\nGrowth of $10 under core SRS rules; cost 0.10R (about a 25-pip stop); 250 trading days; 20,000 paths")
print("trades/day | risk | win rate -> expectancy | P($20 by 60d / 120d / 250d) | median day if reached | drawdown-halted | median end | mean end")
for tpd, label in (([1, 2], "1.5"), ([1], "1.0")):
    for f in (0.015, 0.032):
        for p in (0.30, 0.35, 0.40, 0.45, 0.50):
            c = 0.10
            hit, eq, h = sim(p, c, f, tpd)
            def pr(d): return np.mean(~np.isnan(hit) & (hit <= d))
            med = np.nanmedian(hit) if np.isfinite(hit).any() else float("nan")
            print(f"  {label:>3}      | {f*100:3.1f}% | {p*100:3.0f}% -> {expectancy(p, c):+.2f}R | {pr(60)*100:5.1f}% / {pr(120)*100:5.1f}% / {pr(250)*100:5.1f}% | {med:6.1f} | {h.mean()*100:5.1f}% | ${np.median(eq):6.2f} | ${eq.mean():7.2f}")
print("\nExpected dollars per trade on a $10 account: risk$ x expectancy")
for f in (0.015, 0.032, 0.05):
    print("  risk %.1f%% ($%.2f): " % (f * 100, f * 10) + ", ".join(f"E={e:+.2f}R -> ${f*10*e:+.3f}" for e in (0.1, 0.25, 0.5)))
print("\nTrades needed to double with compounding g = f*E per trade: n = ln2 / ln(1+g)")
for f in (0.015, 0.032):
    print("  risk %.1f%%: " % (f * 100) + ", ".join(f"E={e:+.2f}R -> {np.log(2)/np.log(1+f*e):4.0f} trades" for e in (0.1, 0.25, 0.5)))
print("\nKelly fraction for win rate p, payoff b=2.2-c: f* = p - (1-p)/b   (full Kelly; use <= half)")
for p in (0.35, 0.40, 0.45):
    b = RR - 0.10
    print(f"  p={p*100:.0f}%: full Kelly {(p - (1-p)/b)*100:5.1f}%  half Kelly {(p - (1-p)/b)*50:5.1f}%")
