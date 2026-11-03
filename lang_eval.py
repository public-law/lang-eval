#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "==3.12.15"
# dependencies = []
# [tool.uv]
# exclude-newer = "2026-11-01T00:00:00Z"
# ///
"""Gather language health metrics from public APIs and render docs/index.html."""

import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = ROOT / "languages.toml"
DATA = ROOT / "docs" / "data.json"
PAGE = ROOT / "docs" / "index.html"
NOW = datetime.now(timezone.utc)
UA = "lang-eval/0.1 (+https://github.com/public-law/lang-eval)"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

# Each rule: (label, unit, higher_is_better, thresholds for scores 5,4,3,2; anything else scores 1)
RULES = {
    "days_since_commit": ("Days since last commit", "days", False, [7, 30, 90, 365]),
    "commits_90d": ("Commits, last 90 days", "commits", True, [300, 100, 30, 5]),
    "days_since_release": ("Days since last release", "days", False, [60, 180, 365, 730]),
    "authors_90d": ("Distinct authors, 90 days", "people", True, [30, 15, 5, 2]),
    "authors_12m": ("Distinct authors, 12 months", "people", True, [75, 40, 15, 3]),
    "mergers_90d": ("Distinct people merging PRs, 90 days", "people", True, [6, 4, 3, 2]),
    "top_share_12m": ("Top contributor share of commits, 12 months", "%", False, [20, 35, 50, 75]),
    "outside_merged_90d": ("Outside contributor PRs merged, 90 days", "PRs", True, [40, 15, 5, 1]),
    "outside_accept_90d": ("Outside PR acceptance rate, 90 days", "%", True, [70, 55, 40, 20]),
    "outside_open": ("Open PRs from outside contributors", "PRs", None, None),
    "reddit_per_week": ("Subreddit posts per week", "posts/wk", True, [20, 10, 4, 1]),
    "forum_posts_7d": ("Forum posts, last 7 days", "posts", True, [200, 75, 25, 5]),
    "forum_topics_7d": ("Forum topics, last 7 days", "topics", True, [30, 15, 5, 1]),
    "discussions_30d": ("GitHub Discussions created, 30 days", "threads", True, [30, 10, 3, 1]),
    "discord_members": ("Discord members", "members", None, None),
    "discord_online": ("Discord online now", "online", True, [2000, 800, 300, 50]),
    "platforms": ("Distinct community platforms (config)", "platforms", True, [5, 4, 3, 2]),
    "x_activity": ("Key figures on X", "", None, None),
    "zed_days": ("Zed extension, days since update", "days", False, [30, 90, 180, 365]),
    "zed_downloads": ("Zed extension downloads", "downloads", True, [100_000, 30_000, 10_000, 2_000]),
    "vscode_days": ("VS Code extension, days since update", "days", False, [30, 90, 180, 365]),
    "vscode_installs": ("VS Code extension installs", "installs", True, [500_000, 100_000, 25_000, 5_000]),
    "vscode_rating": ("VS Code extension rating", "/5", True, [4.5, 4.0, 3.5, 3.0]),
    "openvsx_days": ("Open VSX (Cursor) extension, days since update", "days", False, [30, 90, 180, 365]),
    "openvsx_downloads": ("Open VSX downloads", "downloads", True, [100_000, 25_000, 5_000, 1_000]),
    "lsp_days": ("Language server, days since commit", "days", False, [14, 45, 90, 365]),
    "lsp_commits_90d": ("Language server commits, 90 days", "commits", True, [50, 20, 8, 2]),
    "lsp_rating": ("Language server completeness (manual)", "/5", True, [5, 4, 3, 2]),
}
CATEGORIES = {
    "Development": ["days_since_commit", "commits_90d", "days_since_release", "authors_90d", "authors_12m",
                    "mergers_90d", "top_share_12m", "outside_merged_90d", "outside_accept_90d", "outside_open"],
    "Community": ["reddit_per_week", "forum_posts_7d", "forum_topics_7d", "discussions_30d", "discord_members",
                  "discord_online", "platforms", "x_activity"],
    "Editor support": ["zed_days", "zed_downloads", "vscode_days", "vscode_installs", "vscode_rating",
                       "openvsx_days", "openvsx_downloads", "lsp_days", "lsp_commits_90d", "lsp_rating"],
}
STALE_DAYS = 90


