# GPS-OPX Codebase Knowledge Base

> This document serves as a reference for understanding the codebase, database structure, and implementation patterns. Use this to save context tokens in future conversations.

## Project Overview

**GPS Report Dashboard** - A Flask-based web application for GPS tracking reports from GPSWox/Traccar platform.

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | Flask 2.0+ |
| Database | MySQL (remote via SSH) |
| Frontend | Vanilla JS + Tailwind CSS |
| Export | pandas, openpyxl, reportlab |
| Deployment | Render.com (gunicorn) |

## File Structure

```
gps-opx/
├── app.py                    # Main Flask app (routes, queries, export)
├── config.py                 # Projects and report definitions
├── templates/
│   ├── index.html            # Dashboard UI (tabs: Reports, Cross-reference, SIM)
│   └── login.html            # Authentication page
├── unified_devices.json      # Merged device inventory data
├── device_project_mapping.json
├── sim_traffic_data.json     # SIM usage data
├── .env                      # SSH and DB credentials
└── gpswox-ssh-key.pem        # SSH key for DB access
```

## Database Architecture

### Connection Method
```python
# SSH tunnel to remote MySQL - uses paramiko + mysql CLI
SSHMySQLExecutor class in app.py
- connect() via SSH key
- execute() runs: sudo mysql -N -B {db_name} -e "{query}"
- fetchall() parses TSV output
```

### Two Databases

1. **gpswox_web** - Main application database
2. **gpswox_traccar** - Position history (per-device tables)

---

## Database Schema Reference

### gpswox_web.users
```sql
-- User accounts (projects)
id, email, name, ...
-- Key: project email (e.g., 'phase3-pkg8@wakecap.com')
```

### gpswox_web.devices
```sql
id              INT          -- Device ID (used for positions table)
imei            VARCHAR      -- 15-digit IMEI
name            VARCHAR      -- Device/vehicle name
device_model    VARCHAR      -- e.g., 'FMC130'
plate_number    VARCHAR
active          TINYINT      -- 1=active
deleted         TINYINT      -- 0=not deleted
updated_at      TIMESTAMP
```

### gpswox_web.user_device_pivot
```sql
-- Links users to devices
user_id         INT          -- FK to users.id
device_id       INT          -- FK to devices.id
group_id        INT          -- FK to device_groups.id
```

### gpswox_web.device_groups
```sql
id              INT
title           VARCHAR      -- 'HEAVY', 'LIGHT', 'BUS', etc.
```

### gpswox_web.traccar_devices
```sql
-- Real-time device state (latest data)
uniqueId        VARCHAR      -- IMEI (join with devices.imei)
speed           DOUBLE       -- Current speed km/h
latitude        DOUBLE
longitude       DOUBLE
address         TEXT         -- Reverse geocoded address
moved_at        TIMESTAMP    -- Last time started moving
stoped_at       TIMESTAMP    -- Last time stopped
engine_on_at    TIMESTAMP    -- Ignition on time
engine_off_at   TIMESTAMP    -- Ignition off time
updated_at      TIMESTAMP    -- Last position update
protocol        VARCHAR      -- 'teltonika'
```

### gpswox_web.events
```sql
-- Event log (alerts, violations)
id              INT
device_id       INT          -- FK to devices.id
type            VARCHAR      -- 'custom', 'speed', 'overspeed', etc.
message         VARCHAR      -- 'SOS', 'HarshBreaking', 'HarshAcceleration', etc. (camelCase)
speed           DOUBLE
latitude        DOUBLE
longitude       DOUBLE
geofence_id     INT          -- FK to geofences.id (nullable)
created_at      TIMESTAMP
deleted         TINYINT
```

### gpswox_web.geofences
```sql
id              INT
name            VARCHAR      -- Geofence name
```

### gpswox_traccar.positions_{device_id}
```sql
-- Historical positions (separate table per device!)
-- Table name format: positions_{devices.id}
id              BIGINT
altitude        DOUBLE
course          DOUBLE       -- Heading in degrees
latitude        DOUBLE
longitude       DOUBLE
speed           DOUBLE       -- km/h
time            DATETIME     -- GPS time
device_time     DATETIME
server_time     DATETIME
distance        DOUBLE       -- Distance from last position (km)
valid           TINYINT      -- 1=valid GPS fix
protocol        VARCHAR
other           TEXT         -- XML with sensor data
```

