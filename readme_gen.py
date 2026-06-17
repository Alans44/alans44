"""
Automated GitHub‑profile banner generator
────────────────────────────────────────────────────────────
• Pulls live GitHub stats via GraphQL v4
• Rewrites one or more SVG templates in‑place
• Caches per‑repo LOC so the job stays within API limits

"""

from __future__ import annotations

import datetime
import hashlib
import os
import time
from pathlib import Path

import requests
from dateutil import relativedelta
from lxml import etree

# ──────────────────────────────────────a
#  ░░ USER CONFIG ░░                   
# ──────────────────────────────────────
USER_NAME: str = os.getenv("USER_NAME", "Alans44")
BIRTHDAY  = datetime.datetime(2004, 4, 4)          # yyyy, m, d
SVG_FILES = ["dark_mode.svg", "light_mode.svg"]   # templates to update
CACHE_DIR = Path("cache"); CACHE_DIR.mkdir(exist_ok=True)
COMMENT_SIZE = 7                                    # lines reserved at top of cache

# Fine‑grained PAT
# You gotta set this up in github actions -> secrets if you want the script to work
HEADERS = {"authorization": "token " + os.environ["ACCESS_TOKEN"]}

# ──────────────────────────────────────
#  INTERNAL COUNTERS
# ──────────────────────────────────────
QUERY_COUNT = {k: 0 for k in [
    "user_getter","follower_getter","graph_repos_stars",
    "recursive_loc","graph_commits","loc_query"]}

# ╭──────────────────────────────────╮
# │  Utility helpers                │
# ╰──────────────────────────────────╯

def uptime_string(bday: datetime.datetime) -> str:
    diff = relativedelta.relativedelta(datetime.datetime.utcnow(), bday)
    return f"{diff.years} year{'s'*(diff.years!=1)}, {diff.months} month{'s'*(diff.months!=1)}, {diff.days} day{'s'*(diff.days!=1)}"


def perf_counter(fn, *args):
    start = time.perf_counter(); out = fn(*args)
    return out, time.perf_counter() - start


def formatter(lbl: str, dt: float):
    print(f"   {lbl:<22}: {dt*1000:>8.2f} ms" if dt<1 else f"   {lbl:<22}: {dt:>8.2f} s ")


def query_count(k:str): QUERY_COUNT[k]+=1


def simple_request(fname:str,q:str,v:dict):
    r=requests.post("https://api.github.com/graphql",json={"query":q,"variables":v},headers=HEADERS)
    if r.status_code==200: return r
    raise RuntimeError(f"{fname} failed → {r.status_code}: {r.text}")

# ╭──────────────────────────────────╮
# │  SVG helper                     │
# ╰──────────────────────────────────╯

def find_and_replace(root, element_id:str, new_text:str):
    el=root.find(f".//*[@id='{element_id}']")
    if el is None: return
    el.text=str(new_text)


def justify_format(root,eid,new_text,length=0):
    if isinstance(new_text,int): new_text=f"{new_text:,}"
    find_and_replace(root,eid,new_text)
    just_len=max(0,length-len(str(new_text)))
    dot_map={0:'',1:' ',2:'. '}
    dot_string=dot_map.get(just_len,' '+'.'*just_len+' ')
    find_and_replace(root,f"{eid}_dots",dot_string)


def svg_overwrite(fname,*vals):
    age,comm,star,repo,contrib,follow,loc=vals
    tree=etree.parse(fname);root=tree.getroot()
    justify_format(root,'age_data',age,49)
    justify_format(root,'commit_data',comm,22)
    justify_format(root,'star_data',star,14)
    justify_format(root,'repo_data',repo,6)
    justify_format(root,'contrib_data',contrib)
    justify_format(root,'follower_data',follow,10)
    justify_format(root,'loc_data',loc[2],9)
    justify_format(root,'loc_add',loc[0])
    justify_format(root,'loc_del',loc[1],7)
    tree.write(fname,encoding='utf-8',xml_declaration=True)

# ╭──────────────────────────────────╮
# │  GraphQL helpers (user/stats)    │
# ╰──────────────────────────────────╯

def user_getter(username:str):
    query_count('user_getter')
    q="""query($login:String!){ user(login:$login){ id createdAt }}"""
    data=simple_request('user_getter',q,{"login":username}).json()['data']['user']
    return {"id":data['id']},data['createdAt']


def follower_getter(username:str)->int:
    query_count('follower_getter')
    q="""query($login:String!){ user(login:$login){ followers{ totalCount }}}"""
    return int(simple_request('follower_getter',q,{"login":username}).json()['data']['user']['followers']['totalCount'])


