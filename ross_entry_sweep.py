"""Entry-quality sweep: the places his stated rule has a tight and a loose reading."""
import json, statistics, ross
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)

def run(label, over, cost=None):
    saved={k:ross.P[k] for k in list(over)+list(cost or {})}
    for k,v in list(over.items())+list((cost or {}).items()):
        ross.P[k]=(v,ross.P[k][1],ross.P[k][2])
    try:
        eq=2000.0; curve=[eq]; tr=[]
        for d in dates:
            if not wl[d]['picks']: continue
            r=ross.run_day(d, wl[d]['picks'], hist, eq, cfg)
            eq=r['equity_end']; tr+=r['trades']; curve.append(eq)
            if eq<=0: break
        w=[t for t in tr if t['pl']>0]
        risks=sorted(t['risk'] for t in tr) or [0]
        print('  %-40s $%8.2f %7.1f%% %4d tr  win %4.1f%%  med stop $%.3f'
              % (label, eq, 100*(eq/2000-1), len(tr), 100*len(w)/max(1,len(tr)),
                 statistics.median(risks)), flush=True)
    finally:
        for k,v in saved.items(): ross.P[k]=v

# his own tighter readings, at the friction this account actually pays
COST={'slip_entry':0.03,'slip_exit':0.03,'stop_slip_mult':1.0}
print('AT THE MEASURED 3c HALF-SPREAD, sweeping the tight-vs-loose readings', flush=True)
print(flush=True)
run('as configured (retrace 50%)', {}, COST)
run('retrace 25% -- "top 25% of the move"', {'max_retrace':0.25}, COST)
run('retrace 33%', {'max_retrace':0.33}, COST)
run('retrace 25% + pause max 2 bars', {'max_retrace':0.25,'pullback_max':2}, COST)
run('retrace 25% + 1st/2nd pullback only', {'max_retrace':0.25,'max_pullback_idx':1}, COST)
run('retrace 25% + stop cap 10c', {'max_retrace':0.25,'stop_cap':0.10}, COST)
run('retrace 25% + reject stop over 20c', {'max_retrace':0.25,'stop_reject':0.20}, COST)
print(flush=True)
print('SAME, at zero friction (to isolate signal from cost)', flush=True)
Z={'slip_entry':0.0,'slip_exit':0.0,'stop_slip_mult':1.0,'fee_per_share':0.0}
run('as configured (retrace 50%)', {}, Z)
run('retrace 25%', {'max_retrace':0.25}, Z)
run('retrace 25% + stop cap 10c', {'max_retrace':0.25,'stop_cap':0.10}, Z)
print('DONE', flush=True)
