"""Growth of a $10 live account under the current strategy (analysis script, not engine code).

Replays the strategy's real historical trades (2022-12 to 2026-11, 6 pairs, M15, net of replay costs) through each
account type's rules: pre-route lot feasibility, fixed or percent risk, loss throttle, daily/weekly/drawdown
halts, loss-streak halt with weekly reset, one trade at a time. Run from fx_ai_engine/:

    ../.venv/bin/python artifacts/research/small-account-sim/sim10_small_account_growth.py

Hypothetical-edge rows shift every trade's R by a constant; they are NOT the current strategy.
"""
import numpy as np, pandas as pd, math, json
df=pd.read_csv('artifacts/replay/2026-11-03_edge/baseline__policy__edge__base_trades.csv',parse_dates=['entry_time','exit_time'])
df=df.sort_values('entry_time').reset_index(drop=True)
PV={'EURUSD':0.10,'GBPUSD':0.10,'AUDUSD':0.10,'USDJPY':0.0631,'USDCAD':0.0717,'USDCHF':0.1223}  # $ per pip per 0.01 lot
df['pv01']=df.symbol.map(PV)
df['date']=df.entry_time.dt.date
iso=df.entry_time.dt.isocalendar()
df['wk']=iso.year.astype(int)*100+iso.week.astype(int)
# structure: weeks -> days -> trades
weeks=[]
for wk,g in df.groupby('wk',sort=True):
    days=[]
    for d,gg in g.groupby('date',sort=True):
        days.append([(r.entry_time.value,r.exit_time.value,r.r_multiple,r.stop_pips,r.pv01) for r in gg.itertuples()])
    weeks.append(days)
print(f"{len(df)} trades, {sum(len(w) for w in weeks)} trading days, {len(weeks)} ISO weeks, mean R {df.r_multiple.mean():+.3f}")

MILESTONES=[(500,6.0),(200,3.0),(100,1.5*2),(50,1.5),(20,0.75),(0,0.50)]  # (min equity, fixed risk $) per repo phases; refined below
def preserve_risk(eq):
    for lo,r in ((200,6.0),(100,3.0),(50,1.5),(20,0.75),(0,0.50)):
        if eq>=lo: return r
SCEN={
 # name: dict(kind, dailyP, weeklyP, ddP, streak, throttle, risk fn, standard-lot feasibility)
 'A standard acct, core SRS 3.2%':   dict(risk='pct',pct=0.032,daily=.08,weekly=.15,dd=.20,streak=3,thr={1:.75,2:.5},std=True),
 'B cent acct, core SRS 3.2%':       dict(risk='pct',pct=0.032,daily=.08,weekly=.15,dd=.20,streak=3,thr={1:.75,2:.5},std=False),
 'C cent acct, Preserve-$10 ($0.50)':dict(risk='fix',daily=.15,weekly=.25,dd=.30,streak=2,thr={1:.75},std=False),
}
def simulate(sc, week_seq, target=20.0, start=10.0, max_days=None, shift=0.0, first_day_only=False):
    eq=start; peak=start; streak=0; last_exit=-1
    n_trades=0; hit_day=None; day_idx=0; halted=False; daily_stops=0; rejected_lot=0
    for w in week_seq:
        wk_start=eq; wk_stopped=False
        if streak>=sc['streak']: streak=0      # weekly operator reset of loss-streak halt (as in replay)
        for day in w:
            day_idx+=1
            day_start=eq; day_stopped=False
            for (te,tx,r,stop,pv) in day:
                if te<last_exit: continue                 # one trade at a time
                if wk_stopped or day_stopped: continue
                if streak>=sc['streak']: continue
                thr=sc['thr'].get(streak,1.0)
                if sc['risk']=='pct': risk=sc['pct']*eq*thr
                else: risk=preserve_risk(eq)*thr
                if sc['std']:
                    k=math.floor(risk/(stop*pv)+1e-9)       # whole 0.01 lots
                    if k<1: rejected_lot+=1; continue
                    risk=k*stop*pv
                eq+=risk*(r+shift); n_trades+=1; last_exit=tx
                streak = streak+1 if r+shift<0 else (0 if r+shift>0 else streak)
                peak=max(peak,eq)
                if hit_day is None and eq>=target: hit_day=day_idx
                if eq<=0.10: halted=True; break
                if (peak-eq)/peak>=sc['dd']: halted=True; break
                if (day_start-eq)/day_start>=sc['daily']: day_stopped=True; daily_stops+=1
                if (wk_start-eq)/wk_start>=sc['weekly']: wk_stopped=True
            if halted or (max_days and day_idx>=max_days) or first_day_only: break
        if halted or (max_days and day_idx>=max_days) or first_day_only: break
    return dict(eq=eq,trades=n_trades,hit=hit_day,halted=halted,daily_stops=daily_stops,rej=rejected_lot)

