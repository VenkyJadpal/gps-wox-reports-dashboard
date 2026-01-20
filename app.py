#!/usr/bin/env python3
"""
GPS Report Dashboard
A web dashboard for extracting and downloading reports from the database.
"""

import os
import io
import csv
import json
import uuid
import threading
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

from flask import Flask, render_template, request, jsonify, send_file, Response, session, redirect, url_for, flash
from dotenv import load_dotenv
from functools import wraps
import secrets

try:
    import paramiko
    import pandas as pd
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
except ImportError as e:
    print(f"Missing required package: {e}")
    print("\nInstall required packages with:")
    print("pip install flask paramiko python-dotenv pandas reportlab openpyxl")
    exit(1)

from config import PROJECTS, REPORTS

app = Flask(__name__)

# Session configuration
app.secret_key = secrets.token_hex(32)

# Hardcoded credentials (no database required)
ADMIN_EMAIL = "admin@wakecap.com"
ADMIN_PASSWORD = "wakecap@2026!"

# Background job storage directory
JOBS_DIR = Path('/tmp/gps_report_jobs')


class JobManager:
    """Manage background report generation jobs with file-based persistence."""

    def __init__(self):
        JOBS_DIR.mkdir(exist_ok=True)
        self._cleanup_stale_jobs()
        self._start_cleanup_thread()

    def _cleanup_stale_jobs(self):
        """Clean up stale jobs (older than 2 hours) on startup and periodically."""
        try:
            for job_file in JOBS_DIR.glob('*.json'):
                try:
                    job = json.loads(job_file.read_text())
                    # Use completed_at for finished jobs, created_at for pending/processing
                    if job.get('completed_at'):
                        ref_time = datetime.fromisoformat(job['completed_at'])
                    else:
                        ref_time = datetime.fromisoformat(job.get('created_at', ''))
                    age_hours = (datetime.now() - ref_time).total_seconds() / 3600
                    if age_hours > 2:
                        job_file.unlink(missing_ok=True)
                        # Also clean up result file if it exists
                        if job.get('result_file'):
                            Path(job['result_file']).unlink(missing_ok=True)
                except (json.JSONDecodeError, ValueError, KeyError):
                    # Invalid job file, remove it
                    job_file.unlink(missing_ok=True)
        except Exception:
            pass  # Don't fail startup if cleanup fails

    def _start_cleanup_thread(self):
        """Start background thread to periodically clean up old jobs."""
        import threading

        def cleanup_loop():
            while True:
                import time
                time.sleep(1800)  # Run every 30 minutes
                self._cleanup_stale_jobs()

        cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
        cleanup_thread.start()

    def create_job(self, job_type, params):
        """Create job and return job_id."""
        job_id = str(uuid.uuid4())[:8]
        job_data = {
            'id': job_id,
            'type': job_type,
            'params': params,
            'status': 'pending',
            'progress': 0,
            'created_at': datetime.now().isoformat(),
            'error': None,
            'result_file': None
        }
        self._save_job(job_id, job_data)
        return job_id

    def update_progress(self, job_id, progress, status='processing'):
        """Update job progress (0-100)."""
        try:
            job = self._load_job(job_id)
            job['progress'] = progress
            job['status'] = status
            self._save_job(job_id, job)
        except FileNotFoundError:
            pass  # Job might have been cleaned up

    def complete_job(self, job_id, result_file):
        """Mark job as complete with result file path."""
        try:
            job = self._load_job(job_id)
            job['status'] = 'complete'
            job['progress'] = 100
            job['result_file'] = result_file
            job['completed_at'] = datetime.now().isoformat()
            self._save_job(job_id, job)
        except FileNotFoundError:
            pass

    def fail_job(self, job_id, error):
        """Mark job as failed."""
        try:
            job = self._load_job(job_id)
            job['status'] = 'failed'
            job['error'] = str(error)
            job['completed_at'] = datetime.now().isoformat()
            self._save_job(job_id, job)
        except FileNotFoundError:
            pass

    def get_status(self, job_id):
        """Get job status."""
        return self._load_job(job_id)

    def _save_job(self, job_id, data):
        """Atomically save job data to prevent corruption from concurrent reads."""
        import tempfile
        job_file = JOBS_DIR / f'{job_id}.json'
        # Write to temp file first, then rename (atomic on POSIX)
        fd, tmp_path = tempfile.mkstemp(dir=JOBS_DIR, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.replace(tmp_path, job_file)  # Atomic rename
        except Exception:
            # Clean up temp file on error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load_job(self, job_id):
        """Load job data with retry for transient failures."""
        import time
        job_file = JOBS_DIR / f'{job_id}.json'

        # Retry up to 3 times with short delays for transient issues
        max_retries = 3
        for attempt in range(max_retries):
            if not job_file.exists():
                raise FileNotFoundError(f'Job {job_id} not found')

            try:
                content = job_file.read_text()
                if content.strip():
                    return json.loads(content)
                # Empty file - might be mid-write, retry
            except json.JSONDecodeError:
                # Partial write - retry
                pass

            if attempt < max_retries - 1:
                time.sleep(0.1)  # Brief delay before retry

        # All retries failed
        raise ValueError(f'Job file {job_id} is empty or corrupted after {max_retries} retries')


# Initialize job manager
job_manager = JobManager()


def load_config():
    """Load configuration from .env file."""
    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)

    config = {
        "ssh_key": os.getenv("SSH_KEY", "gpswox-ssh-key.pem"),
        "ssh_host": os.getenv("SSH_HOST", ""),
        "db_name": os.getenv("DB_NAME", "gpswox_web"),
        "db_user": os.getenv("DB_USER", "root"),
        "db_password": os.getenv("DB_PASSWORD", ""),
        "db_host": os.getenv("DB_HOST", "127.0.0.1"),
        "db_port": int(os.getenv("DB_PORT", "3306")),
        "mail_from": os.getenv("MAIL_FROM", ""),
        "mail_password": os.getenv("MAIL_PASSWORD", ""),
    }

    if config["ssh_host"]:
        parts = config["ssh_host"].split("@")
        if len(parts) == 2:
            config["ssh_user"] = parts[0]
            config["ssh_server"] = parts[1]
        else:
            config["ssh_server"] = config["ssh_host"]
            config["ssh_user"] = "devops"

    return config


