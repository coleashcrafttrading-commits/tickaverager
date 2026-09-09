"""Why is our win rate 22% and his 71%?

Two candidate mechanisms, and they have different fixes:

  (a) our ENTRIES go underwater immediately, so no exit rule can save them
  (b) our hard STOP fires before his tape signal ever gets a chance, converting
      what would have been his small winners into full-stop losses

He runs a MENTAL stop -- a disaster limit -- and his primary exit is the tape.
We model a hard stop that triggers on any touch. If (b), that difference alone
is the win rate.
"""
import json, statistics, collections
import ross, tape, tapeexit, microbars
from zoneinfo import ZoneInfo
from datetime import timedelta, timezone

d=json.load(open('research/ross/run_2026-01-01_now.json'))
rows=[(x['date'],t) for x in d['days'] for t in x['trades']]
h=tape._headers()
print('%d trades' % len(rows), flush=True)

mfe_at_exit=[]; stop_first=0; tape_first=0; underwater=0; n=0
for date,t in rows[:70]:
    u=ross.to_et(t['t'])
    if u is None: continue
    a=u-timedelta(minutes=11); b=u+timedelta(minutes=25)
    lo=max(a.strftime('%H:%M'), ross.p('session_start')); hi=min(b.strftime('%H:%M'), ross.p('session_end'))
    if hi<=lo: continue
    try: w=tape.window(t['symbol'],date,lo,hi,headers=h)
    except Exception: continue
    if not w['prints'] or not w['quotes']: continue
    ratio=tape.split_ratio(t['symbol'],date,u.strftime('%H:%M'),t['entry'],headers=h) or 1.0
    tp=tapeexit.Tape(w['prints'], w['quotes'])
    e_ns=tape.ns(u.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000000000Z'))
    hit=tapeexit.watch(tp, e_ns, e_ns+int(25*60*1e9))
    # when did price first touch the stop, in raw space?
    stop_raw=t['stop']/ratio
    pr=tp.prints_between(e_ns, e_ns+int(25*60*1e9))
    stop_ns=next((x['t'] for x in pr if x['p']<=stop_raw), None)
    tape_ns=hit['t'] if hit else None
    n+=1
    if stop_ns and (not tape_ns or stop_ns<tape_ns): stop_first+=1
    elif tape_ns: tape_first+=1
    # unrealised P/L at the moment the tape signal fires
    if tape_ns:
        px=(hit['bid'] or tp.last_price(tape_ns))
        if px: mfe_at_exit.append((px*ratio - t['entry'])*100/t['entry'])
    # did it EVER go green before dying?
    hi_raw=max((x['p'] for x in pr[:200]), default=0)
    if hi_raw*ratio <= t['entry']: underwater+=1

print(flush=True)
print('  %d trades measured' % n, flush=True)
print('  stop touched BEFORE any tape signal   %4d  (%.0f%%)' % (stop_first, 100*stop_first/max(1,n)), flush=True)
print('  tape signal fired first               %4d  (%.0f%%)' % (tape_first, 100*tape_first/max(1,n)), flush=True)
print('  never traded above entry at all       %4d  (%.0f%%)' % (underwater, 100*underwater/max(1,n)), flush=True)
if mfe_at_exit:
    print(flush=True)
    print('  P/L at the moment the tape signal fires (%% of price):', flush=True)
    m=sorted(mfe_at_exit)
    print('    median %+.2f%%   25th %+.2f%%   75th %+.2f%%' % (statistics.median(m), m[len(m)//4], m[3*len(m)//4]), flush=True)
    print('    green when it fired: %.0f%%' % (100*sum(1 for x in m if x>0)/len(m)), flush=True)
print('DONE', flush=True)
