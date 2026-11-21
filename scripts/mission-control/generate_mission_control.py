#!/usr/bin/env python3
"""Generate Mission Control SVGs for the crisesarmiento GitHub profile README.

Pulls real GitHub data via the `gh` CLI (GraphQL + public events) and writes
five panels in two themes: exactly ten SVGs, nothing else. No third-party
widget endpoints. No secrets are printed.

Honesty rules:
- Label the time window on every figure.
- The 365-day window is exactly 365 dates, today included (364 days back).
- Daily figures are GitHub's combined contribution totals, one number per
  day, exactly as the profile calendar shows them. Private work is never
  broken out per day, per repo, or per title.
- Private contributions appear only as aggregate totals
  (restrictedContributionsCount), and only on positive evidence: a
  nonzero restricted count anywhere in the fetched history or YTD proves
  the token receives private aggregates. When every restricted count is
  zero we cannot tell "no private work" from "cannot see private work",
  so panels are labeled public-only and private activity is never
  reported as zero.
- Public events from excluded repos (anything under Palehour/ and any
  */londinium repo, case-insensitive) are dropped entirely. Do not name
  those repos in output. Unsafe titles and branch/tag labels —
  including any e-mail address — are dropped; the event itself is kept.
- Flight-log rows collapse only when event, repo, number and day are all
  identical. A PR being opened is never merged into, or hidden by, the
  same PR being merged.
- Never mention useroute.dev; display crisesarmiento/useroute as "Route".
- Language bytes come from public, owned, non-fork repositories: every
  repo and every language, paginating past the first page of each.
- The June quiet stretch is reported only from a measured zero run
  inside a June fully covered by the window (data["june_quiet"]);
  no dates or counts are invented for it.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = Path(
    os.environ.get("MISSION_CONTROL_OUT")
    or REPO_ROOT / "assets" / "mission-control"
)

USER = "crisesarmiento"
SANS = "ui-sans-serif,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

# Repos that must never appear in the flight log or last-signal dates.
EXCLUDE_REPO_PREFIXES = ("Palehour/",)
EXCLUDE_REPO_NAMES = {"Palehour/londinium"}
EXCLUDE_REPO_SUFFIXES = ("/londinium",)
_EXCLUDE_NAMES = {n.lower() for n in EXCLUDE_REPO_NAMES}
_EXCLUDE_PREFIXES = tuple(p.lower() for p in EXCLUDE_REPO_PREFIXES)
_EXCLUDE_SUFFIXES = tuple(s.lower() for s in EXCLUDE_REPO_SUFFIXES)

# Product display names (repo path → label shown on panels).
DISPLAY_NAME = {
    "crisesarmiento/useroute": "Route",
    "crisesarmiento/crisesarmiento": "profile",
    "crisesarmiento/starship-flight-15-sim": "starship-flight-15-sim",
    "crisesarmiento/gastos-abiertos-cba": "gastos-abiertos-cba",
    "crisesarmiento/cstack": "cstack",
    "crisesarmiento/opencode-context-usage": "opencode-context-usage",
    "crisesarmiento/invoice-scope": "invoice-scope",
    "crisesarmiento/testsense": "testsense",
}

FORBIDDEN_IN_TEXT = (
    "useroute.dev",
    "londinium",
    "cristian bergagna",
    "bergagna",
    "horrorsynth",
    "mailto:",
    "@testsense.ai",
)

# Any e-mail address is unsafe in panel text, not just the listed
# domains: names and addresses must never leak into output.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")

THEMES = {
    "dark": {
        "bg": "#0B0F14",
        "text": "#E6EDF3",
        "accent": "#22D3EE",
        "border": "#2A3644",
        "muted": "#8B9AAB",
        "panel": "#11171E",
        "grid": "#1B2430",
        "heat0": "#151C24",
        "heat1": "#0C3D47",
        "heat2": "#0E7490",
        "heat3": "#22D3EE",
        "heat4": "#A5F3FC",
        "private": "#4B5A6A",
        "track_faint": "#2A3644",
    },
    "light": {
        "bg": "#FFFFFF",
        "text": "#0B0F14",
        "accent": "#0891B2",
        "border": "#CBD5E1",
        "muted": "#64748B",
        "panel": "#F8FAFC",
        "grid": "#E2E8F0",
        "heat0": "#F1F5F9",
        "heat1": "#A5F3FC",
        "heat2": "#22D3EE",
        "heat3": "#0891B2",
        "heat4": "#0E7490",
        "private": "#94A3B8",
        "track_faint": "#CBD5E1",
    },
}


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def run_gh(*args: str) -> str:
    """Run one gh subcommand, failing with a short, secret-free message."""
    try:
        result = subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("gh CLI not found; it is required to query the GitHub API") from None
    except subprocess.CalledProcessError as exc:
        # Keep the reason short and never dump raw stderr: it can quote
        # request details. gh's own summary is always the last line.
        lines = (exc.stderr or "").strip().splitlines()
        reason = lines[-1][:200] if lines else f"gh exited with status {exc.returncode}"
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {reason}".strip()) from None
    return result.stdout


def gh_graphql(query: str) -> dict:
    raw = run_gh("api", "graphql", "-f", f"query={query}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("GraphQL response was not valid JSON") from None
    if data.get("errors"):
        first = (data["errors"][0] or {}).get("message") or "unknown GraphQL error"
        raise RuntimeError(f"GraphQL error: {str(first)[:200]}")
    if not data.get("data"):
        raise RuntimeError("GraphQL response had no data")
    return data["data"]


def excluded_repo(name: str) -> bool:
    low = name.lower()
    if low in _EXCLUDE_NAMES:
        return True
    if any(low.startswith(p) for p in _EXCLUDE_PREFIXES):
        return True
    if any(low.endswith(s) for s in _EXCLUDE_SUFFIXES):
        return True
    return False


def forbidden_text(s: str) -> bool:
    if _EMAIL_RE.search(s):
        return True
    low = s.lower()
    return any(tok in low for tok in FORBIDDEN_IN_TEXT)


def display_repo(name: str) -> str:
    if name in DISPLAY_NAME:
        return DISPLAY_NAME[name]
    if "/" in name:
        return name.split("/", 1)[1]
    return name


def fmt_int(n: int) -> str:
    return f"{n:,}"


def fmt_utc(ts: datetime) -> str:
    return ts.strftime("%-d %b %Y  ·  %H:%M UTC").upper()


def fmt_day(date_str: str) -> str:
    if not date_str:
        return "—"
    d = datetime.strptime(date_str[:10], "%Y-%m-%d")
    return d.strftime("%-d %b %Y").upper()


def fmt_short(date_str: str) -> str:
    d = datetime.strptime(date_str[:10], "%Y-%m-%d")
    return d.strftime("%-d %b %Y").upper()


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

REPO_NODE_FIELDS = """
                name
                description
                stargazerCount
                pushedAt
                isArchived
                url
                homepageUrl
                primaryLanguage { name }
                languages(first: 100, orderBy: {field: SIZE, direction: DESC}) {
                  edges { size node { name } }
                  pageInfo { hasNextPage endCursor }
                }
