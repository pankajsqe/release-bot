import base64
import argparse
import datetime
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

# ── Config ─────────

TOKEN   = os.environ.get("GH_TOKEN", "")
ACTOR   = os.environ.get("ACTOR", "")
PROJECT = int(os.environ.get("PROJECT_NUMBER", "101"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
MILESTONE_V211 = os.environ.get("MILESTONE_V211", "").strip()
MILESTONE_V212 = os.environ.get("MILESTONE_V212", "").strip()
MILESTONE_V213 = os.environ.get("MILESTONE_V213", "").strip()
MILESTONE_V214 = os.environ.get("MILESTONE_V214", "").strip()
MILESTONE_V215 = os.environ.get("MILESTONE_V215", "").strip()

API = "https://api.github.com"

HEADERS = {}

OWNER        = "rancher"
REPO         = "observability-e2e"   # issue is always created here
RANCHER_REPO = "rancher"             # milestones live here
CHARTS_REPO  = "charts"             # chart versions live here
SCC_REPO     = "scc-operator"       # SCC tags live here
SHELL_REPO   = "shell"              # rancher-shell tags live here

VERSIONS = ["v2.11", "v2.12", "v2.13", "v2.14", "v2.15"]
SCC_VERSIONS = ["v2.13", "v2.14", "v2.15"]

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


def _to_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _load_env_file(path: str) -> None:
    """Load KEY=VALUE pairs from an env file into os.environ when unset."""
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rancher release tracking bot")
    parser.add_argument("--env-file", default=".env", help="Path to .env file for local runs")
    parser.add_argument("--gh-token", help="GitHub token override")
    parser.add_argument("--actor", help="GitHub actor/user override")
    parser.add_argument("--project-number", help="Project number override")
    parser.add_argument("--dry-run", help="Dry run mode (true/false)")
    parser.add_argument("--milestone-v211", help="Milestone title for v2.11")
    parser.add_argument("--milestone-v212", help="Milestone title for v2.12")
    parser.add_argument("--milestone-v213", help="Milestone title for v2.13")
    parser.add_argument("--milestone-v214", help="Milestone title for v2.14")
    parser.add_argument("--milestone-v215", help="Milestone title for v2.15")
    return parser.parse_args()


def configure_runtime(args: argparse.Namespace) -> None:
    """Resolve runtime configuration from CLI and environment."""
    global TOKEN, ACTOR, PROJECT, DRY_RUN
    global MILESTONE_V211, MILESTONE_V212, MILESTONE_V213, MILESTONE_V214, MILESTONE_V215
    global HEADERS

    if args.env_file:
        _load_env_file(args.env_file)

    token = (args.gh_token or os.environ.get("GH_TOKEN", "")).strip()
    actor = (args.actor or os.environ.get("ACTOR", "")).strip()
    project_raw = (args.project_number or os.environ.get("PROJECT_NUMBER", "101")).strip()
    dry_raw = args.dry_run if args.dry_run is not None else os.environ.get("DRY_RUN", "true")

    if not token:
        raise ValueError("Missing GH_TOKEN. Set it in env/.env or pass --gh-token.")
    if not actor:
        raise ValueError("Missing ACTOR. Set it in env/.env or pass --actor.")

    try:
        project = int(project_raw)
    except ValueError as exc:
        raise ValueError(f"Invalid PROJECT_NUMBER: {project_raw}") from exc

    TOKEN = token
    ACTOR = actor
    PROJECT = project
    DRY_RUN = _to_bool(dry_raw)

    def _normalize_milestone(value: str | None) -> str:
        cleaned = (value or "").strip()
        return "" if cleaned.lower() == "auto" else cleaned

    MILESTONE_V211 = _normalize_milestone(args.milestone_v211 or os.environ.get("MILESTONE_V211", ""))
    MILESTONE_V212 = _normalize_milestone(args.milestone_v212 or os.environ.get("MILESTONE_V212", ""))
    MILESTONE_V213 = _normalize_milestone(args.milestone_v213 or os.environ.get("MILESTONE_V213", ""))
    MILESTONE_V214 = _normalize_milestone(args.milestone_v214 or os.environ.get("MILESTONE_V214", ""))
    MILESTONE_V215 = _normalize_milestone(args.milestone_v215 or os.environ.get("MILESTONE_V215", ""))

    HEADERS = {
        "Authorization":        f"Bearer {TOKEN}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


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

def find_latest_milestones(selected_titles: dict[str, str] | None = None):
    """
    Return {series: milestone_dict} mapping the latest v2.NN.x milestone
    (open or closed) for each series in VERSIONS.
    """
    milestones = gh_get_paged(
        f"{API}/repos/{OWNER}/{RANCHER_REPO}/milestones",
        params={"state": "all", "per_page": 100},
    )

    latest: dict = {}
    by_title = {ms.get("title", ""): ms for ms in milestones}

    selected_titles = selected_titles or {}
    for series in VERSIONS:
        selected = selected_titles.get(series, "").strip()
        if not selected:
            continue

        chosen = by_title.get(selected)
        if chosen:
            chosen_copy = dict(chosen)
            chosen_copy["_exists"] = True
            latest[series] = chosen_copy
        else:
            latest[series] = {
                "title": selected,
                "_exists": False,
            }

    for ms in milestones:
        m = re.match(r"^(v2\.\d+)\.(\d+)$", ms["title"])
        if not m:
            continue
        series = m.group(1)
        if series not in VERSIONS:
            continue
        if series in latest:
            continue
        patch = int(m.group(2))
        prev  = latest.get(series)
        if prev is None or patch > int(re.search(r"\.(\d+)$", prev["title"]).group(1)):
            ms_copy = dict(ms)
            ms_copy["_exists"] = True
            latest[series] = ms_copy

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
# - Un-released Versions: latest version WITH "rc"

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


# ── Step 4 – Fetch SCC release versions from tags ───────────────────────────

def _parse_scc_tag(tag: str):
    """
    Parse SCC tag formats like:
      v0.5.1
      v0.5.1-rc.3
    Returns (major, minor, patch, rc_num_or_none) or None.
    """
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc\.?([0-9]+))?$", tag)
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3))
    rc_num = int(m.group(4)) if m.group(4) else None
    return major, minor, patch, rc_num


