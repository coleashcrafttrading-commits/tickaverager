"""Calibrate the buying-slowing detector to its design target: <=30% of exits.

Its own design critique flagged this as the dangerous one -- buy volume always
decays after the spike you just bought into, so a naive version fires on
everything and exits winners early. The target is STRUCTURAL (a share of exits),
not P/L, so this is not tuning toward an answer. Train split only.
"""
import json, collections, statistics, ross
from pathlib import Path
import tapeexit

table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
cut=dates[int(.6*len(dates))]
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)

def run(split):
    eq=2000.0; curve=[eq]; tr=[]
    for d in dates:
        if split=='train' and d>=cut: continue
        if split=='test' and d<cut: continue
        if not wl[d]['picks']: continue
        r=ross.run_day(d, wl[d]['picks'], hist, eq, cfg)
        eq=r['equity_end']; tr+=r['trades']; curve.append(eq)
        if eq<=0: break
    w=[t for t in tr if t['pl']>0]
    c=collections.Counter(t['reason'] for t in tr)
    d5=100*c.get('buying_slowing',0)/max(1,len(tr))
    return eq, tr, 100*len(w)/max(1,len(tr)), d5

print('CALIBRATING buying_slowing TO ITS DESIGN TARGET (<=30%% of exits)', flush=True)
print('train split only, %d of %d sessions' % (dates.index(cut), len(dates)), flush=True)
print(flush=True)
print('  %-34s %9s %6s %7s %9s' % ('setting','final','n','win%','d5 share'), flush=True)
for arm, collapse, consec in ((45,0.35,3),(60,0.25,3),(90,0.20,4),(120,0.15,5),(180,0.10,5)):
    tapeexit.D['d5_arm_s']=arm; tapeexit.D['d5_collapse']=collapse; tapeexit.D['d5_consec']=consec
    eq,tr,win,d5=run('train')
    flag=' <-- at target' if d5<=30 else ''
    print('  arm %3ds collapse %.2f consec %d      $%8.2f %5d %6.1f%% %8.1f%%%s'
          % (arm,collapse,consec,eq,len(tr),win,d5,flag), flush=True)
print('DONE', flush=True)