def send_report_email(recipient, subject, body, attachment_path, filename):
    """Send report as email attachment via Gmail SMTP.

    Args:
        recipient: Email address to send to
        subject: Email subject line
        body: Email body text
        attachment_path: Path to the file to attach
        filename: Name to give the attachment

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    config = load_config()
    mail_from = config.get("mail_from")
    mail_password = config.get("mail_password")

    if not mail_from or not mail_password:
        return False, "Email not configured. Set MAIL_FROM and MAIL_PASSWORD in .env"

    try:
        # Create message
        msg = MIMEMultipart()
        msg['From'] = mail_from
        msg['To'] = recipient
        msg['Subject'] = subject

        # Add body
        msg.attach(MIMEText(body, 'plain'))

        # Add attachment
        attachment_path = Path(attachment_path)
        if attachment_path.exists():
            with open(attachment_path, 'rb') as f:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
            msg.attach(part)

        # Send via Gmail SMTP with TLS
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(mail_from, mail_password)
            server.send_message(msg)

        print(f"[Email] Successfully sent report to {recipient}")
        return True, None

    except smtplib.SMTPAuthenticationError:
        error = "SMTP authentication failed. Check MAIL_FROM and MAIL_PASSWORD in .env"
        print(f"[Email] {error}")
        return False, error
    except Exception as e:
        error = f"Failed to send email: {str(e)}"
        print(f"[Email] {error}")
        return False, error


class SSHMySQLExecutor:
    """Execute MySQL queries remotely via SSH using the mysql CLI."""

    def __init__(self, config):
        self.config = config
        self.ssh_client = None
        self.db_name = config["db_name"]

    def connect(self):
        """Establish SSH connection."""
        ssh_key_path = Path(__file__).parent / self.config["ssh_key"]
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh_client.connect(
            hostname=self.config["ssh_server"],
            username=self.config["ssh_user"],
            key_filename=str(ssh_key_path),
        )

    def close(self):
        """Close SSH connection."""
        if self.ssh_client:
            self.ssh_client.close()

    def execute(self, query, params=None):
        """Execute a query and return results."""
        if params:
            escaped_params = []
            for p in params:
                if p is None:
                    escaped_params.append("NULL")
                elif isinstance(p, (int, float)):
                    escaped_params.append(str(p))
                else:
                    escaped = str(p).replace("\\", "\\\\").replace("'", "\\'")
                    escaped_params.append(f"'{escaped}'")
            for param in escaped_params:
                query = query.replace("%s", param, 1)

        cmd = f'sudo mysql -N -B {self.db_name} -e "{query}"'
        # Set timeout for long-running queries (10 minutes max)
        stdin, stdout, stderr = self.ssh_client.exec_command(cmd, timeout=600)
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')

        if error and "ERROR" in error:
            raise Exception(f"MySQL Error: {error}")

        return output

    def fetchall(self, query, params=None):
        """Execute SELECT and return list of tuples."""
        output = self.execute(query, params)
        if not output.strip():
            return []

        rows = []
        for line in output.strip().split('\n'):
            values = []
            for v in line.split('\t'):
                if v == 'NULL' or v == '\\N':
                    values.append(None)
                else:
                    values.append(v)
            rows.append(tuple(values))
        return rows

    def fetchone(self, query, params=None):
        """Execute SELECT and return first row as tuple."""
        rows = self.fetchall(query, params)
        return rows[0] if rows else None

    def get_columns(self, table):
        """Get column names for a table."""
        output = self.execute(f"SHOW COLUMNS FROM {table}")
        columns = []
        for line in output.strip().split('\n'):
            if line:
                columns.append(line.split('\t')[0])
        return columns


@contextmanager
def get_db_connection():
    """Create database connection via SSH."""
    config = load_config()
    executor = SSHMySQLExecutor(config)
    try:
        executor.connect()
        yield executor
    finally:
        executor.close()


def get_user_by_email(executor, email):
    """Fetch user by email address."""
    columns = executor.get_columns('users')
    user = executor.fetchone("SELECT * FROM users WHERE email = %s", (email,))
    if not user:
        return None
    return dict(zip(columns, user))


def point_in_polygon(lat, lng, polygon):
    """
    Check if a point (lat, lng) is inside a polygon using ray casting algorithm.
    Polygon is a list of {'lat': float, 'lng': float} dictionaries.
    """
    n = len(polygon)
    if n < 3:
        return False

    inside = False
    j = n - 1

    for i in range(n):
        yi, xi = polygon[i]['lat'], polygon[i]['lng']
        yj, xj = polygon[j]['lat'], polygon[j]['lng']

        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i

    return inside


def load_geofences_for_user(executor, user_id):
    """
    Load all geofences for a user and return them as a list of parsed polygons.
    Returns list of {'id': int, 'name': str, 'polygon': [{'lat': float, 'lng': float}, ...]}
    """
    import json

    geofences_query = f"""
        SELECT id, name, coordinates, type, radius, center
        FROM geofences
        WHERE user_id = {user_id} AND active = 1
    """
    rows = executor.fetchall(geofences_query)

    geofences = []
    for row in rows:
        gf_id, name, coordinates, gf_type, radius, center = row

        if gf_type == 'polygon' and coordinates:
            try:
                polygon = json.loads(coordinates)
                if isinstance(polygon, list) and len(polygon) >= 3:
                    geofences.append({
                        'id': gf_id,
                        'name': name,
                        'type': 'polygon',
                        'polygon': polygon
                    })
            except (json.JSONDecodeError, TypeError):
                continue
        elif gf_type == 'circle' and center and radius:
            try:
                center_point = json.loads(center) if isinstance(center, str) else center
                geofences.append({
                    'id': gf_id,
                    'name': name,
                    'type': 'circle',
                    'center': center_point,
                    'radius': float(radius) if radius else 0
                })
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    return geofences


def find_geofence_for_point(lat, lng, geofences):
    """
    Find which geofence (if any) contains the given point.
    Returns geofence name if found, None otherwise.
    """
    import math

    for gf in geofences:
        if gf['type'] == 'polygon':
            if point_in_polygon(lat, lng, gf['polygon']):
                return gf['name']
        elif gf['type'] == 'circle':
            # Check if point is within circle radius (approximate using Haversine)
            center = gf['center']
            if isinstance(center, dict):
                clat, clng = center.get('lat', 0), center.get('lng', 0)
            elif isinstance(center, list) and len(center) >= 2:
                clat, clng = center[0], center[1]
            else:
                continue

            # Haversine distance approximation
            R = 6371000  # Earth radius in meters
            dlat = math.radians(lat - clat)
            dlng = math.radians(lng - clng)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(clat)) * math.cos(math.radians(lat)) * math.sin(dlng/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            distance = R * c

            if distance <= gf['radius']:
                return gf['name']

    return None


def generate_trip_report_data(executor, user_id, start_date, end_date, progress_callback=None):
    """
    Generate trip report data showing vehicle states (parked, idle, run).

    Returns a list of dictionaries with per-vehicle trip segments.
    Each vehicle section contains:
    - Vehicle summary (name, total trip time, total idle time, total distance)
    - Trip segments with start/stop times, duration, location/geofence, distance, avg speed, state

    Args:
        executor: Database executor
        user_id: User ID
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        progress_callback: Optional callback function(progress) where progress is 0.0-1.0
    """
    import re
    from datetime import timedelta

    # Speed threshold for determining run vs idle (km/h)
    SPEED_THRESHOLD = 2.0

    # Load geofences for this user
    geofences = load_geofences_for_user(executor, user_id)

    # Get all devices for this user
    devices_query = f"""
        SELECT d.id, d.name, d.imei, dg.title as group_name
        FROM devices d
        JOIN user_device_pivot udp ON d.id = udp.device_id
        LEFT JOIN device_groups dg ON udp.group_id = dg.id
        WHERE udp.user_id = {user_id} AND d.deleted = 0
        ORDER BY d.name
    """
    devices = executor.fetchall(devices_query)

    if not devices:
        return [], {}

    all_trip_data = []
    global_stats = {
        'period_start': start_date,
        'period_end': end_date,
        'total_vehicles': 0,
        'total_duration_idle': 0,  # seconds
        'total_duration_parked': 0,  # seconds
        'total_duration_run': 0,  # seconds
        'total_distance': 0.0
    }

    total_devices = len(devices)
    for idx, device in enumerate(devices):
        # Report progress
        if progress_callback:
            progress_callback(idx / total_devices)
        device_id, device_name, imei, group_name = device

        # Query positions from device-specific table in gpswox_traccar
        # Switch to traccar database for position query
        positions_query = f"""
            SELECT
                time,
                speed,
                latitude,
                longitude,
                distance,
                EXTRACTVALUE(other, '//ignition') as ignition
            FROM gpswox_traccar.positions_{device_id}
            WHERE time BETWEEN '{start_date} 00:00:00' AND '{end_date} 23:59:59'
            ORDER BY time ASC
        """

        try:
            positions = executor.fetchall(positions_query)
        except Exception as e:
            # Table might not exist for this device
            continue

        if not positions:
            continue

        # Process positions into trip segments
        segments = []
        current_segment = None
        segment_positions = []

        for pos in positions:
            pos_time, speed, lat, lng, distance, ignition = pos

            # Parse values
            try:
                speed = float(speed) if speed else 0.0
                distance = float(distance) if distance else 0.0
                lat = float(lat) if lat else 0.0
                lng = float(lng) if lng else 0.0
            except (ValueError, TypeError):
                speed = 0.0
                distance = 0.0
                lat = 0.0
                lng = 0.0

            # Determine state based on ignition and speed
            ignition_on = str(ignition).lower() == 'true'

            if not ignition_on:
                state = 'parked'
            elif speed > SPEED_THRESHOLD:
                state = 'run'
            else:
                state = 'idle'

            # Create or extend segment
            if current_segment is None:
                # Look up geofence for this location
                geofence_name = find_geofence_for_point(lat, lng, geofences) if lat and lng else None

                current_segment = {
                    'start_time': pos_time,
                    'state': state,
                    'start_lat': lat,
                    'start_lng': lng,
                    'geofence': geofence_name,
                    'distance': 0.0,
                    'speeds': []
                }
                segment_positions = [(pos_time, speed, distance)]
            elif current_segment['state'] == state:
                # Continue same segment
                segment_positions.append((pos_time, speed, distance))
                if state == 'run':
                    current_segment['distance'] += distance
                    current_segment['speeds'].append(speed)
            else:
                # State changed - close current segment and start new one
                last_time = segment_positions[-1][0] if segment_positions else pos_time
                current_segment['stop_time'] = last_time

                # Calculate average speed for run segments
                if current_segment['state'] == 'run' and current_segment['speeds']:
                    current_segment['avg_speed'] = sum(current_segment['speeds']) / len(current_segment['speeds'])
                else:
                    current_segment['avg_speed'] = 0.0

                segments.append(current_segment)

                # Look up geofence for new segment location
                geofence_name = find_geofence_for_point(lat, lng, geofences) if lat and lng else None

                # Start new segment
                current_segment = {
                    'start_time': pos_time,
                    'state': state,
                    'start_lat': lat,
                    'start_lng': lng,
                    'geofence': geofence_name,
                    'distance': 0.0,
                    'speeds': []
                }
                segment_positions = [(pos_time, speed, distance)]

        # Close last segment
        if current_segment and segment_positions:
            current_segment['stop_time'] = segment_positions[-1][0]
            if current_segment['state'] == 'run' and current_segment['speeds']:
                current_segment['avg_speed'] = sum(current_segment['speeds']) / len(current_segment['speeds'])
            else:
                current_segment['avg_speed'] = 0.0
            segments.append(current_segment)

        if not segments:
            continue

        # Calculate vehicle totals
        vehicle_stats = {
            'total_trip': 0,  # seconds
            'total_idle': 0,  # seconds
            'total_parked': 0,  # seconds
            'total_distance': 0.0
        }

        for seg in segments:
            try:
                start_dt = datetime.strptime(str(seg['start_time'])[:19], '%Y-%m-%d %H:%M:%S')
                stop_dt = datetime.strptime(str(seg['stop_time'])[:19], '%Y-%m-%d %H:%M:%S')
                duration_sec = (stop_dt - start_dt).total_seconds()
            except (ValueError, TypeError):
                duration_sec = 0

            seg['duration_seconds'] = duration_sec

            if seg['state'] == 'run':
                vehicle_stats['total_trip'] += duration_sec
                vehicle_stats['total_distance'] += seg['distance']
            elif seg['state'] == 'idle':
                vehicle_stats['total_idle'] += duration_sec
            else:  # parked
                vehicle_stats['total_parked'] += duration_sec

        # Add to results
        all_trip_data.append({
            'device_id': device_id,
            'device_name': device_name,
            'imei': imei,
            'group': group_name,
            'stats': vehicle_stats,
            'segments': segments
        })

        # Update global stats
        global_stats['total_vehicles'] += 1
        global_stats['total_duration_run'] += vehicle_stats['total_trip']
        global_stats['total_duration_idle'] += vehicle_stats['total_idle']
        global_stats['total_duration_parked'] += vehicle_stats['total_parked']
        global_stats['total_distance'] += vehicle_stats['total_distance']

    return all_trip_data, global_stats


def format_duration(seconds):
    """Format seconds into D:HH:MM:SS format."""
    if not seconds or seconds < 0:
        return "0:00:00"

    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if days > 0:
        return f"{days}:{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        return f"{hours}:{minutes:02d}:{secs:02d}"


def format_hours(seconds):
    """Format seconds into decimal hours (e.g., 11.848333)."""
    if not seconds or seconds < 0:
        return 0
    return round(seconds / 3600, 6)


def generate_fleet_summary_data(executor, user_id, start_date, end_date, progress_callback=None):
    """
    Generate fleet summary report data matching the reference Excel format.

    Returns:
        tuple: (vehicle_data, global_stats) where:
            - vehicle_data: List of per-vehicle dictionaries with trip metrics
            - global_stats: Dictionary with totals for all vehicles

    The report includes per-vehicle:
        - Vehicle Info (name)
        - Start Time, Stop Time (first ignition on to last ignition off)
        - Driver TimeSheet (total hours from first to last position)
        - Total Idle Time (hours engine on but not moving)
        - Total Trip Time (hours actually moving)
        - Total Trip Distance (km)
        - Start Odometer, End Odometer (cumulative distance)
        - Event counts: H-Acceleration, H-Brake, SeatBelt, SOS
    """
    from datetime import timedelta

    # Speed threshold for determining moving vs idle (km/h)
    SPEED_THRESHOLD = 2.0

    # Get all devices for this user
    devices_query = f"""
        SELECT d.id, d.name, d.imei, dg.title as group_name
        FROM devices d
        JOIN user_device_pivot udp ON d.id = udp.device_id
        LEFT JOIN device_groups dg ON udp.group_id = dg.id
        WHERE udp.user_id = {user_id} AND d.deleted = 0
        ORDER BY d.name
    """
    devices = executor.fetchall(devices_query)

    if not devices:
        return [], {}

    # Get event counts for all devices in one query
    # Note: Removed type='custom' filter - events may have different types (alarm, driver, etc.)
    # Using message patterns to identify event types instead
    events_query = f"""
        SELECT
            e.device_id,
            SUM(CASE WHEN UPPER(e.message) LIKE '%ACCELERATION%' OR UPPER(e.message) LIKE '%ACCEL%' THEN 1 ELSE 0 END) as h_accel,
            SUM(CASE WHEN UPPER(e.message) LIKE '%BREAK%' OR UPPER(e.message) LIKE '%BRAK%' OR UPPER(e.message) LIKE '%HARSH%BRAKE%' THEN 1 ELSE 0 END) as h_brake,
            SUM(CASE WHEN UPPER(e.message) LIKE '%SEATBELT%' OR UPPER(e.message) LIKE '%SEAT BELT%' OR UPPER(e.message) LIKE '%SEAT%BELT%' THEN 1 ELSE 0 END) as seatbelt,
            SUM(CASE WHEN UPPER(e.message) = 'SOS' OR UPPER(e.message) LIKE '%SOS%' THEN 1 ELSE 0 END) as sos
        FROM events e
        JOIN devices d ON e.device_id = d.id
        JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
        WHERE e.created_at BETWEEN '{start_date} 00:00:00' AND '{end_date} 23:59:59'
        AND e.deleted = 0
        GROUP BY e.device_id
    """
    try:
        event_rows = executor.fetchall(events_query)
        events_by_device = {int(r[0]): {'h_accel': int(r[1] or 0), 'h_brake': int(r[2] or 0),
                                         'seatbelt': int(r[3] or 0), 'sos': int(r[4] or 0)}
                           for r in event_rows}
    except Exception as e:
        print(f"Warning: Failed to fetch events: {e}")
        events_by_device = {}

    all_vehicle_data = []
    global_stats = {
        'period_start': start_date,
        'period_end': end_date,
        'total_vehicles': 0,
        'total_seatbelt': 0,
        'total_sos': 0,
        'total_h_brake': 0,
        'total_h_accel': 0
    }

    total_devices = len(devices)
    for idx, device in enumerate(devices):
        # Report progress
        if progress_callback:
            progress_callback(idx / total_devices)

        device_id, device_name, imei, group_name = device

        # Query historical cumulative distance (start odometer)
        historical_dist_query = f"""
            SELECT COALESCE(SUM(distance), 0) as total_dist
            FROM gpswox_traccar.positions_{device_id}
            WHERE time < '{start_date} 00:00:00'
        """

        try:
            hist_result = executor.fetchone(historical_dist_query)
            historical_distance = float(hist_result[0]) if hist_result and hist_result[0] else 0
        except Exception:
            historical_distance = 0

        # Query positions from device-specific table
        positions_query = f"""
            SELECT
                time,
                speed,
                distance,
                EXTRACTVALUE(other, '//ignition') as ignition
            FROM gpswox_traccar.positions_{device_id}
            WHERE time BETWEEN '{start_date} 00:00:00' AND '{end_date} 23:59:59'
            ORDER BY time ASC
        """

        try:
            positions = executor.fetchall(positions_query)
        except Exception:
            # Table might not exist for this device
            continue

        if not positions:
            # Device had no data for this period - include with zeros
            device_events = events_by_device.get(device_id, {'h_accel': 0, 'h_brake': 0, 'seatbelt': 0, 'sos': 0})
            all_vehicle_data.append({
                'device_id': device_id,
                'device_name': device_name,
                'imei': imei,
                'group': group_name,
                'start_time': None,
                'stop_time': None,
                'driver_timesheet': 0,
                'total_idle_time': 0,
                'total_trip_time': 0,
                'total_trip_distance': 0,
                'start_odometer': None,
                'end_odometer': None,
                'h_acceleration': device_events['h_accel'],
                'h_brake': device_events['h_brake'],
                'seatbelt': device_events['seatbelt'],
                'sos': device_events['sos']
            })
            global_stats['total_vehicles'] += 1
            global_stats['total_seatbelt'] += device_events['seatbelt']
            global_stats['total_sos'] += device_events['sos']
            global_stats['total_h_brake'] += device_events['h_brake']
            global_stats['total_h_accel'] += device_events['h_accel']
            continue

        # Process positions
        start_time = None
        stop_time = None
        total_idle_seconds = 0
        total_trip_seconds = 0
        total_distance = 0
        cumulative_distance = 0  # Running total for today's distance
        start_odometer = historical_distance  # Start odometer is cumulative before today
        end_odometer = historical_distance  # Will be updated as we process positions

        prev_time = None
        prev_ignition = None

        for pos in positions:
            pos_time, speed, distance, ignition = pos

            # Parse values
            try:
                speed = float(speed) if speed else 0.0
                distance = float(distance) if distance else 0.0
            except (ValueError, TypeError):
                speed = 0.0
                distance = 0.0

            ignition_on = str(ignition).lower() == 'true'

            # Track cumulative distance for odometer
            cumulative_distance += distance

            # Update end odometer (historical + today's cumulative)
            end_odometer = historical_distance + cumulative_distance

            # Track first and last ignition-on times
            if ignition_on:
                if start_time is None:
                    start_time = pos_time
                stop_time = pos_time

            # Calculate time deltas if we have a previous position
            if prev_time is not None:
                try:
                    curr_dt = datetime.strptime(str(pos_time)[:19], '%Y-%m-%d %H:%M:%S')
                    prev_dt = datetime.strptime(str(prev_time)[:19], '%Y-%m-%d %H:%M:%S')
                    delta_seconds = (curr_dt - prev_dt).total_seconds()

                    # Only count time when ignition was on
                    if prev_ignition:
                        if speed > SPEED_THRESHOLD:
                            total_trip_seconds += delta_seconds
                        else:
                            total_idle_seconds += delta_seconds
                except (ValueError, TypeError):
                    pass

            # Add distance when moving
            if speed > SPEED_THRESHOLD:
                total_distance += distance

            prev_time = pos_time
            prev_ignition = ignition_on

        # Calculate driver timesheet (total time from first to last position with ignition)
        driver_timesheet_seconds = 0
        if start_time and stop_time:
            try:
                start_dt = datetime.strptime(str(start_time)[:19], '%Y-%m-%d %H:%M:%S')
                stop_dt = datetime.strptime(str(stop_time)[:19], '%Y-%m-%d %H:%M:%S')
                driver_timesheet_seconds = (stop_dt - start_dt).total_seconds()
            except (ValueError, TypeError):
                pass

        # Get event counts for this device (convert device_id to int for lookup)
        device_events = events_by_device.get(int(device_id), {'h_accel': 0, 'h_brake': 0, 'seatbelt': 0, 'sos': 0})

        # Add to results
        all_vehicle_data.append({
            'device_id': device_id,
            'device_name': device_name,
            'imei': imei,
            'group': group_name,
            'start_time': start_time,
            'stop_time': stop_time,
            'driver_timesheet': driver_timesheet_seconds,
            'total_idle_time': total_idle_seconds,
            'total_trip_time': total_trip_seconds,
            'total_trip_distance': total_distance,
            'start_odometer': start_odometer,
            'end_odometer': end_odometer,
            'h_acceleration': device_events['h_accel'],
            'h_brake': device_events['h_brake'],
            'seatbelt': device_events['seatbelt'],
            'sos': device_events['sos']
        })

        # Update global stats
        global_stats['total_vehicles'] += 1
        global_stats['total_seatbelt'] += device_events['seatbelt']
        global_stats['total_sos'] += device_events['sos']
        global_stats['total_h_brake'] += device_events['h_brake']
        global_stats['total_h_accel'] += device_events['h_accel']

    return all_vehicle_data, global_stats


def export_fleet_summary_to_excel(vehicle_data, global_stats, start_date, end_date):
    """Export fleet summary report to Excel matching the reference format."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Fleet Summary"

    # Header row with period and event totals
    # Row 1: Period start, Total SeatBelt, Total H-Break
    ws.append([
        'Period start:', f'{start_date} 00:00:00', None, None,
        'Total SeatBelt :', global_stats['total_seatbelt'], None, None, None,
        'Total H-Break:', global_stats['total_h_brake']
    ])

    # Row 2: Period end, Total SOS, Total H-Accel
    ws.append([
        'Period end:', f'{end_date} 00:00:00', None, None,
        'Total SOS:', global_stats['total_sos'], None, None, None,
        'Total H-Accel:', global_stats['total_h_accel']
    ])

    # Row 3: Total vehicles
    ws.append(['Total vehicles:', global_stats['total_vehicles']])

    # Row 4: Event Count header
    ws.append([None] * 9 + ['Event Count'])

    # Row 5: Column headers
    ws.append([
        'Vehicle Info', 'Start Time', 'Stop Time', 'Driver TimeSheet',
        'Total Idle Time', 'Total Trip Time', 'Total Trip Distance',
        'Start Odomenter', 'End Odometer', 'H-Acceleration', 'H-Brake', 'SeatBelt', 'SOS'
    ])

    # Data rows
    for vehicle in vehicle_data:
        start_time = str(vehicle['start_time'])[:19] if vehicle.get('start_time') else None
        stop_time = str(vehicle['stop_time'])[:19] if vehicle.get('stop_time') else None

        ws.append([
            vehicle['device_name'],
            start_time,
            stop_time,
            format_hours(vehicle['driver_timesheet']) if vehicle['driver_timesheet'] else None,
            format_hours(vehicle['total_idle_time']) if vehicle['total_idle_time'] else 0,
            format_hours(vehicle['total_trip_time']) if vehicle['total_trip_time'] else 0,
            round(vehicle['total_trip_distance'], 6) if vehicle['total_trip_distance'] else 0,
            round(vehicle['start_odometer'], 6) if vehicle.get('start_odometer') else None,
            round(vehicle['end_odometer'], 6) if vehicle.get('end_odometer') else None,
            vehicle['h_acceleration'],
            vehicle['h_brake'],
            vehicle['seatbelt'],
            vehicle['sos']
        ])

    # Save to BytesIO
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def export_fleet_summary_to_csv(vehicle_data, global_stats, start_date, end_date):
    """Export fleet summary report to CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)

    # Header rows
    writer.writerow([
        'Period start:', f'{start_date} 00:00:00', '', '',
        'Total SeatBelt :', global_stats['total_seatbelt'], '', '', '',
        'Total H-Break:', global_stats['total_h_brake']
    ])
    writer.writerow([
        'Period end:', f'{end_date} 00:00:00', '', '',
        'Total SOS:', global_stats['total_sos'], '', '', '',
        'Total H-Accel:', global_stats['total_h_accel']
    ])
    writer.writerow(['Total vehicles:', global_stats['total_vehicles']])
    writer.writerow([''] * 9 + ['Event Count'])

    # Column headers
    writer.writerow([
        'Vehicle Info', 'Start Time', 'Stop Time', 'Driver TimeSheet',
        'Total Idle Time', 'Total Trip Time', 'Total Trip Distance',
        'Start Odomenter', 'End Odometer', 'H-Acceleration', 'H-Brake', 'SeatBelt', 'SOS'
    ])

    # Data rows
    for vehicle in vehicle_data:
        start_time = str(vehicle['start_time'])[:19] if vehicle.get('start_time') else ''
        stop_time = str(vehicle['stop_time'])[:19] if vehicle.get('stop_time') else ''

        writer.writerow([
            vehicle['device_name'],
            start_time,
            stop_time,
            format_hours(vehicle['driver_timesheet']) if vehicle['driver_timesheet'] else '',
            format_hours(vehicle['total_idle_time']) if vehicle['total_idle_time'] else 0,
            format_hours(vehicle['total_trip_time']) if vehicle['total_trip_time'] else 0,
            round(vehicle['total_trip_distance'], 6) if vehicle['total_trip_distance'] else 0,
            round(vehicle['start_odometer'], 6) if vehicle.get('start_odometer') else '',
            round(vehicle['end_odometer'], 6) if vehicle.get('end_odometer') else '',
            vehicle['h_acceleration'],
            vehicle['h_brake'],
            vehicle['seatbelt'],
            vehicle['sos']
        ])

    output.seek(0)
    return output.getvalue()


def export_trip_report_to_excel(trip_data, global_stats, start_date, end_date):
    """Export trip report to Excel with proper formatting matching the sample."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Trip Report"

    # Global header
    ws.append(['Period start:', f'{start_date} 00:00:00'])
    ws.append(['Period end:', f'{end_date} 23:59:59'])
    ws.append(['Total vehicles:', global_stats['total_vehicles']])
    ws.append(['Total duration idle:', format_duration(global_stats['total_duration_idle'])])
    ws.append(['Total Duration Parking:', format_duration(global_stats['total_duration_parked'])])
    ws.append(['Total distance:', round(global_stats['total_distance'], 6)])
    ws.append([])
    ws.append(['* Idle = Time a vehicle is online and while standing still.'])
    ws.append(['* Trip = Time that a vehicle is online and moving.'])
    ws.append(['* Parked = Time a vehicle is online and while standing with ignition signal off'])

    # Per-vehicle sections
    for vehicle in trip_data:
        ws.append([])
        ws.append([f"Info : {vehicle['device_name']}"])
        ws.append(['Total duration trip:', format_duration(vehicle['stats']['total_trip'])])
        ws.append(['Total duration idle:', format_duration(vehicle['stats']['total_idle'])])
        ws.append(['Total distance:', round(vehicle['stats']['total_distance'], 6)])
        ws.append(['Start Time', 'Stop Time', 'Duration', 'Address', 'Distance', 'Avg Speed', 'Trip State', 'Vehicle Name'])

        vehicle_total_duration = 0
        vehicle_total_distance = 0
        vehicle_total_speed_count = 0
        vehicle_total_speed_sum = 0

        for seg in vehicle['segments']:
            start_time = str(seg['start_time'])[:19] if seg.get('start_time') else ''
            stop_time = str(seg['stop_time'])[:19] if seg.get('stop_time') else ''
            duration = format_duration(seg.get('duration_seconds', 0))

            # Use geofence name if available, otherwise use coordinates
            geofence_name = seg.get('geofence')
            lat = seg.get('start_lat', 0)
            lng = seg.get('start_lng', 0)
            if geofence_name:
                address = geofence_name
            elif lat and lng:
                address = f"{lat:.5f}, {lng:.5f}"
            else:
                address = ''

            state = seg.get('state', '')

            if state == 'run':
                distance = round(seg.get('distance', 0), 6)
                avg_speed = round(seg.get('avg_speed', 0), 6)
                vehicle_name = vehicle['device_name']
                vehicle_total_distance += distance
                if avg_speed > 0:
                    vehicle_total_speed_sum += avg_speed
                    vehicle_total_speed_count += 1
            else:
                distance = None
                avg_speed = None
                vehicle_name = None

            vehicle_total_duration += seg.get('duration_seconds', 0)

            ws.append([start_time, stop_time, duration, address, distance, avg_speed, state, vehicle_name])

        # Vehicle summary row
        overall_avg_speed = vehicle_total_speed_sum / vehicle_total_speed_count if vehicle_total_speed_count > 0 else 0
        ws.append([None, None, format_duration(vehicle_total_duration), None,
                   round(vehicle_total_distance, 6), round(overall_avg_speed, 5), None, None])

    # Save to BytesIO
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def export_trip_report_to_csv(trip_data, global_stats, start_date, end_date):
    """Export trip report to CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)

    # Global header
    writer.writerow(['Period start:', f'{start_date} 00:00:00'])
    writer.writerow(['Period end:', f'{end_date} 23:59:59'])
    writer.writerow(['Total vehicles:', global_stats['total_vehicles']])
    writer.writerow(['Total duration idle:', format_duration(global_stats['total_duration_idle'])])
    writer.writerow(['Total Duration Parking:', format_duration(global_stats['total_duration_parked'])])
    writer.writerow(['Total distance:', round(global_stats['total_distance'], 6)])
    writer.writerow([])

    # Per-vehicle sections
    for vehicle in trip_data:
        writer.writerow([])
        writer.writerow([f"Info : {vehicle['device_name']}"])
        writer.writerow(['Total duration trip:', format_duration(vehicle['stats']['total_trip'])])
        writer.writerow(['Total duration idle:', format_duration(vehicle['stats']['total_idle'])])
        writer.writerow(['Total distance:', round(vehicle['stats']['total_distance'], 6)])
        writer.writerow(['Start Time', 'Stop Time', 'Duration', 'Address', 'Distance', 'Avg Speed', 'Trip State', 'Vehicle Name'])

        for seg in vehicle['segments']:
            start_time = str(seg['start_time'])[:19] if seg.get('start_time') else ''
            stop_time = str(seg['stop_time'])[:19] if seg.get('stop_time') else ''
            duration = format_duration(seg.get('duration_seconds', 0))

            # Use geofence name if available, otherwise use coordinates
            geofence_name = seg.get('geofence')
            lat = seg.get('start_lat', 0)
            lng = seg.get('start_lng', 0)
            if geofence_name:
                address = geofence_name
            elif lat and lng:
                address = f"{lat:.5f}, {lng:.5f}"
            else:
                address = ''

            state = seg.get('state', '')

            if state == 'run':
                distance = round(seg.get('distance', 0), 6)
                avg_speed = round(seg.get('avg_speed', 0), 6)
                vehicle_name = vehicle['device_name']
            else:
                distance = ''
                avg_speed = ''
                vehicle_name = ''

            writer.writerow([start_time, stop_time, duration, address, distance, avg_speed, state, vehicle_name])

    output.seek(0)
    return output.getvalue()


def generate_report_data(executor, project_email, report_id, start_date, end_date):
    """Generate report data based on report type."""
    user = get_user_by_email(executor, project_email)
    if not user:
        return [], []

    user_id = user['id']

    # Get report info
    reports = REPORTS.get(project_email, [])
    report_info = next((r for r in reports if r['id'] == report_id), None)
    if not report_info:
        return [], []

    report_name = report_info['name'].lower()

    # Initialize geofence resolution flags
    needs_geofence = False
    location_col_indices = None

    # Define queries based on report type
    # Note: devices are linked to users via user_device_pivot table
    # traccar_devices contains real-time position data, joined via imei/uniqueId

    if 'device' in report_name or 'imei' in report_name or 'current' in report_name:
        columns = ['ID', 'Device Name', 'IMEI', 'Last Update', 'Speed', 'Latitude', 'Longitude', 'Address']
        query = f"""
            SELECT d.id, d.name, d.imei, t.updated_at,
                   t.speed, t.lastValidLatitude, t.lastValidLongitude, t.address
            FROM devices d
            JOIN user_device_pivot udp ON d.id = udp.device_id
            LEFT JOIN traccar_devices t ON d.imei COLLATE utf8_general_ci = t.uniqueId COLLATE utf8_general_ci
            WHERE udp.user_id = {user_id} AND d.deleted = 0
            ORDER BY d.name
        """
    elif 'overspeeding' in report_name or 'speed' in report_name:
        # Determine vehicle category filter based on report name
        group_filter = ""
        if 'heavy' in report_name:
            group_filter = "AND UPPER(dg.title) = 'HEAVY'"
        elif 'light' in report_name:
            group_filter = "AND UPPER(dg.title) = 'LIGHT'"
        elif 'bus' in report_name:
            group_filter = "AND UPPER(dg.title) = 'BUS'"

        columns = ['Event ID', 'Device Name', 'Group', 'IMEI', 'Speed', 'Event Time', 'Message', 'Location']
        query = f"""
            SELECT e.id, d.name, dg.title, d.imei, e.speed, e.created_at, e.message,
                   e.latitude, e.longitude
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            WHERE (e.type LIKE '%speed%' OR e.type LIKE '%overspeed%')
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            {group_filter}
            ORDER BY e.created_at DESC
        """
        # Flag to indicate this report needs geofence resolution
        needs_geofence = True
        location_col_indices = (7, 8)  # latitude at index 7, longitude at index 8
    elif 'trip' in report_name or 'idle' in report_name:
        columns = ['ID', 'Device Name', 'IMEI', 'Trip Date', 'Last Update']
        query = f"""
            SELECT dt.id, d.name, d.imei, dt.date, d.updated_at
            FROM device_trips dt
            JOIN devices d ON dt.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id
            WHERE udp.user_id = {user_id}
            AND dt.date BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND d.deleted = 0
            ORDER BY dt.date DESC
        """
    elif 'sos' in report_name:
        columns = ['Event ID', 'Device Name', 'Group', 'Event Time', 'Speed', 'Message', 'Location']
        query = f"""
            SELECT e.id, d.name, dg.title, e.created_at, e.speed, e.message,
                   e.latitude, e.longitude
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            WHERE (UPPER(e.message) = 'SOS' OR UPPER(e.message) LIKE '%SOS%')
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            ORDER BY e.created_at DESC
        """
        needs_geofence = True
        location_col_indices = (6, 7)  # latitude at index 6, longitude at index 7
    elif 'harsh' in report_name or 'acceleration' in report_name or 'braking' in report_name:
        if 'acceleration' in report_name:
            event_filter = "(UPPER(e.message) LIKE '%ACCELERATION%' OR UPPER(e.message) LIKE '%ACCEL%')"
        else:
            # Match HARSH BREAKING, HARSH BRAKING, BRAKE, BRAKING, etc.
            event_filter = "(UPPER(e.message) LIKE '%BREAK%' OR UPPER(e.message) LIKE '%BRAK%' OR UPPER(e.message) LIKE '%HARSH%BRAKE%')"
        columns = ['Event ID', 'Device Name', 'Group', 'Event Time', 'Speed', 'Message', 'Location']
        query = f"""
            SELECT e.id, d.name, dg.title, e.created_at, e.speed, e.message,
                   e.latitude, e.longitude
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            WHERE {event_filter}
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            ORDER BY e.created_at DESC
        """
        needs_geofence = True
        location_col_indices = (6, 7)  # latitude at index 6, longitude at index 7
    elif 'signal' in report_name:
        columns = ['Device ID', 'Device Name', 'IMEI', 'Model', 'Last Update', 'Protocol']
        query = f"""
            SELECT d.id, d.name, d.imei, d.device_model, t.updated_at, t.protocol
            FROM devices d
            JOIN user_device_pivot udp ON d.id = udp.device_id
            LEFT JOIN traccar_devices t ON d.imei COLLATE utf8_general_ci = t.uniqueId COLLATE utf8_general_ci
            WHERE udp.user_id = {user_id} AND d.deleted = 0
            ORDER BY d.name
        """
    elif 'distance' in report_name:
        columns = ['Device ID', 'Device Name', 'IMEI', 'Model', 'Plate Number', 'Last Update']
        query = f"""
            SELECT d.id, d.name, d.imei, d.device_model, d.plate_number, d.updated_at
            FROM devices d
            JOIN user_device_pivot udp ON d.id = udp.device_id
            WHERE udp.user_id = {user_id} AND d.deleted = 0
            ORDER BY d.name
        """
    elif 'event' in report_name:
        columns = ['Event ID', 'Device Name', 'Event Type', 'Event Time', 'Speed', 'Message', 'Location']
        query = f"""
            SELECT e.id, d.name, e.type, e.created_at, e.speed, e.message,
                   CONCAT(e.latitude, ', ', e.longitude)
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id
            WHERE udp.user_id = {user_id}
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            ORDER BY e.created_at DESC
            LIMIT 1000
        """
    elif 'time' in report_name or 'location' in report_name:
        columns = ['Device Name', 'Geofence', 'Event Type', 'Event Time', 'Location']
        query = f"""
            SELECT d.name, g.name, e.type, e.created_at,
                   CONCAT(e.latitude, ', ', e.longitude)
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id
            LEFT JOIN geofences g ON e.geofence_id = g.id
            WHERE udp.user_id = {user_id}
            AND e.geofence_id IS NOT NULL
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            ORDER BY e.created_at DESC
        """
    elif 'fleet' in report_name or 'summary' in report_name:
        columns = ['Device Name', 'IMEI', 'Model', 'Plate Number', 'Status', 'Last Update']
        query = f"""
            SELECT d.name, d.imei, d.device_model, d.plate_number,
                   CASE WHEN d.active = 1 THEN 'Active' ELSE 'Inactive' END,
                   d.updated_at
            FROM devices d
            JOIN user_device_pivot udp ON d.id = udp.device_id
            WHERE udp.user_id = {user_id} AND d.deleted = 0
            ORDER BY d.name
        """
    elif 'seat' in report_name or 'belt' in report_name:
        columns = ['Event ID', 'Device Name', 'Group', 'Event Time', 'Speed', 'Message', 'Location']
        query = f"""
            SELECT e.id, d.name, dg.title, e.created_at, e.speed, e.message,
                   e.latitude, e.longitude
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            WHERE (UPPER(e.message) LIKE '%SEATBELT%' OR UPPER(e.message) LIKE '%SEAT BELT%' OR UPPER(e.message) LIKE '%SEAT%BELT%')
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            ORDER BY e.created_at DESC
        """
        needs_geofence = True
        location_col_indices = (6, 7)  # latitude at index 6, longitude at index 7
    elif 'vehicle status' in report_name or 'running time' in report_name:
        # Vehicle Status Report with Running Time, Idle Time, Total Duration
        # Uses traccar_devices timestamps filtered by the selected date
        # Note: This shows the most recent session data for devices active on the selected date
        # - Running Time: Time between moved_at and stoped_at
        # - Total Duration: Time between engine_on_at and engine_off_at
        # - Idle Time: Total Duration - Running Time
        columns = ['Device Name', 'IMEI', 'Group', 'Running Time', 'Idle Time', 'Total Duration']
        query = f"""
            SELECT
                d.name,
                d.imei,
                dg.title,
                -- Running Time: time spent moving (moved_at to stoped_at)
                CASE
                    WHEN t.moved_at IS NULL THEN '0h 0m'
                    WHEN t.stoped_at IS NULL OR t.stoped_at < t.moved_at THEN
                        -- Still moving: calculate to end of selected date or NOW, whichever is earlier
                        CONCAT(
                            FLOOR(TIMESTAMPDIFF(SECOND, t.moved_at, LEAST(NOW(), CONCAT('{end_date}', ' 23:59:59'))) / 3600), 'h ',
                            MOD(FLOOR(TIMESTAMPDIFF(SECOND, t.moved_at, LEAST(NOW(), CONCAT('{end_date}', ' 23:59:59'))) / 60), 60), 'm'
                        )
                    ELSE
                        CONCAT(
                            FLOOR(TIMESTAMPDIFF(SECOND, t.moved_at, t.stoped_at) / 3600), 'h ',
                            MOD(FLOOR(TIMESTAMPDIFF(SECOND, t.moved_at, t.stoped_at) / 60), 60), 'm'
                        )
                END as running_time,
                -- Idle Time: Total Duration minus Running Time
                CASE
                    WHEN t.engine_on_at IS NULL THEN '0h 0m'
                    ELSE
                        CONCAT(
                            FLOOR(GREATEST(0,
                                -- Total duration in seconds
                                TIMESTAMPDIFF(SECOND, t.engine_on_at,
                                    CASE
                                        WHEN t.engine_off_at IS NULL OR t.engine_off_at < t.engine_on_at
                                        THEN LEAST(NOW(), CONCAT('{end_date}', ' 23:59:59'))
                                        ELSE t.engine_off_at
                                    END
                                )
                                -
                                -- Minus running time in seconds
                                CASE
                                    WHEN t.moved_at IS NULL THEN 0
                                    WHEN t.stoped_at IS NULL OR t.stoped_at < t.moved_at
                                    THEN TIMESTAMPDIFF(SECOND, t.moved_at, LEAST(NOW(), CONCAT('{end_date}', ' 23:59:59')))
                                    ELSE TIMESTAMPDIFF(SECOND, t.moved_at, t.stoped_at)
                                END
                            ) / 3600), 'h ',
                            MOD(FLOOR(GREATEST(0,
                                TIMESTAMPDIFF(SECOND, t.engine_on_at,
                                    CASE
                                        WHEN t.engine_off_at IS NULL OR t.engine_off_at < t.engine_on_at
                                        THEN LEAST(NOW(), CONCAT('{end_date}', ' 23:59:59'))
                                        ELSE t.engine_off_at
                                    END
                                )
                                -
                                CASE
                                    WHEN t.moved_at IS NULL THEN 0
                                    WHEN t.stoped_at IS NULL OR t.stoped_at < t.moved_at
                                    THEN TIMESTAMPDIFF(SECOND, t.moved_at, LEAST(NOW(), CONCAT('{end_date}', ' 23:59:59')))
                                    ELSE TIMESTAMPDIFF(SECOND, t.moved_at, t.stoped_at)
                                END
                            ) / 60), 60), 'm'
                        )
                END as idle_time,
                -- Total Duration: engine on time
                CASE
                    WHEN t.engine_on_at IS NULL THEN '0h 0m'
                    WHEN t.engine_off_at IS NULL OR t.engine_off_at < t.engine_on_at THEN
                        CONCAT(
                            FLOOR(TIMESTAMPDIFF(SECOND, t.engine_on_at, LEAST(NOW(), CONCAT('{end_date}', ' 23:59:59'))) / 3600), 'h ',
                            MOD(FLOOR(TIMESTAMPDIFF(SECOND, t.engine_on_at, LEAST(NOW(), CONCAT('{end_date}', ' 23:59:59'))) / 60), 60), 'm'
                        )
                    ELSE
                        CONCAT(
                            FLOOR(TIMESTAMPDIFF(SECOND, t.engine_on_at, t.engine_off_at) / 3600), 'h ',
                            MOD(FLOOR(TIMESTAMPDIFF(SECOND, t.engine_on_at, t.engine_off_at) / 60), 60), 'm'
                        )
                END as total_duration
            FROM devices d
            JOIN user_device_pivot udp ON d.id = udp.device_id
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            LEFT JOIN traccar_devices t ON d.imei COLLATE utf8_general_ci = t.uniqueId COLLATE utf8_general_ci
            WHERE udp.user_id = {user_id}
            AND d.deleted = 0
            AND (
                -- Filter: device had activity on the selected date
                DATE(t.engine_on_at) BETWEEN '{start_date}' AND '{end_date}'
                OR DATE(t.moved_at) BETWEEN '{start_date}' AND '{end_date}'
                OR DATE(t.updated_at) BETWEEN '{start_date}' AND '{end_date}'
            )
            ORDER BY d.name
        """
    else:
        # Default: device list
        columns = ['ID', 'Device Name', 'IMEI', 'Model', 'Plate Number', 'Last Update']
        query = f"""
            SELECT d.id, d.name, d.imei, d.device_model, d.plate_number, d.updated_at
            FROM devices d
            JOIN user_device_pivot udp ON d.id = udp.device_id
            WHERE udp.user_id = {user_id} AND d.deleted = 0
            ORDER BY d.name
        """

    try:
        rows = executor.fetchall(query)

        # Post-process rows to resolve geofences if needed
        if needs_geofence and location_col_indices and rows:
            lat_idx, lng_idx = location_col_indices
            geofences = load_geofences_for_user(executor, user_id)

            processed_rows = []
            for row in rows:
                row_list = list(row)
                lat = row_list[lat_idx]
                lng = row_list[lng_idx]

                # Try to resolve geofence
                location = ''
                if lat and lng:
                    try:
                        lat_f = float(lat)
                        lng_f = float(lng)
                        geofence_name = find_geofence_for_point(lat_f, lng_f, geofences)
                        if geofence_name:
                            location = geofence_name
                        else:
                            location = f"{lat_f:.5f}, {lng_f:.5f}"
                    except (ValueError, TypeError):
                        location = f"{lat}, {lng}"

                # Replace the two lat/lng columns with single location column
                row_list = row_list[:lat_idx] + [location]
                processed_rows.append(tuple(row_list))

            return columns, processed_rows

        return columns, rows
    except Exception as e:
        print(f"Query error: {e}")
        # Return empty data on error
        return columns, []


def export_to_csv(columns, data):
    """Export data to CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in data:
        writer.writerow(row)
    output.seek(0)
    return output.getvalue()