#### Position `other` XML structure
```xml
<info>
  <ignition>true</ignition>   <!-- Engine on/off -->
  <motion>true</motion>       <!-- Moving or stationary -->
  <speed>23</speed>
  <sat>12</sat>               <!-- Satellites -->
  <power>14.389</power>       <!-- External power voltage -->
  <battery>3.953</battery>    <!-- Internal battery -->
  <totaldistance>604727.07</totaldistance>  <!-- Odometer in km -->
  <enginehours>65310</enginehours>          <!-- Engine hours -->
  <!-- Teltonika I/O: di1=ignition, io69=driver ID present -->
</info>
```

---

## Trip State Logic

Based on position data, determine vehicle state:

| State | Condition |
|-------|-----------|
| **run** | `speed > threshold` (typically 2-5 km/h) AND `ignition=true` |
| **idle** | `speed <= threshold` AND `ignition=true` |
| **parked** | `ignition=false` (regardless of speed) |

### Trip Report Data Model

For each vehicle, generate time segments:
```
Start Time | Stop Time | Duration | Address | Distance | Avg Speed | Trip State | Vehicle Name
```

- **Duration**: Time difference between start and stop
- **Distance**: Sum of position.distance values during 'run' state
- **Avg Speed**: Total distance / duration (only for 'run')
- **Address**: Google Maps URL with lat/lng

### Summary Statistics (per vehicle)
```
Total duration trip:  HH:MM:SS
Total duration idle:  HH:MM:SS
Total distance:       XXX.XX km
```

---

## Query Patterns

### Get devices for a project
```sql
SELECT d.*, dg.title as group_name
FROM devices d
JOIN user_device_pivot udp ON d.id = udp.device_id
LEFT JOIN device_groups dg ON udp.group_id = dg.id
WHERE udp.user_id = {user_id} AND d.deleted = 0
```

### Get events with filters
```sql
SELECT e.*, d.name, dg.title
FROM events e
JOIN devices d ON e.device_id = d.id
JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
LEFT JOIN device_groups dg ON udp.group_id = dg.id
WHERE e.type = 'custom'
AND UPPER(e.message) LIKE '%FILTER%'
AND e.created_at BETWEEN '{start}' AND '{end} 23:59:59'
AND e.deleted = 0
```

### Get position history for trip report
```sql
-- Query per device (positions_{device_id} table)
SELECT
    id, time, speed, latitude, longitude, distance,
    EXTRACTVALUE(other, '//ignition') as ignition,
    EXTRACTVALUE(other, '//motion') as motion
FROM positions_{device_id}
WHERE time BETWEEN '{start_date}' AND '{end_date} 23:59:59'
ORDER BY time ASC
```

---

## Report Types

### Standard Reports (all projects)
| ID | Name | Query Pattern |
|----|------|---------------|
| 1 | Device List | `devices + traccar_devices` |
| 2 | Bus Overspeeding | `events WHERE type LIKE '%speed%' AND group='BUS'` |
| 3 | Heavy Overspeeding | `events WHERE type LIKE '%speed%' AND group='HEAVY'` |
| 4 | Light Overspeeding | `events WHERE type LIKE '%speed%' AND group='LIGHT'` |

### Phase 3 Package 8 Additional
| ID | Name | Query Pattern |
|----|------|---------------|
| 6 | Seatbelt Violation | `events WHERE message LIKE '%SEATBELT%'` |
| 7 | SOS Alert | `events WHERE message = 'SOS'` |
| 8 | Harsh Braking | `events WHERE message LIKE '%BRAK%'` |
| 9 | Harsh Acceleration | `events WHERE message LIKE '%ACCELERATION%'` |

### Trip Report (ID: 10)
| ID | Name | Description |
|----|------|-------------|
| 10 | Trip Report | Vehicle trip status with idle, run & parked segments |

**Trip Report Implementation Details:**

1. **Data Source**: Queries per-device `positions_{device_id}` tables in `gpswox_traccar` database
2. **Trip State Logic**:
   - `parked`: ignition=false (regardless of speed)
   - `idle`: speed <= 2 km/h AND ignition=true
   - `run`: speed > 2 km/h AND ignition=true