def main():
    langs = tomllib.loads(CONFIG.read_text())["language"]
    previous = load_previous()
    results = []
    for lang in langs:
        log(f"== {lang['name']}")
        metrics = evaluate(lang)
        carry_forward(metrics, previous.get(lang["name"], {}))
        results.append({"name": lang["name"], "metrics": metrics, "config": lang, **summarize(metrics)})
    results.sort(key=lambda r: -(r["overall"] or 0))
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps({"generated_at": NOW.isoformat(), "languages": results}, indent=1, default=str))
    PAGE.write_text(render(results))
    log(f"wrote {PAGE}")


def evaluate(lang):
    m = {}
    m.update(development(lang))
    m.update(community(lang))
    m.update(editors(lang))
    return m


# ---------- Metric helpers ----------

def ok(value, source, note=None, display=None):
    return {"value": value, "source": source, "note": note, "display": display}


def missing(reason, source=None):
    return {"value": None, "source": source, "note": reason}


def guarded(keys, fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log(f"   failed {keys}: {e}")
        return {k: missing(f"not measured: {short(e)}") for k in keys}


def short(e):
    return re.sub(r"\s+", " ", str(e))[:140]


def score(key, value):
    rule = RULES[key]
    better, cuts = rule[2], rule[3]
    if value is None or cuts is None:
        return None
    for s, cut in zip((5, 4, 3, 2), cuts):
        if (value >= cut) if better else (value <= cut):
            return s
    return 1


def summarize(metrics):
    cats = {}
    for cat, keys in CATEGORIES.items():
        scores = [score(k, metrics[k]["value"]) for k in keys if k in metrics]
        scores = [s for s in scores if s is not None]
        cats[cat] = round(sum(scores) / len(scores), 2) if scores else None
    measured = [v for v in cats.values() if v is not None]
    return {"categories": cats, "overall": round(sum(measured) / len(measured), 2) if measured else None}


def days_since(iso):
    return (NOW - parse_time(iso)).days


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_previous():
    if not DATA.exists():
        return {}
    return {l["name"]: l["metrics"] for l in json.loads(DATA.read_text())["languages"]}


def carry_forward(metrics, old):
    for key, m in metrics.items():
        prev = old.get(key)
        if m["value"] is None and m["note"] and m["note"].startswith("not measured:") and prev and prev.get("value") is not None:
            since = prev.get("cached_from") or prev.get("measured_at")
            metrics[key] = {**prev, "cached_from": since, "note": f"cached from {since[:10]}; latest fetch failed ({m['note'][14:]})"}
        elif m["value"] is not None:
            m["measured_at"] = NOW.isoformat()


# ---------- Development ----------

def development(lang):
    keys = CATEGORIES["Development"]
    repos = lang.get("repos", [])
    if not repos:
        return {k: missing("not measured: no repo configured") for k in keys}
    canonical, notes = pick_canonical(repos)
    if not canonical:
        return {k: missing("not measured: no repo reachable; " + "; ".join(notes)) for k in keys}
    lang["_canonical"] = canonical
    fetcher = github_activity if canonical["host"] == "github" else forgejo_activity
    out = guarded(keys, fetcher, canonical)
    if notes and out["days_since_commit"]["value"] is not None:
        out["days_since_commit"]["note"] = "; ".join(notes)
    return out


def pick_canonical(repos):
    best, notes = None, []
    for r in repos:
        try:
            info = repo_info(r)
        except Exception as e:
            notes.append(f"{r['host']}:{r['repo']} unreachable ({short(e)[:60]})")
            continue
        if info["mirror"]:
            notes.append(f"{r['host']}:{r['repo']} is a mirror")
            continue
        if best is None or info["pushed"] > best["pushed"]:
            best = {**r, **info}
    if best and len(repos) > 1:
        notes.append(f"canonical repo detected as {best['host']}:{best['repo']}")
    return best, notes


def repo_info(r):
    if r["host"] == "github":
        d = gh(f"/repos/{r['repo']}")
        return {"mirror": bool(d.get("mirror_url")), "pushed": d["pushed_at"], "web": d["html_url"]}
    base = forgejo_base(r)
    d = get_json(f"{base}/repos/{r['repo']}")
    return {"mirror": d.get("mirror", False), "pushed": d["updated_at"], "web": d["html_url"]}


def github_activity(r):
    repo, web = r["repo"], r["web"]
    since_12m = (NOW - timedelta(days=365)).isoformat()
    since_90 = NOW - timedelta(days=90)
    commits = gh_paged(f"/repos/{repo}/commits", {"since": since_12m}, limit=250)
    rows = [commit_row(c) for c in commits]
    if rows:
        last = max(x["date"] for x in rows)
    else:
        newest = gh(f"/repos/{repo}/commits", {"per_page": 1})
        last = commit_row(newest[0])["date"] if newest else None
    out = commit_metrics(rows, last, since_90, f"{web}/commits")
    if len(commits) >= 25_000:
        for k in ("authors_12m", "top_share_12m"):
            out[k]["note"] = "history capped at newest 25,000 commits"
    out["days_since_release"] = github_release(repo, web)
    out.update(github_outside_prs(repo, web))
    return out


def commit_row(c):
    author = (c.get("author") or {}).get("login") or c["commit"]["author"].get("email") or c["commit"]["author"]["name"]
    return {"author": author, "date": c["commit"]["committer"]["date"]}


def commit_metrics(rows, last_date, since_90, source):
    rows = [r for r in rows if not is_bot(r["author"])]
    recent = [r for r in rows if parse_time(r["date"]) >= since_90]
    authors_12m = Counter(r["author"] for r in rows)
    top = authors_12m.most_common(1)
    share = round(100 * top[0][1] / len(rows), 1) if top else None
    return {
        "days_since_commit": ok(days_since(last_date), source, display=f"{days_since(last_date)} ({last_date[:10]})") if last_date else missing("not measured: no commits found", source),
        "commits_90d": ok(len(recent), source),
        "authors_90d": ok(len({r["author"] for r in recent}), source),
        "authors_12m": ok(len(authors_12m), source),
        "top_share_12m": ok(share, source, display=f"{share}% ({top[0][0]}, {top[0][1]} of {len(rows)})") if top else missing("not measured: no commits in 12 months", source),
    }


def is_bot(name):
    return bool(name) and (name.endswith("[bot]") or "bot@" in name or name.endswith("-bot"))


def is_release(r):
    return not r["draft"] and not r["prerelease"] and not re.search(r"nightly|latest", r["tag_name"])


def github_release(repo, web):
    rels = [x for x in gh(f"/repos/{repo}/releases", {"per_page": 30}) if is_release(x)]
    if rels:
        when = rels[0]["published_at"]
        return ok(days_since(when), f"{web}/releases", display=f"{days_since(when)} ({rels[0]['tag_name']}, {when[:10]})")
    tags = gh(f"/repos/{repo}/tags", {"per_page": 1})
    if not tags:
        return missing("not measured: no releases or tags", f"{web}/releases")
    c = gh(f"/repos/{repo}/commits/{tags[0]['commit']['sha']}")
    when = c["commit"]["committer"]["date"]
    return ok(days_since(when), f"{web}/tags", note="from latest tag; no GitHub releases", display=f"{days_since(when)} ({tags[0]['name']}, {when[:10]})")


OUTSIDE = {"CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"}


def github_outside_prs(repo, web):
    d90 = (NOW - timedelta(days=90)).date().isoformat()
    merged = gh_merged_prs(f"repo:{repo} is:pr is:merged merged:>={d90}")
    rejected = gh_search(f"repo:{repo} is:pr is:closed is:unmerged closed:>={d90}")
    open_ = gh_search(f"repo:{repo} is:pr is:open")
    outside = lambda items: [i for i in items if i["author_association"] in OUTSIDE and not is_bot(i["user"]["login"])]
    om, orj, oo = outside(merged), outside(rejected), outside(open_)
    src = f"{web}/pulls"
    total = len(om) + len(orj)
    authors = len({i["user"]["login"] for i in om})
    mergers = {i["merged_by"] for i in merged if i["merged_by"] and not is_bot(i["merged_by"])}
    return {
        "mergers_90d": ok(len(mergers), src, display=f"{len(mergers)} ({', '.join(sorted(mergers)[:8])}{', ...' if len(mergers) > 8 else ''})"),
        "outside_merged_90d": ok(len(om), src, display=f"{len(om)} by {authors} people (of {len(merged)} merged)"),
        "outside_accept_90d": ok(round(100 * len(om) / total, 1), src, display=f"{round(100 * len(om) / total)}% ({len(om)} merged, {len(orj)} closed unmerged)") if total else missing("not measured: no outside PRs closed in 90 days", src),
        "outside_open": ok(len(oo), src, display=f"{len(oo)} (of {len(open_)} open)"),
    }


def forgejo_base(r):
    return r.get("api") or f"https://{r['host']}.org/api/v1"


def forgejo_activity(r):
    base, repo, web = forgejo_base(r), r["repo"], r["web"]
    since_90 = NOW - timedelta(days=90)
    commits = forgejo_paged(f"{base}/repos/{repo}/commits", {"since": (NOW - timedelta(days=365)).isoformat(), "stat": "false", "files": "false", "verification": "false"})
    rows = [commit_row(c) for c in commits]
    last = max((x["date"] for x in rows), default=None)
    out = commit_metrics(rows, last, since_90, f"{web}/commits")
    rels = [x for x in get_json(f"{base}/repos/{repo}/releases", {"limit": 30}) if is_release(x)]
    out["days_since_release"] = ok(days_since(rels[0]["published_at"]), f"{web}/releases") if rels else missing("not measured: no releases", f"{web}/releases")
    note = "Forgejo has no author association; outside means not a public member of the owning org"
    owner = repo.split("/")[0]
    try:
        members = {u["login"] for u in forgejo_paged(f"{base}/orgs/{owner}/members", {})}
    except Exception:
        members = {owner}
    prs = [p for p in forgejo_paged(f"{base}/repos/{repo}/pulls", {"state": "closed", "sort": "recentupdate"}, stop=lambda p: parse_time(p["updated_at"]) < since_90)]
    outside = [p for p in prs if p["user"]["login"] not in members and parse_time(p["closed_at"] or p["updated_at"]) >= since_90]
    merged = [p for p in outside if p.get("merged")]
    mergers = {(p.get("merged_by") or {}).get("login") for p in prs if p.get("merged") and parse_time(p["closed_at"] or p["updated_at"]) >= since_90} - {None}
    open_ = [p for p in forgejo_paged(f"{base}/repos/{repo}/pulls", {"state": "open"}) if p["user"]["login"] not in members]
    src = f"{web}/pulls"
    merged_90 = [p for p in prs if p.get("merged") and parse_time(p["closed_at"] or p["updated_at"]) >= since_90]
    out["mergers_90d"] = (ok(len(mergers), src, display=f"{len(mergers)} ({', '.join(sorted(mergers))})") if mergers or not merged_90
                          else missing("not measured: API did not report who merged", src))
    out["outside_merged_90d"] = ok(len(merged), src, note)
    out["outside_accept_90d"] = ok(round(100 * len(merged) / len(outside), 1), src, note) if outside else missing("not measured: no outside PRs closed in 90 days", src)
    out["outside_open"] = ok(len(open_), src, note)
    return out


# ---------- Community ----------

def community(lang):
    out = {}
    out.update(guarded(["reddit_per_week"], reddit, lang.get("subreddit")))
    out.update(guarded(["forum_posts_7d", "forum_topics_7d"], forum, lang.get("forum")))
    out.update(guarded(["discussions_30d"], discussions, lang))
    out.update(guarded(["discord_members", "discord_online"], discord, lang.get("discord_invite")))
    out["platforms"] = platforms(lang)
    out["x_activity"] = x_activity(lang)
    return out


def reddit(sub):
    if not sub:
        return {"reddit_per_week": missing("none configured")}
    url = f"https://www.reddit.com/r/{sub}/new/.rss?limit=100"
    web = f"https://www.reddit.com/r/{sub}/new/"
    time.sleep(3)
    root = ET.fromstring(get(url, headers={"User-Agent": UA}))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    stamps = sorted(parse_time(e.findtext("a:published", namespaces=ns) or e.findtext("a:updated", namespaces=ns)) for e in root.findall("a:entry", ns))
    if not stamps:
        return {"reddit_per_week": ok(0, web)}
    window = 28
    in_window = [t for t in stamps if t >= NOW - timedelta(days=window)]
    if len(in_window) == len(stamps) and len(stamps) >= 25:
        span = max((NOW - stamps[0]).total_seconds() / 86400, 1)
        rate = round(len(stamps) / span * 7, 1)
        note = f"{len(stamps)} newest posts span {span:.1f} days"
    else:
        rate = round(len(in_window) / window * 7, 1)
        note = f"{len(in_window)} posts in last {window} days"
    return {"reddit_per_week": ok(rate, web, note)}


def forum(cfg):
    keys = ["forum_posts_7d", "forum_topics_7d"]
    if not cfg:
        return {k: missing("none configured") for k in keys}
    url = cfg["url"].rstrip("/")
    if cfg["kind"] == "discourse":
        stats = get_json(f"{url}/about.json", headers={"Accept": "application/json"})["about"]["stats"]
        return {
            "forum_posts_7d": ok(stats["posts_7_days"], url, display=f"{stats['posts_7_days']} ({stats['posts_30_days']} in 30d)"),
            "forum_topics_7d": ok(stats["topics_7_days"], url, display=f"{stats['topics_7_days']} ({stats['topics_30_days']} in 30d)"),
        }
    if cfg["kind"] == "nimforum":
        return nimforum(url)
    return {k: missing(f"not measured: unknown forum kind {cfg['kind']}") for k in keys}


def nimforum(url):
    cutoff = NOW - timedelta(days=7)
    threads, start = [], 0
    while True:
        page = get_json(f"{url}/threads.json", {"start": start, "count": 30})["threads"]
        threads += page
        if not page or datetime.fromtimestamp(page[-1]["activity"], timezone.utc) < cutoff or start > 600:
            break
        start += 30
    active = [t for t in threads if datetime.fromtimestamp(t["activity"], timezone.utc) >= cutoff]
    created = sum(datetime.fromtimestamp(t["creation"], timezone.utc) >= cutoff for t in active)
    return {
        "forum_posts_7d": missing("not measured: nimforum API has no post counts; see topics", url),
        "forum_topics_7d": ok(created, url, f"{len(active)} threads had activity in 7 days"),
    }


def discussions(lang):
    repo = lang.get("github_discussions")
    if not repo:
        return {"discussions_30d": missing("none configured")}
    web = f"https://github.com/{repo}/discussions"
    if not TOKEN:
        return {"discussions_30d": missing("not measured: GraphQL needs GITHUB_TOKEN", web)}
    d30 = (NOW - timedelta(days=30)).date().isoformat()
    q = 'query($q:String!){search(query:$q,type:DISCUSSION){discussionCount}}'
    data = gh_graphql(q, {"q": f"repo:{repo} created:>={d30}"})
    return {"discussions_30d": ok(data["search"]["discussionCount"], web)}


def discord(invite):
    keys = ["discord_members", "discord_online"]
    if not invite:
        return {k: missing("none configured") for k in keys}
    d = get_json(f"https://discord.com/api/v10/invites/{invite}", {"with_counts": "true"})
    web = f"https://discord.gg/{invite}"
    name = d.get("guild", {}).get("name", "")
    return {
        "discord_members": ok(d["approximate_member_count"], web, f"server: {name}"),
        "discord_online": ok(d["approximate_presence_count"], web, f"server: {name}"),
    }


def platforms(lang):
    found = []
    if lang.get("subreddit"): found.append("Reddit")
    if lang.get("forum"): found.append("Forum")
    if lang.get("github_discussions"): found.append("GitHub Discussions")
    if lang.get("discord_invite"): found.append("Discord")
    found += [p for p in lang.get("other_platforms", []) if p not in found]
    return ok(len(found), None, "counted from languages.toml", display=f"{len(found)} ({', '.join(found)})")


def x_activity(lang):
    accounts = lang.get("x_accounts", [])
    note = lang.get("x_notes") or "not measured: no X API credentials in CI"
    links = ", ".join(f"@{a}" for a in accounts) or "none listed"
    return {"value": None, "source": f"https://x.com/{accounts[0]}" if accounts else None, "note": f"{note}; accounts: {links}"}


# ---------- Editors ----------

def editors(lang):
    out = {}
    out.update(guarded(["zed_days", "zed_downloads"], zed, lang.get("zed", [])))
    out.update(guarded(["vscode_days", "vscode_installs", "vscode_rating"], vscode, lang.get("vscode", [])))
    out.update(guarded(["openvsx_days", "openvsx_downloads"], openvsx, lang.get("openvsx", [])))
    out.update(guarded(["lsp_days", "lsp_commits_90d"], lsp, lang.get("lsp")))
    rating = lang.get("lsp_rating")
    out["lsp_rating"] = ok(rating, None, lang.get("lsp_notes")) if rating else missing(f"not rated; {lang['lsp_notes']}" if lang.get("lsp_notes") else "not rated (manual field in languages.toml)")
    return out


def best(found, key):
    return max(found, key=key)


def flag(days):
    return f"{days} (stale, over {STALE_DAYS} days)" if days > STALE_DAYS else str(days)


def zed(ids):
    if not ids:
        return {"zed_days": missing("none configured"), "zed_downloads": missing("none configured")}
    found = []
    for i in ids:
        for e in get_json("https://api.zed.dev/extensions", {"filter": i})["data"]:
            if e["id"] == i:
                found.append(e)
    if not found:
        return {k: missing(f"not measured: {ids} not in Zed registry") for k in ("zed_days", "zed_downloads")}
    e = best(found, lambda e: e["published_at"])
    src = f"https://zed.dev/extensions?query={e['id']}"
    others = "; ".join(f"{x['id']} v{x['version']} {days_since(x['published_at'])}d" for x in found)
    d = days_since(e["published_at"])
    return {
        "zed_days": ok(d, src, f"{others}; repo {e['repository']}", display=f"{flag(d)} ({e['id']} v{e['version']})"),
        "zed_downloads": ok(sum(x["download_count"] for x in found), src),
    }


def vscode(ids):
    keys = ("vscode_days", "vscode_installs", "vscode_rating")
    if not ids:
        return {k: missing("none configured") for k in keys}
    found = []
    for i in ids:
        body = {"filters": [{"criteria": [{"filterType": 7, "value": i}]}], "flags": 914}
        res = get_json("https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery", data=body,
                       headers={"Accept": "application/json;api-version=7.1-preview.1"})["results"][0]["extensions"]
        for e in res:
            st = {s["statisticName"]: s["value"] for s in e.get("statistics", [])}
            found.append({"id": i, "updated": e["lastUpdated"], "installs": int(st.get("install", 0)),
                          "rating": st.get("averagerating"), "ratings": int(st.get("ratingcount", 0))})
    if not found:
        return {k: missing(f"not measured: {ids} not found on marketplace") for k in keys}
    e = best(found, lambda e: e["updated"])
    top = best(found, lambda e: e["installs"])
    src = lambda x: f"https://marketplace.visualstudio.com/items?itemName={x['id']}"
    others = "; ".join(f"{x['id']}: {days_since(x['updated'])}d, {x['installs']} installs" for x in found)
    d = days_since(e["updated"])
    return {
        "vscode_days": ok(d, src(e), others, display=f"{flag(d)} ({e['id']})"),
        "vscode_installs": ok(top["installs"], src(top), display=f"{top['installs']:,} ({top['id']})"),
        "vscode_rating": ok(round(top["rating"], 2), src(top), display=f"{top['rating']:.1f} ({top['ratings']} ratings)") if top["rating"] else missing("not measured: no ratings", src(top)),
    }


def openvsx(ids):
    keys = ("openvsx_days", "openvsx_downloads")
    if not ids:
        return {k: missing("none configured") for k in keys}
    found, absent = [], []
    for i in ids:
        ns, name = i.split(".", 1)
        try:
            d = get_json(f"https://open-vsx.org/api/{ns}/{name}")
            found.append({"id": i, "updated": d["timestamp"], "downloads": d.get("downloadCount", 0)})
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            absent.append(i)
    if not found:
        return {k: missing(f"not on Open VSX: {', '.join(absent)}", "https://open-vsx.org") for k in keys}
    e = best(found, lambda e: e["updated"])
    src = f"https://open-vsx.org/extension/{e['id'].replace('.', '/')}"
    d = days_since(e["updated"])
    return {
        "openvsx_days": ok(d, src, f"absent: {', '.join(absent)}" if absent else None, display=f"{flag(d)} ({e['id']})"),
        "openvsx_downloads": ok(sum(x["downloads"] for x in found), src),
    }


def lsp(cfg):
    if not cfg:
        return {k: missing("none configured") for k in ("lsp_days", "lsp_commits_90d")}
    repo = cfg["repo"]
    web = f"https://github.com/{repo}"
    since = (NOW - timedelta(days=90)).isoformat()
    path = {"path": cfg["path"]} if cfg.get("path") else {}
    recent = gh_paged(f"/repos/{repo}/commits", {"since": since, **path})
    last = recent[0] if recent else (gh(f"/repos/{repo}/commits", {"per_page": 1, **path}) or [None])[0]
    note = cfg["name"]
    if not last:
        return {"lsp_days": missing("not measured: no commits", web), "lsp_commits_90d": ok(0, web, note)}
    when = last["commit"]["committer"]["date"]
    shared = " (shared with compiler repo)" if cfg.get("shared") else ""
    return {
        "lsp_days": ok(days_since(when), f"{web}/commits", note, display=f"{days_since(when)} ({when[:10]})"),
        "lsp_commits_90d": ok(len(recent), f"{web}/commits", note + shared),
    }


# ---------- HTTP ----------

def gh(path, params=None):
    return get_json(f"https://api.github.com{path}", params, headers=gh_headers())


def gh_headers():
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def gh_paged(path, params, limit=40):
    items = []
    for page in range(1, limit + 1):
        batch = gh(path, {**params, "per_page": 100, "page": page})
        items += batch
        if len(batch) < 100:
            break
    return items


def gh_search(q):
    items = []
    for page in range(1, 11):
        d = gh("/search/issues", {"q": q, "per_page": 100, "page": page})
        items += d["items"]
        if len(items) >= d["total_count"] or not d["items"]:
            break
    return items


MERGED_Q = """query($q:String!,$after:String){search(query:$q,type:ISSUE,first:100,after:$after){issueCount
pageInfo{hasNextPage endCursor} nodes{... on PullRequest{authorAssociation author{login} mergedBy{login}}}}}"""


def gh_merged_prs(q):
    items, after = [], None
    while True:
        d = gh_graphql(MERGED_Q, {"q": q, "after": after})["search"]
        items += [{"author_association": n["authorAssociation"], "user": {"login": (n["author"] or {}).get("login", "ghost")},
                   "merged_by": (n["mergedBy"] or {}).get("login")} for n in d["nodes"]]
        if not d["pageInfo"]["hasNextPage"]:
            return items
        after = d["pageInfo"]["endCursor"]


def gh_graphql(query, variables):
    d = get_json("https://api.github.com/graphql", data={"query": query, "variables": variables}, headers=gh_headers())
    if d.get("errors"):
        raise RuntimeError(d["errors"][0]["message"])
    return d["data"]


def forgejo_paged(url, params, stop=None, limit=60):
    items = []
    for page in range(1, limit + 1):
        batch = get_json(url, {**params, "limit": 50, "page": page})
        items += batch
        if len(batch) < 50 or (stop and batch and stop(batch[-1])):
            break
    return items


def get_json(url, params=None, data=None, headers=None):
    return json.loads(get(url, params, data, headers))


def get(url, params=None, data=None, headers=None, attempts=4):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"User-Agent": UA, **({"Content-Type": "application/json"} if body else {}), **(headers or {})}
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, hdrs), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            wait = rate_limit_wait(e)
            if wait is None or attempt == attempts - 1:
                raise
            log(f"   rate limited on {url[:80]}, waiting {wait}s")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))


