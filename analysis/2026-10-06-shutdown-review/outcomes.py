import json,glob,re,pandas as pd
SP='vps_pull_20261006_final/analysis/espn/'
def n(s): return re.sub(r'[^a-z0-9]','',str(s).lower().replace('&','and'))
EV={}
for f in glob.glob(SP+'*.json'):
    sp=f.split('/')[-1].split('_')[0]; sp='CFB' if sp=='FCS' else sp
    for ev in json.load(open(f)).get('events',[]):
        c=ev['competitions'][0]
        if ev['status']['type']['state']!='post': continue
        teams=[]
        for x in c['competitors']:
            t=x['team']; keys={n(t.get(k,'')) for k in ('displayName','shortDisplayName','name','location','abbreviation','nickname')}-{''}
            teams.append((keys,n(t.get('displayName','')),x.get('winner')))
        EV[ev['id']]=(sp,pd.Timestamp(ev['date']),teams)
EVL=list(EV.values())
def match(team,keys,full):
    t=n(team)
    if t in keys: return True
    if len(t)>=4 and (full.endswith(t) or full.startswith(t)): return True
    return False
cache={}
def outcome(sport,gd,poly_team,kalshi_team):
    k=(sport,gd,poly_team,kalshi_team)
    if k in cache: return cache[k]
    gd=pd.Timestamp(gd); res=None; hits=[]
    for sp,dt,teams in EVL:
        if sp!=sport or abs((dt-gd).total_seconds())>8*3600: continue
        for i,(keys,full,w) in enumerate(teams):
            if match(poly_team,keys,full):
                o=teams[1-i]
                if kalshi_team==poly_team or match(kalshi_team,o[0],o[1]) or match(kalshi_team,keys,full):
                    hits.append((abs((dt-gd).total_seconds()),w))
    if hits:
        hits.sort(); res=hits[0][1]
        if res is None: res=None
        else: res=bool(res)
    cache[k]=res; return res