def graph_repos_stars(kind:str,aff:list[str],cursor=None):
    query_count('graph_repos_stars')
    q="""query($owner_affiliation:[RepositoryAffiliation],$login:String!,$cursor:String){
      user(login:$login){ repositories(first:100,after:$cursor,ownerAffiliations:$owner_affiliation){ totalCount edges{node{stargazers{totalCount}}} pageInfo{endCursor hasNextPage}}}}"""
    vars={"owner_affiliation":aff,"login":USER_NAME,"cursor":cursor}
    r=simple_request('graph_repos_stars',q,vars).json()['data']['user']['repositories']
    return r['totalCount'] if kind=='repos' else sum(e['node']['stargazers']['totalCount'] for e in r['edges'])


# ╭──────────────────────────────────╮
# │  Commits (contributionsCollection)│
# ╰──────────────────────────────────╯

def graph_commits(owner_id, acc_date):
    """Count total commits by iterating contribution years."""
    total = 0
    now = datetime.datetime.utcnow()
    acc_dt = datetime.datetime.strptime(acc_date, "%Y-%m-%dT%H:%M:%SZ")
    start = acc_dt
    while start < now:
        end = min(start + relativedelta.relativedelta(years=1), now)
        query_count('graph_commits')
        q = """query($id:ID!,$from:DateTime!,$to:DateTime!){
          node(id:$id){...on User{contributionsCollection(from:$from,to:$to){
            totalCommitContributions restrictedContributionsCount}}}}"""
        r = simple_request('graph_commits', q, {
            "id": owner_id["id"],
            "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to":   end.strftime("%Y-%m-%dT%H:%M:%SZ")
        })
        cc = r.json()['data']['node']['contributionsCollection']
        total += cc['totalCommitContributions'] + cc['restrictedContributionsCount']
        start = end
    return total

# ╭──────────────────────────────────╮
# │  LOC (REST stats/contributors)   │
# ╰──────────────────────────────────╯

def get_owned_repo_names():
    """Return list of non‑fork repos owned by the user."""
    repos, cursor = [], None
    while True:
        query_count('loc_query')
        q = """query($login:String!,$cursor:String){
          user(login:$login){repositories(first:100,after:$cursor,ownerAffiliations:OWNER){
            edges{node{nameWithOwner isFork}} pageInfo{endCursor hasNextPage}}}}"""
        r = simple_request('get_owned_repo_names', q, {"login": USER_NAME, "cursor": cursor})
        data = r.json()['data']['user']['repositories']
        repos.extend(e['node']['nameWithOwner'] for e in data['edges'] if not e['node']['isFork'])
        if not data['pageInfo']['hasNextPage']:
            break
        cursor = data['pageInfo']['endCursor']
    return repos


def get_loc(repos):
    """Sum lines added / deleted across all repos via REST stats API."""
    total_add = total_del = 0
    for repo in repos:
        cache_file = CACHE_DIR / (hashlib.md5(repo.encode()).hexdigest() + '.txt')
        if cache_file.exists():
            data_lines = [l for l in cache_file.read_text().strip().splitlines() if not l.startswith('#')]
            if len(data_lines) >= 2:
                total_add += int(data_lines[0])
                total_del += int(data_lines[1])
                continue
        query_count('recursive_loc')
        add = dele = 0
        try:
            for _ in range(3):
                r = requests.get(f"https://api.github.com/repos/{repo}/stats/contributors", headers=HEADERS)
                if r.status_code == 202:   # GitHub is computing — retry
                    time.sleep(2); continue
                if r.status_code != 200 or not r.json():
                    break
                for c in r.json():
                    if c['author']['login'].lower() == USER_NAME.lower():
                        add  = sum(w['a'] for w in c['weeks'])
                        dele = sum(w['d'] for w in c['weeks'])
                        break
                break
        except Exception as e:
            print(f"  Warning: LOC skipped for {repo}: {e}")
        total_add += add; total_del += dele
        cache_file.write_text(f"# {repo}\n{add}\n{dele}\n")
    return total_add, total_del

# ╭──────────────────────────────────╮
# │        Main                      │
# ╰──────────────────────────────────╯
if __name__=='__main__':
    print('Calculation times:')
    user_data,t_user=perf_counter(user_getter,USER_NAME)
    OWNER_ID,acc_date=user_data
    formatter('account data',t_user)
    age,t_age=perf_counter(uptime_string,BIRTHDAY)
    formatter('age calculation',t_age)

    star_data,_ = graph_repos_stars('stars',["OWNER"]),0
    repo_data,_ = graph_repos_stars('repos',["OWNER"]),0
    contrib_data,_ = graph_repos_stars('repos',["OWNER","COLLABORATOR","ORGANIZATION_MEMBER"]),0
    follower_data,_ = follower_getter(USER_NAME),0

    commit_data,t_comm=perf_counter(graph_commits,OWNER_ID,acc_date)
    formatter('commit data',t_comm)

    owned_repos=get_owned_repo_names()
    loc_add,loc_del=get_loc(owned_repos)
    loc_total=[loc_add,loc_del,loc_add-loc_del]

    for svg in SVG_FILES:
        svg_overwrite(svg,age,commit_data,star_data,repo_data,contrib_data,follower_data,loc_total)

    print('Total GraphQL calls:',sum(QUERY_COUNT.values()))