def export_to_excel(columns, data):
    """Export data to Excel format."""
    df = pd.DataFrame(data, columns=columns)
    output = io.BytesIO()
    df.to_excel(output, index=False, engine='openpyxl')
    output.seek(0)
    return output


def export_to_pdf(columns, data, title="Report"):
    """Export data to PDF format."""
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=landscape(letter))
    elements = []

    styles = getSampleStyleSheet()
    title_para = Paragraph(title, styles['Title'])
    elements.append(title_para)
    elements.append(Spacer(1, 20))

    # Prepare table data
    table_data = [columns]
    for row in data[:500]:  # Limit to 500 rows for PDF
        table_data.append([str(cell) if cell else '' for cell in row])

    if len(table_data) > 1:
        # Calculate column widths
        col_count = len(columns)
        col_width = 720 / col_count  # landscape letter width approx

        table = Table(table_data, colWidths=[col_width] * col_count)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('FONTSIZE', (0, 1), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        elements.append(table)
    else:
        elements.append(Paragraph("No data available for the selected criteria.", styles['Normal']))

    doc.build(elements)
    output.seek(0)
    return output


def run_trip_report_job(job_id, project_email, start_date, end_date, format_type, recipient_email=None):
    """Background worker for trip report generation."""
    try:
        print(f"[Trip Report Job {job_id}] Starting for {project_email}, {start_date} to {end_date}")
        with get_db_connection() as executor:
            user = get_user_by_email(executor, project_email)
            if not user:
                job_manager.fail_job(job_id, 'Project not found')
                return

            # Update progress: starting
            job_manager.update_progress(job_id, 10, 'processing')

            # Generate trip data with progress callbacks
            def progress_cb(p):
                # Map progress 0-1 to 10-80 range
                job_manager.update_progress(job_id, 10 + int(p * 70), 'processing')

            trip_data, global_stats = generate_trip_report_data(
                executor, user['id'], start_date, end_date,
                progress_callback=progress_cb
            )

            # Update progress: exporting
            job_manager.update_progress(job_id, 80, 'exporting')

            # Export to file
            ext = {'csv': '.csv', 'excel': '.xlsx', 'pdf': '.pdf'}.get(format_type, '.csv')
            if format_type == 'csv':
                result_file = JOBS_DIR / f'{job_id}.csv'
                content = export_trip_report_to_csv(trip_data, global_stats, start_date, end_date)
                result_file.write_text(content)
            elif format_type == 'excel':
                result_file = JOBS_DIR / f'{job_id}.xlsx'
                content = export_trip_report_to_excel(trip_data, global_stats, start_date, end_date)
                result_file.write_bytes(content.getvalue())
            elif format_type == 'pdf':
                # For PDF, convert to flat table format
                columns = ['Vehicle', 'Start Time', 'Stop Time', 'Duration', 'Location', 'Distance', 'Avg Speed', 'State']
                rows = []
                for vehicle in trip_data:
                    for seg in vehicle['segments']:
                        geofence_name = seg.get('geofence')
                        lat = seg.get('start_lat', 0)
                        lng = seg.get('start_lng', 0)
                        if geofence_name:
                            location = geofence_name
                        elif lat and lng:
                            location = f"{lat:.4f}, {lng:.4f}"
                        else:
                            location = ''

                        rows.append([
                            vehicle['device_name'],
                            str(seg['start_time'])[:19] if seg.get('start_time') else '',
                            str(seg['stop_time'])[:19] if seg.get('stop_time') else '',
                            format_duration(seg.get('duration_seconds', 0)),
                            location,
                            round(seg.get('distance', 0), 2) if seg.get('state') == 'run' else '',
                            round(seg.get('avg_speed', 0), 2) if seg.get('state') == 'run' else '',
                            seg.get('state', '')
                        ])

                result_file = JOBS_DIR / f'{job_id}.pdf'
                project_name = PROJECTS.get(project_email, {}).get('name', 'Unknown')
                title = f"{project_name} - Trip Report\n{start_date} to {end_date}"
                content = export_to_pdf(columns, rows, title)
                result_file.write_bytes(content.getvalue())
            else:
                job_manager.fail_job(job_id, f'Invalid format: {format_type}')
                return

            # Send email if recipient provided
            email_sent = False
            email_error = None
            if recipient_email:
                job_manager.update_progress(job_id, 90, 'sending_email')
                project_name = PROJECTS.get(project_email, {}).get('name', 'Unknown')
                filename = f"Trip_Report_{start_date}_to_{end_date}{ext}"
                subject = f"{project_name} - Trip Report ({start_date} to {end_date})"
                body = f"""Your Trip Report is ready.

Project: {project_name}
Date Range: {start_date} to {end_date}
Format: {format_type.upper()}

The report is attached to this email.

---
GPS Report Dashboard
"""
                email_sent, email_error = send_report_email(
                    recipient_email, subject, body, str(result_file), filename
                )

            job_manager.complete_job(job_id, str(result_file))
            # Store email status in job for frontend
            job = job_manager._load_job(job_id)
            job['email_sent'] = email_sent
            job['email_recipient'] = recipient_email
            job['email_error'] = email_error
            job_manager._save_job(job_id, job)
            print(f"[Trip Report Job {job_id}] Completed successfully (email_sent={email_sent})")

    except Exception as e:
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"[Trip Report Job {job_id}] FAILED: {error_msg}")
        traceback.print_exc()
        job_manager.fail_job(job_id, error_msg)


