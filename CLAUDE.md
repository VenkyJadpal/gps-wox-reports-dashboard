# CLAUDE.md - Project Context & Analysis Guide

## Project Overview

This repository contains research and operational tools for GPS tracking platform migration from GPSGate to GPSWox/Traccar, including device inventory management across multiple platforms.

## Key Platforms & Data Sources

### 1. FOTA (Teltonika Firmware Over The Air)
- **Purpose:** Device firmware management, configuration deployment, inventory tracking
- **Data:** 4,194 Teltonika FMC130/FMC920 GPS trackers
- **API Base:** `https://api.teltonika.lt`
- **Auth:** Bearer token

```bash
# Example: List all devices
curl -s -H "Authorization: Bearer YOUR_FOTA_API_TOKEN" \
  -H "Accept: application/json" \
  "https://api.teltonika.lt/devices?per_page=100&page=1"
```

### 2. GPSWox (GPS Tracking Platform)
- **Purpose:** Real-time GPS tracking, fleet management
- **Data:** 2,496 active devices
- **Access:** MySQL database on GCP VM
- **Databases:** `gpswox_web`, `gpswox_traccar`

```bash
# SSH to GPSWox server and query database
gcloud compute ssh gpswox-machine --zone=me-central2-b --quiet --command="
  mysql -u root -p'YOUR_DB_PASSWORD' gpswox_traccar -e 'SELECT * FROM tc_devices'
"
```

### 3. Mobily SIM Provider (Cisco Jasper)
- **Purpose:** SIM card management for Saudi Arabia
- **Data:** 193 SIMs
- **API Base:** `https://restapi8.jasper.com/rws/api/v1/`
- **Auth:** Basic Auth (username:api_key)

```bash
# Example: Get device details
curl -s -H "Authorization: Basic $(echo -n 'USERNAME:API_KEY' | base64)" \
  -H "Accept: application/json" \
  "https://restapi8.jasper.com/rws/api/v1/devices/ICCID"
```

### 4. M2MI SIM Provider (KORE Wireless)
- **Purpose:** SIM card management (Netherlands KPN roaming)
- **Data:** 3,500 SIMs
- **Access:** Web portal with TOTP 2FA (export via UI)
- **Portal:** SIM Insight Portal

## Data Patterns & Matching Logic

### IMEI Matching
| Platform | IMEI Format | Matching Strategy |
|----------|-------------|-------------------|
| FOTA | 15 digits | Exact match |
| GPSWox | 15 digits | Exact match |
| Mobily | 16 digits (padded) | **First 14 digits** |
| M2MI | 15 digits | Exact match |

```python
# Mobily uses 16-digit IMEIs with trailing padding
# Match on first 14 digits (TAC + Serial without check digit)
mobily_imei = "8637190615711800"  # 16 digits
fota_imei = "863719061571189"    # 15 digits
match = mobily_imei[:14] == fota_imei[:14]  # True
```

### ICCID Patterns (SIM Provider Identification)
```python
def get_sim_provider(iccid):
    if iccid.startswith('899660') or iccid.startswith('8996601'):
        return 'Mobily (Saudi)'
    elif iccid.startswith('8931084') or iccid.startswith('8931085'):
        return 'M2MI/KPN (Netherlands)'
    elif iccid.startswith('899664'):
        return 'STC (Saudi)'
    elif iccid.startswith('899665'):
        return 'Zain (Saudi)'
    else:
        return 'Unknown'
```

## Analysis Scripts

### Cross-Reference Analysis
```python
# Load all data sources
fota_devices = load_json('/tmp/fota_all_devices.json')
gpswox_devices = load_tsv('/tmp/gpswox_devices.tsv')
mobily_sims = load_json('/tmp/mobily_detailed_devices.json')
m2mi_sims = load_json('/tmp/m2mi_all_sims.json')

# Build indexes
fota_by_imei = {str(d['imei']): d for d in fota_devices}
gpswox_by_imei = {str(d['imei']): d for d in gpswox_devices}
mobily_by_imei14 = {str(d['imei'])[:14]: d for d in mobily_sims if d.get('imei')}
m2mi_by_imei = {str(int(d['IMEI'])): d for d in m2mi_sims if d.get('IMEI')}

# Cross-reference
in_both = set(fota_by_imei.keys()) & set(gpswox_by_imei.keys())
fota_only = set(fota_by_imei.keys()) - set(gpswox_by_imei.keys())
gpswox_only = set(gpswox_by_imei.keys()) - set(fota_by_imei.keys())
```

