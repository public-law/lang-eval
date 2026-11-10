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
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NotRequired, TypedDict

type JSON = Any
type Params = dict[str, str | int | None]

ROOT = Path(__file__).parent
CONFIG = ROOT / "languages.toml"
DATA = ROOT / "docs" / "data.json"
PAGE = ROOT / "docs" / "index.html"
HISTORY = ROOT / "docs" / "history.jsonl"
NOW = datetime.now(timezone.utc)
UA = "lang-eval/0.1 (+https://github.com/public-law/lang-eval)"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

type Rule = tuple[str, str, bool | None, list[float] | None]

# Each rule: (label, unit, higher_is_better, thresholds for scores 5,4,3,2; anything else scores 1)
RULES: dict[str, Rule] = {
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
CATEGORIES: dict[str, list[str]] = {
    "Development": ["days_since_commit", "commits_90d", "days_since_release", "authors_90d", "authors_12m",
                    "mergers_90d", "top_share_12m", "outside_merged_90d", "outside_accept_90d", "outside_open"],
    "Community": ["reddit_per_week", "forum_posts_7d", "forum_topics_7d", "discussions_30d", "discord_members",
                  "discord_online", "platforms", "x_activity"],
    "Editor support": ["zed_days", "zed_downloads", "vscode_days", "vscode_installs", "vscode_rating",
                       "openvsx_days", "openvsx_downloads", "lsp_days", "lsp_commits_90d", "lsp_rating"],
}
STALE_DAYS = 90



class Metric(TypedDict):
    value: float | None
    source: str | None
    note: str | None
    display: NotRequired[str | None]
    measured_at: NotRequired[str]
    cached_from: NotRequired[str | None]
    series: NotRequired[dict[str, int]]
    values: NotRequired[list[float | None]]


type Metrics = dict[str, Metric]


@dataclass(frozen=True)
class Repo:
    host: str
    repo: str
    api: str | None = None


@dataclass(frozen=True)
class Canonical:
    host: str
    repo: str
    api: str | None
    mirror: bool
    pushed: str
    web: str

    def to_json(self) -> dict[str, JSON]:
        api = {"api": self.api} if self.api else {}
        return {"host": self.host, "repo": self.repo, **api, "mirror": self.mirror, "pushed": self.pushed, "web": self.web}


@dataclass(frozen=True)
class Forum:
    kind: str
    url: str


@dataclass(frozen=True)
class Lsp:
    name: str
    repo: str
    path: str | None = None
    shared: bool = False


@dataclass(frozen=True)
class Packages:
    kind: str
    url: str | None = None


@dataclass(frozen=True)
class Governance:
    body: str | None = None
    body_url: str | None = None
    roadmap: str | None = None
    roadmap_updated: str | None = None
    rfc_url: str | None = None
    funding: str | None = None
    funding_url: str | None = None
    notes: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Language:
    name: str
    raw: dict[str, JSON] = field(repr=False, compare=False)
    repos: tuple[Repo, ...] = ()
    subreddit: str | None = None
    forum: Forum | None = None
    github_discussions: str | None = None
    discord_invite: str | None = None
    other_platforms: tuple[str, ...] = ()
    x_accounts: tuple[str, ...] = ()
    x_notes: str | None = None
    zed: tuple[str, ...] = ()
    vscode: tuple[str, ...] = ()
    openvsx: tuple[str, ...] = ()
    lsp: Lsp | None = None
    lsp_rating: int | None = None
    lsp_notes: str | None = None
    hn_query: str | None = None
    packages: Packages | None = None
    governance: Governance | None = None


class Summary(TypedDict):
    categories: dict[str, float | None]
    overall: float | None


class Result(Summary):
    name: str
    metrics: Metrics
    config: dict[str, JSON]


def main(argv: list[str]) -> None:
    langs = load_config(CONFIG.read_text())
    if "--check-config" in argv:
        log(f"{CONFIG.name}: {len(langs)} languages, no problems")
        return
    previous = load_previous()
    history = load_history()
    results: list[Result] = []
    for lang in langs:
        log(f"== {lang.name}")
        metrics, canonical = evaluate(lang, history)
        carry_forward(metrics, previous.get(lang.name, {}))
        s = summarize(metrics)
        config = {**lang.raw, **({"_canonical": canonical.to_json()} if canonical else {})}
        results.append({"name": lang.name, "metrics": metrics, "config": config, "categories": s["categories"], "overall": s["overall"]})
    results.sort(key=lambda r: -(r["overall"] or 0))
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps({"generated_at": NOW.isoformat(), "languages": results}, indent=1, default=str))
    append_history(results)
    PAGE.write_text(render(results, load_history()))
    log(f"wrote {PAGE}")


def evaluate(lang: Language, history: list[dict[str, JSON]] | None = None) -> tuple[Metrics, Canonical | None]:
    m, canonical = development(lang)
    m.update(community(lang))
    m.update(editors(lang))
    m.update(trajectory(lang, canonical, history or load_history()))
    return m, canonical


# ---------- Config ----------

class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"{CONFIG.name}: {len(problems)} problem(s)\n" + "\n".join(f"  {p}" for p in problems))


HOSTS = {"github", "codeberg", "gitlab"}
FORUM_KINDS = {"discourse", "nimforum"}
SLUG = r"[A-Za-z0-9_.-]+"
OWNER_REPO = rf"{SLUG}/{SLUG}"
EXTENSION_ID = r"[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+"


def load_config(text: str) -> list[Language]:
    problems: list[str] = []
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"invalid TOML: {e}"]) from None
    top = Table(raw, "", problems)
    tables = top.tables("language", required=True)
    top.finish()
    langs = [parse_language(t) for t in tables]
    seen: dict[str, int] = {}
    for i, lang in enumerate(langs):
        if lang.name and lang.name.lower() in seen:
            problems.append(f"language[{i}].name: duplicate of language[{seen[lang.name.lower()]}] ({lang.name!r})")
        seen.setdefault(lang.name.lower(), i)
    if problems:
        raise ConfigError(problems)
    return langs


def parse_language(t: "Table") -> Language:
    repos = tuple(parse_repo(r) for r in t.tables("repos"))
    forum = t.table("forum")
    lsp = t.table("lsp")
    lang = Language(
        name=t.text("name", required=True) or "",
        raw=t.data,
        repos=repos,
        subreddit=t.text("subreddit", pattern=r"[A-Za-z0-9_]+"),
        forum=parse_forum(forum) if forum else None,
        github_discussions=t.text("github_discussions", pattern=OWNER_REPO),
        discord_invite=t.text("discord_invite", pattern=r"[A-Za-z0-9-]+"),
        other_platforms=t.texts("other_platforms"),
        x_accounts=t.texts("x_accounts", pattern=r"[A-Za-z0-9_]{1,15}"),
        x_notes=t.text("x_notes"),
        zed=t.texts("zed", pattern=r"[a-z0-9_-]+"),
        vscode=t.texts("vscode", pattern=EXTENSION_ID),
        openvsx=t.texts("openvsx", pattern=EXTENSION_ID),
        lsp=parse_lsp(lsp) if lsp else None,
        lsp_rating=t.integer("lsp_rating", 1, 5),
        lsp_notes=t.text("lsp_notes"),
        hn_query=t.text("hn_query"),
        packages=parse_packages(pkg) if (pkg := t.table("packages")) else None,
        governance=parse_governance(gov) if (gov := t.table("governance")) else None,
    )
    t.finish()
    return lang


def parse_repo(t: "Table") -> Repo:
    host = t.text("host", required=True, choices=HOSTS) or ""
    repo = Repo(host, t.text("repo", required=True, pattern=OWNER_REPO) or "", t.url("api"))
    t.finish()
    return repo


def parse_forum(t: "Table") -> Forum:
    forum = Forum(t.text("kind", required=True, choices=FORUM_KINDS) or "", t.url("url", required=True) or "")
    t.finish()
    return forum


def parse_lsp(t: "Table") -> Lsp:
    lsp = Lsp(t.text("name", required=True) or "", t.text("repo", required=True, pattern=OWNER_REPO) or "",
              t.text("path"), t.boolean("shared"))
    t.finish()
    return lsp


PACKAGE_KINDS = {"hex"}
DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"


def parse_packages(t: "Table") -> Packages:
    pkg = Packages(t.text("kind", required=True, choices=PACKAGE_KINDS) or "", t.url("url"))
    t.finish()
    return pkg


def parse_governance(t: "Table") -> Governance:
    gov = Governance(
        body=t.text("body"),
        body_url=t.url("body_url"),
        roadmap=t.url("roadmap"),
        roadmap_updated=t.text("roadmap_updated", pattern=DATE),
        rfc_url=t.url("rfc_url"),
        funding=t.text("funding"),
        funding_url=t.url("funding_url"),
        notes=t.text("notes"),
        sources=t.texts("sources", pattern=r"https://\S+"),
    )
    t.finish()
    return gov


