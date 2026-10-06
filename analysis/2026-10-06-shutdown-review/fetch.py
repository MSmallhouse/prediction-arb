import pandas as pd, glob, json, os, subprocess, datetime as dt
SP='vps_pull_20261006_final/analysis/espn/'
R='./'
files=[R+'vps_pull_20261006_2027/arb_durations_4.csv',R+'vps_pull_20261006_2027/arb_durations_3.csv']+glob.glob(R+'historical-data/arb_durations_*.csv')
need=set()
path={'MLB':'baseball/mlb','NHL':'hockey/nhl','NBA':'basketball/nba','CFB':'football/college-football'}
for f in files:
    try: a=pd.read_csv(f,usecols=['sport','game_datetime'])
    except Exception as ex: print(f,ex); continue
    a=a.dropna()
    for sp,gd in a.drop_duplicates().itertuples(index=False):
        if sp not in path: continue
        d=pd.to_datetime(gd)
        for k in (-1,0): need.add((sp,(d+pd.Timedelta(days=k)).strftime('%Y%m%d')))
print(len(need))
import concurrent.futures as cf
def get(x):
    sp,d=x; fn=f'{SP}{sp}_{d}.json'
    if os.path.exists(fn) and os.path.getsize(fn)>100: return
    url=f'https://site.api.espn.com/apis/site/v2/sports/{path[sp]}/scoreboard?dates={d}&limit=400'+('&groups=80' if sp=='CFB' else '')
    subprocess.run(['curl','-s','-m','30','-o',fn,url])
with cf.ThreadPoolExecutor(8) as ex: list(ex.map(get,sorted(need)))