def run_fleet_summary_job(job_id, project_email, start_date, end_date, format_type, recipient_email=None):
    """Background worker for fleet summary report generation."""
    try:
        with get_db_connection() as executor:
            user = get_user_by_email(executor, project_email)
            if not user:
                job_manager.fail_job(job_id, 'Project not found')
                return

            # Update progress: starting
            job_manager.update_progress(job_id, 10, 'processing')

            # Generate fleet summary data with progress callbacks
            def progress_cb(p):
                # Map progress 0-1 to 10-80 range
                job_manager.update_progress(job_id, 10 + int(p * 70), 'processing')

            vehicle_data, global_stats = generate_fleet_summary_data(
                executor, user['id'], start_date, end_date,
                progress_callback=progress_cb
            )

            # Update progress: exporting
            job_manager.update_progress(job_id, 80, 'exporting')

            # Export to file
            ext = {'csv': '.csv', 'excel': '.xlsx', 'pdf': '.pdf'}.get(format_type, '.csv')
            if format_type == 'csv':
                result_file = JOBS_DIR / f'{job_id}.csv'
                content = export_fleet_summary_to_csv(vehicle_data, global_stats, start_date, end_date)
                result_file.write_text(content)
            elif format_type == 'excel':
                result_file = JOBS_DIR / f'{job_id}.xlsx'
                content = export_fleet_summary_to_excel(vehicle_data, global_stats, start_date, end_date)
                result_file.write_bytes(content.getvalue())
            elif format_type == 'pdf':
                # For PDF, convert to flat table format
                columns = ['Vehicle Info', 'Start Time', 'Stop Time', 'Driver TimeSheet',
                           'Total Idle', 'Total Trip', 'Distance', 'H-Accel', 'H-Brake', 'SeatBelt', 'SOS']
                rows = []
                for v in vehicle_data:
                    rows.append([
                        v['device_name'],
                        str(v['start_time'])[:19] if v.get('start_time') else '',
                        str(v['stop_time'])[:19] if v.get('stop_time') else '',
                        format_hours(v['driver_timesheet']) if v['driver_timesheet'] else '',
                        format_hours(v['total_idle_time']) if v['total_idle_time'] else 0,
                        format_hours(v['total_trip_time']) if v['total_trip_time'] else 0,
                        round(v['total_trip_distance'], 2) if v['total_trip_distance'] else 0,
                        v['h_acceleration'],
                        v['h_brake'],
                        v['seatbelt'],
                        v['sos']
                    ])

                result_file = JOBS_DIR / f'{job_id}.pdf'
                project_name = PROJECTS.get(project_email, {}).get('name', 'Unknown')
                title = f"{project_name} - Fleet Summary\n{start_date} to {end_date}"
                content = export_to_pdf(columns, rows, title)
                result_file.write_bytes(content.getvalue())
            else:
                job_manager.fail_job(job_id, f'Invalid format: {format_type}')
                return

            # Send email if recipient provided
            email_sent = False
            email_error = None
            if recipient_email:
                job_manager.update_progress(job_id, 90, 'sending_email')
                project_name = PROJECTS.get(project_email, {}).get('name', 'Unknown')
                filename = f"Fleet_Summary_{start_date}_to_{end_date}{ext}"
                subject = f"{project_name} - Fleet Summary ({start_date} to {end_date})"
                body = f"""Your Fleet Summary Report is ready.

Project: {project_name}
Date Range: {start_date} to {end_date}
Format: {format_type.upper()}

The report is attached to this email.

---
GPS Report Dashboard
"""
                email_sent, email_error = send_report_email(
                    recipient_email, subject, body, str(result_file), filename
                )

            job_manager.complete_job(job_id, str(result_file))
            # Store email status in job for frontend
            job = job_manager._load_job(job_id)
            job['email_sent'] = email_sent
            job['email_recipient'] = recipient_email
            job['email_error'] = email_error
            job_manager._save_job(job_id, job)

    except Exception as e:
        job_manager.fail_job(job_id, str(e))