class Table:
    def __init__(self, data: dict[str, JSON], path: str, problems: list[str]) -> None:
        self.data, self.path, self.problems = data, path, problems
        self.used: set[str] = set()

    def finish(self) -> None:
        for key in self.data.keys() - self.used:
            self.problem(key, f"unknown key {key!r}")

    def problem(self, key: str, message: str) -> None:
        self.problems.append(f"{self.at(key)}: {message}")

    def at(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def get(self, key: str, required: bool) -> object:
        self.used.add(key)
        if key not in self.data and required:
            self.problem(key, "missing required field")
        return self.data.get(key)

    def text(self, key: str, required: bool = False, pattern: str | None = None, choices: set[str] | None = None) -> str | None:
        v = self.get(key, required)
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            self.problem(key, f"expected a non-empty string, got {v!r}")
            return None
        self.check(key, v, pattern, choices)
        return v

    def check(self, key: str, v: str, pattern: str | None, choices: set[str] | None) -> None:
        if pattern and not re.fullmatch(pattern, v):
            self.problem(key, f"{v!r} does not match {pattern}")
        if choices and v not in choices:
            self.problem(key, f"{v!r} is not one of {', '.join(sorted(choices))}")

    def texts(self, key: str, pattern: str | None = None) -> tuple[str, ...]:
        v = self.get(key, False)
        if v is None:
            return ()
        if not isinstance(v, list):
            self.problem(key, f"expected a list of strings, got {v!r}")
            return ()
        out = []
        for i, x in enumerate(v):
            if not isinstance(x, str) or not x.strip():
                self.problem(f"{key}[{i}]", f"expected a non-empty string, got {x!r}")
                continue
            self.check(f"{key}[{i}]", x, pattern, None)
            out.append(x)
        return tuple(out)

    def integer(self, key: str, low: int, high: int) -> int | None:
        v = self.get(key, False)
        if v is None:
            return None
        if not isinstance(v, int) or isinstance(v, bool) or not low <= v <= high:
            self.problem(key, f"expected an integer from {low} to {high}, got {v!r}")
            return None
        return v

    def boolean(self, key: str) -> bool:
        v = self.get(key, False)
        if v is not None and not isinstance(v, bool):
            self.problem(key, f"expected true or false, got {v!r}")
        return v is True

    def url(self, key: str, required: bool = False) -> str | None:
        v = self.text(key, required)
        if v is None:
            return None
        u = urllib.parse.urlsplit(v)
        if u.scheme != "https" or not u.hostname or "." not in u.hostname:
            self.problem(key, f"expected an https URL with a host, got {v!r}")
            return None
        return v

    def table(self, key: str) -> "Table | None":
        v = self.get(key, False)
        if v is None:
            return None
        if not isinstance(v, dict):
            self.problem(key, f"expected a table, got {v!r}")
            return None
        return Table(v, self.at(key), self.problems)

    def tables(self, key: str, required: bool = False) -> list["Table"]:
        v = self.get(key, required)
        if v is None:
            return []
        if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
            self.problem(key, "expected an array of tables")
            return []
        return [Table(x, f"{self.at(key)}[{i}]", self.problems) for i, x in enumerate(v)]


# ---------- Metric helpers ----------

def ok(value: float | None, source: str | None, note: str | None = None, display: str | None = None) -> Metric:
    return {"value": value, "source": source, "note": note, "display": display}


def missing(reason: str, source: str | None = None) -> Metric:
    return {"value": None, "source": source, "note": reason}


def guarded[*Ts](keys: Iterable[str], fn: Callable[[*Ts], Metrics], *args: *Ts) -> Metrics:
    try:
        return fn(*args)
    except Exception as e:
        log(f"   failed {keys}: {e}")
        return {k: missing(f"not measured: {short(e)}") for k in keys}


def short(e: Exception) -> str:
    return re.sub(r"\s+", " ", str(e))[:140]


def score(key: str, value: float | None) -> int | None:
    _, _, better, cuts = RULES[key]
    if value is None or cuts is None:
        return None
    for s, cut in zip((5, 4, 3, 2), cuts):
        if (value >= cut) if better else (value <= cut):
            return s
    return 1


def summarize(metrics: Metrics) -> Summary:
    cats: dict[str, float | None] = {}
    for cat, keys in CATEGORIES.items():
        scores = [s for k in keys if k in metrics if (s := score(k, metrics[k]["value"])) is not None]
        cats[cat] = round(sum(scores) / len(scores), 2) if scores else None
    measured = [v for v in cats.values() if v is not None]
    return {"categories": cats, "overall": round(sum(measured) / len(measured), 2) if measured else None}


def days_since(iso: str) -> int:
    return (NOW - parse_time(iso)).days


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_previous() -> dict[str, Metrics]:
    if not DATA.exists():
        return {}
    return {l["name"]: l["metrics"] for l in json.loads(DATA.read_text())["languages"]}


def carry_forward(metrics: Metrics, old: Metrics) -> None:
    for key, m in metrics.items():
        prev = old.get(key)
        note = m["note"] or ""
        since = (prev.get("cached_from") or prev.get("measured_at")) if prev else None
        if m["value"] is None and note.startswith("not measured:") and prev and prev["value"] is not None and since:
            cached = prev.copy()
            cached["cached_from"] = since
            cached["note"] = f"cached from {since[:10]}; latest fetch failed ({note[14:]})"
            metrics[key] = cached
        elif m["value"] is not None:
            m["measured_at"] = NOW.isoformat()


# ---------- Development ----------

def development(lang: Language) -> tuple[Metrics, Canonical | None]:
    keys = CATEGORIES["Development"]
    if not lang.repos:
        return {k: missing("not measured: no repo configured") for k in keys}, None
    canonical, notes = pick_canonical(lang.repos)
    if not canonical:
        return {k: missing("not measured: no repo reachable; " + "; ".join(notes)) for k in keys}, None
    fetcher = {"github": github_activity, "gitlab": gitlab_activity}.get(canonical.host, forgejo_activity)
    out = guarded(keys, fetcher, canonical)
    if notes and out["days_since_commit"]["value"] is not None:
        out["days_since_commit"]["note"] = "; ".join(notes)
    return out, canonical


def pick_canonical(repos: tuple[Repo, ...]) -> tuple[Canonical | None, list[str]]:
    best: Canonical | None = None
    notes: list[str] = []
    for r in repos:
        try:
            c = repo_info(r)
        except Exception as e:
            notes.append(f"{r.host}:{r.repo} unreachable ({short(e)[:60]})")
            continue
        if c.mirror:
            notes.append(f"{r.host}:{r.repo} is a mirror")
            continue
        if best is None or c.pushed > best.pushed:
            best = c
    if best and len(repos) > 1:
        notes.append(f"canonical repo detected as {best.host}:{best.repo}")
    return best, notes


def repo_info(r: Repo) -> Canonical:
    if r.host == "github":
        d = gh(f"/repos/{r.repo}")
        return Canonical(r.host, r.repo, r.api, bool(d.get("mirror_url")), str(d["pushed_at"]), str(d["html_url"]))
    if r.host == "gitlab":
        d = get_json(f"{gitlab_base(r.api)}/projects/{quote_path(r.repo)}")
        pushed = str(d.get("last_activity_at") or d.get("updated_at"))
        return Canonical(r.host, r.repo, r.api, bool(d.get("mirror")), pushed, str(d["web_url"]))
    d = get_json(f"{forgejo_base(r.host, r.api)}/repos/{r.repo}")
    return Canonical(r.host, r.repo, r.api, bool(d.get("mirror", False)), str(d["updated_at"]), str(d["html_url"]))


@dataclass(frozen=True)
class Commit:
    author: str
    date: str


def parse_commit(c: JSON) -> Commit:
    author = (c.get("author") or {}).get("login") or c["commit"]["author"].get("email") or c["commit"]["author"]["name"]
    return Commit(str(author), str(c["commit"]["committer"]["date"]))


def github_activity(r: Canonical) -> Metrics:
    repo, web = r.repo, r.web
    since_12m = (NOW - timedelta(days=365)).isoformat()
    since_90 = NOW - timedelta(days=90)
    commits = gh_paged(f"/repos/{repo}/commits", {"since": since_12m}, limit=250)
    rows = [parse_commit(c) for c in commits]
    last: str | None
    if rows:
        last = max(x.date for x in rows)
    else:
        newest = gh(f"/repos/{repo}/commits", {"per_page": 1})
        last = parse_commit(newest[0]).date if newest else None
    out = commit_metrics(rows, last, since_90, f"{web}/commits")
    if len(commits) >= 25_000:
        for k in ("authors_12m", "top_share_12m"):
            out[k]["note"] = "history capped at newest 25,000 commits"
    out["days_since_release"] = github_release(repo, web)
    out.update(github_outside_prs(repo, web))
    return out


def commit_metrics(rows: list[Commit], last_date: str | None, since_90: datetime, source: str) -> Metrics:
    rows = [r for r in rows if not is_bot(r.author)]
    recent = [r for r in rows if parse_time(r.date) >= since_90]
    authors_12m = Counter(r.author for r in rows)
    top = authors_12m.most_common(1)
    share = round(100 * top[0][1] / len(rows), 1) if top else None
    return {
        "days_since_commit": ok(days_since(last_date), source, display=f"{days_since(last_date)} ({last_date[:10]})") if last_date else missing("not measured: no commits found", source),
        "commits_90d": ok(len(recent), source),
        "authors_90d": ok(len({r.author for r in recent}), source),
        "authors_12m": ok(len(authors_12m), source),
        "top_share_12m": ok(share, source, display=f"{share}% ({top[0][0]}, {top[0][1]} of {len(rows)})") if top else missing("not measured: no commits in 12 months", source),
    }


def is_bot(name: str | None) -> bool:
    return name is not None and (name.endswith("[bot]") or "bot@" in name or name.endswith("-bot"))


@dataclass(frozen=True)
class Release:
    tag: str
    published_at: str


def parse_releases(items: JSON) -> list[Release]:
    return [Release(str(r["tag_name"]), str(r["published_at"])) for r in items
            if not r["draft"] and not r["prerelease"] and r["published_at"] and not re.search(r"nightly|latest", r["tag_name"])]


def github_release(repo: str, web: str) -> Metric:
    rels = parse_releases(gh(f"/repos/{repo}/releases", {"per_page": 30}))
    if rels:
        when = rels[0].published_at
        return ok(days_since(when), f"{web}/releases", display=f"{days_since(when)} ({rels[0].tag}, {when[:10]})")
    tags = gh(f"/repos/{repo}/tags", {"per_page": 1})
    if not tags:
        return missing("not measured: no releases or tags", f"{web}/releases")
    when = parse_commit(gh(f"/repos/{repo}/commits/{tags[0]['commit']['sha']}")).date
    return ok(days_since(when), f"{web}/tags", note="from latest tag; no GitHub releases", display=f"{days_since(when)} ({tags[0]['name']}, {when[:10]})")


OUTSIDE = {"CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"}


@dataclass(frozen=True)
class PullRequest:
    association: str
    author: str
    merged_by: str | None = None

    @property
    def outside(self) -> bool:
        return self.association in OUTSIDE and not is_bot(self.author)


def github_outside_prs(repo: str, web: str) -> Metrics:
    d90 = (NOW - timedelta(days=90)).date().isoformat()
    merged = gh_merged_prs(f"repo:{repo} is:pr is:merged merged:>={d90}")
    rejected = gh_search(f"repo:{repo} is:pr is:closed is:unmerged closed:>={d90}")
    open_ = gh_search(f"repo:{repo} is:pr is:open")
    om, orj, oo = ([p for p in ps if p.outside] for ps in (merged, rejected, open_))
    src = f"{web}/pulls"
    total = len(om) + len(orj)
    authors = len({p.author for p in om})
    mergers = {p.merged_by for p in merged if p.merged_by and not is_bot(p.merged_by)}
    return {
        "mergers_90d": ok(len(mergers), src, display=f"{len(mergers)} ({', '.join(sorted(mergers)[:8])}{', ...' if len(mergers) > 8 else ''})"),
        "outside_merged_90d": ok(len(om), src, display=f"{len(om)} by {authors} people (of {len(merged)} merged)"),
        "outside_accept_90d": ok(round(100 * len(om) / total, 1), src, display=f"{round(100 * len(om) / total)}% ({len(om)} merged, {len(orj)} closed unmerged)") if total else missing("not measured: no outside PRs closed in 90 days", src),
        "outside_open": ok(len(oo), src, display=f"{len(oo)} (of {len(open_)} open)"),
    }


def forgejo_base(host: str, api: str | None) -> str:
    return api or f"https://{host}.org/api/v1"


@dataclass(frozen=True)
class ForgejoPull:
    author: str
    merged: bool
    merged_by: str | None
    closed: datetime


def parse_forgejo_pull(p: JSON) -> ForgejoPull:
    return ForgejoPull(str(p["user"]["login"]), bool(p.get("merged")), (p.get("merged_by") or {}).get("login"),
                       parse_time(p["closed_at"] or p["updated_at"]))


def forgejo_release(base: str, repo: str, web: str) -> Metric:
    try:
        rels = parse_releases(get_json(f"{base}/repos/{repo}/releases", {"limit": 30}))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        rels = []
    if rels:
        when = rels[0].published_at
        return ok(days_since(when), f"{web}/releases", display=f"{days_since(when)} ({rels[0].tag}, {when[:10]})")
    tags = get_json(f"{base}/repos/{repo}/tags", {"limit": 1})
    if not tags:
        return missing("not measured: no releases or tags", f"{web}/releases")
    when = str(tags[0]["commit"]["created"])
    return ok(days_since(when), f"{web}/tags", note="from latest tag; releases disabled or empty", display=f"{days_since(when)} ({tags[0]['name']}, {when[:10]})")


def forgejo_activity(r: Canonical) -> Metrics:
    base, repo, web = forgejo_base(r.host, r.api), r.repo, r.web
    since_90 = NOW - timedelta(days=90)
    commits = forgejo_paged(f"{base}/repos/{repo}/commits", {"since": (NOW - timedelta(days=365)).isoformat(), "stat": "false", "files": "false", "verification": "false"})
    rows = [parse_commit(c) for c in commits]
    last = max((x.date for x in rows), default=None)
    out = commit_metrics(rows, last, since_90, f"{web}/commits")
    out["days_since_release"] = forgejo_release(base, repo, web)
    note = "Forgejo has no author association; outside means not a public member of the owning org"
    owner = repo.split("/")[0]
    try:
        members = {str(u["login"]) for u in forgejo_paged(f"{base}/orgs/{owner}/members", {})}
    except Exception:
        members = {owner}
    prs = [parse_forgejo_pull(p) for p in forgejo_paged(f"{base}/repos/{repo}/pulls", {"state": "closed", "sort": "recentupdate"},
                                                       stop=lambda p: parse_time(p["updated_at"]) < since_90)]
    outside = [p for p in prs if p.author not in members and p.closed >= since_90]
    merged = [p for p in outside if p.merged]
    merged_90 = [p for p in prs if p.merged and p.closed >= since_90]
    mergers = {p.merged_by for p in merged_90 if p.merged_by}
    open_ = [p for p in forgejo_paged(f"{base}/repos/{repo}/pulls", {"state": "open"}) if p["user"]["login"] not in members]
    src = f"{web}/pulls"
    out["mergers_90d"] = (ok(len(mergers), src, display=f"{len(mergers)} ({', '.join(sorted(mergers))})") if mergers or not merged_90
                          else missing("not measured: API did not report who merged", src))
    out["outside_merged_90d"] = ok(len(merged), src, note)
    out["outside_accept_90d"] = ok(round(100 * len(merged) / len(outside), 1), src, note) if outside else missing("not measured: no outside PRs closed in 90 days", src)
    out["outside_open"] = ok(len(open_), src, note)
    return out


# ---------- Community ----------

def community(lang: Language) -> Metrics:
    out: Metrics = {}
    out.update(guarded(["reddit_per_week"], reddit, lang.subreddit))
    out.update(guarded(["forum_posts_7d", "forum_topics_7d"], forum, lang.forum))
    out.update(guarded(["discussions_30d"], discussions, lang))
    out.update(guarded(["discord_members", "discord_online"], discord, lang.discord_invite))
    out["platforms"] = platforms(lang)
    out["x_activity"] = x_activity(lang)
    return out


ATOM = {"a": "http://www.w3.org/2005/Atom"}


def reddit(sub: str | None) -> Metrics:
    if not sub:
        return {"reddit_per_week": missing("none configured")}
    url = f"https://www.reddit.com/r/{sub}/new/.rss?limit=100"
    web = f"https://www.reddit.com/r/{sub}/new/"
    time.sleep(3)
    root = ET.fromstring(get(url, headers={"User-Agent": UA}))
    stamps = sorted(entry_time(e) for e in root.findall("a:entry", ATOM))
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


def entry_time(e: ET.Element) -> datetime:
    stamp = e.findtext("a:published", namespaces=ATOM) or e.findtext("a:updated", namespaces=ATOM)
    if not stamp:
        raise ValueError("feed entry has no published or updated time")
    return parse_time(stamp)


def forum(cfg: Forum | None) -> Metrics:
    keys = ["forum_posts_7d", "forum_topics_7d"]
    if not cfg:
        return {k: missing("none configured") for k in keys}
    url = cfg.url.rstrip("/")
    if cfg.kind == "discourse":
        stats = get_json(f"{url}/about.json", headers={"Accept": "application/json"})["about"]["stats"]
        return {
            "forum_posts_7d": ok(stats["posts_7_days"], url, display=f"{stats['posts_7_days']} ({stats['posts_30_days']} in 30d)"),
            "forum_topics_7d": ok(stats["topics_7_days"], url, display=f"{stats['topics_7_days']} ({stats['topics_30_days']} in 30d)"),
        }
    if cfg.kind == "nimforum":
        return nimforum(url)
    return {k: missing(f"not measured: unknown forum kind {cfg.kind}") for k in keys}


@dataclass(frozen=True)
class Thread:
    activity: datetime
    creation: datetime


def parse_thread(t: JSON) -> Thread:
    return Thread(datetime.fromtimestamp(t["activity"], timezone.utc), datetime.fromtimestamp(t["creation"], timezone.utc))


def nimforum(url: str) -> Metrics:
    cutoff = NOW - timedelta(days=7)
    threads: list[Thread] = []
    start = 0
    while True:
        page = [parse_thread(t) for t in get_json(f"{url}/threads.json", {"start": start, "count": 30})["threads"]]
        threads += page
        if not page or page[-1].activity < cutoff or start > 600:
            break
        start += 30
    active = [t for t in threads if t.activity >= cutoff]
    created = sum(t.creation >= cutoff for t in active)
    return {
        "forum_posts_7d": missing("not measured: nimforum API has no post counts; see topics", url),
        "forum_topics_7d": ok(created, url, f"{len(active)} threads had activity in 7 days"),
    }


def discussions(lang: Language) -> Metrics:
    repo = lang.github_discussions
    if not repo:
        return {"discussions_30d": missing("none configured")}
    web = f"https://github.com/{repo}/discussions"
    if not TOKEN:
        return {"discussions_30d": missing("not measured: GraphQL needs GITHUB_TOKEN", web)}
    d30 = (NOW - timedelta(days=30)).date().isoformat()
    q = 'query($q:String!){search(query:$q,type:DISCUSSION){discussionCount}}'
    data = gh_graphql(q, {"q": f"repo:{repo} created:>={d30}"})
    return {"discussions_30d": ok(int(data["search"]["discussionCount"]), web)}


def discord(invite: str | None) -> Metrics:
    keys = ["discord_members", "discord_online"]
    if not invite:
        return {k: missing("none configured") for k in keys}
    d = get_json(f"https://discord.com/api/v10/invites/{invite}", {"with_counts": "true"})
    web = f"https://discord.gg/{invite}"
    name = d.get("guild", {}).get("name", "")
    return {
        "discord_members": ok(int(d["approximate_member_count"]), web, f"server: {name}"),
        "discord_online": ok(int(d["approximate_presence_count"]), web, f"server: {name}"),
    }


def platforms(lang: Language) -> Metric:
    found = []
    if lang.subreddit: found.append("Reddit")
    if lang.forum: found.append("Forum")
    if lang.github_discussions: found.append("GitHub Discussions")
    if lang.discord_invite: found.append("Discord")
    found += [p for p in lang.other_platforms if p not in found]
    return ok(len(found), None, "counted from languages.toml", display=f"{len(found)} ({', '.join(found)})")


def x_activity(lang: Language) -> Metric:
    accounts = lang.x_accounts
    note = lang.x_notes or "not measured: no X API credentials in CI"
    links = ", ".join(f"@{a}" for a in accounts) or "none listed"
    return {"value": None, "source": f"https://x.com/{accounts[0]}" if accounts else None, "note": f"{note}; accounts: {links}"}


# ---------- Editors ----------

def editors(lang: Language) -> Metrics:
    out: Metrics = {}
    out.update(guarded(["zed_days", "zed_downloads"], zed, lang.zed))
    out.update(guarded(["vscode_days", "vscode_installs", "vscode_rating"], vscode, lang.vscode))
    out.update(guarded(["openvsx_days", "openvsx_downloads"], openvsx, lang.openvsx))
    out.update(guarded(["lsp_days", "lsp_commits_90d"], lsp, lang.lsp))
    rating = lang.lsp_rating
    notes = lang.lsp_notes
    out["lsp_rating"] = ok(rating, None, notes) if rating else missing(f"not rated; {notes}" if notes else "not rated (manual field in languages.toml)")
    return out


def flag(days: int) -> str:
    return f"{days} (stale, over {STALE_DAYS} days)" if days > STALE_DAYS else str(days)


@dataclass(frozen=True)
class ZedExtension:
    id: str
    version: str
    published_at: str
    repository: str
    downloads: int


def zed(ids: tuple[str, ...]) -> Metrics:
    if not ids:
        return {"zed_days": missing("none configured"), "zed_downloads": missing("none configured")}
    found = [ZedExtension(i, str(e["version"]), str(e["published_at"]), str(e["repository"]), int(e["download_count"]))
             for i in ids for e in get_json("https://api.zed.dev/extensions", {"filter": i})["data"] if e["id"] == i]
    if not found:
        return {k: missing(f"not measured: {ids} not in Zed registry") for k in ("zed_days", "zed_downloads")}
    e = max(found, key=lambda e: e.published_at)
    src = f"https://zed.dev/extensions?query={e.id}"
    others = "; ".join(f"{x.id} v{x.version} {days_since(x.published_at)}d" for x in found)
    d = days_since(e.published_at)
    return {
        "zed_days": ok(d, src, f"{others}; repo {e.repository}", display=f"{flag(d)} ({e.id} v{e.version})"),
        "zed_downloads": ok(sum(x.downloads for x in found), src),
    }


@dataclass(frozen=True)
class VscodeExtension:
    id: str
    updated: str
    installs: int
    rating: float | None
    ratings: int

    @property
    def url(self) -> str:
        return f"https://marketplace.visualstudio.com/items?itemName={self.id}"


def parse_vscode(i: str, e: JSON) -> VscodeExtension:
    st = {s["statisticName"]: s["value"] for s in e.get("statistics", [])}
    rating = st.get("averagerating")
    return VscodeExtension(i, str(e["lastUpdated"]), int(st.get("install", 0)),
                           float(rating) if rating is not None else None, int(st.get("ratingcount", 0)))


def vscode(ids: tuple[str, ...]) -> Metrics:
    keys = ("vscode_days", "vscode_installs", "vscode_rating")
    if not ids:
        return {k: missing("none configured") for k in keys}
    found: list[VscodeExtension] = []
    for i in ids:
        body = {"filters": [{"criteria": [{"filterType": 7, "value": i}]}], "flags": 914}
        res = get_json("https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery", data=body,
                       headers={"Accept": "application/json;api-version=7.1-preview.1"})["results"][0]["extensions"]
        found += [parse_vscode(i, e) for e in res]
    if not found:
        return {k: missing(f"not measured: {ids} not found on marketplace") for k in keys}
    e = max(found, key=lambda e: e.updated)
    top = max(found, key=lambda e: e.installs)
    others = "; ".join(f"{x.id}: {days_since(x.updated)}d, {x.installs} installs" for x in found)
    d = days_since(e.updated)
    return {
        "vscode_days": ok(d, e.url, others, display=f"{flag(d)} ({e.id})"),
        "vscode_installs": ok(top.installs, top.url, display=f"{top.installs:,} ({top.id})"),
        "vscode_rating": ok(round(top.rating, 2), top.url, display=f"{top.rating:.1f} ({top.ratings} ratings)") if top.rating else missing("not measured: no ratings", top.url),
    }


@dataclass(frozen=True)
class OpenVsxExtension:
    id: str
    updated: str
    downloads: int


def openvsx(ids: tuple[str, ...]) -> Metrics:
    keys = ("openvsx_days", "openvsx_downloads")
    if not ids:
        return {k: missing("none configured") for k in keys}
    found: list[OpenVsxExtension] = []
    absent: list[str] = []
    for i in ids:
        ns, name = i.split(".", 1)
        try:
            d = get_json(f"https://open-vsx.org/api/{ns}/{name}")
            found.append(OpenVsxExtension(i, str(d["timestamp"]), int(d.get("downloadCount", 0))))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            absent.append(i)
    if not found:
        return {k: missing(f"not on Open VSX: {', '.join(absent)}", "https://open-vsx.org") for k in keys}
    newest = max(found, key=lambda x: x.updated)
    src = f"https://open-vsx.org/extension/{newest.id.replace('.', '/')}"
    d = days_since(newest.updated)
    return {
        "openvsx_days": ok(d, src, f"absent: {', '.join(absent)}" if absent else None, display=f"{flag(d)} ({newest.id})"),
        "openvsx_downloads": ok(sum(x.downloads for x in found), src),
    }


def lsp(cfg: Lsp | None) -> Metrics:
    if not cfg:
        return {k: missing("none configured") for k in ("lsp_days", "lsp_commits_90d")}
    repo = cfg.repo
    web = f"https://github.com/{repo}"
    since = (NOW - timedelta(days=90)).isoformat()
    path: Params = {"path": cfg.path} if cfg.path else {}
    recent = gh_paged(f"/repos/{repo}/commits", {"since": since, **path})
    last = recent[0] if recent else (gh(f"/repos/{repo}/commits", {"per_page": 1, **path}) or [None])[0]
    note = cfg.name
    if not last:
        return {"lsp_days": missing("not measured: no commits", web), "lsp_commits_90d": ok(0, web, note)}
    when = parse_commit(last).date
    shared = " (shared with compiler repo)" if cfg.shared else ""
    return {
        "lsp_days": ok(days_since(when), f"{web}/commits", note, display=f"{days_since(when)} ({when[:10]})"),
        "lsp_commits_90d": ok(len(recent), f"{web}/commits", note + shared),
    }


# ---------- HTTP ----------

def gh(path: str, params: Params | None = None) -> JSON:
    return get_json(f"https://api.github.com{path}", params, headers=gh_headers())


def gh_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def gh_paged(path: str, params: Params, limit: int = 40) -> list[JSON]:
    items: list[JSON] = []
    for page in range(1, limit + 1):
        batch = gh(path, {**params, "per_page": 100, "page": page})
        items += batch
        if len(batch) < 100:
            break
    return items


def gh_search(q: str) -> list[PullRequest]:
    items: list[JSON] = []
    for page in range(1, 11):
        d = gh("/search/issues", {"q": q, "per_page": 100, "page": page})
        items += d["items"]
        if len(items) >= d["total_count"] or not d["items"]:
            break
    return [PullRequest(str(i["author_association"]), str(i["user"]["login"])) for i in items]


MERGED_Q = """query($q:String!,$after:String){search(query:$q,type:ISSUE,first:100,after:$after){issueCount
pageInfo{hasNextPage endCursor} nodes{... on PullRequest{authorAssociation author{login} mergedBy{login}}}}}"""


def gh_merged_prs(q: str) -> list[PullRequest]:
    items: list[PullRequest] = []
    after: str | None = None
    while True:
        d = gh_graphql(MERGED_Q, {"q": q, "after": after})["search"]
        items += [PullRequest(str(n["authorAssociation"]), str((n["author"] or {}).get("login", "ghost")), (n["mergedBy"] or {}).get("login"))
                  for n in d["nodes"]]
        if not d["pageInfo"]["hasNextPage"]:
            return items
        after = d["pageInfo"]["endCursor"]


def gh_graphql(query: str, variables: Params) -> JSON:
    d = get_json("https://api.github.com/graphql", data={"query": query, "variables": variables}, headers=gh_headers())
    if d.get("errors"):
        raise RuntimeError(d["errors"][0]["message"])
    return d["data"]


def forgejo_paged(url: str, params: Params, stop: Callable[[JSON], bool] | None = None, limit: int = 60) -> list[JSON]:
    items: list[JSON] = []
    for page in range(1, limit + 1):
        batch = get_json(url, {**params, "limit": 50, "page": page})
        items += batch
        if len(batch) < 50 or (stop and batch and stop(batch[-1])):
            break
    return items


def get_json(url: str, params: Params | None = None, data: JSON = None, headers: dict[str, str] | None = None) -> JSON:
    return json.loads(get(url, params, data, headers))


def get(url: str, params: Params | None = None, data: JSON = None, headers: dict[str, str] | None = None, attempts: int = 4) -> bytes:
    return get_response(url, params, data, headers, attempts)[0]


def get_response(url: str, params: Params | None = None, data: JSON = None, headers: dict[str, str] | None = None, attempts: int = 4) -> tuple[bytes, dict[str, str]]:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"User-Agent": UA, **({"Content-Type": "application/json"} if body else {}), **(headers or {})}
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, hdrs), timeout=60) as r:
                content: bytes = r.read()
                return content, dict(r.headers)
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
    raise ValueError(f"attempts must be at least 1, got {attempts}")


