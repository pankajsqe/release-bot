import base64
import datetime
import os
import re
import sys
from pathlib import Path

import requests
import yaml

# ── Config ─────────

TOKEN   = os.environ["GH_TOKEN"]   # GITHUB_TOKEN — auto, no setup needed
ACTOR   = os.environ["ACTOR"]
PROJECT = int(os.environ["PROJECT_NUMBER"])
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

API = "https://api.github.com"

HEADERS = {
    "Authorization":        f"Bearer {TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

OWNER        = "rancher"
REPO         = "observability-e2e"   # issue is always created here
RANCHER_REPO = "rancher"             # milestones live here
CHARTS_REPO  = "charts"             # chart versions live here

VERSIONS = ["v2.11", "v2.12", "v2.13", "v2.14", "v2.15"]

CHARTS = [
    "rancher-monitoring",
    "rancher-logging",
    "prometheus-federator",
    "rancher-alerting-drivers",
    "rancher-backup",
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def gh_get_paged(url, params=None):
    """Follow GitHub pagination and return the combined list."""
    items = []
    while url:
        r = requests.get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        items.extend(r.json())
        url    = r.links.get("next", {}).get("url")
        params = None   # only on the first request
    return items


# ── Step 1 – Find latest milestones ──────────────────────────────────────────

def find_latest_milestones():
    """
    Return {series: milestone_dict} mapping the latest v2.NN.x milestone
    (open or closed) for each series in VERSIONS.
    """
    milestones = gh_get_paged(
        f"{API}/repos/{OWNER}/{RANCHER_REPO}/milestones",
        params={"state": "all", "per_page": 100},
    )

    latest: dict = {}
    for ms in milestones:
        m = re.match(r"^(v2\.\d+)\.(\d+)$", ms["title"])
        if not m:
            continue
        series = m.group(1)
        if series not in VERSIONS:
            continue
        patch = int(m.group(2))
        prev  = latest.get(series)
        if prev is None or patch > int(re.search(r"\.(\d+)$", prev["title"]).group(1)):
            latest[series] = ms

    return latest


# ── Step 2 – Search board issues for each milestone via GitHub search API ────
#
# Query: is:issue state:open project:rancher/{PROJECT} milestone:{title}
# This returns exactly the issues that are both open AND on the project board
# AND in that milestone — no GraphQL or separate board fetch required.

def search_board_issues(milestone_title: str) -> list:
    """
    Use the GitHub search API to find ALL issues (open + closed) that belong to
    project rancher/{PROJECT} and the given milestone.
    Returns a list of issue objects with a 'state' field ('open' or 'closed').
    """
    q      = f'is:issue project:{OWNER}/{PROJECT} milestone:"{milestone_title}"'
    issues = []
    url    = f"{API}/search/issues"
    params = {"q": q, "per_page": 100, "page": 1}

    while True:
        r = requests.get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        issues.extend(data["items"])
        if len(issues) >= data["total_count"] or not data["items"]:
            break
        params["page"] += 1

    return issues


# ── Step 3 – Fetch chart versions ─────────────────────────────────────────────
#
# Strategy: 2 API calls per chart × branch — fast regardless of how many
# version directories exist in the chart.
#
#   Call 1: GET /repos/rancher/charts/commits
#               ?path=charts/<chart>&sha=<branch>&per_page=1
#           → latest commit SHA + committer date
#
#   Call 2: GET /repos/rancher/charts/commits/<sha>
#           → list of files changed; extract the version dir from the path
#             charts/<chart>/<version-dir>/...
#
# Then read Chart.yaml from that version dir for the canonical version string.

def latest_chart_version(chart: str, branch: str) -> dict:
    """
    Return {"version": str, "date": str} using the last-commit-date strategy.
    """
    # ── Call 1: latest commit that touched charts/<chart>/ ────────────────
    r = requests.get(
        f"{API}/repos/{OWNER}/{CHARTS_REPO}/commits",
        headers=HEADERS,
        params={"path": f"charts/{chart}", "sha": branch, "per_page": 1},
    )
    if r.status_code in (404, 422):
        return {"version": "N/A", "date": ""}
    r.raise_for_status()
    commits = r.json()
    if not commits:
        return {"version": "N/A", "date": ""}

    commit     = commits[0]
    commit_sha = commit["sha"]
    raw_date   = commit["commit"]["committer"]["date"]   # 2025-08-01T12:34:56Z
    date       = raw_date[:10]                           # YYYY-MM-DD

    # ── Call 2: files changed in that commit ─────────────────────────────
    r2 = requests.get(
        f"{API}/repos/{OWNER}/{CHARTS_REPO}/commits/{commit_sha}",
        headers=HEADERS,
    )
    r2.raise_for_status()
    files = r2.json().get("files", [])

    # Extract version dir: charts/<chart>/<version-dir>/anything
    version_dir = ""
    prefix = f"charts/{chart}/"
    for f in files:
        fname = f.get("filename", "")
        if fname.startswith(prefix):
            rest = fname[len(prefix):]          # e.g. "110.0.0+up80.9.1/Chart.yaml"
            part = rest.split("/")[0]           # e.g. "110.0.0+up80.9.1"
            if part:
                version_dir = part
                break

    if not version_dir:
        return {"version": "N/A", "date": date}

    # ── Read Chart.yaml for the canonical version string ──────────────────
    r3 = requests.get(
        f"{API}/repos/{OWNER}/{CHARTS_REPO}/contents/charts/{chart}/{version_dir}/Chart.yaml",
        headers=HEADERS,
        params={"ref": branch},
    )
    if r3.status_code == 404:
        return {"version": version_dir, "date": date}
    r3.raise_for_status()

    parsed  = yaml.safe_load(base64.b64decode(r3.json()["content"]).decode())
    version = parsed.get("version", version_dir)
    return {"version": version, "date": date}


def fetch_chart_versions() -> dict[str, dict[str, dict]]:
    """Return {chart: {series: {"version": str, "date": str}}}."""
    result: dict = {}
    for chart in CHARTS:
        result[chart] = {}
        for v in VERSIONS:
            info = latest_chart_version(chart, f"dev-{v}")
            result[chart][v] = info
            print(f"    {chart} @ dev-{v}: {info['version']}  (last commit {info['date'] or 'unknown'})")
    return result


# ── Step 5 – Validate ─────────────────────────────────────────────────────────

def build_validation(milestones, board_issues, chart_versions) -> tuple[list[str], bool]:
    passed = True
    lines: list[str] = []

    def row(label: str, ok: bool):
        nonlocal passed
        lines.append(f"  {'✓' if ok else '✗'} {label}")
        if not ok:
            passed = False

    row(f"Repository exists ({OWNER}/{REPO})", True)
    row(f"Project #{OWNER}/{PROJECT} reachable via search", True)

    for v in VERSIONS:
        ms = milestones.get(v)
        if ms:
            count = len(board_issues.get(v, []))
            row(f"{v}.x milestone found — {ms['title']} ({count} board issues)", True)
        else:
            row(f"{v}.x milestone NOT found", False)

    for chart in CHARTS:
        any_found = any(
            chart_versions.get(chart, {}).get(v, {}).get("version", "N/A") not in ("N/A", "")
            for v in VERSIONS
        )
        row(f"{chart} found", any_found)

    lines.append("")
    lines.append(f"  RESULT: {'PASS' if passed else 'FAIL'}")
    return lines, passed


# ── Step 6 – Build issue body ─────────────────────────────────────────────────

def build_body(milestones, board_issues, chart_versions) -> str:
    """
    board_issues = {series: [issue, ...]} — all issues (open + closed) on the
    project board for that milestone.
    """
    lines: list[str] = []

    lines += [
        "# Rancher Release Tracking",
        "",
        f"Generated by: @{ACTOR}",
        "",
    ]

    # Summary table
    lines += [
        "## Summary",
        "",
        "| Milestone | Open | Closed | Total |",
        "|---|---:|---:|---:|",
    ]
    for v in VERSIONS:
        ms = milestones.get(v)
        if ms:
            issues = board_issues.get(v, [])
            n_open   = sum(1 for i in issues if i["state"] == "open")
            n_closed = sum(1 for i in issues if i["state"] == "closed")
            lines.append(f"| {ms['title']} | {n_open} | {n_closed} | {len(issues)} |")
        else:
            lines.append(f"| _not found_ | — | — | — |")
    lines.append("")

    # Per-version issue lists
    lines.append("## Milestone Issues (on project board)")
    lines.append("")
    for v in VERSIONS:
        ms = milestones.get(v)
        if not ms:
            continue
        issues = board_issues.get(v, [])
        lines += [f"### {ms['title']}", ""]
        if not issues:
            lines.append("_No issues found on the board for this milestone._")
        else:
            for issue in issues:
                state = issue["state"]          # "open" or "closed"
                state_tag = "🟢 open" if state == "open" else "✅ closed"
                checkbox  = "[ ]" if state == "open" else "[x]"
                lines.append(
                    f"- {checkbox} `{state_tag}` [#{issue['number']}]({issue['html_url']})"
                    f" — {issue['title']}"
                )
        lines.append("")

    # Chart versions — one table per release, newest first
    lines.append("## Chart Versions")
    lines.append("")
    for v in reversed(VERSIONS):
        ms    = milestones.get(v)
        label = ms["title"] if ms else f"{v}.x"
        lines += [
            f"### Release {label}",
            "",
            "| Chart | Version | Last Commit | Remark |",
            "|---|---|---|---|",
        ]
        for chart in CHARTS:
            info    = chart_versions[chart].get(v, {})
            version = info.get("version", "N/A")
            date    = info.get("date") or "—"
            lines.append(f"| {chart} | {version} | {date} |  |")
        lines.append("")

    return "\n".join(lines)


# ── Report writers ───────────────────────────────────────────────────────────

def _val_html(val_lines: list[str]) -> str:
    rows = []
    for line in val_lines:
        line = line.strip()
        if not line or line.startswith("RESULT"):
            continue
        ok   = line.startswith("✓")
        icon = "<span class='ok'>✓</span>" if ok else "<span class='fail'>✗</span>"
        text = line[1:].strip()
        rows.append(f"<li>{icon} {text}</li>")
    return "<ul class='validation'>" + "".join(rows) + "</ul>"


def write_html(milestones, board_issues, chart_versions, val_lines, val_ok,
               issue_url: str | None = None) -> Path:
    now   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    mode  = "DRY RUN" if DRY_RUN else "LIVE"
    badge = "<span class='badge dry'>DRY RUN</span>" if DRY_RUN else "<span class='badge live'>LIVE</span>"
    result_cls = "pass" if val_ok else "fail"

    # Summary rows
    summary_rows = []
    for v in VERSIONS:
        ms    = milestones.get(v)
        if ms:
            issues   = board_issues.get(v, [])
            n_open   = sum(1 for i in issues if i["state"] == "open")
            n_closed = sum(1 for i in issues if i["state"] == "closed")
            summary_rows.append(
                f"<tr><td>{ms['title']}</td>"
                f"<td class='num open-count'>{n_open} open</td>"
                f"<td class='num closed-count'>{n_closed} closed</td>"
                f"<td class='num'>{len(issues)}</td></tr>"
            )
        else:
            summary_rows.append(
                f"<tr><td><em>(not found)</em></td>"
                f"<td colspan='3' class='num'>—</td></tr>"
            )

    # Issue sections
    issue_sections = []
    for v in VERSIONS:
        ms     = milestones.get(v)
        if not ms:
            continue
        issues = board_issues.get(v, [])
        n_open   = sum(1 for i in issues if i.get("state") == "open")
        n_closed = sum(1 for i in issues if i.get("state") == "closed")
        if not issues:
            body = "<p class='none'>No issues found on the board for this milestone.</p>"
        else:
            items = ""
            for i in issues:
                state     = i.get("state", "open")
                state_cls = "state-open" if state == "open" else "state-closed"
                state_lbl = "open" if state == "open" else "closed"
                items += (
                    "<li>"
                    "<span class='{cls}'>{lbl}</span> "
                    "<a href='{url}' target='_blank'>#{num}</a> &mdash; {title}"
                    "</li>"
                ).format(
                    cls=state_cls, lbl=state_lbl,
                    url=i["html_url"], num=i["number"], title=i["title"]
                )
            body = f"<ul>{items}</ul>"
        issue_sections.append(
            f"<section>"
            f"<h3>{ms['title']} "
            f"<span class='count'>"
            f"<span class='state-open'>{n_open} open</span>&nbsp;"
            f"<span class='state-closed'>{n_closed} closed</span>"
            f"</span></h3>{body}</section>"
        )

    # Chart tables — one per release, newest first
    chart_sections = []
    for v in reversed(VERSIONS):
        ms    = milestones.get(v)
        label = ms["title"] if ms else f"{v}.x"
        rows  = ""
        for chart in CHARTS:
            info    = chart_versions[chart].get(v, {})
            version = info.get("version", "N/A")
            date    = info.get("date") or "—"
            na_cls  = " class='na'" if version == "N/A" else ""
            rows += (
                f"<tr>"
                f"<td class='chart-name'>{chart}</td>"
                f"<td{na_cls}>{version}</td>"
                f"<td class='date'>{date}</td>"
                f"<td class='remark'></td>"
                f"</tr>"
            )
        chart_sections.append(
            f"<section>"
            f"<h3>Release {label}</h3>"
            f"<table>"
            f"<thead><tr><th>Chart</th><th>Version</th><th>Last Commit</th><th>Remark</th></tr></thead>"
            f"<tbody>{rows}</tbody>"
            f"</table></section>"
        )

    issue_banner = ""
    if issue_url:
        issue_banner = f"<div class='issue-link'>Issue created: <a href='{issue_url}' target='_blank'>{issue_url}</a></div>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rancher Release Tracking — {now}</title>
<style>
  body  {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           margin: 0; padding: 24px; background: #f6f8fa; color: #24292f; }}
  h1    {{ font-size: 1.6rem; margin-bottom: 4px; }}
  h2    {{ font-size: 1.2rem; border-bottom: 1px solid #d0d7de; padding-bottom: 6px; margin-top: 32px; }}
  h3    {{ font-size: 1rem; margin: 12px 0 6px; }}
  .meta {{ color: #57606a; font-size: .9rem; margin-bottom: 20px; }}
  .badge      {{ display:inline-block; padding:2px 10px; border-radius:12px;
                 font-size:.8rem; font-weight:600; margin-left:8px; }}
  .badge.dry  {{ background:#fff8c5; color:#9a6700; }}
  .badge.live {{ background:#dafbe1; color:#116329; }}
  table  {{ border-collapse: collapse; width: 100%; font-size: .88rem; }}
  th,td  {{ border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; }}
  th     {{ background: #f0f3f6; }}
  td.num {{ text-align: right; font-weight: 600; }}
  td.chart-name {{ font-family: monospace; white-space: nowrap; }}
  th.sub  {{ font-size:.78rem; font-weight:500; color:#57606a; background:#f6f8fa; }}
  td.date {{ font-size:.8rem; color:#57606a; white-space:nowrap; }}
  td.remark {{ min-width: 120px; color:#57606a; font-style:italic; }}
  td.na   {{ color:#cf222e; font-style:italic; }}
  .state-open   {{ display:inline-block; padding:1px 7px; border-radius:10px;
                   background:#dafbe1; color:#116329; font-size:.78rem; font-weight:600;
                   white-space:nowrap; }}
  .state-closed {{ display:inline-block; padding:1px 7px; border-radius:10px;
                   background:#f0f3f6; color:#57606a; font-size:.78rem; font-weight:600;
                   white-space:nowrap; }}
  .open-count   {{ color:#116329; }}
  .closed-count {{ color:#57606a; }}
  code   {{ background:#f0f3f6; padding:2px 5px; border-radius:4px; font-size:.82rem; }}
  a      {{ color: #0969da; text-decoration: none; }}
  a:hover{{ text-decoration: underline; }}
  .validation {{ list-style:none; padding:0; margin:0; font-size:.9rem; }}
  .validation li {{ padding: 3px 0; }}
  .ok   {{ color: #1a7f37; font-weight: 700; }}
  .fail {{ color: #cf222e; font-weight: 700; }}
  .result-pass {{ color:#1a7f37; font-weight:700; }}
  .result-fail {{ color:#cf222e; font-weight:700; }}
  .none {{ color:#57606a; font-style:italic; margin:4px 0; }}
  .count{{ color:#57606a; font-weight:400; font-size:.9em; }}
  section {{ margin-bottom: 16px; }}
  .issue-link {{ margin-top:16px; padding:10px 14px; background:#dafbe1;
                 border-radius:6px; font-size:.9rem; }}
  pre   {{ background:#f6f8fa; padding:16px; border:1px solid #d0d7de;
           border-radius:6px; overflow-x:auto; font-size:.82rem; }}
</style>
</head>
<body>
<h1>Rancher Release Tracking {badge}</h1>
<div class="meta">
  Generated by <strong>@{ACTOR}</strong> &nbsp;·&nbsp;
  Project <strong>#{PROJECT}</strong> &nbsp;·&nbsp;
  {now}
</div>

<h2>Validation</h2>
{_val_html(val_lines)}
<p class="result-{result_cls}">RESULT: {'PASS' if val_ok else 'FAIL'}</p>

<h2>Summary</h2>
<table>
  <thead><tr><th>Milestone</th><th>Open</th><th>Closed</th><th>Total</th></tr></thead>
  <tbody>{''.join(summary_rows)}</tbody>
</table>

<h2>Milestone Issues (on project board)</h2>
{''.join(issue_sections)}

<h2>Chart Versions</h2>
{''.join(chart_sections)}

{issue_banner}
</body>
</html>
"""

    out = Path("report.html")
    out.write_text(html, encoding="utf-8")
    return out


def write_markdown(body: str, val_lines: list[str], val_ok: bool,
                   issue_url: str | None = None) -> Path:
    now   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"<!-- generated {now} -->", ""]
    lines += body.splitlines()
    if issue_url:
        lines += ["", "---", "", f"**Issue created:** {issue_url}"]

    out = Path("report.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching milestones …")
    milestones = find_latest_milestones()

    print("Searching project board issues …")
    board_issues: dict = {}
    for v, ms in milestones.items():
        print(
            f"  is:issue state:open project:{OWNER}/{PROJECT} milestone:{ms['title']}"
        )
        board_issues[v] = search_board_issues(ms["title"])
        print(f"    → {len(board_issues[v])} issues")

    print("Fetching chart versions …")
    chart_versions = fetch_chart_versions()

    body              = build_body(milestones, board_issues, chart_versions)
    val_lines, val_ok = build_validation(milestones, board_issues, chart_versions)

    # ---------------------------------------------------------
    # Dry run / Create issue
    # ---------------------------------------------------------

    print("\n" + "=" * 80)
    print("DRY RUN" if DRY_RUN else "CREATING GITHUB ISSUE")
    print("=" * 80)

    print("\nRepository:")
    print(f"  {OWNER}/{REPO}")

    print("\nTriggered by:")
    print(f"  @{ACTOR}")

    print("\nProject:")
    print(f"  #{PROJECT}")

    print("\nVALIDATION")
    print("─" * 50)
    for line in val_lines:
        print(line)

    print("\nGenerated ticket:")
    print("-" * 80)
    print(body)
    print("-" * 80)

    issue_url: str | None = None

    if DRY_RUN:
        print("\nDRY RUN: No GitHub issue was created.")
    else:
        issue = requests.post(
            f"{API}/repos/{OWNER}/{REPO}/issues",
            headers=HEADERS,
            json={
                "title": "Rancher Release Tracking",
                "body":  body,
            },
        )
        issue.raise_for_status()
        issue_url = issue.json()["html_url"]
        print("\nCreated:", issue_url)

    # ── Write local reports ───────────────────────────────────────────────────
    html_path = write_html(milestones, board_issues, chart_versions,
                           val_lines, val_ok, issue_url)
    md_path   = write_markdown(body, val_lines, val_ok, issue_url)
    print(f"\nReports written:")
    print(f"  {html_path.resolve()}")
    print(f"  {md_path.resolve()}")

    if DRY_RUN and not val_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
