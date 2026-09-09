import json, statistics, ross
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)

# DIAGNOSTIC ONLY. A trailing stop is NOT his rule. The question is narrower:
# is the 1.52R of favourable excursion reachable by ANY causal rule, or is it
# only visible with hindsight? If a trailing stop cannot capture it either,
# the excursion is an artifact of measuring the maximum after the fact.
def replay(trail_r, cap_bars=20):
    tot=0.0; n=0; wins=0; holds=[]
    for d in dates:
        for r in wl[d]['picks']:
            days=hist.get(r['symbol']) or {}; bars=days.get(d) or []
            if not bars: continue
            q=ross.qualified(bars,r,ross.volume_baseline(days,d,ross.p('rvol_window')))
            if not q: continue
            seg=bars[q['bar']:]
            for t in ross.run_symbol(r['symbol'],seg,2000.0,dict(cfg)):
                i0=next((i for i,b in enumerate(seg) if str(b['t'])==str(t['t'])),None)
                if i0 is None: continue
                e=t['entry']; risk=t['risk']; stop=t['stop']
                peak=e; px=None
                for b in seg[i0:i0+cap_bars]:
                    lo,hi,c=float(b['l']),float(b['h']),float(b['c'])
                    if lo<=stop: px=stop; break
                    peak=max(peak,hi)
                    ns_=peak-trail_r*risk          # trail behind the high water mark
                    stop=max(stop,ns_)
                if px is None: px=float(seg[min(i0+cap_bars,len(seg))-1]['c'])
                pl=(px-0.05-e)*t['shares']
                tot+=pl; n+=1; wins+= pl>0
    return tot,n,wins

print('DIAGNOSTIC: is the favourable excursion reachable causally?', flush=True)
print('  (a trailing stop is NOT his rule -- this only tests whether the', flush=True)
print('   1.52R median excursion can be captured without hindsight)', flush=True)
print(flush=True)
print('  %-22s %12s %8s %8s' % ('rule','total P/L','trades','win%'), flush=True)
for tr_ in (0.5,1.0,1.5,2.0,3.0):
    tot,n,w=replay(tr_)
    print('  trail %.1fR behind high  $%11.2f %8d %7.1f%%' % (tr_,tot,n,100*w/max(1,n)), flush=True)
print(flush=True)
print('  bar-rule baseline for the same entries: -$1267.61, win 17.7%', flush=True)
print('DONE', flush=True)