def rate_limit_wait(e: urllib.error.HTTPError) -> int | None:
    if e.code >= 500:
        return 10
    retry_after, reset = e.headers.get("retry-after"), e.headers.get("x-ratelimit-reset")
    if e.code == 429 or (e.code == 403 and e.headers.get("x-ratelimit-remaining") == "0"):
        if retry_after:
            return min(max(int(float(retry_after)) + 1, 15), 120)
        if reset:
            r = int(float(reset))
            return min(max((r if r < 1e9 else r - int(time.time())) + 2, 15), 900)
        return 30
    if e.code == 403 and "secondary rate limit" in e.read().decode(errors="ignore").lower():
        return 60
    return None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)




def gitlab_base(api: str | None) -> str:
    return (api or "https://gitlab.com/api/v4").rstrip("/")


def quote_path(repo: str) -> str:
    return urllib.parse.quote(repo, safe="")


def parse_gitlab_commit(c: JSON) -> Commit:
    author = str(c.get("author_email") or c.get("author_name") or "unknown")
    return Commit(author, str(c["created_at"]))


def gitlab_paged(url: str, params: Params, stop: Callable[[JSON], bool] | None = None, limit: int = 40) -> list[JSON]:
    items: list[JSON] = []
    for page in range(1, limit + 1):
        batch = get_json(url, {**params, "per_page": 100, "page": page})
        if not isinstance(batch, list):
            break
        items += batch
        if len(batch) < 100 or (stop and batch and stop(batch[-1])):
            break
    return items


