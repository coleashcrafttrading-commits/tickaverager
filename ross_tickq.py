"""Does the TICK pullback stop filtering out the movers?

Same test as before, same held-to-cutoff comparison: if detecting the pullback
live off the tape rather than from closed bars recovers the -8.15% the bar
detector was costing, the aggregation was the problem all along.
"""
import json, statistics, ross, tape, tickpullback
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)
h=tape._headers()

base=[]; bar=[]; tick=[]; nset=[]
for d in dates:
    for r in wl[d]['picks']:
        days=hist.get(r['symbol']) or {}; bars=days.get(d) or []
        if len(bars)<120: continue
        q=ross.qualified(bars,r,ross.volume_baseline(days,d,ross.p('rvol_window')))
        if not q: continue
        seg=bars[q['bar']:]
        if len(seg)<60: continue
        b0=float(seg[0]['c']); bN=float(seg[-1]['c'])
        base.append(100*(bN-b0)/b0)
        # the same window, from the tape
        t0=str(seg[0].get('et',''))[11:16]; t1=str(seg[-1].get('et',''))[11:16]
        if not t0 or not t1 or t1<=t0: continue
        try: w=tape.window(r['symbol'], d, t0, t1, headers=h)
        except Exception: continue
        pr=w['prints']
        if len(pr)<50: continue
        sets=tickpullback.gated(pr, w['quotes'])
        nset.append(len(sets))
        if sets:
            e=sets[0]['fill']
            last=pr[-1]['p']
            tick.append(100*(last-e)/e)
print('TICK PULLBACK vs BAR PULLBACK -- both held to the same cutoff')
print()
print('  buy the scanner hit, hold     n=%4d  mean %+7.2f%%  median %+6.2f%%'
      % (len(base), statistics.mean(base), statistics.median(base)))
if tick:
    print('  buy the TICK pullback, hold   n=%4d  mean %+7.2f%%  median %+6.2f%%'
          % (len(tick), statistics.mean(tick), statistics.median(tick)))
    print()
    print('  bar detector was worth        -8.15%% per trade vs buying the hit')
    print('  tick detector is worth        %+.2f%% per trade vs buying the hit'
          % (statistics.mean(tick)-statistics.mean(base)))
if nset:
    print()
    print('  setups per qualified name: mean %.1f, median %d, max %d'
          % (statistics.mean(nset), statistics.median(nset), max(nset)))
print('DONE')