def run_standard_report_job(job_id, project_email, report_id, report_name, start_date, end_date, format_type, recipient_email=None):
    """Background worker for standard report generation (reports 1-9)."""
    try:
        print(f"[Standard Report Job {job_id}] Starting {report_name} for {project_email}")
        with get_db_connection() as executor:
            # Update progress: starting
            job_manager.update_progress(job_id, 10, 'processing')

            # Generate report data
            columns, rows = generate_report_data(executor, project_email, report_id, start_date, end_date)

            # Update progress: exporting
            job_manager.update_progress(job_id, 80, 'exporting')

            # Export to file
            ext = {'csv': '.csv', 'excel': '.xlsx', 'pdf': '.pdf'}.get(format_type, '.csv')
            safe_name = report_name.replace(' ', '_').replace('/', '_')

            if format_type == 'csv':
                result_file = JOBS_DIR / f'{job_id}.csv'
                content = export_to_csv(columns, rows)
                result_file.write_text(content)
            elif format_type == 'excel':
                result_file = JOBS_DIR / f'{job_id}.xlsx'
                content = export_to_excel(columns, rows)
                result_file.write_bytes(content.getvalue())
            elif format_type == 'pdf':
                result_file = JOBS_DIR / f'{job_id}.pdf'
                project_name = PROJECTS.get(project_email, {}).get('name', 'Unknown')
                title = f"{project_name} - {report_name}\n{start_date} to {end_date}"
                content = export_to_pdf(columns, rows, title)
                result_file.write_bytes(content.getvalue())
            else:
                job_manager.fail_job(job_id, f'Invalid format: {format_type}')
                return

            # Send email if recipient provided
            email_sent = False
            email_error = None
            if recipient_email:
                job_manager.update_progress(job_id, 90, 'sending_email')
                project_name = PROJECTS.get(project_email, {}).get('name', 'Unknown')
                filename = f"{safe_name}_{start_date}_to_{end_date}{ext}"
                subject = f"{project_name} - {report_name} ({start_date} to {end_date})"
                body = f"""Your {report_name} report is ready.

Project: {project_name}
Date Range: {start_date} to {end_date}
Format: {format_type.upper()}

The report is attached to this email.

---
GPS Report Dashboard
"""
                email_sent, email_error = send_report_email(
                    recipient_email, subject, body, str(result_file), filename
                )

            job_manager.complete_job(job_id, str(result_file))
            # Store email status in job for frontend
            job = job_manager._load_job(job_id)
            job['email_sent'] = email_sent
            job['email_recipient'] = recipient_email
            job['email_error'] = email_error
            job_manager._save_job(job_id, job)
            print(f"[Standard Report Job {job_id}] Completed successfully (email_sent={email_sent})")

    except Exception as e:
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"[Standard Report Job {job_id}] FAILED: {error_msg}")
        traceback.print_exc()
        job_manager.fail_job(job_id, error_msg)