def gitlab_tag_date(t: JSON) -> str | None:
    commit = t.get("commit") or {}
    when = commit.get("committed_date") or commit.get("created_at") or t.get("created_at")
    return str(when) if when else None


def is_prerelease_tag(name: str) -> bool:
    if re.search(r"(?i)(^|[.-])(rc|alpha|beta|preview|nightly|latest|pre|dev|start)\d*\b", name):
        return True
    return not re.search(r"\d+\.\d+", name)


def release_key(name: str) -> str:
    return re.sub(r"(?i)-release$", "", name)


def gitlab_release(base: str, repo: str, web: str) -> Metric:
    encoded = quote_path(repo)
    rels = parse_releases(get_json(f"{base}/projects/{encoded}/releases", {"per_page": 30}) or [])
    if rels:
        when = rels[0].published_at
        return ok(days_since(when), f"{web}/-/releases", display=f"{days_since(when)} ({rels[0].tag}, {when[:10]})")
    tags = [t for t in gitlab_paged(f"{base}/projects/{encoded}/repository/tags", {}) if not is_prerelease_tag(str(t["name"])) and gitlab_tag_date(t)]
    if not tags:
        return missing("not measured: no stable releases or tags", f"{web}/-/tags")
    when = str(gitlab_tag_date(tags[0]))
    return ok(days_since(when), f"{web}/-/tags", note="from latest stable tag; GitLab releases empty or unused",
              display=f"{days_since(when)} ({release_key(str(tags[0]['name']))}, {when[:10]})")