def rate_limit_wait(e):
    if e.code >= 500:
        return 10
    if e.code == 429 or (e.code == 403 and e.headers.get("x-ratelimit-remaining") == "0"):
        if e.headers.get("retry-after"):
            return min(max(int(float(e.headers["retry-after"])) + 1, 15), 120)
        if e.headers.get("x-ratelimit-reset"):
            reset = int(float(e.headers["x-ratelimit-reset"]))
            return min(max((reset if reset < 1e9 else reset - int(time.time())) + 2, 15), 900)
        return 30
    if e.code == 403 and "secondary rate limit" in e.read().decode(errors="ignore").lower():
        return 60
    return None


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------- Page ----------

def render(results):
    stamp = NOW.strftime("%Y-%m-%d %H:%M UTC")
    return TEMPLATE.format(
        stamp=stamp,
        summary=summary_table(results),
        details="\n".join(detail(r) for r in results),
        thresholds=thresholds_table(),
    )


def stars(s):
    if s is None:
        return '<span class="nm">not measured</span>'
    return f'<span class="score s{round(s)}">{s:.1f}</span>'


def summary_table(results):
    head = "".join(f'<th data-type="num">{c}</th>' for c in CATEGORIES)
    rows = []
    for r in results:
        cells = "".join(f'<td data-v="{r["categories"][c] or -1}">{stars(r["categories"][c])}</td>' for c in CATEGORIES)
        rows.append(f'<tr><td data-v="{esc(r["name"])}"><a href="#{slug(r["name"])}">{esc(r["name"])}</a></td>'
                    f'<td data-v="{r["overall"] or -1}">{stars(r["overall"])}</td>{cells}</tr>')
    return (f'<table class="sortable"><thead><tr><th data-type="text">Language</th><th data-type="num">Overall</th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def detail(r):
    m = r["metrics"]
    sections = []
    for cat, keys in CATEGORIES.items():
        rows = "".join(metric_row(k, m[k]) for k in keys if k in m)
        sections.append(f'<h3>{cat} <span class="dim">score</span> {stars(r["categories"][cat])}</h3>'
                        f'<table class="metrics"><thead><tr><th>Metric</th><th>Value</th><th>Score</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table>')
    canon = r["config"].get("_canonical")
    repo = f'<a href="{canon["web"]}">{canon["host"]}:{canon["repo"]}</a>' if canon else '<span class="nm">none</span>'
    return (f'<section class="lang" id="{slug(r["name"])}"><h2>{esc(r["name"])} <span class="dim">overall</span> {stars(r["overall"])}</h2>'
            f'<p><span class="dim">Main repo</span> {repo}</p>{"".join(sections)}</section>')


def metric_row(key, m):
    label, unit = RULES[key][0], RULES[key][1]
    v = m.get("value")
    if v is None:
        value = f'<span class="nm">{"n/a" if (m.get("note") or "").startswith("none") else "not measured"}</span>'
    else:
        shown = m.get("display") or (f"{v:,}" if isinstance(v, int) else str(v))
        head, _, rest = shown.partition(" ")
        head = head.removesuffix(unit) if unit == "%" else head
        value = f'{esc(head)} <span class="dim">{esc(unit)}</span> {esc(rest)}'
    if "stale" in (m.get("display") or ""):
        value = f'<span class="warn">{value}</span>'
    src = f' <a class="src" href="{esc(m["source"])}">source</a>' if m.get("source") else ""
    s = score(key, v)
    sc = f'<span class="score s{s}">{s}</span>' if s else '<span class="dim">unscored</span>'
    return f'<tr><td class="dim">{esc(label)}</td><td>{value}</td><td>{sc}</td><td class="note">{esc(m.get("note") or "")}{src}</td></tr>'


def thresholds_table():
    rows = []
    for key, (label, unit, better, cuts) in RULES.items():
        if cuts is None:
            rows.append(f'<tr><td class="dim">{esc(label)}</td><td colspan="5" class="dim">shown for context, not scored</td></tr>')
            continue
        op = "&ge;" if better else "&le;"
        cells = "".join(f"<td>{op} {c:g}</td>" for c in cuts)
        rows.append(f'<tr><td class="dim">{esc(label)} <span class="dim">({esc(unit)})</span></td>{cells}<td class="dim">otherwise</td></tr>')
    return ('<table class="metrics"><thead><tr><th>Metric</th><th>5</th><th>4</th><th>3</th><th>2</th><th>1</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def esc(s):
    return html.escape(str(s))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower())


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Language evaluation</title>
<style>
:root {{ --bg:#1d2024; --panel:#262a30; --fg:#e8e4dc; --line:#3a4048; --accent:#8fb8de; --warn:#e0a85c; }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; margin:0; padding:2rem; max-width:1200px; margin:auto; }}
a {{ color:var(--accent); }}
h1, h2, h3 {{ font-weight:600; }}
h2 {{ border-top:1px solid var(--line); padding-top:1.5rem; margin-top:2.5rem; }}
.dim, th, .note {{ opacity:.7; }}
.note {{ font-size:13px; }}
.nm {{ opacity:.55; font-style:italic; }}
.warn {{ color:var(--warn); }}
table {{ border-collapse:collapse; width:100%; background:var(--panel); margin:.5rem 0 1rem; }}
th, td {{ text-align:left; padding:.4rem .7rem; border-bottom:1px solid var(--line); vertical-align:top; }}
table.sortable th {{ cursor:pointer; user-select:none; }}
table.sortable th:after {{ content:" \\2195"; opacity:.5; }}
.score {{ display:inline-block; min-width:2.2em; text-align:center; border-radius:4px; padding:0 .3em; font-weight:600; color:#1d2024; }}
.s5 {{ background:#7fc98f; }} .s4 {{ background:#b5d37a; }} .s3 {{ background:#e3cf6e; }} .s2 {{ background:#e6a065; }} .s1 {{ background:#de7b72; }}
</style></head><body>
<h1>Programming language evaluation</h1>
<p><span class="dim">Generated at</span> {stamp} <span class="dim">from live API calls. Click a column to sort. Category scores average the measured metrics; overall averages the categories. Metrics that could not be fetched say so and are left out of the averages.</span></p>
{summary}
{details}
<h2>Scoring thresholds</h2>
<p class="dim">Each metric maps to a 1 to 5 score using these cut points.</p>
{thresholds}
<script>
document.querySelectorAll("table.sortable th").forEach((th, i) => th.addEventListener("click", () => {{
  const body = th.closest("table").tBodies[0];
  const num = th.dataset.type === "num";
  const dir = th.dataset.dir = th.dataset.dir === "desc" ? "asc" : "desc";
  const val = r => r.cells[i].dataset.v;
  [...body.rows].sort((a, b) => {{
    const c = num ? val(a) - val(b) : val(a).localeCompare(val(b));
    return dir === "asc" ? c : -c;
  }}).forEach(r => body.appendChild(r));
}}));
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
