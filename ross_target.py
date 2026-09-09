"""How far is his first target, really?

For 71% of trades to be winners, most must reach the first target before the
stop. Ours sits at 2R and only ~38% of trades ever get there; ~54% reach 1R.
His own sources give THREE different first targets and they are not the same
distance: 2R (momentum page), 1R (flat-top flashcard), and a flat 10-15 cents
(the micro-pullback page, which on a 5c stop is 2-3R and on a 20c stop is under
1R). All three are his. Run them all.
"""
import json, ross, tapeexit
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
cut=dates[int(.6*len(dates))]
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)
tapeexit.ENABLED=['resting_seller','hidden_seller','red_burst']

def go(label, tgt, scale, setups):
    ross.P['target_r']=(tgt,'his',''); ross.P['scale_frac']=(scale,'his','')
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

FIVE=['micro_pullback','gap_and_go','flat_top','bull_flag','first_pullback','abcd']
for tgt in (2.0, 1.5, 1.0, 0.75):
    go('all setups, target %.2fR, half off' % tgt, tgt, 0.50, FIVE)
print(flush=True)
for sc in (0.50, 0.75):
    go('target 1.0R, %.0f%% off' % (100*sc), 1.0, sc, FIVE)
print('DONE', flush=True)
