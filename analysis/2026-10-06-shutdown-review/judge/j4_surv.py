exec(open('j_outcomes.py').read())
D='./vps_pull_20261006_2027/'
a=pd.read_csv(D+'arb_durations_4.csv'); op=a[(a.event=='OPEN')&(a.first_seen>='2026-09-19')].drop_duplicates('first_seen')
g=op[(op.opener=='kalshi')&(op.gross_spread>=0.04)&(op.poly_depth>=1)&(op.poly_ask>=0.15)&(op.minutes_to_first_pitch<=180)].copy()
g['won']=[won(s,d,p,k) for s,d,p,k in zip(g.sport,g.game_datetime,g.poly_team,g.kalshi_team)]
g=g[g.won.notna()].copy(); g['won']=g.won.astype(float); g['h']=g.won-g.poly_ask-0.05*g.poly_ask*(1-g.poly_ask)
p=pd.read_pickle('j_paths.pkl'); g['logged']=g.first_seen.isin(set(p.first_seen))
print(g.groupby('logged').agg(n=('h','size'),hold=('h','mean'),gross=('gross_spread','median'),depth=('poly_depth','median')))
print(g.groupby(['sport','logged']).h.agg(['size','mean']).round(3))
# time pattern of logging
g['d']=g.first_seen.str[:10]; print(g.groupby('d').logged.mean().round(2).to_dict())
# fills: how many in conv log
f=pd.read_pickle('j_fills.pkl'); print('fills in conv log', f.arb_id.isin(set(pd.read_csv(D+'convergence_log.csv',usecols=['arb_id']).arb_id)).sum())
# fills gated?
print('fills passing my gate replay', f.arb_id.isin(set(g.first_seen)).sum(), 'of', len(f))
# attempts/day by sport by week
e=pd.read_csv(D+'executions.csv'); e=e[(e.timestamp>='2026-09-19')&e.action.isin(['BUY','BUY_FAILED'])]
e['wk']=pd.to_datetime(e.timestamp,format='ISO8601').dt.strftime('%m-%d').str[:5]
e['d']=e.timestamp.str[:10]
print(e.pivot_table(index='d',columns='sport',values='arb_id',aggfunc='count').fillna(0).astype(int))
