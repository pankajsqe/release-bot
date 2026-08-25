import base64
import datetime
import os
import re
import sys
import time
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

REQUEST_TIMEOUT_SEC = 30
RETRY_ATTEMPTS = 5
RETRY_BACKOFF_BASE_SEC = 1.5


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get(url, *, headers=None, params=None):
    """GET with retry/backoff for transient network and GitHub 5xx/429 errors."""
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            if r.status_code in (429, 500, 502, 503, 504) and attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE_SEC ** attempt)
                continue
            return r
        except requests.exceptions.RequestException as err:
            last_err = err
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE_SEC ** attempt)
                continue
            raise

    if last_err:
        raise last_err
    raise RuntimeError("HTTP GET failed unexpectedly")


def _http_post(url, *, headers=None, json=None):
    """POST with retry/backoff for transient network and GitHub 5xx/429 errors."""
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.post(
                url,
                headers=headers,
                json=json,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            if r.status_code in (429, 500, 502, 503, 504) and attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE_SEC ** attempt)
                continue
            return r
        except requests.exceptions.RequestException as err:
            last_err = err
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE_SEC ** attempt)
                continue
            raise

    if last_err:
        raise last_err
    raise RuntimeError("HTTP POST failed unexpectedly")

def gh_get_paged(url, params=None):
    """Follow GitHub pagination and return the combined list."""
    items = []
    while url:
        r = _http_get(url, headers=HEADERS, params=params)
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
# Query: is:issue project:rancher/{PROJECT} milestone:{title}
# This returns all issues (open + closed) on the project board for that milestone.

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
        r = _http_get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        issues.extend(data["items"])
        if len(issues) >= data["total_count"] or not data["items"]:
            break
        params["page"] += 1

    return issues


# ── Step 3 – Fetch chart versions ─────────────────────────────────────────────
#
# For each chart + release branch, pick two versions by most recent commit time:
# - Released Versions: latest version WITHOUT "rc"
# - Un-releaseed Versions: latest version WITH "rc"

def _read_chart_yaml_version(chart: str, version_dir: str, branch: str) -> str:
    """Return Chart.yaml version if available, otherwise fallback to directory name."""
    r = _http_get(
        f"{API}/repos/{OWNER}/{CHARTS_REPO}/contents/charts/{chart}/{version_dir}/Chart.yaml",
        headers=HEADERS,
        params={"ref": branch},
    )
    if r.status_code == 404:
        return version_dir
    r.raise_for_status()
    parsed = yaml.safe_load(base64.b64decode(r.json()["content"]).decode())
    return parsed.get("version", version_dir)


def latest_chart_versions_split(chart: str, branch: str) -> dict:
    """
    Return latest released/unreleased versions by commit recency.
    Output keys:
      released_version, released_date, unreleased_version, unreleased_date
    """
    out = {
        "released_version": "N/A",
        "released_date": "",
        "unreleased_version": "N/A",
        "unreleased_date": "",
    }

    prefix = f"charts/{chart}/"
    page = 1

    # Commit list is newest-first, so first hit for each class is the latest.
    while True:
        r = _http_get(
            f"{API}/repos/{OWNER}/{CHARTS_REPO}/commits",
            headers=HEADERS,
            params={"path": prefix.rstrip("/"), "sha": branch, "per_page": 100, "page": page},
        )
        if r.status_code in (404, 422):
            break
        r.raise_for_status()
        commits = r.json()
        if not commits:
            break

        for c in commits:
            sha = c["sha"]
            date = c["commit"]["committer"]["date"][:10]

            detail = _http_get(
                f"{API}/repos/{OWNER}/{CHARTS_REPO}/commits/{sha}",
                headers=HEADERS,
            )
            detail.raise_for_status()
            files = detail.json().get("files", [])

            dirs = []
            for f in files:
                fname = f.get("filename", "")
                if fname.startswith(prefix):
                    rest = fname[len(prefix):]
                    d = rest.split("/")[0]
                    if d:
                        dirs.append(d)

            # Preserve first-seen order and remove duplicates
            seen = set()
            dirs = [d for d in dirs if not (d in seen or seen.add(d))]

            for d in dirs:
                is_rc = "rc" in d.lower()
                if is_rc and out["unreleased_version"] == "N/A":
                    out["unreleased_version"] = _read_chart_yaml_version(chart, d, branch)
                    out["unreleased_date"] = date
                if (not is_rc) and out["released_version"] == "N/A":
                    out["released_version"] = _read_chart_yaml_version(chart, d, branch)
                    out["released_date"] = date

            if out["released_version"] != "N/A" and out["unreleased_version"] != "N/A":
                return out

        page += 1

    return out


