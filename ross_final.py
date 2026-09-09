"""All five of his setups, with the one detector I know is a bad proxy off.

buying_slowing is HIS rule. This implementation of it is mine and it is bad:
101 exits, -$822, while his other three detectors are flat-to-positive on the
same trades. Turning it off is not a claim about his method, it is an admission
about my proxy for it, and it stays flagged as the top gap.
Train and test reported separately; nothing chosen on test.
"""
import json, collections, ross, tapeexit
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
cut=dates[int(.6*len(dates))]
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)

def go(label, detectors, setups):
    tapeexit.ENABLED=detectors
    ross.P['setups']=(tuple(setups),'his','')
    for split in ('train','test'):
        eq=2000.0; tr=[]
        for d in dates:
            if split=='train' and d>=cut: continue
            if split=='test' and d<cut: continue
            if not wl[d]['picks']: continue
            r=ross.run_day(d, wl[d]['picks'], hist, eq, cfg)
            eq=r['equity_end']; tr+=r['trades']
            if eq<=0: break
        w=[t for t in tr if t['pl']>0]
        print('  %-34s %6s $%9.2f %7.1f%% %5d tr  win %5.1f%%'
              % (label, split, eq, 100*(eq/2000-1), len(tr), 100*len(w)/max(1,len(tr))), flush=True)

ALL4=['resting_seller','hidden_seller','red_burst','buying_slowing']
NO5=['resting_seller','hidden_seller','red_burst']
FIVE=['micro_pullback','gap_and_go','flat_top','bull_flag','first_pullback','abcd']
CORE=['micro_pullback']

go('all setups, all 4 detectors', ALL4, FIVE)
go('all setups, no buying_slowing', NO5, FIVE)
go('micro pullback only, no b_slowing', NO5, CORE)
print('DONE', flush=True)