"""

MAIN_QUERY = """
    query {
      user(login: "%s") {
        createdAt
        followers { totalCount }
        repositories(privacy: PUBLIC, ownerAffiliations: OWNER, isFork: false, first: 100) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {%s}
        }
        thisYear: contributionsCollection(from: "%s", to: "%s") {
          contributionCalendar { totalContributions }
          totalCommitContributions
          totalIssueContributions
          totalPullRequestContributions
          totalPullRequestReviewContributions
          restrictedContributionsCount
        }
        last365: contributionsCollection(from: "%s", to: "%s") {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays { date contributionCount weekday }
            }
          }
        }
      }
    }
"""

REPOSITORIES_PAGE_QUERY = """
    query {
      user(login: "%s") {
        repositories(privacy: PUBLIC, ownerAffiliations: OWNER, isFork: false, first: 100, after: "%s") {
          pageInfo { hasNextPage endCursor }
          nodes {%s}
        }
      }
    }
"""

LANGUAGES_PAGE_QUERY = """
    query {
      repository(owner: "%s", name: "%s") {
        languages(first: 100, after: "%s", orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
"""


def sees_private_totals(years: list[dict], private_ytd: int) -> bool:
    """True only on positive evidence: a nonzero restricted (private)
    aggregate anywhere in the fetched yearly history or YTD.

    Viewer identity is not evidence: an owner token without private
    visibility reports all-zero restricted counts just like a token that
    cannot see private work at all. Whoever demonstrably receives
    private aggregates may render them; when every restricted count is
    zero, private access cannot be established and the panels stay
    public-only.
    """
    return private_ytd > 0 or any((y.get("private") or 0) > 0 for y in years)


def complete_repo_languages(node: dict, fetch_page: Callable[[str], dict] | None = None) -> None:
    """Extend a repository node's language edges past the first page.

    Language bytes must cover every language in the repo, not just the
    first 100 the main query returns.
    """
    fetch = fetch_page or gh_graphql
    conn = node.get("languages") or {}
    if not (conn.get("pageInfo") or {}).get("hasNextPage"):
        return
    guard = 0
    while (conn.get("pageInfo") or {}).get("hasNextPage"):
        guard += 1
        if guard > 20:
            raise RuntimeError(f"language pagination for repo {node.get('name')} did not converge")
        cursor = (conn.get("pageInfo") or {}).get("endCursor")
        if not cursor:
            raise RuntimeError(f"language pagination for repo {node.get('name')} lost its cursor")
        block = fetch(LANGUAGES_PAGE_QUERY % (USER, node["name"], cursor)) or {}
        conn = ((block.get("repository") or {}).get("languages")) or {}
        node["languages"]["edges"].extend(conn.get("edges") or [])
        node["languages"]["pageInfo"] = conn.get("pageInfo") or {"hasNextPage": False, "endCursor": None}


def collect_repositories(first_block: dict, fetch_page: Callable[[str], dict] | None = None) -> list[dict]:
    """Every page of public owned non-fork repositories, languages complete."""
    fetch = fetch_page or gh_graphql
    nodes: list[dict] = []
    block = first_block or {}
    pages = 0
    while True:
        pages += 1
        if pages > 20:
            raise RuntimeError("repository pagination did not converge")
        for n in block.get("nodes") or []:
            complete_repo_languages(n, fetch_page=fetch)
            nodes.append(n)
        info = block.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        if not cursor:
            raise RuntimeError("repository pagination lost its cursor")
        nxt = (fetch(REPOSITORIES_PAGE_QUERY % (USER, cursor, REPO_NODE_FIELDS)) or {}).get("user") or {}
        block = nxt.get("repositories") or {}
        if not block:
            raise RuntimeError("repository pagination returned an empty page")
    return nodes


def fetch_year_history(
    created_at: str,
    end_now: str,
    current_year: int,
    fetch_page: Callable[[str], dict] | None = None,
) -> list[dict]:
    """Per-year contribution totals in one aliased query.

    Only aggregate numbers are requested: the combined total and the
    restricted (private) count. No daily breakdown, no repo names.
    """
    fetch = fetch_page or gh_graphql
    created_year = int((created_at or "")[:4] or current_year)
    if created_year > current_year:
        created_year = current_year
    parts = []
    for y in range(created_year, current_year + 1):
        y_from = f"{y}-01-01T00:00:00Z"
        y_to = f"{y}-12-31T23:59:59Z" if y < current_year else end_now
        parts.append(
            f'y{y}: contributionsCollection(from: "{y_from}", to: "{y_to}") '
            "{ contributionCalendar { totalContributions } restrictedContributionsCount }"
        )
    query = 'query { user(login: "%s") { %s } }' % (USER, " ".join(parts))
    data = (fetch(query) or {}).get("user") or {}
    years = []
    for y in range(created_year, current_year + 1):
        block = data.get(f"y{y}") or {}
        total = ((block.get("contributionCalendar") or {}).get("totalContributions")) or 0
        private = block.get("restrictedContributionsCount") or 0
        years.append({"year": y, "total": total, "private": private,
                      "public": max(total - private, 0), "ytd": y == current_year})
    return years


def fetch_public_events(max_pages: int = 3) -> list[dict]:
    events: list[dict] = []
    for page in range(1, max_pages + 1):
        raw = run_gh("api", f"users/{USER}/events/public?per_page=100&page={page}")
        try:
            batch = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("public events response was not valid JSON") from None
        if not isinstance(batch, list):
            raise RuntimeError("public events response had an unexpected shape")
        if not batch:
            break
        events.extend(batch)
        if len(batch) < 100:
            break
    return events


def fetch_data() -> dict:
    now = datetime.now(timezone.utc)
    year = now.year
    # Exactly 365 dates, today included: 364 days back through today.
    start_365 = (now - timedelta(days=364)).strftime("%Y-%m-%dT00:00:00Z")
    end_now = now.strftime("%Y-%m-%dT23:59:59Z")
    year_from = f"{year}-01-01T00:00:00Z"

    user = gh_graphql(
        MAIN_QUERY % (USER, REPO_NODE_FIELDS, year_from, end_now, start_365, end_now)
    )["user"]

    repos = user.get("repositories") or {}
    nodes = collect_repositories(repos)
    user["repositories"] = {"totalCount": repos.get("totalCount") or len(nodes), "nodes": nodes}

    years = fetch_year_history(user.get("createdAt") or "", end_now, year)

    cal = ((user.get("last365") or {}).get("contributionCalendar")) or {}
    days365 = normalize_days(flatten_days(cal), window_dates(now))

    events = fetch_public_events()
    private_ytd = (user.get("thisYear") or {}).get("restrictedContributionsCount") or 0
    # Evidence, not identity: private aggregates render only when the
    # fetched data demonstrably contains a nonzero restricted count.
    sees_private = sees_private_totals(years, private_ytd)

    return {
        "generated_at": now,
        "user": user,
        "years": years,
        "days365": days365,
        "june_quiet": june_quiet_evidence(days365),
        "events": events,
        "sees_private": sees_private,
        "notes": visibility_notes(sees_private, private_ytd),
    }


def flatten_days(calendar: dict) -> list[dict]:
    days = []
    for week in (calendar.get("weeks") or []):
        for day in week["contributionDays"]:
            days.append(day)
    return days


def window_dates(now: datetime) -> list[str]:
    """Exactly 365 dates ending today (UTC), inclusive of both ends."""
    today = now.date()
    start = today - timedelta(days=364)
    return [(start + timedelta(days=i)).isoformat() for i in range(365)]


def normalize_days(days: list[dict], window: list[str]) -> list[dict]:
    """Reindex API calendar days onto the exact window.

    A from/to window of 365 dates comes back as exactly 365 days
    (verified against the live API), but this guard keeps the invariant
    even if the calendar ever starts padding to week boundaries. A
    missing date is an error: inventing a zero would be dishonest.
    """
    by_date = {d["date"]: d for d in days}
    out: list[dict] = []
    for date in window:
        d = by_date.get(date)
        if d is None:
            raise RuntimeError(f"contribution calendar is missing {date} inside the requested window")
        out.append({"date": date,
                    "contributionCount": int(d["contributionCount"]),
                    "weekday": int(d["weekday"])})
    return out


def current_streak(days: list[dict]) -> tuple[int, str, str]:
    """Return (length, start_date, end_date). Today may still be in progress."""
    if not days:
        return 0, "", ""
    end_idx = len(days) - 1
    if days[-1]["contributionCount"] == 0:
        if len(days) >= 2 and days[-2]["contributionCount"] > 0:
            end_idx = len(days) - 2
        else:
            return 0, "", days[-1]["date"]
    start_idx = end_idx
    i = end_idx
    while i >= 0 and days[i]["contributionCount"] > 0:
        start_idx = i
        i -= 1
    length = end_idx - start_idx + 1
    return length, days[start_idx]["date"], days[end_idx]["date"]


def longest_streak(days: list[dict]) -> tuple[int, str, str]:
    best = 0
    best_start = best_end = ""
    run = 0
    run_start = ""
    for day in days:
        if day["contributionCount"] > 0:
            if run == 0:
                run_start = day["date"]
            run += 1
            if run > best:
                best = run
                best_start, best_end = run_start, day["date"]
        else:
            run = 0
    return best, best_start, best_end


def week_columns(days: list[dict]) -> list[list[dict]]:
    """Sun–Sat columns over the window; first and last may be partial.

    A new column starts at every Sunday (weekday 0), so the dates inside
    a column ascend Sunday → Saturday, matching the S M T W T F S rows
    of the heatmap. A 365-day window always yields 53 columns.
    """
    columns: list[list[dict]] = []
    current: list[dict] = []
    for day in days:
        if day["weekday"] == 0 and current:
            columns.append(current)
            current = []
        current.append(day)
    if current:
        columns.append(current)
    return columns


def june_quiet_evidence(days: list[dict], min_zero_run: int = 7) -> dict:
    """Honest evidence for the June quiet stretch.

    Looks for the longest run of zero-contribution days inside a June
    that is fully covered by the 365-day window. Nothing is invented:
    the dates and the length come from the fetched calendar, and quiet
    is True only when that run spans at least a full week.
    """
    by_date = {d["date"]: d for d in days}
    best = {"quiet": False, "start": None, "end": None, "zero_days": 0}

    def consider(start: str | None, end: str, length: int) -> None:
        nonlocal best
        if length > best["zero_days"]:
            best = {"quiet": length >= min_zero_run,
                    "start": start, "end": end, "zero_days": length}

    for year in sorted({d[:4] for d in by_date}):
        june = [f"{year}-06-{n:02d}" for n in range(1, 31)]
        if not all(date in by_date for date in june):
            continue  # June is not fully inside the window
        run_start = None
        run_len = 0
        for date in june:
            if by_date[date]["contributionCount"] == 0:
                if run_start is None:
                    run_start = date
                run_len += 1
            else:
                if run_len and run_start is not None:
                    prev = (datetime.strptime(date, "%Y-%m-%d").date()
                            - timedelta(days=1)).isoformat()
                    consider(run_start, prev, run_len)
                run_start, run_len = None, 0
        if run_len and run_start is not None:
            consider(run_start, june[-1], run_len)
    return best


def visibility_notes(sees_private: bool, private_ytd: int) -> dict:
    """Caption fragments scoped to what the data actually proves."""
    if sees_private:
        return {
            "sees_private": True,
            "year": f"figures include {fmt_int(private_ytd)} private contributions, shown only as a total",
            "streak": "counting any contribution, public or private",
            "daily": "Combined daily totals; private work is never broken out per day.",
            "history": "HISTORY bars: cyan = public, muted = private.",
        }
    return {
        "sees_private": False,
        "year": "public contributions only: no private totals were visible to this token, and any private work is not shown as zero",
        "streak": "counting the contributions visible to this token",
        "daily": "Daily totals as reported for this token; private work, if any, is not shown as zero.",
        "history": "HISTORY bars: public contributions only; private work, if any, is not shown as zero.",
    }


def streak_phrase(length: int, start: str, end: str) -> str:
    if length <= 0:
        return "no active streak"
    return f"streak {length} days ({fmt_day(start)} – {fmt_day(end)})"


def heat_color(count: int, max_count: int, theme: dict) -> str:
    if count <= 0 or max_count <= 0:
        return theme["heat0"]
    t = count / max_count
    if t < 0.15:
        return theme["heat1"]
    if t < 0.35:
        return theme["heat2"]
    if t < 0.65:
        return theme["heat3"]
    return theme["heat4"]


def build_flight_log(events: list[dict], limit: int = 11) -> list[dict]:
    rows: list[dict] = []
    collapse: dict[tuple, dict] = {}
    events = sorted(events, key=lambda e: e.get("created_at") or "", reverse=True)
    for ev in events:
        repo = ev.get("repo", {}).get("name") or ""
        if excluded_repo(repo) or forbidden_text(repo):
            continue
        ts = ev.get("created_at") or ""
        if not ts:
            continue
        typ = ev.get("type")
        payload = ev.get("payload") or {}
        verb = extra = title = ""
        if typ == "PushEvent":
            commits = payload.get("commits") or []
            n = payload.get("size")
            if not n:
                n = len(commits)
            branch = (payload.get("ref") or "").replace("refs/heads/", "")
            msg = commits[-1].get("message", "").split("\n")[0] if commits else ""
            verb = "PUSH"
            parts = []
            if n:
                parts.append(f"{n} commit" + ("s" if n != 1 else ""))
            if branch and branch not in {"master", "main"}:
                parts.append(branch)
            extra = " · ".join(parts)
            title = msg
        elif typ == "PullRequestEvent":
            pr = payload.get("pull_request") or {}
            action = payload.get("action") or ""
            if payload.get("merged") or (action == "closed" and pr.get("merged")):
                action = "merged"
            if action not in {"opened", "merged", "closed", "reopened"}:
                continue
            verb = f"PR {action.upper()}"
            num = pr.get("number") or payload.get("number")
            extra = f"#{num}" if num else ""
            title = pr.get("title") or ""
        elif typ == "IssuesEvent":
            action = payload.get("action") or ""
            if action not in {"opened", "closed", "reopened"}:
                continue
            issue = payload.get("issue") or {}
            verb = f"ISSUE {action.upper()}"
            num = issue.get("number")
            extra = f"#{num}" if num else ""
            title = issue.get("title") or ""
        elif typ == "CreateEvent" and payload.get("ref_type") == "repository":
            verb = "CREATE REPO"
            title = ""
        elif typ == "ReleaseEvent":
            rel = payload.get("release") or {}
            verb = "RELEASE"
            extra = rel.get("tag_name") or ""
            title = rel.get("name") or ""
        elif typ == "PullRequestReviewEvent":
            verb = "REVIEW"
            pr = payload.get("pull_request") or {}
            num = pr.get("number")
            extra = f"#{num}" if num else ""
            title = pr.get("title") or ""
        else:
            continue

        # Field-level sanitization: an unsafe title or branch/tag label is
        # dropped, but the event itself stays. Repos were filtered above.
        if forbidden_text(title):
            title = ""
        if forbidden_text(extra):
            extra = ""

        # Collapse only truly identical rows: same event, same repo, same
        # number/label, same day. PR OPENED and PR MERGED differ in verb,
        # so they never collapse into one row.
        key = (ts[:10], repo, verb, extra)
        prev = collapse.get(key)
        if prev is not None:
            prev["count"] += 1
            if title and not prev["title"]:
                prev["title"] = title
            continue
        row = {
            "ts": ts,
            "repo": repo,
            "label": display_repo(repo),
            "verb": verb,
            "extra": extra,
            "title": title,
            "count": 1,
        }
        collapse[key] = row
        rows.append(row)

    return rows[:limit]


def language_spectrum(nodes: list[dict], top: int = 5) -> list[tuple[str, int, float]]:
    sizes: dict[str, int] = defaultdict(int)
    for n in nodes:
        for e in (n.get("languages") or {}).get("edges") or []:
            sizes[e["node"]["name"]] += e["size"]
    total = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda kv: -kv[1])[:top]
    return [(name, size, size / total) for name, size in ranked]


# ---------------------------------------------------------------------------
# SVG primitives
# ---------------------------------------------------------------------------

def svg_open(width: int, height: int, theme: dict, title: str, desc: str, uid: str) -> str:
    t = THEMES[theme] if isinstance(theme, str) else theme
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-labelledby="{uid}-t {uid}-d">\n'
        f'<title id="{uid}-t">{esc(title)}</title>\n'
        f'<desc id="{uid}-d">{esc(desc)}</desc>\n'
        f'<style>\n'
        f".pulse{{animation:pulse 2.8s ease-in-out infinite}}\n"
        f"@keyframes pulse{{50%{{opacity:.35}}}}\n"
        f"@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}}}\n"
        f"</style>\n"
        f'<rect width="{width}" height="{height}" rx="14" fill="{t["bg"]}" '
        f'stroke="{t["border"]}" stroke-width="1"/>\n'
    )


def svg_close() -> str:
    return "</svg>\n"


def tick(x: int, y: int, accent: str) -> str:
    return f'<rect x="{x}" y="{y}" width="28" height="3" rx="1.5" fill="{accent}"/>\n'


def text(x, y, content, fill, size, weight="400", family="sans", anchor="start", ls="0"):
    font = SANS if family == "sans" else MONO
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="{font}" '
        f'font-size="{size}" font-weight="{weight}" letter-spacing="{ls}" fill="{fill}">'
        f"{esc(content)}</text>\n"
    )


CAPTION_MAX_W = 804  # 860 - 2 * 28 padding
CAPTION_PITCH = 13


def est_width(s: str, size: float, family: str = "sans") -> float:
    """Conservative text width estimate; the SVG has no text measuring."""
    return len(s) * size * (0.6 if family == "mono" else 0.47)


def wrap_text(s: str, size: float = 9, max_w: float = CAPTION_MAX_W) -> list[str]:
    """Greedy word wrap. Never drops words."""
    lines: list[str] = []
    cur = ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and est_width(trial, size) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def clip(s: str, max_w: float, size: float, family: str = "sans") -> str:
    """Truncate with an ellipsis only when the estimate overflows."""
    if est_width(s, size, family) <= max_w:
        return s
    per = size * (0.6 if family == "mono" else 0.47)
    keep = max(int(max_w / per) - 1, 1)
    return s[:keep].rstrip() + "…"


def caption_lines(paragraphs: list[str]) -> list[str]:
    lines: list[str] = []
    for p in paragraphs:
        lines.extend(wrap_text(p))
    return lines


def caption_block(lines: list[str], theme: dict, h: int) -> str:
    """Bottom-anchored caption: last line stays at h - 12 (reference layout)."""
    n = len(lines)
    return "".join(
        text(28, h - 12 - (n - 1 - i) * CAPTION_PITCH, line, theme["muted"], 9, "400", "sans")
        for i, line in enumerate(lines)
    )


def header_bar(theme: dict, kicker: str, right: str, y_tick=18, y_text=42) -> str:
    parts = tick(28, y_tick, theme["accent"])
    parts += text(28, y_text, kicker, theme["accent"], 12, "600", "mono", ls="1.8")
    # LIVE pip
    parts += f'<circle class="pulse" cx="818" cy="{y_text - 4}" r="3.5" fill="{theme["accent"]}"/>\n'
    parts += text(808, y_text, right, theme["muted"], 11, "400", "mono", anchor="end", ls="0.4")
    return parts


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def render_telemetry(data: dict, theme_name: str) -> str:
    t = THEMES[theme_name]
    user = data["user"]
    ytd = user["thisYear"]
    days = data["days365"]
    streak_len, streak_start, streak_end = current_streak(days)
    generated = fmt_utc(data["generated_at"])
    public_repos = user["repositories"]["totalCount"]
    total = ytd["contributionCalendar"]["totalContributions"]
    year = data["generated_at"].year
    notes = data["notes"]

    ytd_tag = f"{year} YTD"
    cells = [
        (fmt_int(total), f"{year} CONTRIB.", ytd_tag),
        (fmt_int(ytd["totalCommitContributions"]), "COMMITS", ytd_tag),
        (fmt_int(ytd["totalPullRequestContributions"]), "PULL REQUESTS", ytd_tag),
        (fmt_int(ytd["totalIssueContributions"]), "ISSUES", ytd_tag),
        (f"{streak_len} DAYS", "CURRENT STREAK", f"TO {fmt_day(streak_end)}" if streak_len else "NONE ACTIVE"),
        (str(public_repos), "PUBLIC REPOS", "NOW"),
    ]

    cap1 = (
        f"{year} YTD {notes['year']}. "
        f"{streak_phrase(streak_len, streak_start, streak_end).capitalize()}, {notes['streak']}."
    )
    cap_lines = caption_lines([cap1, notes["history"]])
    h = 278 + 14 * max(len(cap_lines) - 2, 0)
    out = [svg_open(860, h, t, "Mission Control telemetry",
                    f"GitHub telemetry for {USER} in {year}: {total} contributions, "
                    f"{streak_len}-day streak.", f"tel-{theme_name}")]
    out.append(header_bar(t, "TELEMETRY  //  CRISESARMIENTO", generated))

    # Readouts
    gap, left, top, cell_h = 10, 28, 62, 90
    cell_w = (860 - 2 * left - 5 * gap) / 6
    for i, (value, label, sub) in enumerate(cells):
        x = left + i * (cell_w + gap)
        out.append(
            f'<rect x="{x:.1f}" y="{top}" width="{cell_w:.1f}" height="{cell_h}" '
            f'rx="8" fill="{t["panel"]}" stroke="{t["border"]}"/>\n'
        )
        out.append(text(x + cell_w / 2, top + 42, value, t["text"], 26, "600", "sans", "middle", "-0.5"))
        out.append(text(x + cell_w / 2, top + 66, label, t["muted"], 10, "600", "mono", "middle", "1.2"))
        out.append(text(x + cell_w / 2, top + 80, sub, t["accent"], 8, "600", "mono", "middle", "1"))

    # Year history: public vs private
    years = data["years"]
    y0, bar_h, base_x = 176, 28, 28
    out.append(text(28, y0, "HISTORY", t["muted"], 9, "600", "mono", ls="1.4"))
    max_total = max((y["total"] for y in years), default=0) or 1
    track_x = 100
    track_w = 732
    slot = track_w / len(years) if years else track_w
    for i, y in enumerate(years):
        bx = track_x + i * slot + 8
        bw = slot - 16
        pub_h = (y["public"] / max_total) * bar_h
        priv_h = (y["private"] / max_total) * bar_h
        if y["public"] and pub_h < 2:
            pub_h = 2
        if y["private"] and priv_h < 2:
            priv_h = 2
        by = y0 + 8 + max(bar_h - (pub_h + priv_h), 0)
        # private (dim) on the bottom, public (accent) on top — but only
        # when the fetched data proves the token receives private totals.
        if notes["sees_private"] and y["private"]:
            out.append(
                f'<rect x="{bx:.1f}" y="{by + pub_h:.1f}" width="{bw:.1f}" '
                f'height="{max(priv_h, 0):.1f}" rx="2" fill="{t["private"]}"/>\n'
            )
        if y["public"]:
            out.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" '
                f'height="{max(pub_h, 0):.1f}" rx="2" fill="{t["accent"]}"/>\n'
            )
        label = f"{y['year']}" + (" YTD" if y["ytd"] else "")
        out.append(text(bx + bw / 2, y0 + 8 + bar_h + 16, label, t["muted"], 9, "400", "mono", "middle"))

    for i, line in enumerate(cap_lines):
        out.append(text(28, h - 14 - (len(cap_lines) - 1 - i) * 14, line, t["muted"], 9, "400", "sans"))
    out.append(svg_close())
    return "".join(out)


def render_ground_track(data: dict, theme_name: str) -> str:
    t = THEMES[theme_name]
    days = data["days365"]
    columns = week_columns(days)
    weeks = [(col[0]["date"], sum(d["contributionCount"] for d in col)) for col in columns]
    streak_len, streak_start, streak_end = current_streak(days)
    peak = max(days, key=lambda d: d["contributionCount"])
    active = sum(1 for d in days if d["contributionCount"] > 0)
    total = data["user"]["last365"]["contributionCalendar"]["totalContributions"]
    first, last = days[0]["date"], days[-1]["date"]
    max_week = max((w for _, w in weeks), default=1) or 1
    max_day = peak["contributionCount"] or 1
    notes = data["notes"]

    caption = (
        f"{fmt_int(total)} contributions · {active} active days · "
        f"peak {peak['contributionCount']} on {fmt_day(peak['date'])} · "
        f"{streak_phrase(streak_len, streak_start, streak_end)}. {notes['daily']}"
    )
    if data.get("june_quiet", {}).get("quiet"):
        caption += " The quiet stretch in June is real."
    cap_lines = caption_lines([caption])
    h = 340 + CAPTION_PITCH * (len(cap_lines) - 1)
    out = [svg_open(860, h, t, "Ground track of contributions",
                    f"Contribution ground track from {first} to {last}: {total} contributions, "
                    f"{active} active days, peak {peak['contributionCount']} on {peak['date']}.",
                    f"gt-{theme_name}")]
    out.append(header_bar(t, "GROUND TRACK  //  LAST 365 DAYS",
                          f"{fmt_day(first)}  →  {fmt_day(last)}"))

    # Faint "map" grid + sinusoidal ground track
    track_top, track_h = 58, 70
    x0, x1 = 40, 820
    # horizontal grid lines
    for i in range(4):
        y = track_top + i * (track_h / 3)
        out.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" '
            f'stroke="{t["grid"]}" stroke-width="1"/>\n'
        )
    # sinusoidal baseline
    pts = []
    n = len(weeks)
    for i, (wdate, wtotal) in enumerate(weeks):
        x = x0 + (x1 - x0) * (i / max(n - 1, 1))
        y = track_top + track_h / 2 + math.sin(i / n * math.pi * 6) * (track_h / 2 - 8)
        pts.append((x, y, wtotal, wdate))
    # faint full path
    d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y, *_ in pts)
    out.append(f'<path d="{d}" fill="none" stroke="{t["track_faint"]}" stroke-width="1.2"/>\n')
    # signal path with per-segment opacity via individual strokes
    for i in range(len(pts) - 1):
        x_a, y_a, tot_a, _ = pts[i]
        x_b, y_b, tot_b, _ = pts[i + 1]
        mag = max(tot_a, tot_b) / max_week
        if mag <= 0:
            continue
        width = 1.2 + 3.2 * mag
        opacity = 0.25 + 0.75 * mag
        out.append(
            f'<path d="M {x_a:.1f} {y_a:.1f} L {x_b:.1f} {y_b:.1f}" fill="none" '
            f'stroke="{t["accent"]}" stroke-width="{width:.2f}" stroke-linecap="round" '
            f'opacity="{opacity:.2f}"/>\n'
        )
    # dots on weeks with signal
    for x, y, tot, _ in pts:
        if tot <= 0:
            continue
        r = 1.4 + 2.4 * (tot / max_week)
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{t["accent"]}"/>\n')

    # Weekly bars
    bar_top, bar_h = 140, 56
    out.append(text(28, bar_top - 8, "WEEKLY SIGNAL", t["muted"], 9, "600", "mono", ls="1.4"))
    bw = (x1 - x0) / n
    for i, (wdate, wtotal) in enumerate(weeks):
        x = x0 + i * bw
        bh = 2 if wtotal == 0 else (wtotal / max_week) * (bar_h - 2)
        y = bar_top + bar_h - bh
        color = t["grid"] if wtotal == 0 else t["accent"]
        opacity = 1 if wtotal == 0 else 0.35 + 0.65 * (wtotal / max_week)
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(bw - 1.2, 1):.1f}" height="{bh:.1f}" '
            f'rx="1" fill="{color}" opacity="{opacity:.2f}"/>\n'
        )
    # month ticks
    last_month = ""
    for i, (wdate, _) in enumerate(weeks):
        month = wdate[:7]
        if month != last_month:
            last_month = month
            x = x0 + i * bw
            label = datetime.strptime(wdate, "%Y-%m-%d").strftime("%b").upper()
            out.append(text(x, bar_top + bar_h + 14, label, t["muted"], 9, "400", "mono"))

    # Daily heatmap (7 rows, Sun–Sat; GraphQL weekday 0 = Sunday)
    heat_top = 228
    cell = 10
    gap = 2
    step = cell + gap
    grid_w = len(columns) * step - gap
    hx = (860 - grid_w) / 2
    dow = ["S", "M", "T", "W", "T", "F", "S"]
    for r, lab in enumerate(dow):
        if r % 2 == 0:
            out.append(text(hx - 10, heat_top + r * step + 8, lab, t["muted"], 7, "400", "mono", "end"))
    for c, col in enumerate(columns):
        for day in col:
            r = day["weekday"]
            x = hx + c * step
            y = heat_top + r * step
            fill = heat_color(day["contributionCount"], max_day, t)
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell}" height="{cell}" rx="2" fill="{fill}">'
                f'<title>{esc(day["date"])}: {day["contributionCount"]}</title></rect>\n'
            )

    out.append(caption_block(cap_lines, t, h))
    out.append(svg_close())
    return "".join(out)


def render_flight_log(data: dict, theme_name: str) -> str:
    t = THEMES[theme_name]
    rows = build_flight_log(data["events"], limit=11)
    row_h = 22
    header_h = 64
    cap_lines = caption_lines([
        "Public events only. Private off-hours work is omitted, not shown as zero. "
        "Titles that would leak a private name or a reserved domain are dropped."
    ])
    caption_h = 28 + CAPTION_PITCH * (len(cap_lines) - 1)
    h = header_h + 20 + len(rows) * row_h + caption_h
    out = [svg_open(860, h, t, "Public GitHub flight log",
                    "Recent public GitHub events, excluding private off-hours work.",
                    f"fl-{theme_name}")]
    out.append(header_bar(t, "FLIGHT LOG  //  PUBLIC EVENTS", "NEWEST FIRST"))
    # column headers
    y = 64
    out.append(text(28, y, "TIME (UTC)", t["muted"], 9, "600", "mono", ls="1.2"))
    out.append(text(200, y, "EVENT", t["muted"], 9, "600", "mono", ls="1.2"))
    out.append(text(360, y, "VEHICLE", t["muted"], 9, "600", "mono", ls="1.2"))
    out.append(text(560, y, "NOTE", t["muted"], 9, "600", "mono", ls="1.2"))
    out.append(f'<line x1="28" y1="{y + 8}" x2="832" y2="{y + 8}" stroke="{t["border"]}" stroke-width="1"/>\n')

    for i, r in enumerate(rows):
        yy = y + 28 + i * row_h
        ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).strftime("%-d %b  %H:%M").upper()
        verb = r["verb"]
        if r["count"] > 1:
            verb = f"{verb} ×{r['count']}"
        note = r["title"] or r["extra"] or ""
        if len(note) > 42:
            note = note[:41] + "…"
        note = clip(note, 272, 11)
        fill = t["text"]
        out.append(text(28, yy, ts, t["muted"], 11, "400", "mono"))
        out.append(text(200, yy, clip(verb, 150, 11, "mono"), t["accent"], 11, "600", "mono"))
        out.append(text(360, yy, clip(r["label"], 190, 11, "mono"), fill, 11, "400", "mono"))
        out.append(text(560, yy, note, t["muted"], 11, "400", "sans"))

    out.append(caption_block(cap_lines, t, h))
    out.append(svg_close())
    return "".join(out)


def render_mission_board(data: dict, theme_name: str) -> str:
    t = THEMES[theme_name]
    nodes = {n["name"]: n for n in data["user"]["repositories"]["nodes"]}

    def last_signal(repo: str) -> str:
        n = nodes.get(repo)
        if not n or not n.get("pushedAt"):
            return "—"
        return fmt_short(n["pushedAt"])

    # Status is editorial but tied to facts already in the published README
    # or to public push dates. Palehour has no last-signal from a repo date.
    missions = [
        ("BUILDING", "Route", last_signal("useroute"), "Honest model routing observability"),
        ("LIVE", "Starship Flight 15 sim", last_signal("starship-flight-15-sim"), "Live demo on GitHub Pages"),
        ("LIVE", "Gastos Abiertos Córdoba", last_signal("gastos-abiertos-cba"), "Civic budget explorer"),
        ("BUILDING", "cstack", last_signal("cstack"), "Versioned AI coding stack"),
        ("AVAILABLE", "opencode-context-usage", last_signal("opencode-context-usage"), "OpenCode /context command"),
        ("AVAILABLE", "invoice-scope", last_signal("invoice-scope"), "Invoice generator for contractors"),
        ("PAUSED", "TestSense", last_signal("testsense"), "Early 2023 experiment, now paused"),
        ("OFF-HOURS", "Palehour", "PRIVATE", "Studio site only; the game repo stays private"),
    ]

    row_h = 28
    cap_lines = caption_lines([
        "LIVE / BUILDING are current public work. AVAILABLE means the repo is public and idle. "
        "PAUSED is as stated in the README. Palehour last-signal is withheld because the repo is private."
    ])
    h = 64 + 18 + len(missions) * row_h + 32 + CAPTION_PITCH * (len(cap_lines) - 1)
    out = [svg_open(860, h, t, "Mission board of public projects",
                    "Status board of public projects and off-hours studio work.",
                    f"mb-{theme_name}")]
    out.append(header_bar(t, "MISSION BOARD", "STATUS FROM PUBLIC SIGNALS"))
    y = 64
    out.append(text(28, y, "STATUS", t["muted"], 9, "600", "mono", ls="1.2"))
    out.append(text(160, y, "MISSION", t["muted"], 9, "600", "mono", ls="1.2"))
    out.append(text(430, y, "LAST SIGNAL", t["muted"], 9, "600", "mono", ls="1.2"))
    out.append(text(580, y, "NOTE", t["muted"], 9, "600", "mono", ls="1.2"))
    out.append(f'<line x1="28" y1="{y + 8}" x2="832" y2="{y + 8}" stroke="{t["border"]}" stroke-width="1"/>\n')

    for i, (status, name, signal, note) in enumerate(missions):
        yy = y + 30 + i * row_h
        live = status in {"LIVE", "BUILDING"}
        fill = t["accent"] if live else t["muted"]
        # LIVE = filled pip, BUILDING = hollow pip, others = dim filled
        cx, cy = 36, yy - 4
        if status == "BUILDING":
            out.append(f'<circle cx="{cx}" cy="{cy}" r="4.5" fill="none" stroke="{t["accent"]}" stroke-width="1.6"/>\n')
            out.append(f'<circle class="pulse" cx="{cx}" cy="{cy}" r="2" fill="{t["accent"]}"/>\n')
        elif status == "LIVE":
            out.append(f'<circle cx="{cx}" cy="{cy}" r="4.5" fill="{t["accent"]}"/>\n')
        else:
            out.append(f'<circle cx="{cx}" cy="{cy}" r="4.5" fill="{t["muted"]}" opacity=".55"/>\n')
        out.append(text(52, yy, status, fill, 11, "600", "mono"))
        name_svg = text(160, yy, clip(name, 262, 13), t["text"], 13, "600", "sans")
        if name == "Palehour":
            # Studio site only; never the private game repo.
            name_svg = f'<a href="https://palehour.github.io/" target="_blank">{name_svg}</a>\n'
        out.append(name_svg)
        out.append(text(430, yy, signal, t["muted"], 11, "400", "mono"))
        out.append(text(580, yy, clip(note, 252, 11), t["muted"], 11, "400", "sans"))

    out.append(caption_block(cap_lines, t, h))
    out.append(svg_close())
    return "".join(out)


def render_spectrum(data: dict, theme_name: str) -> str:
    t = THEMES[theme_name]
    bands = language_spectrum(data["user"]["repositories"]["nodes"], top=5)
    cap_lines = caption_lines([
        "Bytes in every public, owned, non-fork repository; every language counted. "
        "Private consultancy work is not included, and is not shown as zero."
    ])
    h = 188 + CAPTION_PITCH * (len(cap_lines) - 1)
    out = [svg_open(860, h, t, "Language spectrum of public repositories",
                    "Language byte shares in public owned non-fork repositories only.",
                    f"sp-{theme_name}")]
    out.append(header_bar(t, "SPECTRUM  //  PUBLIC REPOS ONLY", "BY SOURCE BYTES"))
    top = 68
    bar_x, bar_w, bar_h = 200, 580, 12
    for i, (name, _size, share) in enumerate(bands):
        yy = top + i * 20
        out.append(text(28, yy + 8, name, t["text"], 12, "400", "sans"))
        out.append(
            f'<rect x="{bar_x}" y="{yy}" width="{bar_w}" height="{bar_h}" rx="2" fill="{t["panel"]}" '
            f'stroke="{t["border"]}"/>\n'
        )
        out.append(
            f'<rect x="{bar_x}" y="{yy}" width="{bar_w * share:.1f}" height="{bar_h}" rx="2" fill="{t["accent"]}"/>\n'
        )
        out.append(text(bar_x + bar_w + 8, yy + 10, f"{share * 100:.1f}%", t["muted"], 11, "400", "mono"))

    out.append(caption_block(cap_lines, t, h))
    out.append(svg_close())
    return "".join(out)


RENDERERS = {
    "telemetry": render_telemetry,
    "ground-track": render_ground_track,
    "flight-log": render_flight_log,
    "mission-board": render_mission_board,
    "spectrum": render_spectrum,
}


def write_all(data: dict, directory: Path) -> list[Path]:
    written: list[Path] = []
    directory.mkdir(parents=True, exist_ok=True)
    for name, fn in RENDERERS.items():
        for theme in ("dark", "light"):
            path = directory / f"{name}-{theme}.svg"
            path.write_text(fn(data, theme))
            written.append(path)
    stamp = data["generated_at"].strftime("%Y-%m-%dT%H:%MZ")
    print(f"wrote {len(written)} SVGs to {directory}  ({stamp})", file=sys.stderr)
    return written


def main() -> int:
    data = fetch_data()
    write_all(data, OUT_DIR)
    # Brief stdout summary for the Action log / local check.
    ytd = data["user"]["thisYear"]
    length, start, end = current_streak(data["days365"])
    mode = "sees private totals" if data["sees_private"] else "public-only view"
    streak = f"streak {length}d ({start}-{end})" if length else "no active streak"
    print(
        f"OK  {USER}  {ytd['contributionCalendar']['totalContributions']} contrib in "
        f"{data['generated_at'].year}  ({mode})  {streak}  "
        f"log {len(build_flight_log(data['events']))} lines"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