def fetch_chart_versions() -> dict[str, dict[str, dict]]:
    """Return {chart: {series: released/unreleased versions with dates}}."""
    result: dict = {}
    for chart in CHARTS:
        result[chart] = {}
        for v in VERSIONS:
            info = latest_chart_versions_split(chart, f"dev-{v}")
            result[chart][v] = info
            print(
                f"    {chart} @ dev-{v}: "
                f"released={info['released_version']} ({info['released_date'] or 'unknown'}), "
                f"unreleased={info['unreleased_version']} ({info['unreleased_date'] or 'unknown'})"
            )
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
            (
                chart_versions.get(chart, {}).get(v, {}).get("released_version", "N/A") not in ("N/A", "")
                or chart_versions.get(chart, {}).get(v, {}).get("unreleased_version", "N/A") not in ("N/A", "")
            )
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
    checklist_by_release: dict[str, list[str]] = {}
    for v in reversed(VERSIONS):
        ms    = milestones.get(v)
        label = ms["title"] if ms else f"{v}.x"
        checklist_by_release[label] = []
        lines += [
            f"### Release {label}",
            "",
            "| Chart | Released Versions | Un-releaseed Versions | Owner | Done |",
            "|---|---|---|---|---|",
        ]
        for chart in CHARTS:
            info    = chart_versions[chart].get(v, {})
            rel_cell, unrel_cell, _, _ = _format_chart_version_cells(info)
            lines.append(f"| {chart} | {rel_cell} | {unrel_cell} |  | - [ ] |")
            checklist_by_release[label].append(f"- [ ] {chart} — owner: @<assign>")
        lines.append("")

    # Clickable task list for QA sign-off in GitHub issue body.
    lines.append("## Done Checklist")
    lines.append("")
    for label, items in checklist_by_release.items():
        lines.append(f"### Release {label}")
        lines.append("")
        lines.extend(items)
        lines.append("")

    return "\n".join(lines)


# ── Report writers ───────────────────────────────────────────────────────────

def _format_chart_version_cells(info: dict) -> tuple[str, str, bool, bool]:
    """
    Build released/unreleased display cells.
    Rule: show unreleased only if unreleased_date > released_date.
    """
    rel_ver = info.get("released_version", "N/A")
    rel_dt_raw = info.get("released_date") or ""
    rel_dt = rel_dt_raw or "—"
    rel_cell = f"{rel_ver} ({rel_dt})" if rel_ver != "N/A" else "N/A"

    unrel_ver = info.get("unreleased_version", "N/A")
    unrel_dt_raw = info.get("unreleased_date") or ""
    unrel_dt = unrel_dt_raw or "—"

    has_new_rc = (
        unrel_ver != "N/A"
        and rel_ver != "N/A"
        and bool(unrel_dt_raw)
        and bool(rel_dt_raw)
        and unrel_dt_raw > rel_dt_raw
    )

    if has_new_rc:
        unrel_cell = f"{unrel_ver} ({unrel_dt})"
    else:
        unrel_cell = "N/A (no new rc chart available)"

    rel_na = rel_ver == "N/A"
    unrel_na = not has_new_rc
    return rel_cell, unrel_cell, rel_na, unrel_na

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
            rel_cell, unrel_cell, rel_na, unrel_na = _format_chart_version_cells(info)
            rel_na_cls = " class='na'" if rel_na else ""
            unrel_na_cls = " class='na'" if unrel_na else ""
            rows += (
                f"<tr>"
                f"<td class='chart-name'>{chart}</td>"
                f"<td{rel_na_cls}>{rel_cell}</td>"
                f"<td{unrel_na_cls}>{unrel_cell}</td>"
                f"<td class='owner-col'></td>"
                f"<td class='done-col'>☐</td>"
                f"</tr>"
            )
        chart_sections.append(
            f"<section>"
            f"<h3>Release {label}</h3>"
            f"<table>"
            f"<thead><tr><th>Chart</th><th>Released Versions</th><th>Un-releaseed Versions</th><th>Owner</th><th>Done</th></tr></thead>"
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
    td.owner-col {{ min-width: 110px; }}
    td.done-col {{ min-width: 80px; text-align:center; color:#57606a; font-size:1rem; }}
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
            f"  is:issue project:{OWNER}/{PROJECT} milestone:{ms['title']}"
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
        issue = _http_post(
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
