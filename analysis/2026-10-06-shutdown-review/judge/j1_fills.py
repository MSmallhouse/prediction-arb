exec(open('j_outcomes.py').read())
D='./vps_pull_20261006_2027/'
e=pd.read_csv(D+'executions.csv'); e=e[e.timestamp>='2026-09-19']
a=pd.read_csv(D+'arb_durations_4.csv'); op=a[a.event=='OPEN'].drop_duplicates('first_seen')
buys=e[e.action=='BUY']; sells=e[e.action.str.startswith('SELL')].drop_duplicates('arb_id')
print('buys',len(buys),'sells',len(sells), 'attempts', (e.action.isin(['BUY','BUY_FAILED'])).sum())
f=buys.drop(columns=['profit','exit_reason','sell_price','sell_fee']).merge(sells[['arb_id','action','profit','exit_reason','sell_price','sell_fee']],on='arb_id',how='left',suffixes=('','_s'))
f=f.merge(op[['first_seen','poly_team','kalshi_team','game_datetime','poly_ask','poly_bid','poly_depth']].rename(columns={'first_seen':'arb_id','poly_depth':'dep0'}),on='arb_id',how='left')
print('joined to arb_durations', f.poly_team.notna().sum(), '| buy_price==poly_ask within 1c', (abs(f.buy_price-f.poly_ask)<=0.01).mean())
f['won']=[won(s,g,p,k) if isinstance(p,str) else None for s,g,p,k in zip(f.sport,f.game_datetime,f.poly_team,f.kalshi_team)]
m=f[f.won.notna()].copy(); m['won']=m.won.astype(float)
m['hold']=m.won-m.buy_price-m.buy_fee
print('with outcome',len(m),'games',m.game.nunique())
print('realized sum',round(m.profit.sum(),2),'mean c',round(100*m.profit.mean(),2),'| hold sum',round(m.hold.sum(),2),'mean c',round(100*m.hold.mean(),2))
# game-clustered bootstrap
rng=np.random.default_rng(1)
def boot(df,col,B=4000):
    g=df.groupby('game')[col].agg(['sum','count']).values
    idx=rng.integers(0,len(g),(B,len(g))); s=g[idx,0].sum(1)/g[idx,1].sum(1)
    return np.percentile(100*s,[2.5,97.5]) , (s<0).mean() 
print('hold CI',boot(m,'hold'),' realized CI',boot(m,'profit'))
m['diff']=m.hold-m.profit; print('hold-minus-realized CI',boot(m,'diff'))
print(m.groupby('exit_reason').agg(n=('hold','size'),real=('profit','sum'),hold=('hold','sum')) )
print(m.groupby('sport').agg(n=('hold','size'),real=('profit','sum'),hold=('hold','sum'),winrate=('won','mean'),px=('buy_price','mean')) )
# calibration of our fills: win rate vs price
print('fill win rate',m.won.mean() ,'avg price',m.buy_price.mean() )
# cross-check with ledger resolutions where we held
m.to_pickle('j_fills.pkl')