def gitlab_activity(r: Canonical) -> Metrics:
    base, repo, web = gitlab_base(r.api), r.repo, r.web
    encoded = quote_path(repo)
    since_90 = NOW - timedelta(days=90)
    since_12m = NOW - timedelta(days=365)
    commits = gitlab_paged(f"{base}/projects/{encoded}/repository/commits", {"since": since_12m.isoformat()})
    rows = [parse_gitlab_commit(c) for c in commits]
    last = max((x.date for x in rows), default=None)
    out = commit_metrics(rows, last, since_90, f"{web}/-/commits")
    if len(commits) >= 4000:
        for k in ("authors_12m", "top_share_12m"):
            out[k]["note"] = "history capped at newest 4,000 commits"
    out["days_since_release"] = gitlab_release(base, repo, web)
    note = "GitLab has no author association; outside means not a public member of the owning group"
    owner = repo.split("/")[0]
    try:
        members = {str(u["username"]) for u in gitlab_paged(f"{base}/groups/{quote_path(owner)}/members", {})}
    except Exception:
        members = {owner}
    mrs = gitlab_paged(f"{base}/projects/{encoded}/merge_requests", {"state": "merged", "order_by": "updated_at", "sort": "desc"},
                       stop=lambda p: parse_time(p.get("merged_at") or p["updated_at"]) < since_90)
    merged_90 = [p for p in mrs if p.get("merged_at") and parse_time(p["merged_at"]) >= since_90]
    closed = gitlab_paged(f"{base}/projects/{encoded}/merge_requests", {"state": "closed", "order_by": "updated_at", "sort": "desc"},
                          stop=lambda p: parse_time(p["updated_at"]) < since_90)
    closed_90 = [p for p in closed if not p.get("merged_at") and parse_time(p["updated_at"]) >= since_90]
    open_ = get_json(f"{base}/projects/{encoded}/merge_requests", {"state": "opened", "per_page": 100})
    if not isinstance(open_, list):
        open_ = []
    outside_merged = [p for p in merged_90 if str((p.get("author") or {}).get("username")) not in members]
    outside_closed = [p for p in closed_90 if str((p.get("author") or {}).get("username")) not in members]
    outside_open = [p for p in open_ if str((p.get("author") or {}).get("username")) not in members]
    mergers = {str((p.get("merged_by") or {}).get("username")) for p in merged_90 if p.get("merged_by")}
    mergers.discard("None")
    src = f"{web}/-/merge_requests"
    total = len(outside_merged) + len(outside_closed)
    out["mergers_90d"] = (ok(len(mergers), src, display=f"{len(mergers)} ({', '.join(sorted(x for x in mergers if x)[:8])}{', ...' if len(mergers) > 8 else ''})")
                          if mergers or not merged_90 else missing("not measured: API did not report who merged", src))
    out["outside_merged_90d"] = ok(len(outside_merged), src, note, display=f"{len(outside_merged)} of {len(merged_90)} merged")
    out["outside_accept_90d"] = ok(round(100 * len(outside_merged) / total, 1), src, note) if total else missing("not measured: no outside MRs closed in 90 days", src)
    out["outside_open"] = ok(len(outside_open), src, note, display=f"{len(outside_open)} (of {len(open_)} open)")
    return out


# ---------- Trajectory ----------

QUARTERS = 8
COMMIT_PAGE_CAP = 40


def trajectory(lang: Language, canonical: Canonical | None, history: list[dict[str, JSON]]) -> Metrics:
    cached = cached_quarters(lang.name, history)
    out: Metrics = {}
    out.update(guarded(["authors_prior_12m", "authors_change_pct", "authors_retained_pct"], contributor_trend, canonical))
    out.update(guarded(["commits_quarters", "prs_quarters"], volume_quarters, canonical, cached))
    out.update(guarded(["releases_per_year", "release_gap_days"], release_cadence, canonical))
    out.update(guarded(["issues_opened_12m", "issues_closed_12m", "issue_close_ratio"], issue_trend, canonical))
    out.update(guarded(["packages_last_12m", "packages_prior_12m", "packages_change_pct"], package_trend, lang, cached))
    out.update(guarded(["hn_quarters"], hn_trend, lang, cached))
    out["trajectory"] = trajectory_direction(out)
    return out


def cached_quarters(name: str, history: list[dict[str, JSON]]) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {"commits_quarters": {}, "prs_quarters": {}, "hn_quarters": {}, "packages": {}}
    complete = {label for label, _, end in recent_quarters() if end < NOW}
    for row in history:
        lang = ((row.get("languages") or {}).get(name) or {})
        for key in ("commits_quarters", "prs_quarters", "hn_quarters"):
            series = (lang.get(key) or {}).get("series") or {}
            for q, n in series.items():
                if q in complete and q not in found[key] and isinstance(n, (int, float)):
                    found[key][q] = int(n)
        packs = lang.get("packages_last_12m") or {}
        if packs.get("series"):
            for q, n in packs["series"].items():
                if isinstance(n, (int, float)):
                    found["packages"][q] = int(n)
    return found