## Key Files

| File | Purpose |
|------|---------|
| `unified_device_report.html` | Interactive cross-reference report (open in browser) |
| `UNIFIED_CROSS_REFERENCE_REPORT.md` | Summary statistics and action items |
| `unified_device_model.py` | Python data model for unified device records |
| `GPSWOX_INFRASTRUCTURE_REVIEW.md` | GPSWox server analysis |
| `AVL_TO_GPSWOX_MIGRATION_SUMMARY.md` | Migration documentation |

## Lessons Learned

### 1. IMEI Format Variations
Different platforms store IMEIs differently:
- Standard IMEI: 15 digits (TAC + Serial + Check)
- Some platforms: 14 digits (no check digit)
- Mobily: 16 digits (extra padding)
- **Solution:** Match on first 14 digits for maximum compatibility

### 2. ICCID Length Variations
- Standard ICCID: 19-20 digits
- Some providers: 18-19 digits
- **Solution:** Use prefix matching or truncate to shortest common length

### 3. API Pagination
FOTA API uses pagination - must iterate through all pages:
```python
all_devices = []
page = 1
while True:
    response = fetch_page(page, per_page=100)
    if not response['data']:
        break
    all_devices.extend(response['data'])
    page += 1
```

### 4. Rate Limiting
- Mobily API: Add 0.5s delay between requests
- FOTA API: 100 requests/minute limit
- **Solution:** Batch requests, add delays, cache results

### 5. Data Export Strategy
For portals without API (M2MI):
1. Use browser automation or manual export
2. Save as JSON/CSV for processing
3. Parse numeric fields carefully (ICCID/IMEI as strings)

## Re-Running Analysis

### Prerequisites
1. API credentials for FOTA, Mobily (see `~/.claude/rules/teltonika-apis.md`)
2. SSH access to GPSWox GCP VM
3. M2MI portal access with TOTP authenticator
4. Python 3.x with standard library

### Steps
```bash
# 1. Fetch FOTA devices
python3 /tmp/fetch_fota_devices.py

# 2. Export GPSWox devices
gcloud compute ssh gpswox-machine --zone=me-central2-b --command="
  mysql -u root -pPASSWORD gpswox_traccar -e 'SELECT * FROM tc_devices' > /tmp/gpswox.tsv
"
gcloud compute scp gpswox-machine:/tmp/gpswox.tsv /tmp/gpswox_devices.tsv

# 3. Fetch Mobily SIMs (API)
python3 /tmp/fetch_mobily_detailed.py

# 4. Export M2MI SIMs (manual from portal)
# Login → SIM Finder → Export All → Save as m2mi_all_sims.json

# 5. Run cross-reference analysis
python3 /tmp/complete_cross_reference.py

# 6. Generate HTML report
python3 /tmp/generate_html_report.py
```

## Statistics Snapshot (2026-01-17)

```
Platform Totals:
├── FOTA:    4,194 devices
├── GPSWox:  2,496 devices
├── Mobily:    193 SIMs
└── M2MI:    3,500 SIMs

Cross-Reference:
├── FOTA ↔ GPSWox: 2,433 matched
├── FOTA only:     1,664 (need GPSWox import)
├── GPSWox only:      63 (not in FOTA)
└── No ICCID:        754 (undeployed stock)

SIM Provider Distribution:
├── M2MI/KPN:      2,656 (63%)
├── Mobily:          830 (20%)
├── No ICCID:        754 (18%)
└── STC:               5 (<1%)
```

## Common Tasks

### Find device by IMEI
```bash
# In FOTA data
jq '.[] | select(.imei == 863540061694924)' /tmp/fota_all_devices.json

# In GPSWox
grep "863540061694924" /tmp/gpswox_devices.tsv
```

### Check device online status
```python
from datetime import datetime, timedelta

def is_online(seen_at):
    if not seen_at:
        return False
    seen = datetime.strptime(seen_at[:19], '%Y-%m-%d %H:%M:%S')
    return (datetime.now() - seen) < timedelta(hours=24)
```

### Export devices needing action
```python
# Devices in FOTA but not GPSWox (need import)
action_needed = [d for d in fota_devices
                 if str(d['imei']) not in gpswox_by_imei
                 and d.get('seen_at')]
```
