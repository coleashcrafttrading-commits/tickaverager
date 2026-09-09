"""Which exit actually beats holding?

The universe returns +13.70% per hit if you buy what the scanner flags and hold
to the cutoff. Every exit rule we have built underperforms that. This tests the
exits against each other and against doing nothing, on the full sample.
"""
import json, collections, ross, tapeexit
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
cut=dates[int(.6*len(dates))]
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)

def go(label, mode, detectors, first_red=True, bail=2):
    ross.P['exit_mode']=(mode,'pick','')
    ross.P['first_red_exits']=(first_red,'his','')
    ross.P['bailout_bars']=(bail,'his','')
    tapeexit.ENABLED=detectors
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
        aw=sum(t['pl'] for t in w)/max(1,len(w))
        l=[t for t in tr if t['pl']<=0]
        al=abs(sum(t['pl'] for t in l)/max(1,len(l)))
        print('  %-38s %6s $%9.2f %8.1f%% %5d tr win %5.1f%% W/L %.2f'
              % (label, split, eq, 100*(eq/2000-1), len(tr), 100*len(w)/max(1,len(tr)),
                 aw/al if al else 0), flush=True)

ALL=['resting_seller','hidden_seller','red_burst','buying_slowing']
go('all four tape detectors', 'tape', ALL)
go('resting_seller only (the positive one)', 'tape', ['resting_seller'])
go('no tape -- target 2R + stop only', 'target', [], first_red=False, bail=999)
go('no tape, no first-red, no bailout', 'indicator', [], first_red=False, bail=999)
print('DONE', flush=True)
