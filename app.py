#!/usr/bin/env python3
"""
GPS Report Dashboard
A web dashboard for extracting and downloading reports from the database.
"""

import os
import io
import csv
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
        stdin, stdout, stderr = self.ssh_client.exec_command(cmd)
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
                   CONCAT(e.latitude, ', ', e.longitude)
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
                   CONCAT(e.latitude, ', ', e.longitude)
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            WHERE e.type = 'custom'
            AND UPPER(e.message) = 'SOS'
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            ORDER BY e.created_at DESC
        """
    elif 'harsh' in report_name or 'acceleration' in report_name or 'braking' in report_name:
        if 'acceleration' in report_name:
            event_filter = "UPPER(e.message) LIKE '%ACCELERATION%'"
        else:
            event_filter = "(UPPER(e.message) LIKE '%BREAKING%' OR UPPER(e.message) LIKE '%BRAKING%')"
        columns = ['Event ID', 'Device Name', 'Group', 'Event Time', 'Speed', 'Message', 'Location']
        query = f"""
            SELECT e.id, d.name, dg.title, e.created_at, e.speed, e.message,
                   CONCAT(e.latitude, ', ', e.longitude)
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            WHERE e.type = 'custom'
            AND {event_filter}
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            ORDER BY e.created_at DESC
        """
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
                   CONCAT(e.latitude, ', ', e.longitude)
            FROM events e
            JOIN devices d ON e.device_id = d.id
            JOIN user_device_pivot udp ON d.id = udp.device_id AND udp.user_id = {user_id}
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            WHERE e.type = 'custom'
            AND (UPPER(e.message) LIKE '%SEATBELT%' OR UPPER(e.message) LIKE '%SEAT BELT%')
            AND e.created_at BETWEEN '{start_date}' AND '{end_date} 23:59:59'
            AND e.deleted = 0
            ORDER BY e.created_at DESC
        """
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
    """Generate and download a report."""
    data = request.json
    project_email = data.get('project')
    report_id = int(data.get('report_id'))
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    format_type = data.get('format', 'csv')

    # Get report name for filename
    reports = REPORTS.get(project_email, [])
    report_info = next((r for r in reports if r['id'] == report_id), None)
    report_name = report_info['name'] if report_info else 'report'

    try:
        with get_db_connection() as executor:
            columns, rows = generate_report_data(executor, project_email, report_id, start_date, end_date)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_name = report_name.replace(' ', '_').replace('/', '_')

    if format_type == 'csv':
        content = export_to_csv(columns, rows)
        return Response(
            content,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={safe_name}_{timestamp}.csv'}
        )
    elif format_type == 'excel':
        content = export_to_excel(columns, rows)
        return send_file(
            content,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'{safe_name}_{timestamp}.xlsx'
        )
    elif format_type == 'pdf':
        project_name = PROJECTS.get(project_email, {}).get('name', 'Unknown')
        title = f"{project_name} - {report_name}\n{start_date} to {end_date}"
        content = export_to_pdf(columns, rows, title)
        return send_file(
            content,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'{safe_name}_{timestamp}.pdf'
        )

    return jsonify({'error': 'Invalid format'}), 400


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
