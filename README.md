# Job Search Agent

An automated job search and application assistant. Fetches jobs from Greenhouse and Lever, scores them, and auto-fills application forms using deterministic mapping + Claude AI for ambiguous fields.

---

## Architecture

```
Job Sources (Greenhouse / Lever)
        |
        v
  fetch_and_store()          # core/fetcher.py
        |
        v
  Score + Compliance Gate   # core/scoring.py + core/compliance.py
        |
        v
  SQLite Queue (jobs.db)     # core/models.py
        |
        v
  FastAPI UI + REST API      # apps/api/main.py
        |
        v (user approves job)
  apply_to_job()             # core/applier.py
        |
        v
  discover_application_surface()
        |
        v
  ai_fill_form()             # core/ai_filler.py
     |       |
     v       v
  DET     LLM Phase 1+2
  (question_map)  (Claude CLI)
     |       |
     v       v
  Validate + Merge actions
        |
        v
  Execute via interaction.py
        |
        v
  ApplyResult (structured)   # core/outcome.py
        |
        v
  Debug Artifacts            # /tmp/apply_debug/{job_id}/
```

---

## Auto-Apply System

### How it works

1. The applier opens the job URL in a Playwright browser (headless on Linux/CI, visible on macOS).
2. It discovers the application form (handles ATSes like Greenhouse, Lever, Workday).
3. The form-fill loop runs up to 8 attempts per application.
4. Each attempt: extract fields -> classify page -> deterministic mapping -> LLM for gaps -> validate -> execute -> navigate.
5. If unresolved required fields remain, the apply is aborted (not submitted).
6. On success, `ApplyResult(success=True)` is returned and the job is marked "applied".

### Phase 1: Deterministic field mapping

`core/question_map.py` contains a CATALOG of ~40+ canonical field types (name, email, phone, work authorization, EEOC, etc.). Each field on the form is scored against the catalog using keyword + alias matching.

Fields with confidence >= 0.75 are filled from your `profile.json` without involving the LLM. This covers 80-90% of fields on typical job applications.

### Phase 2: LLM analysis (only for ambiguous fields)

For fields the deterministic system cannot confidently map, two LLM calls are made (via the local `claude` CLI):
- Phase 1: Analyze the form HTML and produce a structured field inventory.
- Phase 2: Generate fill actions for the unresolved fields only.

LLM actions are validated before execution: bad selectors, missing files, and options not in the extracted option list are rejected.

### Phase 3: Action execution with verification

`core/interaction.py` provides verified primitives:
- `fill_field`: fills text inputs, verifies readback
- `select_option`: handles native `<select>` with normalised matching
- `interact_combobox`: React-Select / custom combobox with scoped option discovery, synonym matching, and readback verification. Never picks the first option blindly.
- `check_radio`: matches against LABEL TEXT (not value attribute), verifies `.checked` state
- `toggle_checkbox`: auto-checks consent/terms boxes, skips marketing/newsletter boxes
- `upload_file`: checks file exists before uploading

---

## Setup

### Requirements

- Python 3.11+
- Node.js (for Playwright browser binaries)
- Claude CLI installed at `~/.local/bin/claude` (for LLM-assisted filling)

### Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

### Profile configuration

Navigate to `http://localhost:8000/setup` after starting the server, or create `profile.json` manually in the project root:

```json
{
  "first_name": "Your",
  "last_name": "Name",
  "email": "you@example.com",
  "phone": "5551234567",
  "phone_country_code": "+1",
  "city": "Seattle",
  "state": "WA",
  "country": "United States",
  "zip": "98101",
  "location": "Seattle, WA",
  "address": "123 Main St",
  "current_company": "Acme Corp",
  "current_title": "Senior Engineer",
  "years_experience": "5",
  "linkedin": "https://www.linkedin.com/in/yourprofile",
  "work_authorized": "Yes",
  "requires_sponsorship": "No",
  "resume_path": "/absolute/path/to/your/resume.pdf",
  "education_degree": "Master of Science",
  "education_field": "Computer Science",
  "education_school": "University of Washington",
  "education_year": "2019",
  "gender": "Male",
  "disability": "No",
  "veteran": "I am not a protected veteran",
  "ethnicity": "Asian",
  "race": "Asian",
  "referral_source": "Job board",
  "salary_min": "150000",
  "salary_max": "200000",
  "salary_range": "$150,000 - $200,000",
  "willing_to_relocate": "No",
  "start_date": "Immediately"
}
```

### Session persistence / storage_state

To stay logged in across apply attempts (avoiding repeated login prompts):

