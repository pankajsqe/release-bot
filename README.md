# Release Bot

Automates release tracking ticket creation for Rancher observability work.

The bot:
- Finds latest milestones for `v2.11` to `v2.15` in `rancher/rancher`
- Pulls project issues from GitHub project filter: `project:rancher/<number>`
- Builds release tables for chart versions from `rancher/charts`
- Splits chart versions into:
  - `Released Versions` (non-rc)
  - `Un-releaseed Versions` (rc, only shown when rc commit date is newer)
- Generates:
  - `report.md`
  - `report.html`
- Creates one issue in `rancher/observability-e2e` when dry-run is disabled

## Repository Structure

- `bot.py` - main bot logic
- `.github/workflows/release-bot.yml` - manual GitHub Actions workflow
- `requirements.txt` - Python dependencies
- `.env` - local runtime variables (not committed)

## Requirements

- Python 3.12+ recommended
- A GitHub token with:
  - `repo`
  - `read:org`

## Local Setup

1. Create virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `.env` in project root:

```env
GH_TOKEN=ghp_xxx
ACTOR=your-github-username
PROJECT_NUMBER=githubprojectnumber
DRY_RUN=true
```

4. Run:

```bash
set -a && source .env && set +a && python3 bot.py
```

## GitHub Actions Usage

Workflow: `Release Bot`

Trigger:
- `workflow_dispatch`
- Inputs:
  - `project_number` (required)
  - `dry_run` (default: `true`)

The workflow uses secret:
- `RANCHER_BOT_TOKEN` (mapped to `GH_TOKEN`)

Add this in repository settings:
- `Settings -> Secrets and variables -> Actions -> New repository secret`

## Dry Run vs Live Run

- `dry_run=true`
  - No issue is created
  - Reports are generated
  - Full output printed in logs

- `dry_run=false`
  - Creates issue in `your given project repo`

## Output Format

### Summary
- Milestone
- Open
- Closed
- Total

### Milestone Issues
- Issue lists by milestone with state markers

### Chart Versions (per release)
Columns:
- Chart
- Released Versions
- Un-releaseed Versions
- Owner
- Done

Notes:
- Table-cell checkbox text (`- [ ]`) is visual in GitHub tables.
- Clickable task checkboxes are provided in the separate `Done Checklist` section.

## Troubleshooting

### `422 Unprocessable Entity` on search/issues
Usually token scope/visibility issue.

Check:
- Token has `read:org`
- Token user can access `project:repo/<PROJECT_NUMBER>`

### `RemoteDisconnected` or transient network failure
The bot includes retry + backoff + timeout for API calls. Re-run if GitHub is unstable.


## Safety

Recommended default is `DRY_RUN=true` for manual runs.
