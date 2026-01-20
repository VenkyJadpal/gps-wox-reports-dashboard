# Changelog

## 2026-01-20

### .gitignore Updates

**Added:**
- `.claude/` - Claude Code configuration directory

**Commented (optional to enable):**
- Large data files: `unified_devices.json`, `device_project_mapping.json`, `sim_traffic_data.json`
- Generated HTML files

**Warning:** The following sensitive files are already tracked in git history:
- `.env` - environment variables with credentials
- `gpswox-ssh-key.pem` - SSH private key
- `__pycache__/` - compiled Python files

To remove from tracking (keeps local files): `git rm --cached <file>`

---

### Date Picker Improvements (templates/index.html)

**Changes made:**
1. **End date cannot be in the future**: Added `max` attribute to the end date input, set dynamically to today's date
2. **Start date must be <= End date**:
   - Added event listeners on both date inputs
   - When end date changes, start date's max is updated to match end date
   - When start date changes, end date's min is updated to match start date
   - Auto-corrects invalid selections (if start > end, adjusts the changed field)

**Code locations:**
- HTML input: Line ~193 (`end_date` input with `max=""` attribute)
- JavaScript: Lines ~930-958 (date initialization and validation logic)

**Behavior:**
- Default: Last 7 days (start = today - 7, end = today)
- End date is capped at today (no future dates)
- Start date picker disables dates after the selected end date
- End date picker disables dates before the selected start date