def report24(sc, shift=0.0):
    out=[]
    for w in weeks:
        for i in range(len(w)):
            out.append(simulate(sc,[[w[i]]],first_day_only=True,shift=shift))
    e=np.array([o['eq'] for o in out]); t=np.array([o['trades'] for o in out])
    return dict(days=len(e),mean=e.mean(),median=np.median(e),p5=np.percentile(e,5),p95=np.percentile(e,95),
                p_up=(e>10.0001).mean(),p_flat=(abs(e-10)<1e-9).mean(),p_down=(e<9.9999).mean(),
                p20=(e>=20).mean(),trades=t.mean(),p_notrade=(t==0).mean(),
                p_dstop=np.mean([o['daily_stops']>0 for o in out]),mx=e.max(),mn=e.min())
print("\n=== 24-HOUR RESULTS: one UTC trading day from a fresh $10, every historical weekday (N=%d) ==="%sum(len(w) for w in weeks))
for name,sc in SCEN.items():
    r=report24(sc)
    print(f"{name:36s} mean ${r['mean']:.2f} median ${r['median']:.2f} p5 ${r['p5']:.2f} p95 ${r['p95']:.2f} | up {r['p_up']*100:4.1f}% flat/no-trade {r['p_flat']*100:4.1f}% down {r['p_down']*100:4.1f}% | reach $20 in a day {r['p20']*100:.2f}% (best ${r['mx']:.2f}, worst ${r['mn']:.2f}) | trades/day {r['trades']:.2f} | daily-stop hit {r['p_dstop']*100:.1f}%")

# multi-day: week-block bootstrap, horizon 250 trading days
rng=np.random.default_rng(7)
def multi(sc,shift=0.0,sims=3000,max_days=250):
    res=[]
    for _ in range(sims):
        idx=rng.integers(len(weeks),size=max_days//5+2)
        res.append(simulate(sc,[weeks[i] for i in idx],max_days=max_days,shift=shift))
    hit=np.array([o['hit'] if o['hit'] is not None else np.nan for o in res],float)
    e=np.array([o['eq'] for o in res]); h=np.array([o['halted'] for o in res])
    def pr(d): return np.nanmean(np.where(np.isnan(hit),0,hit<=d))*1.0 if False else float(np.mean([(x==x) and x<=d for x in hit]))
    return dict(p30=pr(30),p90=pr(90),p250=pr(250),med_hit=(np.nanmedian(hit) if np.isfinite(hit).any() else float('nan')),
                halted=h.mean(),med_end=np.median(e),p_end_gt10=(e>10).mean(),mean_end=e.mean(),
                trades=np.mean([o['trades'] for o in res]))
print("\n=== TIME TO REACH $20 (+$10), week-block bootstrap of real history, 250 trading days, 3000 paths ===")
for name,sc in SCEN.items():
    r=multi(sc)
    print(f"{name:36s} P($20 by day 30) {r['p30']*100:5.1f}% | by 90 {r['p90']*100:5.1f}% | by 250 {r['p250']*100:5.1f}% | median day if reached {r['med_hit']} | drawdown-halted {r['halted']*100:4.1f}% | median end ${r['med_end']:.2f} | ended above $10 {r['p_end_gt10']*100:.1f}%")
print("\n=== HYPOTHETICAL: same trades, expectancy shifted (NOT the current strategy) ===")
for shift,label in ((0.35,'~0.00R'),(0.65,'~+0.30R'),(0.95,'~+0.60R')):
    for name in ('B cent acct, core SRS 3.2%','C cent acct, Preserve-$10 ($0.50)'):
        r=multi(SCEN[name],shift=shift,sims=2000)
        r24=report24(SCEN[name],shift=shift)
        print(f"E~{label:7s} {name:36s} 24h mean ${r24['mean']:.2f} P(>$10) {r24['p_up']*100:4.1f}% | P($20 by 30d) {r['p30']*100:5.1f}% by 90d {r['p90']*100:5.1f}% by 250d {r['p250']*100:5.1f}% | median day {r['med_hit']} | halted {r['halted']*100:.1f}%")
