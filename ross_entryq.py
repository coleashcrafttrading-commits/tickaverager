"""Do our ENTRIES add value, or only subtract it?

Buying every scanner hit at the moment it qualifies and holding to the cutoff
returns +13.70% per hit. If entering at our micro-pullback trigger and holding
the SAME way returns less than that, the entry rule is destroying value and no
exit can rescue it. Measured per-trade in percent, so position sizing and
compounding cannot contaminate the comparison.
"""
import json, statistics, ross
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)
import setups

base=[]; ent=[]
for d in dates:
    for r in wl[d]['picks']:
        days=hist.get(r['symbol']) or {}; bars=days.get(d) or []
        if len(bars)<120: continue
        q=ross.qualified(bars,r,ross.volume_baseline(days,d,ross.p('rvol_window')))
        if not q: continue
        seg=bars[q['bar']:]
        if len(seg)<60: continue
        # BASELINE: buy the qualifying bar's close, hold to the cutoff
        b0=float(seg[0]['c']); bN=float(seg[-1]['c'])
        base.append(100*(bN-b0)/b0)
        # OURS: buy at the first trigger our rules produce, hold to the cutoff
        closes=[float(x['c']) for x in seg]; m,sg=ross.macd(closes)
        op=[x for x in seg if '09:30'<=str(x.get('et',''))[11:16]<'09:31']
        pre=[x for x in seg if str(x.get('et',''))[11:16]<'09:30']
        ctx={'vwap':ross.vwap_session(seg),'ema9':ross.ema(closes,9),'macd':m,'sig':sg,
             'pullback_idx':1,'hod_i':None,
             'or_high':max((float(x['h']) for x in op),default=None),
             'or_low':min((float(x['l']) for x in op),default=None),
             'pm_high':max((float(x['h']) for x in pre),default=None)}
        hod=-1e9
        for i in range(30,len(seg)-1):
            if float(seg[i]['h'])>hod: hod=float(seg[i]['h']); ctx['hod_i']=i; ctx['pullback_idx']=1
            pl=setups.detect(seg,i,ctx,list(ross.p('setups')))
            if pl:
                e=float(seg[i+1]['o'])
                ent.append(100*(bN-e)/e)
                break
print('ENTRY QUALITY -- both held to the same cutoff, measured per trade in %%')
print()
print('  buy the scanner hit, hold      n=%4d  mean %+6.2f%%  median %+6.2f%%'
      % (len(base), statistics.mean(base), statistics.median(base)))
print('  buy OUR trigger, hold          n=%4d  mean %+6.2f%%  median %+6.2f%%'
      % (len(ent), statistics.mean(ent), statistics.median(ent)))
if base and ent:
    print()
    print('  our entry rule is worth %+.2f%% per trade against simply buying the hit'
          % (statistics.mean(ent)-statistics.mean(base)))
print('DONE')
