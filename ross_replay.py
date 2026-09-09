import json, tape, tapeexit, statistics, ross, collections, sys
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

d=json.load(open('research/ross/run_2026-01-01_2026-09-04.json'))
tr=[t for x in d['days'] for t in x['trades']]
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=sorted({r['d'] for recs in table.values() for r in recs if r['d']>='2026-01-01'})
wl=ross.watchlists(table,dates,cfg)
hist=ross.load_history(wl, log=lambda *a: None)
H=tape._headers()

# pre-load every window once so sensitivity settings are compared on identical data
ctx=[]
for t in tr:
    u=datetime.fromisoformat(str(t['t']).replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York'))
    date=u.strftime('%Y-%m-%d'); hhmm=u.strftime('%H:%M')
    bars=(hist.get(t['symbol']) or {}).get(date) or []
    bc=next((float(b['c']) for b in bars if b['et'][11:16]==hhmm), None)
    if bc is None: continue
    ratio=tape.split_ratio(t['symbol'],date,hhmm,bc,headers=H)
    if not ratio or ratio<=0: continue
    s=u-timedelta(minutes=11); e=u+timedelta(minutes=20)
    if s.strftime('%H:%M')<'09:30': s=s.replace(hour=9,minute=30)
    if e.hour>=11: e=e.replace(hour=10,minute=59,second=0)
    try: w=tape.window(t['symbol'],date,s.strftime('%H:%M'),e.strftime('%H:%M'),headers=H)
    except Exception: continue
    if not w['prints'] or not w['quotes']: continue
    ctx.append((t,date,tapeexit.Tape(w['prints'],w['quotes']),
                tape.ns(u.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000000000Z')),ratio))
print('%d trades loaded' % len(ctx), flush=True)

# TRAIN on the first 60% of sessions, REPORT on the rest. Calibrated to HIS
# documented hold times (~180s winners, ~120s losers), never to our P/L.
alld=sorted({c[1] for c in ctx}); cut=alld[int(.6*len(alld))]
def run(split):
    rows=[]
    for t,date,tp,e_ns,ratio in ctx:
        if split=='train' and date>=cut: continue
        if split=='test' and date<cut: continue
        hit=tapeexit.watch(tp,e_ns,e_ns+int(20*60*1e9))
        if hit:
            raw=hit['bid'] or tp.last_price(hit['t']); why=hit['detector']; held=(hit['t']-e_ns)/1e9
        else:
            raw=tp.last_price(e_ns+int(20*60*1e9)); why='no indicator'; held=1200
        if not raw: continue
        pl=((raw-tapeexit.p('exit_slip'))*ratio-t['entry'])*t['shares']
        rows.append(dict(bar=t['pl'],tap=pl,why=why,held=held))
    return rows

def show(tag,rows):
    if not rows: return
    b=sum(r['bar'] for r in rows); v=sum(r['tap'] for r in rows)
    w=[r for r in rows if r['tap']>0]; l=[r for r in rows if r['tap']<=0]
    hw=statistics.median([r['held'] for r in w]) if w else 0
    hl=statistics.median([r['held'] for r in l]) if l else 0
    print('  %-26s bar \$%8.2f | tape \$%8.2f | win %4.1f%% | hold W %4.0fs L %4.0fs | n=%d'
          % (tag,b,v,100*len(w)/len(rows),hw,hl,len(rows)), flush=True)

print(flush=True)
print('CALIBRATING TO HIS HOLD TIMES (~180s winners / ~120s losers), on TRAIN only', flush=True)
print(flush=True)
for arm,z,ratio_min,bs in ((45,3.0,3.0,0.60),(60,4.0,4.0,0.65),(90,5.0,5.0,0.70),(120,6.0,6.0,0.75)):
    tapeexit.D['d5_arm_s']=arm; tapeexit.D['d3_z_min']=z
    tapeexit.D['d3_ratio_min']=ratio_min; tapeexit.D['d2_buyshare_min']=bs
    show('arm%ds z%.0f ratio%.0f bs%.2f'%(arm,z,ratio_min,bs), run('train'))
print('DONE', flush=True)