def login_required(f):
    """Decorator to require authentication for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handle user login."""
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        
        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['user_email'] = email
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='Invalid email or password')
    
    # If already logged in, redirect to dashboard
    if session.get('logged_in'):
        return redirect(url_for('index'))
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Handle user logout."""
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    """Render the main dashboard."""
    return render_template('index.html', projects=PROJECTS)


@app.route('/api/reports/<project_email>')
@login_required
def get_reports(project_email):
    """Get available reports for a project."""
    reports = REPORTS.get(project_email, [])
    return jsonify(reports)


@app.route('/api/debug/table/<table_name>')
def debug_table_structure(table_name):
    """Debug endpoint to show table structure."""
    # Only allow specific tables for security
    allowed_tables = ['device_trips', 'traccar_devices', 'devices']
    if table_name not in allowed_tables:
        return jsonify({'error': 'Table not allowed'}), 400

    try:
        with get_db_connection() as executor:
            columns = executor.get_columns(table_name)
            # Also get a sample row
            sample = executor.fetchone(f"SELECT * FROM {table_name} LIMIT 1")
            return jsonify({
                'table': table_name,
                'columns': columns,
                'sample_row': sample
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/debug/vehicle-status/<project_email>')
def debug_vehicle_status(project_email):
    """Debug endpoint to test vehicle status query."""
    try:
        with get_db_connection() as executor:
            # Get user
            user = get_user_by_email(executor, project_email)
            if not user:
                return jsonify({'error': 'User not found'}), 404

            user_id = user['id']

            # Simple query to check data availability
            query = f"""
                SELECT
                    d.name,
                    d.imei,
                    t.uniqueId,
                    t.moved_at,
                    t.stoped_at,
                    t.engine_on_at,
                    t.engine_off_at,
                    t.updated_at
                FROM devices d
                JOIN user_device_pivot udp ON d.id = udp.device_id
                LEFT JOIN traccar_devices t ON d.imei COLLATE utf8_general_ci = t.uniqueId COLLATE utf8_general_ci
                WHERE udp.user_id = {user_id}
                AND d.deleted = 0
                LIMIT 10
            """
            rows = executor.fetchall(query)

            return jsonify({
                'user_id': user_id,
                'query': query,
                'row_count': len(rows),
                'data': rows
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/generate', methods=['POST'])
@login_required
def generate_report():
    """Generate and download a report. All reports run in background queue."""
    data = request.json
    project_email = data.get('project')
    report_id = int(data.get('report_id'))
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    format_type = data.get('format', 'csv')
    recipient_email = data.get('email', '').strip() or None

    # Get report name for filename
    reports = REPORTS.get(project_email, [])
    report_info = next((r for r in reports if r['id'] == report_id), None)
    report_name = report_info['name'] if report_info else 'report'

    # All reports now go through background job queue
    if report_id == 10:  # Trip Report
        job_id = job_manager.create_job('trip_report', {
            'project': project_email,
            'report_name': report_name,
            'start_date': start_date,
            'end_date': end_date,
            'format': format_type,
            'email': recipient_email
        })

        # Start background thread
        thread = threading.Thread(
            target=run_trip_report_job,
            args=(job_id, project_email, start_date, end_date, format_type, recipient_email)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Trip report generation started in background'
        })

    elif report_id == 11:  # Fleet Summary
        job_id = job_manager.create_job('fleet_summary', {
            'project': project_email,
            'report_name': report_name,
            'start_date': start_date,
            'end_date': end_date,
            'format': format_type,
            'email': recipient_email
        })

        # Start background thread
        thread = threading.Thread(
            target=run_fleet_summary_job,
            args=(job_id, project_email, start_date, end_date, format_type, recipient_email)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Fleet Summary report generation started in background'
        })

    else:  # Standard reports (1-9)
        job_id = job_manager.create_job('standard_report', {
            'project': project_email,
            'report_id': report_id,
            'report_name': report_name,
            'start_date': start_date,
            'end_date': end_date,
            'format': format_type,
            'email': recipient_email
        })

        # Start background thread
        thread = threading.Thread(
            target=run_standard_report_job,
            args=(job_id, project_email, report_id, report_name, start_date, end_date, format_type, recipient_email)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': f'{report_name} generation started in background'
        })


@app.route('/api/job-status/<job_id>')
@login_required
def get_job_status(job_id):
    """Get status of a background job."""
    try:
        status = job_manager.get_status(job_id)
        return jsonify(status)
    except FileNotFoundError:
        return jsonify({'error': 'Job not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/job-download/<job_id>')
@login_required
def download_job_result(job_id):
    """Download completed job result."""
    try:
        status = job_manager.get_status(job_id)
        if status['status'] != 'complete':
            return jsonify({'error': 'Job not complete'}), 400

        result_file = Path(status['result_file'])
        if not result_file.exists():
            return jsonify({'error': 'Result file not found'}), 404

        # Determine MIME type based on extension
        ext = result_file.suffix.lower()
        mime_types = {
            '.csv': 'text/csv',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.pdf': 'application/pdf'
        }
        mimetype = mime_types.get(ext, 'application/octet-stream')

        # Generate a better filename
        params = status.get('params', {})
        start_date = params.get('start_date', '')
        end_date = params.get('end_date', '')
        download_name = f"Trip_Report_{start_date}__{end_date}{ext}"

        return send_file(
            result_file,
            mimetype=mimetype,
            as_attachment=True,
            download_name=download_name
        )
    except FileNotFoundError:
        return jsonify({'error': 'Job not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cross-reference')
@login_required
def get_cross_reference():
    """Get cross-reference device data with optional project filtering."""
    import json as json_module

    project_filter = request.args.get('project')  # Optional project email filter

    try:
        # Load unified device data from JSON
        json_path = Path(__file__).parent / "unified_devices.json"
        if not json_path.exists():
            return jsonify({'error': 'Device data file not found. Run update_device_data.py first.'}), 404

        with open(json_path, 'r', encoding='utf-8') as f:
            data = json_module.load(f)

        all_devices = data.get('devices', [])
        statistics = data.get('statistics', {})

        # Filter by project if specified
        if project_filter:
            devices = [d for d in all_devices if d.get('project_email') == project_filter]
        else:
            devices = all_devices

        # Get list of available projects
        available_projects = {}
        for project, count in statistics.get('by_project', {}).items():
            if project != 'Unassigned':
                # Find the email for this project name
                for d in all_devices:
                    if d.get('project_name') == project and d.get('project_email'):
                        available_projects[d['project_email']] = project
                        break

        return jsonify({
            'devices': devices,
            'total': len(devices),
            'filtered_by': project_filter,
            'available_projects': available_projects,
            'statistics': statistics,
            'generated_at': data.get('generated_at', '')
        })

    except FileNotFoundError:
        return jsonify({'error': 'Device data file not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sim-insight')
@login_required
def get_sim_insight():
    """Get SIM insight data with usage information grouped by project."""
    import json as json_module

    project_filter = request.args.get('project')  # Optional project email filter

    try:
        # Load unified device data from JSON
        json_path = Path(__file__).parent / "unified_devices.json"
        if not json_path.exists():
            return jsonify({'error': 'Device data file not found. Run update_device_data.py first.'}), 404

        with open(json_path, 'r', encoding='utf-8') as f:
            data = json_module.load(f)

        # Load traffic data if available
        traffic_path = Path(__file__).parent / "sim_traffic_data.json"
        traffic_data = {}
        data_limit_mb = 30  # Default 30MB limit
        if traffic_path.exists():
            with open(traffic_path, 'r', encoding='utf-8') as f:
                traffic_json = json_module.load(f)
                traffic_data = traffic_json.get('traffic', {})
                data_limit_mb = traffic_json.get('data_limit_mb', 30)

        all_devices = data.get('devices', [])

        # Filter to devices with SIM data (ICCID present)
        sim_devices = [d for d in all_devices if d.get('iccid')]

        # Merge traffic data with each device
        for device in sim_devices:
            iccid = device.get('iccid', '')
            if iccid in traffic_data:
                device['data_used_mb'] = traffic_data[iccid].get('total_data_mb', 0)
                device['data_used_kb'] = traffic_data[iccid].get('total_data_kb', 0)
            else:
                device['data_used_mb'] = 0
                device['data_used_kb'] = 0

        # Filter by project if specified
        if project_filter:
            sim_devices = [d for d in sim_devices if d.get('project_email') == project_filter]

        # Group by project for summary statistics
        project_summary = {}
        provider_summary = {}

        for device in sim_devices:
            project = device.get('project_name') or 'Unassigned'
            provider = device.get('sim_provider') or 'Unknown'

            # Project summary
            if project not in project_summary:
                project_summary[project] = {
                    'project_email': device.get('project_email', ''),
                    'total_sims': 0,
                    'active': 0,
                    'online': 0,
                    'offline': 0,
                    'by_provider': {}
                }
            project_summary[project]['total_sims'] += 1
            if device.get('sim_status') == 'Active':
                project_summary[project]['active'] += 1
            if device.get('status') == 'Online':
                project_summary[project]['online'] += 1
            elif device.get('status') in ['Offline', 'Inactive']:
                project_summary[project]['offline'] += 1

            # Provider counts per project
            if provider not in project_summary[project]['by_provider']:
                project_summary[project]['by_provider'][provider] = 0
            project_summary[project]['by_provider'][provider] += 1

            # Overall provider summary
            if provider not in provider_summary:
                provider_summary[provider] = {
                    'total': 0,
                    'active': 0,
                    'by_status': {'Online': 0, 'Recent': 0, 'Offline': 0, 'Inactive': 0}
                }
            provider_summary[provider]['total'] += 1
            if device.get('sim_status') == 'Active':
                provider_summary[provider]['active'] += 1
            status = device.get('status', 'Unknown')
            if status in provider_summary[provider]['by_status']:
                provider_summary[provider]['by_status'][status] += 1

        # Get list of available projects (that have SIM data)
        available_projects = {}
        for device in all_devices:
            if device.get('iccid') and device.get('project_email') and device.get('project_name'):
                if device['project_email'] not in available_projects:
                    available_projects[device['project_email']] = device['project_name']

        return jsonify({
            'devices': sim_devices,
            'total': len(sim_devices),
            'filtered_by': project_filter,
            'available_projects': available_projects,
            'project_summary': project_summary,
            'provider_summary': provider_summary,
            'data_limit_mb': data_limit_mb,
            'generated_at': data.get('generated_at', '')
        })

    except FileNotFoundError:
        return jsonify({'error': 'Device data file not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/preview', methods=['POST'])
@login_required
def preview_report():
    """Preview report data with pagination."""
    data = request.json
    project_email = data.get('project')
    report_id = int(data.get('report_id'))
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    page = int(data.get('page', 1))
    page_size = int(data.get('page_size', 50))

    # Special handling for Trip Report
    if report_id == 10:
        try:
            with get_db_connection() as executor:
                user = get_user_by_email(executor, project_email)
                if not user:
                    return jsonify({'error': 'Project not found'}), 404

                trip_data, global_stats = generate_trip_report_data(
                    executor, user['id'], start_date, end_date
                )
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        # Flatten trip data for preview
        columns = ['Vehicle', 'Start Time', 'Stop Time', 'Duration', 'Location', 'Distance (km)', 'Avg Speed (km/h)', 'State']
        rows = []
        for vehicle in trip_data:
            for seg in vehicle['segments']:
                # Use geofence name if available, otherwise use coordinates
                geofence_name = seg.get('geofence')
                lat = seg.get('start_lat', 0)
                lng = seg.get('start_lng', 0)
                if geofence_name:
                    location = geofence_name
                elif lat and lng:
                    location = f"{lat:.5f}, {lng:.5f}"
                else:
                    location = ''

                rows.append([
                    vehicle['device_name'],
                    str(seg['start_time'])[:19] if seg.get('start_time') else '',
                    str(seg['stop_time'])[:19] if seg.get('stop_time') else '',
                    format_duration(seg.get('duration_seconds', 0)),
                    location,
                    round(seg.get('distance', 0), 3) if seg.get('state') == 'run' else '',
                    round(seg.get('avg_speed', 0), 1) if seg.get('state') == 'run' else '',
                    seg.get('state', '')
                ])

        # Calculate pagination
        total_rows = len(rows)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_rows = rows[start_idx:end_idx]

        return jsonify({
            'columns': columns,
            'data': paginated_rows,
            'total_rows': total_rows,
            'page': page,
            'page_size': page_size,
            'summary': {
                'total_vehicles': global_stats['total_vehicles'],
                'total_distance': round(global_stats['total_distance'], 2),
                'total_run_time': format_duration(global_stats['total_duration_run']),
                'total_idle_time': format_duration(global_stats['total_duration_idle']),
                'total_parked_time': format_duration(global_stats['total_duration_parked'])
            }
        })

    # Special handling for Fleet Summary Report
    if report_id == 11:
        try:
            with get_db_connection() as executor:
                user = get_user_by_email(executor, project_email)
                if not user:
                    return jsonify({'error': 'Project not found'}), 404

                vehicle_data, global_stats = generate_fleet_summary_data(
                    executor, user['id'], start_date, end_date
                )
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        # Format for preview
        columns = ['Vehicle Info', 'Start Time', 'Stop Time', 'Driver TimeSheet (h)',
                   'Idle Time (h)', 'Trip Time (h)', 'Trip Distance (km)',
                   'H-Accel', 'H-Brake', 'SeatBelt', 'SOS']
        rows = []
        for v in vehicle_data:
            rows.append([
                v['device_name'],
                str(v['start_time'])[:19] if v.get('start_time') else '',
                str(v['stop_time'])[:19] if v.get('stop_time') else '',
                round(format_hours(v['driver_timesheet']), 2) if v['driver_timesheet'] else '',
                round(format_hours(v['total_idle_time']), 2) if v['total_idle_time'] else 0,
                round(format_hours(v['total_trip_time']), 2) if v['total_trip_time'] else 0,
                round(v['total_trip_distance'], 2) if v['total_trip_distance'] else 0,
                v['h_acceleration'],
                v['h_brake'],
                v['seatbelt'],
                v['sos']
            ])

        # Calculate pagination
        total_rows = len(rows)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_rows = rows[start_idx:end_idx]

        return jsonify({
            'columns': columns,
            'data': paginated_rows,
            'total_rows': total_rows,
            'page': page,
            'page_size': page_size,
            'summary': {
                'total_vehicles': global_stats['total_vehicles'],
                'total_seatbelt': global_stats['total_seatbelt'],
                'total_sos': global_stats['total_sos'],
                'total_h_brake': global_stats['total_h_brake'],
                'total_h_accel': global_stats['total_h_accel']
            }
        })

    # Standard report handling
    try:
        with get_db_connection() as executor:
            columns, rows = generate_report_data(executor, project_email, report_id, start_date, end_date)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Calculate pagination
    total_rows = len(rows)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated_rows = rows[start_idx:end_idx]

    return jsonify({
        'columns': columns,
        'data': paginated_rows,
        'total_rows': total_rows,
        'page': page,
        'page_size': page_size
    })


if __name__ == '__main__':
    # Use PORT environment variable for Render deployment
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug)
