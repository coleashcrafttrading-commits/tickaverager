"""Does our scanner pick the names he actually traded?

The direct test. For every trade he published, walk our funnel and record the
exact stage the name was lost at. If his tickers never reach our watchlist,
nothing downstream matters and the scanner is what needs fixing.
"""
import json, collections
from pathlib import Path
import scanner, ross, floatdata

his=json.load(open('research/ross_his_trades.json'))
table=json.loads(Path('research/scanner/daily_table.json').read_text(encoding='utf-8'))
cfg=ross.scanner_cfg()
dates=set(r['d'] for recs in table.values() for r in recs)
stage=collections.Counter(); rows=[]

for t in his:
    d, sym = t['date'], t['ticker'].upper()
    if d not in dates:
        stage['session not in our data']+=1; continue
    recs = table.get(sym)
    if not recs:
        stage['symbol absent from our universe']+=1
        rows.append((d,sym,'not in universe','')); continue
    rec = next((r for r in recs if r['d']==d), None)
    if not rec:
        stage['no bar for that day']+=1
        rows.append((d,sym,'no bar that day','')); continue
    px = rec.get('open_raw')
    detail = 'open $%s gap %+.1f%%' % (px, rec.get('gap_pct') or 0)
    if px is None:
        stage['no raw price']+=1; rows.append((d,sym,'no raw price',detail)); continue
    if not (cfg['min_price'] <= px <= cfg['max_price']):
        stage['PRICE band $%g-$%g' % (cfg['min_price'],cfg['max_price'])]+=1
        rows.append((d,sym,'price band',detail)); continue
    # the scanner no longer requires a gap -- his gap ranking freezes at 09:30
    # and he trades intraday runners after that -- so the check must not either
    if rec['avg_vol_30']*rec['open'] < scanner.DEFAULTS['min_dollar_vol']:
        stage['dollar volume']+=1; rows.append((d,sym,'dollar volume',detail)); continue
    c=scanner.attach_float([dict(rec, symbol=sym)], d, cfg)
    res=scanner.float_filter(c, d, table, cfg)
    if not res['candidates']:
        why=[k for k,v in res['dropped'].items() if v]
        stage['FLOAT (%s)' % (why[0] if why else '?')]+=1
        rows.append((d,sym,'float: %s'%(why[0] if why else '?'),detail)); continue
    if ross.p('require_catalyst'):
        import catalyst as _cat
        cc=_cat.catalyst(sym, ross._cat_instant(d), ross.p('catalyst_lookback_h'))
        if not cc.get('stock_specific'):
            stage['CATALYST (no stock-specific headline)']+=1
            rows.append((d,sym,'catalyst',detail+' | %d articles'%cc['n_articles'])); continue
    stage['REACHED OUR WATCHLIST']+=1
    rows.append((d,sym,'ON WATCHLIST',detail))

print('WHERE HIS ACTUAL TRADES DIE IN OUR FUNNEL  (%d trades)' % len(his))
print()
for k,v in stage.most_common():
    print('  %-42s %4d  (%.0f%%)' % (k, v, 100*v/max(1,len(his))))
print()
print('  %-11s %-7s %-22s %s' % ('date','ticker','outcome','detail'))
for d,s,o,x in rows[:34]:
    print('  %-11s %-7s %-22s %s' % (d,s,o,x))
print('DONE')