3. **Output Format** (matches GPSWox sample):
   - Global summary header (period, total vehicles, durations, distance)
   - Per-vehicle sections with:
     - Vehicle info header
     - Summary stats (trip time, idle time, distance)
     - Trip segments table (Start Time, Stop Time, Duration, Address, Distance, Avg Speed, State)

4. **Key Functions** in `app.py`:
   - `generate_trip_report_data(executor, user_id, start_date, end_date)` - Processes positions into segments
   - `export_trip_report_to_excel(trip_data, global_stats, start_date, end_date)` - Excel export
   - `export_trip_report_to_csv(trip_data, global_stats, start_date, end_date)` - CSV export
   - `format_duration(seconds)` - Formats seconds to D:HH:MM:SS
   - `point_in_polygon(lat, lng, polygon)` - Ray casting algorithm for geofence check
   - `load_geofences_for_user(executor, user_id)` - Loads and parses user's geofences
   - `find_geofence_for_point(lat, lng, geofences)` - Returns geofence name if point is inside

5. **Location Display Logic**:
   - If position is inside a geofence: shows geofence name
   - Otherwise: shows Google Maps link (Excel/CSV) or coordinates (preview/PDF)
   - Geofence types supported: polygon (point-in-polygon), circle (Haversine distance)

6. **Frontend Features**:
   - Trip summary cards showing: Vehicles, Total Distance, Run/Idle/Parked times
   - Color-coded state column: green (run), yellow (idle), gray (parked)

---

## API Endpoints

```
GET  /                           # Dashboard (requires login)
GET  /login, POST /login         # Authentication
GET  /logout                     # Clear session

GET  /api/reports/{email}        # Available reports for project
POST /api/preview                # Preview report data (paginated)
POST /api/generate               # Download report (CSV/Excel/PDF)

GET  /api/cross-reference        # Unified device inventory
GET  /api/sim-insight            # SIM data with traffic usage

GET  /api/debug/table/{name}     # Table structure (dev only)
```

---

## Frontend Tabs

1. **Reports** - Select project, report type, date range, export format
2. **Cross-reference** - Unified device inventory with filtering
3. **SIM Consolidated** - Summary by project and provider
4. **SIM Insight** - Detailed SIM data with traffic usage

---

## Configuration

### config.py structure
```python
PROJECTS = {
    "email@domain.com": {
        "id": 8,
        "name": "Display Name",
        "app_id": "app_8_Name"
    }
}

STANDARD_REPORTS = [
    {"id": 1, "name": "Report Name", "description": "..."}
]

REPORTS = {
    "email@domain.com": STANDARD_REPORTS,
    "special@domain.com": STANDARD_REPORTS + EXTRA_REPORTS
}
```

### Environment Variables (.env)
```
SSH_KEY=gpswox-ssh-key.pem
SSH_HOST=devops@34.166.43.194
DB_NAME=gpswox_web
DB_USER=root
DB_PASSWORD=
DB_HOST=127.0.0.1
DB_PORT=3306
```

---

## IMEI Matching Patterns

| Platform | Format | Matching |
|----------|--------|----------|
| FOTA | 15 digits | Exact |
| GPSWox | 15 digits | Exact |
| Mobily | 16 digits | First 14 |
| M2MI | 15 digits | Exact |

---

## Key Implementation Notes

1. **SSH-based DB access**: No direct MySQL connection, uses `sudo mysql` via SSH
2. **Per-device position tables**: `positions_{device_id}` in gpswox_traccar
3. **IMEI join**: `devices.imei COLLATE utf8_general_ci = traccar_devices.uniqueId`
4. **Date filtering**: Always use `BETWEEN 'start' AND 'end 23:59:59'`
5. **Delete safety**: Always check `deleted = 0` on devices, events

---

## Adding New Reports

1. Add report definition to `config.py` (STANDARD_REPORTS or project-specific)
2. Add query pattern in `app.py:generate_report_data()` with condition
3. Define columns list and SQL query
4. Frontend auto-populates from `/api/reports/{email}` endpoint

---

*Last updated: 2026-01-19*