1. Log into the job site manually in Playwright and save the storage state:
   ```python
   from playwright.sync_api import sync_playwright
   with sync_playwright() as p:
       browser = p.chromium.launch(headless=False)
       ctx = browser.new_context()
       page = ctx.new_page()
       page.goto("https://boards.greenhouse.io/...")
       # Log in manually
       input("Press Enter after logging in...")
       ctx.storage_state(path="greenhouse_session.json")
       browser.close()
   ```
2. Add to `profile.json`:
   ```json
   "storage_state_path": "/absolute/path/to/greenhouse_session.json"
   ```

### Resume and cover letter

- Set `resume_path` in `profile.json` to the absolute path of your resume PDF.
- Cover letters are generated automatically by Claude CLI per job. A fallback template is used if Claude is unavailable.

---

## Running

### Start server

```bash
uvicorn apps.api.main:app --reload --port 8000
```

### Fetch jobs

```bash
# Via API
curl -X POST http://localhost:8000/api/fetch

# Via scheduler (runs every hour automatically)
```

### Apply flow

1. Open `http://localhost:8000/queue`
2. Review scored jobs
3. Click "Approve" on jobs you want to apply to
4. Click "Apply" or use the API:
   ```bash
   curl -X POST http://localhost:8000/api/jobs/{id}/apply
   ```
5. Check status:
   ```bash
   curl http://localhost:8000/api/jobs/{id}/apply-status
   ```

---

## Failure Types and What They Mean

| FailureType | Meaning | Retryable | Action Needed |
|---|---|---|---|
| `missing_profile` | No profile.json found | No | Complete setup at /setup |
| `missing_resume` | Resume file not found | No | Fix `resume_path` in profile |
| `missing_claude` | Claude CLI not at ~/.local/bin/claude | No | Install Claude CLI |
| `browser_launch_failed` | Playwright couldn't launch Chromium | Yes | Check Playwright install |
| `login_required` | Login wall detected | No | Set up session persistence |
| `captcha_or_human_verification` | CAPTCHA detected | No | Manual solve required |
| `unresolved_required_fields` | Required fields couldn't be filled | No | Review debug artifacts |
| `unsupported_widget` | Unknown form widget type | No | Manual apply |
| `selector_resolution_failed` | CSS selector not found | Yes | Check debug HTML snapshot |
| `llm_analysis_failed` | Claude CLI failed for Phase 1 | Yes | Check Claude CLI |
| `llm_action_generation_failed` | Claude CLI failed for Phase 2 | Yes | Check Claude CLI |
| `repeated_validation_errors` | Same validation errors on retry | No | Review debug artifacts |
| `stuck_same_page_no_progress` | Loop on same page | No | Review debug artifacts |
| `cycling_between_steps` | Oscillating between 2 steps | No | Review debug artifacts |
| `submission_not_confirmed` | Submit clicked but no success page | Yes | Manual verify |
| `site_error` | ATS site error | Yes | Retry later |
| `timeout` | Navigation timeout | Yes | Retry later |

---

## Limitations

- **CAPTCHA**: Detected and surfaced as `captcha_or_human_verification`. Not bypassed.
- **OTP/Email verification**: Detected as login_required if encountered. Not handled automatically.
- **Login flows**: Require manual session setup via storage_state.
- **Workday**: Complex multi-step forms may require more LLM assist calls.
- **File uploads other than resume**: Not supported for arbitrary file types.
- **Phone country code dropdowns**: Handled for common patterns (intl-tel-input), but not all implementations.
- **Conditional fields**: Fields that appear/disappear based on other answers may confuse the loop.

---

## Debug Artifacts

### Location

`/tmp/apply_debug/{job_id}/{timestamp}_attempt{n}/`

### Contents

| File | Description |
|---|---|
| `manifest.json` | Summary: field count, action counts, failure info |
| `fields.json` | Full FieldMeta list with selector_candidates |
| `mappings.json` | Deterministic + LLM mappings with confidence scores |
| `actions_proposed.json` | LLM proposed actions (pre-validation) |
| `actions_rejected.json` | Rejected actions with rejection reasons |
| `actions_executed.json` | Executed actions with per-action verification results |
| `unresolved_fields.json` | Required fields that couldn't be filled before submit |
| `page_classifications.json` | Page type at each step |
| `state_fingerprints.json` | State fingerprints for loop detection |
| `states.json` | Full state tracker log |
| `failure.json` | Structured failure type + reason |
| `html_snapshot_step{n}.html` | HTML snapshot at each attempt (truncated to 200KB) |
| `*.png` | Screenshots: page_load, pre_fill, post_fill, pre_submit, post_submit, error |

### How to inspect