def recent_quarters() -> list[tuple[str, datetime, datetime]]:
    y, m = NOW.year, ((NOW.month - 1) // 3) * 3 + 1
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    out: list[tuple[str, datetime, datetime]] = []
    for _ in range(QUARTERS):
        end = start + timedelta(days=92)
        end = datetime(end.year, ((end.month - 1) // 3) * 3 + 1, 1, tzinfo=timezone.utc)
        label = f"{start.year}Q{(start.month - 1) // 3 + 1}"
        out.append((label, start, end))
        start = datetime(start.year if start.month > 3 else start.year - 1, start.month - 3 if start.month > 3 else 10, 1, tzinfo=timezone.utc)
    return list(reversed(out))


def series_metric(series: dict[str, int], source: str, note: str | None = None) -> Metric:
    labels = [q for q, _, _ in recent_quarters()]
    values: list[float | None] = [series.get(q) for q in labels]
    last4, prev4 = sum(v or 0 for v in values[-4:]), sum(v or 0 for v in values[:4]) if len(values) >= 8 else None
    change = round(100 * (last4 - prev4) / prev4, 1) if prev4 not in (None, 0) else (100.0 if last4 and prev4 == 0 else None)
    display = " ".join(f"{v}" if v is not None else "-" for v in values)
    if change is not None:
        display += f" ({change:+.0f}% recent 4q vs prior 4q)"
    m: Metric = ok(change, source, note, display=display)
    m["series"] = series
    m["values"] = values
    return m


def contributor_trend(canonical: Canonical | None) -> Metrics:
    if not canonical:
        return {k: missing("not measured: no repo reachable") for k in ("authors_prior_12m", "authors_change_pct", "authors_retained_pct")}
    mid, start = NOW - timedelta(days=365), NOW - timedelta(days=730)
    rows = list_commits(canonical, start, COMMIT_PAGE_CAP)
    src = f"{canonical.web}/commits" if canonical.host == "github" else f"{canonical.web}/-/commits"
    last = {r.author for r in rows if parse_time(r.date) >= mid and not is_bot(r.author)}
    prior = {r.author for r in rows if start <= parse_time(r.date) < mid and not is_bot(r.author)}
    note = "history capped; older window may be incomplete" if len(rows) >= COMMIT_PAGE_CAP * 100 else None
    retained = len(last & prior)
    change = round(100 * (len(last) - len(prior)) / len(prior), 1) if prior else (100.0 if last else None)
    retain_pct = round(100 * retained / len(prior), 1) if prior else None
    return {
        "authors_prior_12m": ok(len(prior), src, note, display=f"{len(prior)} (vs {len(last)} in last 12 months)"),
        "authors_change_pct": ok(change, src, note, display=f"{change:+.0f}%" if change is not None else None) if change is not None else missing("not measured: no authors in prior 12 months", src),
        "authors_retained_pct": ok(retain_pct, src, f"{retained} of {len(prior)} prior-year authors also committed this year",
                                   display=f"{retain_pct:.0f}% ({retained} of {len(prior)})") if retain_pct is not None else missing("not measured: no authors in prior 12 months", src),
    }


def list_commits(r: Canonical, since: datetime, limit: int) -> list[Commit]:
    if r.host == "github":
        return [parse_commit(c) for c in gh_paged(f"/repos/{r.repo}/commits", {"since": since.isoformat()}, limit=limit)]
    if r.host == "gitlab":
        url = f"{gitlab_base(r.api)}/projects/{quote_path(r.repo)}/repository/commits"
        return [parse_gitlab_commit(c) for c in gitlab_paged(url, {"since": since.isoformat()}, limit=limit)]
    url = f"{forgejo_base(r.host, r.api)}/repos/{r.repo}/commits"
    return [parse_commit(c) for c in forgejo_paged(url, {"since": since.isoformat(), "stat": "false", "files": "false", "verification": "false"}, limit=limit)]


def volume_quarters(canonical: Canonical | None, cached: dict[str, dict[str, int]]) -> Metrics:
    if not canonical:
        return {k: missing("not measured: no repo reachable") for k in ("commits_quarters", "prs_quarters")}
    commits: dict[str, int] = dict(cached.get("commits_quarters") or {})
    prs: dict[str, int] = dict(cached.get("prs_quarters") or {})
    missing_q = [(label, start, end) for label, start, end in recent_quarters() if label not in commits]
    if missing_q and canonical.host != "github":
        commits.update(bucket_commits(canonical))
        missing_q = [(label, start, end) for label, start, end in recent_quarters() if label not in commits]
    for label, start, end in missing_q:
        commits[label] = count_commits(canonical, start, end)
    missing_pr = [(label, start, end) for label, start, end in recent_quarters() if label not in prs]
    if missing_pr and canonical.host != "github":
        prs.update(bucket_merged_prs(canonical))
        missing_pr = [(label, start, end) for label, start, end in recent_quarters() if label not in prs]
    for label, start, end in missing_pr:
        prs[label] = count_merged_prs(canonical, start, end)
    commit_src = canonical.web + ("/commits" if canonical.host == "github" else "/-/commits")
    pr_src = canonical.web + ("/pulls" if canonical.host != "gitlab" else "/-/merge_requests")
    return {
        "commits_quarters": series_metric(commits, commit_src),
        "prs_quarters": series_metric(prs, pr_src),
    }


def quarter_label(when: datetime) -> str:
    return f"{when.year}Q{(when.month - 1) // 3 + 1}"


def bucket_commits(r: Canonical) -> dict[str, int]:
    start = recent_quarters()[0][1]
    counts: dict[str, int] = {label: 0 for label, _, _ in recent_quarters()}
    for row in list_commits(r, start, COMMIT_PAGE_CAP):
        when = parse_time(row.date)
        if when >= start:
            counts[quarter_label(when)] = counts.get(quarter_label(when), 0) + 1
    return counts


def bucket_merged_prs(r: Canonical) -> dict[str, int]:
    start = recent_quarters()[0][1]
    counts: dict[str, int] = {label: 0 for label, _, _ in recent_quarters()}
    if r.host == "gitlab":
        url = f"{gitlab_base(r.api)}/projects/{quote_path(r.repo)}/merge_requests"
        rows = gitlab_paged(url, {"state": "merged", "order_by": "updated_at", "sort": "desc"},
                            stop=lambda p: parse_time(p.get("merged_at") or p["updated_at"]) < start, limit=20)
        times = [parse_time(p["merged_at"]) for p in rows if p.get("merged_at")]
    else:
        url = f"{forgejo_base(r.host, r.api)}/repos/{r.repo}/pulls"
        rows = forgejo_paged(url, {"state": "closed", "sort": "recentupdate"},
                             stop=lambda p: parse_time(p["updated_at"]) < start, limit=20)
        times = [parse_time(p["closed_at"]) for p in rows if p.get("merged") and p.get("closed_at")]
    for when in times:
        if when >= start:
            counts[quarter_label(when)] = counts.get(quarter_label(when), 0) + 1
    return counts


def count_commits(r: Canonical, start: datetime, end: datetime) -> int:
    if r.host == "github":
        q = f"repo:{r.repo} committer-date:{start.date().isoformat()}..{(end - timedelta(days=1)).date().isoformat()}"
        return gh_search_count("/search/commits", q)
    if r.host == "gitlab":
        url = f"{gitlab_base(r.api)}/projects/{quote_path(r.repo)}/repository/commits"
        rows = gitlab_paged(url, {"since": start.isoformat(), "until": end.isoformat()})
        return len(rows)
    url = f"{forgejo_base(r.host, r.api)}/repos/{r.repo}/commits"
    rows = forgejo_paged(url, {"since": start.isoformat(), "stat": "false", "files": "false", "verification": "false"},
                         stop=lambda c: parse_time(c["commit"]["committer"]["date"]) < start, limit=30)
    return sum(1 for c in rows if start <= parse_time(c["commit"]["committer"]["date"]) < end)


def count_merged_prs(r: Canonical, start: datetime, end: datetime) -> int:
    if r.host == "github":
        q = f"repo:{r.repo} is:pr is:merged merged:{start.date().isoformat()}..{(end - timedelta(days=1)).date().isoformat()}"
        return gh_search_count("/search/issues", q)
    if r.host == "gitlab":
        url = f"{gitlab_base(r.api)}/projects/{quote_path(r.repo)}/merge_requests"
        rows = gitlab_paged(url, {"state": "merged", "order_by": "updated_at", "sort": "desc"},
                            stop=lambda p: parse_time(p.get("merged_at") or p["updated_at"]) < start, limit=20)
        return sum(1 for p in rows if p.get("merged_at") and start <= parse_time(p["merged_at"]) < end)
    url = f"{forgejo_base(r.host, r.api)}/repos/{r.repo}/pulls"
    rows = forgejo_paged(url, {"state": "closed", "sort": "recentupdate"},
                         stop=lambda p: parse_time(p["updated_at"]) < start, limit=20)
    return sum(1 for p in rows if p.get("merged") and p.get("closed_at") and start <= parse_time(p["closed_at"]) < end)


def release_cadence(canonical: Canonical | None) -> Metrics:
    if not canonical:
        return {k: missing("not measured: no repo reachable") for k in ("releases_per_year", "release_gap_days")}
    dates = release_dates(canonical)
    src = canonical.web + ("/releases" if canonical.host == "github" else "/-/tags")
    if len(dates) < 2:
        empty = missing("not measured: fewer than two dated stable releases", src)
        return {"releases_per_year": empty, "release_gap_days": empty}
    span_days = max((NOW - dates[-1]).days, 1)
    per_year = round(len(dates) / (span_days / 365.25), 2)
    gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
    mean = sum(gaps) / len(gaps)
    return {
        "releases_per_year": ok(per_year, src, display=f"{per_year} ({len(dates)} stable releases in {span_days} days)"),
        "release_gap_days": ok(round(mean, 1), src, display=f"{mean:.0f} days mean interval ({min(gaps)}-{max(gaps)})"),
    }


def release_dates(r: Canonical) -> list[datetime]:
    cutoff = NOW - timedelta(days=365 * 3)
    dates: list[datetime] = []
    if r.host == "github":
        rels = parse_releases(gh_paged(f"/repos/{r.repo}/releases", {}, limit=3))
        dates = [parse_time(x.published_at) for x in rels]
        if not dates:
            tags = gh_paged(f"/repos/{r.repo}/tags", {}, limit=3)
            for t in tags:
                if is_prerelease_tag(str(t["name"])):
                    continue
                dates.append(parse_time(parse_commit(gh(f"/repos/{r.repo}/commits/{t['commit']['sha']}")).date))
                if len(dates) >= 12:
                    break
    elif r.host == "gitlab":
        base, enc = gitlab_base(r.api), quote_path(r.repo)
        rels = parse_releases(get_json(f"{base}/projects/{enc}/releases", {"per_page": 100}) or [])
        dates = [parse_time(x.published_at) for x in rels]
        if not dates:
            for t in gitlab_paged(f"{base}/projects/{enc}/repository/tags", {}, limit=5):
                if is_prerelease_tag(str(t["name"])):
                    continue
                when = gitlab_tag_date(t)
                if when:
                    dates.append(parse_time(when))
    else:
        try:
            rels = parse_releases(get_json(f"{forgejo_base(r.host, r.api)}/repos/{r.repo}/releases", {"limit": 50}))
            dates = [parse_time(x.published_at) for x in rels]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            for t in get_json(f"{forgejo_base(r.host, r.api)}/repos/{r.repo}/tags", {"limit": 50}):
                if is_prerelease_tag(str(t.get("name", ""))) or not t.get("commit", {}).get("created"):
                    continue
                dates.append(parse_time(str(t["commit"]["created"])))
    unique: dict[str, datetime] = {}
    for d in dates:
        if d >= cutoff:
            unique.setdefault(d.date().isoformat(), d)
    return sorted(unique.values(), reverse=True)


def issue_trend(canonical: Canonical | None) -> Metrics:
    if not canonical:
        return {k: missing("not measured: no repo reachable") for k in ("issues_opened_12m", "issues_closed_12m", "issue_close_ratio")}
    start = (NOW - timedelta(days=365)).date().isoformat()
    if canonical.host == "github":
        opened = gh_search_count("/search/issues", f"repo:{canonical.repo} is:issue created:>={start}")
        closed = gh_search_count("/search/issues", f"repo:{canonical.repo} is:issue is:closed closed:>={start}")
        src = f"{canonical.web}/issues"
        note = None
    elif canonical.host == "gitlab":
        base = gitlab_base(canonical.api)
        enc = quote_path(canonical.repo)
        opened_s = get_json(f"{base}/projects/{enc}/issues_statistics", {"created_after": start})["statistics"]["counts"]
        opened = int(opened_s["all"])
        closed = int(opened_s["closed"])
        note = "GitLab counts issues opened in the last 12 months that are now closed, not all closes in the window"
        unfiltered = get_json(f"{base}/projects/{enc}/issues_statistics")["statistics"]["counts"]
        try:
            closed_s = get_json(f"{base}/projects/{enc}/issues_statistics", {"closed_after": start})["statistics"]["counts"]
            candidate = int(closed_s["closed"] if "closed" in closed_s else closed_s["all"])
            if candidate != int(unfiltered["closed"]) or int(closed_s["all"]) != int(unfiltered["all"]):
                closed = candidate
                note = None
        except Exception:
            pass
        src = f"{canonical.web}/-/issues"
    else:
        base = forgejo_base(canonical.host, canonical.api)
        src = f"{canonical.web}/issues"
        opened = len(forgejo_paged(f"{base}/repos/{canonical.repo}/issues", {"state": "all", "type": "issues", "since": start},
                                   stop=lambda i: parse_time(i.get("created_at") or i["updated_at"]) < parse_time(start + "T00:00:00+00:00"), limit=20))
        closed = 0
        note = "Forgejo issue close dates are not counted separately; see opened count only"
        return {
            "issues_opened_12m": ok(opened, src, note),
            "issues_closed_12m": missing(note, src),
            "issue_close_ratio": missing(note, src),
        }
    ratio = round(closed / opened, 2) if opened else None
    return {
        "issues_opened_12m": ok(opened, src, note),
        "issues_closed_12m": ok(closed, src, note),
        "issue_close_ratio": ok(ratio, src, note, display=f"{ratio} ({closed} closed / {opened} opened)") if ratio is not None else missing("not measured: no issues opened in 12 months", src),
    }


def package_trend(lang: Language, cached: dict[str, dict[str, int]]) -> Metrics:
    keys = ("packages_last_12m", "packages_prior_12m", "packages_change_pct")
    if not lang.packages:
        return {k: missing("none configured") for k in keys}
    if lang.packages.kind == "hex":
        last, prior, src = hex_new_packages()
    else:
        return {k: missing(f"not measured: unknown registry {lang.packages.kind}") for k in keys}
    change = round(100 * (last - prior) / prior, 1) if prior else (100.0 if last else None)
    note = "Hex.pm hosts Elixir, Erlang, and Gleam packages; counts are the shared registry"
    return {
        "packages_last_12m": ok(last, src, note),
        "packages_prior_12m": ok(prior, src, note),
        "packages_change_pct": ok(change, src, note, display=f"{change:+.0f}%") if change is not None else missing("not measured: no packages in prior 12 months", src),
    }


def hex_new_packages() -> tuple[int, int, str]:
    last = prior = 0
    mid, start = NOW - timedelta(days=365), NOW - timedelta(days=730)
    for page in range(1, 80):
        batch = get_json("https://hex.pm/api/packages", {"sort": "inserted_at", "page": page})
        if not isinstance(batch, list) or not batch:
            break
        for p in batch:
            when = parse_time(str(p["inserted_at"]))
            if when >= mid:
                last += 1
            elif when >= start:
                prior += 1
            else:
                return last, prior, "https://hex.pm/packages"
        if parse_time(str(batch[-1]["inserted_at"])) < start:
            break
        time.sleep(0.2)
    return last, prior, "https://hex.pm/packages"


def hn_trend(lang: Language, cached: dict[str, dict[str, int]]) -> Metrics:
    query = lang.hn_query or lang.name
    series: dict[str, int] = dict(cached.get("hn_quarters") or {})
    for label, start, end in recent_quarters():
        if label not in series:
            series[label] = hn_count(query, start, end)
    src = "https://hn.algolia.com/?q=" + urllib.parse.quote(query)
    return {"hn_quarters": series_metric(series, src, f"Algolia HN story search: {query}")}


def hn_count(query: str, start: datetime, end: datetime) -> int:
    d = get_json("https://hn.algolia.com/api/v1/search", {
        "query": query,
        "tags": "story",
        "hitsPerPage": 0,
        "numericFilters": f"created_at_i>={int(start.timestamp())},created_at_i<{int(end.timestamp())}",
    })
    return int(d["nbHits"])


def trajectory_direction(m: Metrics) -> Metric:
    votes: list[str] = []
    reasons: list[str] = []
    def vote(key: str, label: str, invert: bool = False) -> None:
        metric = m.get(key)
        v = metric["value"] if metric else None
        if v is None:
            return
        raw = -v if invert else v
        if raw >= 15:
            votes.append("up"); reasons.append(f"{label} {v:+.0f}%" if not invert else f"{label} {v}")
        elif raw <= -15:
            votes.append("down"); reasons.append(f"{label} {v:+.0f}%" if not invert else f"{label} {v}")
        else:
            votes.append("flat"); reasons.append(f"{label} steady")

    vote("authors_change_pct", "contributors")
    vote("commits_quarters", "commits")
    vote("prs_quarters", "merged PRs")
    vote("packages_change_pct", "new packages")
    vote("hn_quarters", "HN mentions")
    close = m.get("issue_close_ratio")
    ratio = close["value"] if close else None
    if ratio is not None:
        if ratio >= 1.0:
            votes.append("up"); reasons.append(f"issues closing at {ratio}x")
        elif ratio < 0.7:
            votes.append("down"); reasons.append(f"issues closing at {ratio}x")
        else:
            votes.append("flat"); reasons.append("issue close rate keeping up")
    if not votes:
        return missing("not measured: no trend signals")
    up, down = votes.count("up"), votes.count("down")
    if up and down:
        direction, n = "mixed", 0
    elif up and not down:
        direction, n = "up", up
    elif down and not up:
        direction, n = "down", down
    else:
        direction, n = "flat", votes.count("flat")
    return ok({"up": 1, "flat": 0, "down": -1, "mixed": 0}.get(direction, 0), None, "; ".join(reasons), display=direction)


# ---------- History ----------

def load_history() -> list[dict[str, JSON]]:
    rows: list[dict[str, JSON]] = []
    if HISTORY.exists():
        for line in HISTORY.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    rows.extend(history_from_git([r.get("date") for r in rows if r.get("date")]))
    rows.sort(key=lambda r: str(r.get("date") or r.get("generated_at") or ""))
    return rows


def history_from_git(have: list[object]) -> list[dict[str, JSON]]:
    git = ROOT / ".git"
    if not git.exists():
        return []
    try:
        log = subprocess.run(["git", "log", "--format=%H %cI", "--", "docs/data.json"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    out: list[dict[str, JSON]] = []
    seen = {str(d)[:10] for d in have}
    for line in log.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        sha, when = parts
        day = when[:10]
        if day in seen:
            continue
        try:
            raw = subprocess.run(["git", "show", f"{sha}:docs/data.json"], cwd=ROOT, capture_output=True, text=True, timeout=30)
            if raw.returncode != 0 or not raw.stdout.strip():
                continue
            data = json.loads(raw.stdout)
        except Exception:
            continue
        langs = {item["name"]: compact_history_lang(item) for item in data.get("languages", [])}
        out.append({"date": day, "generated_at": data.get("generated_at", when), "source": f"git:{sha[:12]}", "languages": langs})
        seen.add(day)
    return out


def compact_history_lang(item: dict[str, JSON]) -> dict[str, JSON]:
    metrics = item.get("metrics") or {}
    keep = {}
    for key, val in metrics.items():
        if not isinstance(val, dict):
            continue
        entry = {k: val[k] for k in ("value", "display", "note", "source", "series", "values") if k in val}
        if entry:
            keep[key] = entry
    return keep


def append_history(results: list[Result]) -> None:
    day = NOW.date().isoformat()
    row: dict[str, JSON] = {"date": day, "generated_at": NOW.isoformat(), "languages": {r["name"]: compact_history_lang(dict(r)) for r in results}}
    existing = [r for r in (load_history_file_only()) if r.get("date") != day]
    existing.append(row)
    existing.sort(key=lambda r: str(r.get("date") or ""))
    HISTORY.write_text("".join(json.dumps(r, default=str) + "\n" for r in existing))


def load_history_file_only() -> list[dict[str, JSON]]:
    if not HISTORY.exists():
        return []
    return [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]


def gh_search_count(path: str, q: str) -> int:
    d = gh(path, {"q": q, "per_page": 1})
    return int(d["total_count"])



# ---------- Page ----------

def render(results: list[Result], history: list[dict[str, JSON]] | None = None) -> str:
    stamp = NOW.strftime("%Y-%m-%d %H:%M UTC")
    return TEMPLATE.format(
        stamp=stamp,
        summary=summary_table(results),
        trajectory=trajectory_section(results),
        details="\n".join(detail(r) for r in results),
        thresholds=thresholds_table(),
        gh=GITHUB_LINK,
    )


def stars(s: float | None) -> str:
    if s is None:
        return '<span class="nm">not measured</span>'
    return f'<span class="score s{round(s)}">{s:.1f}</span>'


def summary_table(results: list[Result]) -> str:
    head = "".join(f'<th data-type="num" aria-sort="none">{c}</th>' for c in CATEGORIES)
    rows = []
    for r in results:
        cells = "".join(f'<td data-v="{r["categories"][c] or -1}">{stars(r["categories"][c])}</td>' for c in CATEGORIES)
        mark, cls, dv = direction_cell(r)
        rows.append(f'<tr><td data-v="{esc(r["name"])}"><a href="#{slug(r["name"])}">{esc(r["name"])}</a></td>'
                    f'<td data-v="{r["overall"] or -1}">{stars(r["overall"])}</td>'
                    f'<td data-v="{dv}"><span class="{cls}">{esc(mark)}</span></td>{cells}</tr>')
    return (f'<table class="sortable"><thead><tr><th data-type="text" aria-sort="none">Language</th>'
            f'<th data-type="num" aria-sort="descending">Overall</th>'
            f'<th data-type="num" aria-sort="none">Trajectory</th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def detail(r: Result) -> str:
    m = r["metrics"]
    sections = []
    for cat, keys in CATEGORIES.items():
        rows = "".join(metric_row(k, m[k]) for k in keys if k in m)
        sections.append(f'<h3>{cat} <span class="dim">score</span> {stars(r["categories"][cat])}</h3>'
                        f'<table class="metrics"><thead><tr><th>Metric</th><th>Value</th><th>Score</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table>')
    canon = r["config"].get("_canonical")
    repo = f'<a href="{canon["web"]}">{canon["host"]}:{canon["repo"]}</a>' if canon else '<span class="nm">none</span>'
    return (f'<section class="lang" id="{slug(r["name"])}"><h2>{esc(r["name"])} <span class="dim">overall</span> {stars(r["overall"])}</h2>'
            f'<p><span class="dim">Main repo</span> {repo}</p>{governance_block(r)}{"".join(sections)}</section>')



def direction_cell(r: Result) -> tuple[str, str, float]:
    m = r["metrics"].get("trajectory")
    if not m or m.get("display") in (None, ""):
        return "not measured", "nm", -9
    label = str(m["display"])
    pretty = {"up": "up", "down": "down", "flat": "steady", "mixed": "mixed"}.get(label, label)
    cls = {"up": "dir-up", "down": "dir-down", "flat": "dir-flat", "mixed": "dir-mixed"}.get(label, "nm")
    return pretty, cls, float(m["value"] or 0)


def sparkline(values: list[float | None], width: int = 84, height: int = 24) -> str:
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1
    n = max(len(values) - 1, 1)
    coords = []
    for i, v in pts:
        x = i * (width - 2) / n + 1
        y = height - 2 - (v - lo) / span * (height - 4)
        coords.append(f"{x:.1f},{y:.1f}")
    return (f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true">'
            f'<polyline points="{" ".join(coords)}"/></svg>')


def trajectory_section(results: list[Result]) -> str:
    rows = []
    for r in results:
        m = r["metrics"]
        mark, cls, dv = direction_cell(r)
        rows.append(
            f'<tr><td data-v="{esc(r["name"])}"><a href="#{slug(r["name"])}">{esc(r["name"])}</a></td>'
            f'<td data-v="{dv}"><span class="{cls}">{esc(mark)}</span></td>'
            f'{traj_cell(m, "authors_change_pct")}'
            f'{traj_cell(m, "commits_quarters", spark=True)}'
            f'{traj_cell(m, "prs_quarters", spark=True)}'
            f'{traj_cell(m, "releases_per_year")}'
            f'{traj_cell(m, "issue_close_ratio")}'
            f'{traj_cell(m, "packages_change_pct")}'
            f'{traj_cell(m, "hn_quarters", spark=True)}</tr>'
        )
    table = (
        '<table class="sortable"><thead><tr>'
        '<th data-type="text" aria-sort="none">Language</th>'
        '<th data-type="num" aria-sort="none">Direction</th>'
        '<th data-type="num" aria-sort="none">Contributors, 12m vs prior 12m</th>'
        '<th data-type="num" aria-sort="none">Commits by quarter</th>'
        '<th data-type="num" aria-sort="none">Merged PRs by quarter</th>'
        '<th data-type="num" aria-sort="none">Stable releases / year</th>'
        '<th data-type="num" aria-sort="none">Issue close ratio, 12m</th>'
        '<th data-type="num" aria-sort="none">New packages, 12m vs prior</th>'
        '<th data-type="num" aria-sort="none">HN stories by quarter</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )
    method = (
        '<p class="method dim">Trajectory measures change over time and is not part of the overall score. '
        "Direction summarizes contributor counts, commit and merged-PR volume (recent four quarters vs the four before), "
        "new packages where a registry API exists, Hacker News story mentions, and the trailing 12-month issue close ratio "
        "(issues closed vs opened). A signal counts as up or down when it moves by at least 15%, or when the close ratio "
        "is at least 1.0 or below 0.7; otherwise it is treated as steady. Mixed means the signals disagree. "
        "The current quarter is partial. Completed-quarter counts are reused from docs/history.jsonl on later runs. "
        "Reddit and forum history is not available from those APIs for a two-year window; later weekly snapshots will fill that gap. "
        "Package growth is included only when a public API provides first-seen dates. "
        "Hacker News counts come from the Algolia HN Search API (story posts) using the query in languages.toml. "
        "Governance facts below each language are manual and sourced; they are not scored.</p>"
        "<p class=\"method dim\">Aspects considered: whether the contributor base is growing or shrinking, whether commits and "
        "merged work are accelerating, whether releases arrive on a regular cadence, whether issues are being closed as fast as they open, "
        "whether the package ecosystem is adding new packages, and whether public discussion on Hacker News is rising or falling.</p>"
    )
    return f'<section class="traj"><h2>Trajectory</h2>{method}{table}</section>'


def traj_cell(metrics: Metrics, key: str, spark: bool = False) -> str:
    m = metrics.get(key)
    if not m or m["value"] is None:
        note = (m["note"] if m else None) or ""
        label = "n/a" if note.startswith("none") else "not measured"
        return f'<td data-v="-999"><span class="nm">{label}</span></td>'
    shown = m.get("display") or (f"{m['value']:+.0f}%" if key.endswith("_pct") else str(m["value"]))
    svg = sparkline(list(m.get("values") or [])) if spark else ""
    src = f' <a class="src" href="{esc(m["source"])}">source</a>' if m.get("source") else ""
    return f'<td data-v="{m["value"]}">{svg} {esc(shown)}{src}</td>'


def governance_block(r: Result) -> str:
    g = (r["config"].get("governance") or None)
    if not g:
        return ""
    items: list[str] = []
    body = esc(g["body"]) if g.get("body") else "not listed"
    if g.get("body_url"):
        body = f'<a href="{esc(g["body_url"])}">{body if g.get("body") else esc(g["body_url"])}</a>'
    items.append(f"<li><span class=\"dim\">Steering body</span> {body}</li>")
    if g.get("roadmap"):
        updated = f' (page dated {esc(g["roadmap_updated"])})' if g.get("roadmap_updated") else ""
        items.append(f'<li><span class="dim">Roadmap</span> <a href="{esc(g["roadmap"])}">{esc(g["roadmap"])}</a>{updated}</li>')
    if g.get("rfc_url"):
        items.append(f'<li><span class="dim">RFC / proposal process</span> <a href="{esc(g["rfc_url"])}">{esc(g["rfc_url"])}</a></li>')
    if g.get("funding") or g.get("funding_url"):
        fund = esc(g["funding"]) if g.get("funding") else "listed"
        if g.get("funding_url"):
            fund = f'<a href="{esc(g["funding_url"])}">{fund}</a>'
        items.append(f'<li><span class="dim">Funding</span> {fund}</li>')
    if g.get("notes"):
        items.append(f'<li><span class="dim">Notes</span> {esc(g["notes"])}</li>')
    sources = g.get("sources") or []
    if sources:
        links = ", ".join(f'<a href="{esc(u)}">{esc(u)}</a>' for u in sources)
        items.append(f'<li><span class="dim">Sources</span> {links}</li>')
    return f'<h3>Governance</h3><ul>{"".join(items)}</ul>'


def metric_row(key: str, m: Metric) -> str:
    label, unit = RULES[key][0], RULES[key][1]
    v = m["value"]
    display = m.get("display")
    if v is None:
        value = f'<span class="nm">{"n/a" if (m["note"] or "").startswith("none") else "not measured"}</span>'
    else:
        shown = display or (f"{v:,}" if isinstance(v, int) else str(v))
        head, _, rest = shown.partition(" ")
        head = head.removesuffix(unit) if unit == "%" else head
        value = f'{esc(head)} <span class="dim">{esc(unit)}</span> {esc(rest)}'
    if "stale" in (display or ""):
        value = f'<span class="warn">{value}</span>'
    src = f' <a class="src" href="{esc(m["source"])}">source</a>' if m["source"] else ""
    s = score(key, v)
    sc = f'<span class="score s{s}">{s}</span>' if s else '<span class="dim">unscored</span>'
    return f'<tr><td class="dim">{esc(label)}</td><td>{value}</td><td>{sc}</td><td class="note">{esc(m["note"] or "")}{src}</td></tr>'


def thresholds_table() -> str:
    rows = []
    for label, unit, better, cuts in RULES.values():
        if cuts is None:
            rows.append(f'<tr><td class="dim">{esc(label)}</td><td colspan="5" class="dim">shown for context, not scored</td></tr>')
            continue
        op = "&ge;" if better else "&le;"
        cells = "".join(f"<td>{op} {c:g}</td>" for c in cuts)
        rows.append(f'<tr><td class="dim">{esc(label)} <span class="dim">({esc(unit)})</span></td>{cells}<td class="dim">otherwise</td></tr>')
    return ('<table class="metrics"><thead><tr><th>Metric</th><th>5</th><th>4</th><th>3</th><th>2</th><th>1</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def esc(s: object) -> str:
    return html.escape(str(s))


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower())


REPO_URL = "https://github.com/public-law/lang-eval"
GITHUB_LINK = (f'<a class="gh" href="{REPO_URL}" aria-label="View source on GitHub"><svg viewBox="0 0 16 16" aria-hidden="true">'
               '<path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94'
               '-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07'
               '-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27'
               ' 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93'
               '-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>View on GitHub</a>')
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
table.sortable th:after {{ content:"\\2195"; margin-left:.35em; opacity:.35; }}
table.sortable th[aria-sort="ascending"]:after {{ content:"\\25B2"; opacity:.9; color:var(--accent); }}
table.sortable th[aria-sort="descending"]:after {{ content:"\\25BC"; opacity:.9; color:var(--accent); }}
.gh {{ display:inline-flex; align-items:center; gap:.4em; font-size:13px; text-decoration:none; color:var(--fg); opacity:.7; border:1px solid var(--line); border-radius:6px; padding:.3em .65em; }}
.gh:hover {{ opacity:1; border-color:var(--accent); }}
.gh svg {{ width:16px; height:16px; fill:currentColor; }}
header {{ display:flex; justify-content:space-between; align-items:center; gap:1rem; }}
footer {{ border-top:1px solid var(--line); margin-top:2.5rem; padding-top:1rem; display:flex; justify-content:space-between; align-items:center; }}
.score {{ display:inline-block; min-width:2.2em; text-align:center; border-radius:4px; padding:0 .3em; font-weight:600; color:#1d2024; }}
.s5 {{ background:#7fc98f; }} .s4 {{ background:#b5d37a; }} .s3 {{ background:#e3cf6e; }} .s2 {{ background:#e6a065; }} .s1 {{ background:#de7b72; }}
.dir-up {{ color:#7fc98f; }} .dir-down {{ color:#de7b72; }} .dir-flat {{ color:#e3cf6e; }} .dir-mixed {{ color:#8fb8de; }}
svg.spark {{ vertical-align:middle; overflow:visible; }}
svg.spark polyline {{ fill:none; stroke:var(--accent); stroke-width:1.5; }}
.traj p.method {{ max-width:70rem; }}
</style></head><body>
<header><h1>Programming language evaluation</h1>{gh}</header>
<p><span class="dim">Generated at</span> {stamp} <span class="dim">from live API calls. Click a column to sort. Category scores average the measured metrics; overall averages the categories. The Trajectory column is a separate change-over-time indicator and is not folded into overall. Metrics that could not be fetched say so and are left out of the averages.</span></p>
{summary}
{trajectory}
{details}
<h2>Scoring thresholds</h2>
<p class="dim">Each metric maps to a 1 to 5 score using these cut points.</p>
{thresholds}
<footer><span class="dim">Generated at {stamp}</span>{gh}</footer>
<script>
document.querySelectorAll("table.sortable th").forEach((th, i) => th.addEventListener("click", () => {{
  const body = th.closest("table").tBodies[0];
  const num = th.dataset.type === "num";
  const dir = th.getAttribute("aria-sort") === "descending" ? "ascending" : "descending";
  th.closest("tr").querySelectorAll("th").forEach(h => h.setAttribute("aria-sort", "none"));
  th.setAttribute("aria-sort", dir);
  const val = r => r.cells[i].dataset.v;
  [...body.rows].sort((a, b) => {{
    const c = num ? val(a) - val(b) : val(a).localeCompare(val(b));
    return dir === "ascending" ? c : -c;
  }}).forEach(r => body.appendChild(r));
}}));
</script>
</body></html>
"""


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ConfigError as e:
        sys.exit(str(e))