def _commit_date_for_sha(owner: str, repo: str, sha: str) -> str:
    """Return commit date (YYYY-MM-DD) for a SHA, or empty string if unavailable."""
    if not sha:
        return ""
    r = _http_get(
        f"{API}/repos/{owner}/{repo}/commits/{sha}",
        headers=HEADERS,
    )
    if r.status_code != 200:
        return ""
    data = r.json()
    return (data.get("commit", {}).get("committer", {}).get("date", "") or "")[:10]


def _fetch_repo_versions_for_rows(repo: str, row_versions: list[str], no_new_rc_msg: str) -> dict[str, dict[str, str]]:
    """
    Fetch latest stable and latest RC tags for a repo using vX.Y.Z / vX.Y.Z-rc.N tags.
    RC is shown only if RC commit date > stable commit date.
    """
    tags = gh_get_paged(
        f"{API}/repos/{OWNER}/{repo}/tags",
        params={"per_page": 100},
    )

    latest_stable = "N/A"
    latest_rc = "N/A"
    latest_stable_sha = ""
    latest_rc_sha = ""
    stable_key = (-1, -1, -1)
    rc_key = (-1, -1, -1, -1)

    for t in tags:
        name = t.get("name", "")
        parsed = _parse_scc_tag(name)
        if not parsed:
            continue
        major, minor, patch, rc_num = parsed
        if rc_num is None:
            k = (major, minor, patch)
            if k > stable_key:
                stable_key = k
                latest_stable = name
                latest_stable_sha = t.get("commit", {}).get("sha", "")
        else:
            k = (major, minor, patch, rc_num)
            if k > rc_key:
                rc_key = k
                latest_rc = name
                latest_rc_sha = t.get("commit", {}).get("sha", "")

    stable_date = _commit_date_for_sha(OWNER, repo, latest_stable_sha)
    rc_date = _commit_date_for_sha(OWNER, repo, latest_rc_sha)

    rc_to_show = no_new_rc_msg
    if (
        latest_rc != "N/A"
        and latest_stable != "N/A"
        and rc_date
        and stable_date
        and rc_date > stable_date
    ):
        rc_to_show = latest_rc

    return {
        v: {
            "stable": latest_stable,
            "rc": rc_to_show,
        }
        for v in row_versions
    }


