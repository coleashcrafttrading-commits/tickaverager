"""Which session window is actually his?

His worksheet says 07:00-11:00. But his live room, his recaps and his
gap-and-go teaching are all the 09:30 open, and 62% of our trades were landing
pre-market -- the thinnest, widest-spread part of the day, and the part where
LULD bands do not operate at all so a stop has no circuit breaker behind it.
Train and test reported separately.
"""
import json, collections, ross, tapeexit
from pathlib import Path
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
cut=dates[int(.6*len(dates))]
tapeexit.ENABLED=['resting_seller','hidden_seller','red_burst']

def go(start, end, label):
    ross.P['session_start']=(start,'his',''); ross.P['session_end']=(end,'his','')
    wl=ross.watchlists(table,dates,cfg)
    hist=ross.load_history(wl, log=lambda *a: None)
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
        print('  %-26s %6s $%9.2f %7.1f%% %5d tr  win %5.1f%%'
              % (label, split, eq, 100*(eq/2000-1), len(tr), 100*len(w)/max(1,len(tr))), flush=True)

print('SESSION WINDOW -- his worksheet vs where he actually trades', flush=True)
print(flush=True)
go('07:00','11:30','07:00-11:30 (worksheet)')
go('09:30','11:30','09:30-11:30 (momentum page)')
go('09:30','11:00','09:30-11:00')
go('09:30','10:30','09:30-10:30 (the open)')
print('DONE', flush=True)
