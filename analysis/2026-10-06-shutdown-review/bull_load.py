import pandas as pd, numpy as np
D='./vps_pull_20261006_2027/'
e=pd.read_csv(D+'executions.csv'); e['ts']=pd.to_datetime(e.timestamp,format='ISO8601')
e=e[e.ts>='2026-09-19']
b=e[e.action=='BUY'].set_index('arb_id')
s=e[e.action.str.startswith('SELL')].set_index('arb_id')
t=s.join(b[['buy_latency_ms','poly_depth','poly_ws_age_ms','kalshi_ws_age_ms','intent','buy_fee']],rsuffix='_b')
t['win']=t.profit>0