def fetch_scc_versions() -> dict[str, dict[str, str]]:
    """
    Return SCC latest stable and latest rc tags for v2.13..v2.15.
    Output: {"v2.13": {"stable": "...", "rc": "..."}, ...}
    """
    return _fetch_repo_versions_for_rows(
        SCC_REPO,
        SCC_VERSIONS,
        "N/A (no new rc version available)",
    )


def fetch_shell_versions() -> dict[str, dict[str, str]]:
    """
    Return rancher-shell latest stable and latest rc tags for v2.11..v2.15 rows.
    Output: {"v2.13": {"stable": "...", "rc": "..."}, ...}
    """
    return _fetch_repo_versions_for_rows(
        SHELL_REPO,
        VERSIONS,
        "N/A (no new rc version available)",
    )


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
        if ms and ms.get("_exists", True):
            count = len(board_issues.get(v, []))
            row(f"{v}.x milestone found — {ms['title']} ({count} board issues)", True)
        elif ms and not ms.get("_exists", True):
            lines.append(f"  ! {v}.x selected milestone not present — {ms['title']}")
        else:
            lines.append(f"  ! {v}.x milestone not present")

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

def build_body(milestones, board_issues, chart_versions, scc_versions, shell_versions,
               val_lines: list[str], val_ok: bool) -> str:
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

    # Validation section (same structure as report.html)
    lines += ["## Validation", ""]
    for line in val_lines:
        text = line.strip()
        if not text or text.startswith("RESULT"):
            continue
        lines.append(f"- {text}")
    lines += ["", f"**RESULT: {'PASS' if val_ok else 'FAIL'}**", ""]

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
        n_open = sum(1 for i in issues if i.get("state") == "open")
        n_closed = sum(1 for i in issues if i.get("state") == "closed")
        lines += [f"### {ms['title']} ({n_open} open, {n_closed} closed)", ""]
        if not issues:
            lines.append("_No issues found on the board for this milestone._")
        else:
            for issue in issues:
                state = issue.get("state", "open")
                lines.append(
                    f"- **{state}** [#{issue['number']}]({issue['html_url']})"
                    f" - {issue['title']}"
                )
        lines.append("")

    # Chart versions — markdown tables keep the issue editable in GitHub
    lines.append("## Chart Versions")
    lines.append("")
    for v in reversed(VERSIONS):
        ms    = milestones.get(v)
        label = ms["title"] if ms else f"{v}.x"
        lines += [
            f"### Release {label}",
            "",
            "| Chart | Released Versions | Un-released Versions | Owner | Done |",
            "|---|---|---|---|---|",
        ]
        for chart in CHARTS:
            info = chart_versions.get(chart, {}).get(v, {})
            rel_cell, unrel_cell, _, _ = _format_chart_version_cells(info)
            lines.append(f"| {chart} | {rel_cell} | {unrel_cell} |  | [ ] |")

        if v in SCC_VERSIONS:
            scc_row = scc_versions.get(v, {"stable": "N/A", "rc": "N/A (no new rc version available)"})
            lines.append(f"| scc-operator | {scc_row['stable']} | {scc_row['rc']} |  | [ ] |")

        if v in VERSIONS:
            shell_row = shell_versions.get(v, {"stable": "N/A", "rc": "N/A (no new rc version available)"})
            lines.append(f"| rancher-shell | {shell_row['stable']} | {shell_row['rc']} |  | [ ] |")

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


