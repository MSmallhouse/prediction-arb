import pandas as pd, numpy as np
SP='vps_pull_20261006_final/analysis/'
op=pd.read_pickle(SP+'hold.pkl'); op['gdate']=op.game_datetime.str[:10]
g=op[(op.opener=='kalshi')&(op.gross_spread>=0.04)&(op.poly_depth>=1)&(op.poly_ask>=0.15)&(op.minutes_to_first_pitch<=180)].copy()
cl=pd.read_csv('./vps_pull_20261006_2027/convergence_log.csv',usecols=['arb_id','game','poly_team','t_offset_ms','poly_ask','poly_bid','kalshi_ask_now','source'])
cl=cl[cl.arb_id.isin(set(g.first_seen))]
cl=cl.merge(g[['first_seen','game','poly_team','poly_ask','won','fee','kalshi_ask']].rename(columns={'first_seen':'arb_id','poly_ask':'ask0','kalshi_ask':'k0'}),on=['arb_id','game','poly_team'])
cl.to_pickle(SP+'cl_g.pkl'); print(len(cl), cl.arb_id.nunique())
