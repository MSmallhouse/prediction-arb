# Judge's independent outcome matcher (does not reuse outcomes.py)
import json,glob,re,pandas as pd,numpy as np
SP='vps_pull_20261006_final/analysis/espn/'
def nz(s): return re.sub(r'[^a-z0-9]','',str(s).lower())
GAMES=[]  # (sport, start, [(names set, winner bool)]*2)
for f in glob.glob(SP+'*.json'):
    sp=f.split('/')[-1].split('_')[0]; sp='CFB' if sp=='FCS' else sp
    for ev in json.load(open(f)).get('events',[]):
        if ev['status']['type']['state']!='post': continue
        c=ev['competitions'][0]; side=[]
        for x in c['competitors']:
            t=x['team']; names={nz(t.get(k)) for k in ('displayName','shortDisplayName','name','location','abbreviation')}-{'','none'}
            side.append((names,x.get('winner')))
        if len(side)==2 and side[0][1] is not None: GAMES.append((sp,pd.Timestamp(ev['date']),side))
GAMES={(g[0],g[1],tuple(sorted(next(iter(s[0])) for s in g[2]))):g for g in GAMES}.values()  # dedupe
BYSP={}
for g in GAMES: BYSP.setdefault(g[0],[]).append(g)
def hit(team,names):
    t=nz(team)
    return t in names or any(len(t)>=5 and (n.startswith(t) or t.startswith(n) and len(n)>=5) for n in names)
_c={}
def won(sport,gdt,team,other):
    k=(sport,gdt,team,other)
    if k in _c: return _c[k]
    gdt=pd.Timestamp(gdt); cands=[]
    for sp,st,side in BYSP.get(sport,[]):
        dt=abs((st-gdt).total_seconds())
        if dt>6*3600: continue
        for i in (0,1):
            if hit(team,side[i][0]) and (other is None or hit(other,side[1-i][0])):
                cands.append((dt,bool(side[i][1])))
    r=None
    if cands:
        cands.sort(); r=cands[0][1]
    _c[k]=r; return r
