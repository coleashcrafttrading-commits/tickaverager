"""Is 'buying slowing down' the loss, or is our PROXY for it the loss?

He states it as an exit indicator, so dropping it is a fidelity cost. But an
implementation that loses money on 53% of exits while his other three
detectors are flat-to-positive is more likely a bad proxy than a bad rule.
Train and test reported separately; nothing is chosen on test.
"""
import json, collections, ross, tapeexit
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
cut=dates[int(.6*len(dates))]
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)

def run(split, enabled):
    tapeexit.ENABLED = enabled
    eq=2000.0; tr=[]
    for d in dates:
        if split=='train' and d>=cut: continue
        if split=='test' and d<cut: continue
        if not wl[d]['picks']: continue
        r=ross.run_day(d, wl[d]['picks'], hist, eq, cfg)
        eq=r['equity_end']; tr+=r['trades']
        if eq<=0: break
    w=[t for t in tr if t['pl']>0]
    return eq, len(tr), 100*len(w)/max(1,len(tr)), collections.Counter(t['reason'] for t in tr)

ALL=['resting_seller','hidden_seller','red_burst','buying_slowing']
NO5=['resting_seller','hidden_seller','red_burst']
print('  %-30s %6s %10s %5s %7s' % ('','split','final','n','win%'), flush=True)
for lab, en in (('all six indicators', ALL), ('without buying_slowing', NO5)):
    for split in ('train','test'):
        eq,n,win,c = run(split, en)
        print('  %-30s %6s $%9.2f %5d %6.1f%%   %s'
              % (lab, split, eq, n, win, dict(c.most_common(3))), flush=True)
print('DONE', flush=True)