```bash
# List all attempts for a job
ls /tmp/apply_debug/42/

# Inspect the latest attempt
cat /tmp/apply_debug/42/*/manifest.json
cat /tmp/apply_debug/42/*/failure.json

# See what fields were found
cat /tmp/apply_debug/42/*/fields.json | jq '.[].label'

# See what the LLM proposed vs. what was rejected
cat /tmp/apply_debug/42/*/actions_rejected.json

# Open the HTML snapshot in a browser
open /tmp/apply_debug/42/*/html_snapshot_step1.html
```

---

## Config/Profile Keys Reference

| Key | Type | Default | Description |
|---|---|---|---|
| `first_name` | string | required | Legal first name |
| `last_name` | string | required | Legal last name |
| `email` | string | required | Contact email |
| `phone` | string | required | Phone digits only (no dashes) |
| `phone_country_code` | string | `+1` | Phone country code |
| `address` | string | | Street address |
| `city` | string | | City of residence |
| `state` | string | | State/province |
| `zip` | string | | ZIP/postal code |
| `country` | string | `United States` | Country |
| `location` | string | | City, State combined |
| `current_company` | string | | Current employer |
| `current_title` | string | | Current job title |
| `years_experience` | string | | Total years of experience |
| `linkedin` | string | | LinkedIn profile URL |
| `github` | string | | GitHub profile URL |
| `resume_path` | string | required | Absolute path to resume PDF |
| `work_authorized` | string | | `Yes`/`No` — authorized to work in US |
| `requires_sponsorship` | string | | `Yes`/`No` — needs visa sponsorship |
| `visa_status` | string | | Current visa (e.g. `H-1B`) |
| `salary_min` | string | | Minimum acceptable salary |
| `salary_max` | string | | Maximum salary expectation |
| `salary_range` | string | | Combined range string |
| `education_degree` | string | | Highest degree earned |
| `education_school` | string | | Institution name |
| `education_field` | string | | Field of study/major |
| `education_year` | string | | Graduation year |
| `gender` | string | | Gender identity |
| `disability` | string | `No` | Disability status |
| `veteran` | string | `I am not a protected veteran` | Veteran status |
| `race` | string | | Race/ethnicity |
| `ethnicity` | string | | Hispanic/Latino status |
| `transgender` | string | `No` | Transgender identity |
| `orientation` | string | | Sexual orientation |
| `willing_to_relocate` | string | | `Yes`/`No` |
| `start_date` | string | `Immediately` | Available start date |
| `referral_source` | string | `Job board` | How you heard about the job |
| `notice_period` | string | | Required notice period |
| `background_check` | string | `Yes` | Consent to background check |
| `security_clearance` | string | `No` | Current security clearance |
| `age_verification` | string | `Yes` | Are you 18 or older? |
| `country_of_citizenship` | string | | Country of citizenship |
| `work_location_preference` | string | | Remote/Hybrid/Onsite preference |
| `portfolio_url` | string | | Portfolio or personal website URL |
| `storage_state_path` | string | | Path to Playwright storage_state.json |
| `headless` | bool | `false` on macOS, `true` on Linux | Override browser headless mode |
| `slow_mo` | int | `0` | Playwright slowMo in milliseconds |
| `browser_user_agent` | string | Chrome 120 | Override browser user-agent |
| `viewport_width` | int | `1280` | Browser viewport width |
| `viewport_height` | int | `900` | Browser viewport height |

---

## Running Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test module
python -m pytest tests/test_outcome.py -v
python -m pytest tests/test_page_classifier.py -v
python -m pytest tests/test_interaction.py -v
python -m pytest tests/test_question_map.py -v
python -m pytest tests/test_form_state.py -v

# Quick smoke test
python -c "from core.ai_filler import ai_fill_form; from core.applier import apply_to_job; from core.outcome import ApplyResult, FailureType; print('imports ok')"
```

---

## Known Edge Cases

- **Same-fingerprint progress**: The state tracker now detects decreasing field counts and URL path changes as progress, preventing false "stuck" declarations on legitimate multi-step forms.
- **Combobox no-match**: If no trustworthy option match is found (threshold 0.8), the dropdown is closed and the field is left unresolved rather than picking the first option blindly.
- **Marketing checkboxes**: Checkboxes with labels containing "newsletter", "marketing", "product update" are automatically skipped. Terms/consent checkboxes are auto-checked.
- **Radio by label**: Radio button selection always matches against the visible label text, not the HTML value attribute, since label text is what users see and what profile values correspond to.
- **Greenhouse embed iframe**: Automatically detected and selected as the highest-scoring frame.
- **New tabs**: If clicking "Apply" opens a new browser tab, the applier follows it automatically.
