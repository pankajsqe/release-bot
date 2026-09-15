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
MILESTONE_V215=v2.15.2
MILESTONE_V214=v2.14.6
MILESTONE_V213=v2.13.10
MILESTONE_V212=v2.12.14
MILESTONE_V211=v2.11.18
```

You can also set any milestone value to `auto` in `.env` to let the bot pick
the newest milestone automatically for that series.

4. Run with shell-exported env:

```bash
set -a && source .env && set +a && python3 bot.py
```

5. Or run directly with `.env` file loading (no `source` needed):

```bash
python3 bot.py --env-file .env
```

6. Optional CLI overrides (CLI takes precedence over `.env`/env vars):

```bash
python3 bot.py \
  --env-file .env \
  --project-number 101 \
  --dry-run true \
  --milestone-v215 v2.15.2 \
  --milestone-v214 v2.14.6 \
  --milestone-v213 v2.13.10 \
  --milestone-v212 v2.12.14 \
  --milestone-v211 v2.11.18
```

## GitHub Actions Usage

Workflow: `Release Bot`

Trigger:
- `workflow_dispatch`
- Inputs:
  - `project_number` (required)
  - `dry_run` (default: `true`)
  - `milestone_v215` (text, default: `auto`)
  - `milestone_v214` (text, default: `auto`)
  - `milestone_v213` (text, default: `auto`)
  - `milestone_v212` (text, default: `auto`)
  - `milestone_v211` (text, default: `auto`)

`auto` means the bot will resolve the latest milestone in that series at run
time. If you pick a specific value, that exact milestone is used.

Accepted values in the text box:
- `auto`
- A full milestone string like `v2.15.3`, `v2.14.7`, `v2.13.11`, `v2.12.18`, or `v2.11.18`

The workflow uses secret:
- `RANCHER_BOT_TOKEN` (mapped to `GH_TOKEN`)

After the run completes, the workflow uploads two artifacts:
- `release-bot-report-html` for `report.html`
- `release-bot-report-md` for `report.md`

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
- Checkbox entries are rendered in the `Done` table column for each row.

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