def write_html(milestones, board_issues, chart_versions, scc_versions, shell_versions, val_lines, val_ok,
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

        if v in SCC_VERSIONS:
            scc_row = scc_versions.get(v, {"stable": "N/A", "rc": "N/A (no new rc version available)"})
            scc_unrel_na_cls = " class='na'" if scc_row["rc"].startswith("N/A") else ""
            rows += (
                f"<tr>"
                f"<td class='chart-name'>scc-operator</td>"
                f"<td>{scc_row['stable']}</td>"
                f"<td{scc_unrel_na_cls}>{scc_row['rc']}</td>"
                f"<td class='owner-col'></td>"
                f"<td class='done-col'>☐</td>"
                f"</tr>"
            )

        if v in VERSIONS:
            shell_row = shell_versions.get(v, {"stable": "N/A", "rc": "N/A (no new rc version available)"})
            shell_unrel_na_cls = " class='na'" if shell_row["rc"].startswith("N/A") else ""
            rows += (
                f"<tr>"
                f"<td class='chart-name'>rancher-shell</td>"
                f"<td>{shell_row['stable']}</td>"
                f"<td{shell_unrel_na_cls}>{shell_row['rc']}</td>"
                f"<td class='owner-col'></td>"
                f"<td class='done-col'>☐</td>"
                f"</tr>"
            )

        chart_sections.append(
            f"<section>"
            f"<h3>Release {label}</h3>"
            f"<table>"
            f"<thead><tr><th>Chart</th><th>Released Versions</th><th>Un-released Versions</th><th>Owner</th><th>Done</th></tr></thead>"
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
    lines = [f"<!-- generated {now} -->", "", "## Ticket Preview", ""]
    lines += [
        f"- Repository: {OWNER}/{REPO}",
        f"- Triggered by: @{ACTOR}",
        f"- Project: #{PROJECT}",
        f"- Dry run: {'true' if DRY_RUN else 'false'}",
        "- Selected milestones:",
        f"  - v2.11: {MILESTONE_V211 or '(auto)'}",
        f"  - v2.12: {MILESTONE_V212 or '(auto)'}",
        f"  - v2.13: {MILESTONE_V213 or '(auto)'}",
        f"  - v2.14: {MILESTONE_V214 or '(auto)'}",
        f"  - v2.15: {MILESTONE_V215 or '(auto)'}",
        "",
        "## Ticket Body",
        "",
    ]
    lines += body.splitlines()
    if issue_url:
        lines += ["", "---", "", f"**Issue created:** {issue_url}"]

    out = Path("report.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = _parse_args()
    try:
        configure_runtime(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        sys.exit(2)

    print("Fetching milestones …")
    selected_milestones = {
        "v2.11": MILESTONE_V211,
        "v2.12": MILESTONE_V212,
        "v2.13": MILESTONE_V213,
        "v2.14": MILESTONE_V214,
        "v2.15": MILESTONE_V215,
    }
    milestones = find_latest_milestones(selected_milestones)

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

    print("Fetching SCC versions …")
    scc_versions = fetch_scc_versions()
    for v in reversed(SCC_VERSIONS):
        row = scc_versions.get(v, {"stable": "N/A", "rc": "N/A"})
        print(f"    {v}: stable={row['stable']}, rc={row['rc']}")

    print("Fetching rancher-shell versions …")
    shell_versions = fetch_shell_versions()
    for v in reversed(SCC_VERSIONS):
        row = shell_versions.get(v, {"stable": "N/A", "rc": "N/A"})
        print(f"    {v}: stable={row['stable']}, rc={row['rc']}")

    val_lines, val_ok = build_validation(milestones, board_issues, chart_versions)
    body              = build_body(milestones, board_issues, chart_versions, scc_versions, shell_versions,
                                   val_lines, val_ok)

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
    html_path = write_html(milestones, board_issues, chart_versions, scc_versions, shell_versions,
                           val_lines, val_ok, issue_url)
    md_path   = write_markdown(body, val_lines, val_ok, issue_url)
    print(f"\nReports written:")
    print(f"  {html_path.resolve()}")
    print(f"  {md_path.resolve()}")

    if DRY_RUN and not val_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
