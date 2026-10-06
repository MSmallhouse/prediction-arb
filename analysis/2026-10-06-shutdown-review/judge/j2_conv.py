exec(open('j_outcomes.py').read())
D='./vps_pull_20261006_2027/'
a=pd.read_csv(D+'arb_durations_4.csv'); op=a[(a.event=='OPEN')&(a.first_seen>='2026-09-19')].drop_duplicates('first_seen')
g=op[(op.opener=='kalshi')&(op.gross_spread>=0.04)&(op.poly_depth>=1)&(op.poly_ask>=0.15)&(op.minutes_to_first_pitch<=180)].copy()
print('gated OPEN since 09-19',len(g))
g['won']=[won(s,d,p,k) for s,d,p,k in zip(g.sport,g.game_datetime,g.poly_team,g.kalshi_team)]
print('outcome coverage',g.won.notna().mean())
g=g[g.won.notna()].copy(); g['won']=g.won.astype(float)
# cross-check vs bull hold.pkl
hp=pd.read_pickle('../hold.pkl'); hp=hp[['first_seen','poly_team','won']].drop_duplicates(['first_seen','poly_team'])
x=g.merge(hp,on=['first_seen','poly_team'],suffixes=('','_bull'))
print('agreement with bull outcomes',(x.won==x.won_bull).mean(),len(x))
c=pd.read_csv(D+'convergence_log.csv',usecols=['arb_id','poly_team','t_offset_ms','poly_ask','poly_bid','poly_depth','source'])
c=c[c.arb_id.isin(set(g.first_seen))]
print('conv rows',len(c),'arbs w/ conv',c.arb_id.nunique())
c=c.sort_values(['arb_id','t_offset_ms'])
rows=[]
for aid,h in c.groupby('arb_id',sort=False):
    t=h.t_offset_ms.values; ask=h.poly_ask.values; dep=h.poly_depth.values; bid=h.poly_bid.values
    a0=ask[0]; d0=dep[0]
    r={'first_seen':aid,'a0':a0,'d0':d0}
    for T in (0,50,100,200,500,1000,5000,15000):
        i=np.searchsorted(t,T,side='right')-1; r[f'a{T}']=ask[max(i,0)]
    w=t<=200; r['lift200']=bool((ask[w]>a0+1e-9).any())
    w=t<=100; r['lift100']=bool((ask[w]>a0+1e-9).any())
    w=t<=15000; r['conv15']=bool((bid[w]>=a0+0.05-1e-9).any())
    r['last_t']=t[-1]
    rows.append(r)
p=pd.DataFrame(rows).merge(g[['first_seen','game','sport','poly_team','won','gross_spread','game_datetime']],on='first_seen')
p['day']=p.game_datetime.str[:10]
p.to_pickle('j_paths.pkl'); print('paths',len(p),'games',p.game.nunique())
