"""Does a looser pause catch the movers our structure test is filtering out?

The universe's return is carried by rare huge moves (mean +14.81% vs median
+1.59%). Our entry requires a resolvable pause -- and the stocks that run 200%
do not pause in a way any bar aggregation resolves. He enters on hesitation so
slight it is a wick. This sweeps the pause definition from his stated structure
down to the smallest thing that still counts as one, measured per trade in
percent against simply buying the hit.
"""
import json, statistics, ross, setups
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)

def measure(label, secs, retrace, pause_max, taper, gates):
    ross.P['bar_seconds']=(secs,'his',''); ross.P['max_retrace']=(retrace,'his','')
    ross.P['pullback_max']=(pause_max,'his',''); ross.P['taper_volume']=(taper,'his','')
    for k,v in gates.items(): ross.P[k]=(v,'his','')
    got=[]; base=[]
    for d in dates:
        for r in wl[d]['picks']:
            days=hist.get(r['symbol']) or {}; bars=days.get(d) or []
            if len(bars)<120: continue
            q=ross.qualified(bars,r,ross.volume_baseline(days,d,ross.p('rvol_window')))
            if not q: continue
            seg=bars[q['bar']:]
            if len(seg)<60: continue
            bN=float(seg[-1]['c']); b0=float(seg[0]['c'])
            base.append(100*(bN-b0)/b0)
            closes=[float(x['c']) for x in seg]; m,sg=ross.macd(closes)
            ctx={'vwap':ross.vwap_session(seg),'ema9':ross.ema(closes,9),'macd':m,'sig':sg,
                 'pullback_idx':1,'hod_i':None,'or_high':None,'or_low':None,'pm_high':None}
            hod=-1e9
            for i in range(20,len(seg)-1):
                if float(seg[i]['h'])>hod: hod=float(seg[i]['h']); ctx['hod_i']=i; ctx['pullback_idx']=1
                if ross.find_pullback(seg,i,ctx):
                    e=float(seg[i+1]['o']); got.append(100*(bN-e)/e); break
    if got:
        print('  %-44s n=%4d  mean %+7.2f%%  median %+6.2f%%  (hit: %+6.2f%%)'
              % (label, len(got), statistics.mean(got), statistics.median(got),
                 statistics.mean(base)), flush=True)

print('LOOSENING THE PAUSE -- can we stop filtering out the movers?', flush=True)
print(flush=True)
G={'require_vwap':True,'require_ema9':True,'require_macd':True,'max_pullback_idx':2}
measure('his structure (as built)', 10, 0.25, 3, True, G)
measure('retrace 50% (his outer bound)', 10, 0.50, 3, True, G)
measure('no volume taper required', 10, 0.50, 3, False, G)
measure('pause may be 1 bar only', 10, 0.50, 1, False, G)
measure('+ no MACD gate', 10, 0.50, 1, False, dict(G, require_macd=False))
measure('+ any pullback, not just 1st/2nd', 10, 0.50, 1, False,
        dict(G, require_macd=False, max_pullback_idx=99))
measure('5-second bars, loosest', 5, 0.50, 1, False,
        dict(G, require_macd=False, max_pullback_idx=99))
print('DONE', flush=True)
