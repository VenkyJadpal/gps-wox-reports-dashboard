#!/usr/bin/env python3
"""
GpsWox User Clone Script

Clones all user data from a source user to a target user including:
- Devices/Objects
- Geofences (with randomized eye-friendly colors)
- Device Groups
- Geofence Groups
- Alerts
- Alert-Object assignments

Usage:
    python clone_user.py source@example.com target@example.com
"""

import argparse
import os
import sys
import random
from pathlib import Path
from contextlib import contextmanager

try:
    import paramiko
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Missing required package: {e}")
    print("\nInstall required packages with:")
    print("pip install paramiko python-dotenv")
    sys.exit(1)


# Eye-friendly colors for geofences (distinct, not too bright or straining)
EYE_FRIENDLY_COLORS = [
    "#4A90A4",  # Steel Blue
    "#7B68EE",  # Medium Slate Blue
    "#3CB371",  # Medium Sea Green
    "#CD853F",  # Peru
    "#9370DB",  # Medium Purple
    "#20B2AA",  # Light Sea Green
    "#DAA520",  # Goldenrod
    "#8FBC8F",  # Dark Sea Green
    "#6B8E23",  # Olive Drab
    "#BC8F8F",  # Rosy Brown
    "#5F9EA0",  # Cadet Blue
    "#D2691E",  # Chocolate
    "#6A5ACD",  # Slate Blue
    "#2E8B57",  # Sea Green
    "#B8860B",  # Dark Goldenrod
    "#708090",  # Slate Gray
    "#9ACD32",  # Yellow Green
    "#8B4513",  # Saddle Brown
    "#4682B4",  # Steel Blue
    "#32CD32",  # Lime Green
    "#BA55D3",  # Medium Orchid
    "#FF7F50",  # Coral
    "#6495ED",  # Cornflower Blue
    "#F4A460",  # Sandy Brown
    "#00CED1",  # Dark Turquoise
    "#BDB76B",  # Dark Khaki
    "#7FFF00",  # Chartreuse (muted)
    "#DC143C",  # Crimson
    "#00FA9A",  # Medium Spring Green
    "#FFB6C1",  # Light Pink
    "#87CEEB",  # Sky Blue
    "#DDA0DD",  # Plum
    "#98FB98",  # Pale Green
    "#FFDAB9",  # Peach Puff
    "#E6E6FA",  # Lavender
    "#F0E68C",  # Khaki
    "#ADD8E6",  # Light Blue
    "#90EE90",  # Light Green
    "#FFE4B5",  # Moccasin
    "#AFEEEE",  # Pale Turquoise
]


class ColorGenerator:
    """Generates unique eye-friendly colors without repetition."""

    def __init__(self):
        self.used_colors = set()
        self.available_colors = list(EYE_FRIENDLY_COLORS)
        random.shuffle(self.available_colors)

    def get_next_color(self):
        """Get the next unique color. Generates new shades if all predefined colors are used."""
        if self.available_colors:
            color = self.available_colors.pop()
            self.used_colors.add(color)
            return color

        # Generate a new unique color if we've exhausted the predefined list
        while True:
            # Generate muted, eye-friendly colors (avoiding pure bright colors)
            r = random.randint(60, 200)
            g = random.randint(60, 200)
            b = random.randint(60, 200)
            color = f"#{r:02X}{g:02X}{b:02X}"
            if color not in self.used_colors:
                self.used_colors.add(color)
                return color


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

    # Parse SSH host
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

        print(f"Connecting to {self.config['ssh_server']} via SSH...")

        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh_client.connect(
            hostname=self.config["ssh_server"],
            username=self.config["ssh_user"],
            key_filename=str(ssh_key_path),
        )
        print("SSH connection established")

    def close(self):
        """Close SSH connection."""
        if self.ssh_client:
            self.ssh_client.close()
            print("SSH connection closed")

    def execute(self, query, params=None):
        """Execute a query and return results as list of dicts."""
        if params:
            # Escape parameters for shell safety
            escaped_params = []
            for p in params:
                if p is None:
                    escaped_params.append("NULL")
                elif isinstance(p, (int, float)):
                    escaped_params.append(str(p))
                else:
                    # Escape single quotes and backslashes for MySQL
                    escaped = str(p).replace("\\", "\\\\").replace("'", "\\'")
                    escaped_params.append(f"'{escaped}'")

            # Replace %s placeholders with escaped params
            for param in escaped_params:
                query = query.replace("%s", param, 1)

        # Execute via mysql CLI with tab-separated output
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
            # Handle tab-separated values, converting NULL strings
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

    def insert(self, table, data):
        """Insert a row and return the last insert ID."""
        columns = ', '.join(data.keys())

        values = []
        for v in data.values():
            if v is None:
                values.append("NULL")
            elif isinstance(v, (int, float)):
                values.append(str(v))
            else:
                escaped = str(v).replace("\\", "\\\\").replace("'", "\\'")
                values.append(f"'{escaped}'")

        values_str = ', '.join(values)
        query = f"INSERT INTO {table} ({columns}) VALUES ({values_str}); SELECT LAST_INSERT_ID();"

        output = self.execute(query)
        # Get the last insert ID from output
        for line in output.strip().split('\n'):
            if line.strip().isdigit():
                return int(line.strip())
        return None


@contextmanager
def get_db_connection(config):
    """Create database connection via SSH."""
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


def get_user_devices(executor, user_id):
    """Fetch all devices/objects for a user."""
    columns = executor.get_columns('devices')
    rows = executor.fetchall("SELECT * FROM devices WHERE user_id = %s", (user_id,))
    return [dict(zip(columns, row)) for row in rows]


def get_user_device_groups(executor, user_id):
    """Fetch all device groups for a user."""
    columns = executor.get_columns('device_groups')
    rows = executor.fetchall("SELECT * FROM device_groups WHERE user_id = %s", (user_id,))
    return [dict(zip(columns, row)) for row in rows]


def get_user_geofences(executor, user_id):
    """Fetch all geofences for a user."""
    columns = executor.get_columns('geofences')
    rows = executor.fetchall("SELECT * FROM geofences WHERE user_id = %s", (user_id,))
    return [dict(zip(columns, row)) for row in rows]


def get_user_geofence_groups(executor, user_id):
    """Fetch all geofence groups for a user."""
    columns = executor.get_columns('geofence_groups')
    rows = executor.fetchall("SELECT * FROM geofence_groups WHERE user_id = %s", (user_id,))
    return [dict(zip(columns, row)) for row in rows]


def get_user_alerts(executor, user_id):
    """Fetch all alerts for a user."""
    columns = executor.get_columns('alerts')
    rows = executor.fetchall("SELECT * FROM alerts WHERE user_id = %s", (user_id,))
    return [dict(zip(columns, row)) for row in rows]


def get_alert_devices(executor, alert_id):
    """Fetch device assignments for an alert."""
    columns = executor.get_columns('alert_device')
    rows = executor.fetchall("SELECT * FROM alert_device WHERE alert_id = %s", (alert_id,))
    return [dict(zip(columns, row)) for row in rows]


def get_alert_geofences(executor, alert_id):
    """Fetch geofence assignments for an alert."""
    columns = executor.get_columns('alert_geofence')
    rows = executor.fetchall("SELECT * FROM alert_geofence WHERE alert_id = %s", (alert_id,))
    return [dict(zip(columns, row)) for row in rows]


def clone_device_groups(executor, source_groups, target_user_id):
    """Clone device groups and return mapping of old_id -> new_id."""
    group_id_map = {}

    for group in source_groups:
        old_id = group['id']

        # Prepare insert data (exclude id, update user_id)
        insert_data = {k: v for k, v in group.items() if k != 'id'}
        insert_data['user_id'] = target_user_id

        new_id = executor.insert('device_groups', insert_data)
        group_id_map[old_id] = new_id
        print(f"  Cloned device group: {group.get('name', 'unnamed')} (ID: {old_id} -> {new_id})")

    return group_id_map


def clone_geofence_groups(executor, source_groups, target_user_id):
    """Clone geofence groups and return mapping of old_id -> new_id."""
    group_id_map = {}

    for group in source_groups:
        old_id = group['id']

        # Prepare insert data (exclude id, update user_id)
        insert_data = {k: v for k, v in group.items() if k != 'id'}
        insert_data['user_id'] = target_user_id

        new_id = executor.insert('geofence_groups', insert_data)
        group_id_map[old_id] = new_id
        print(f"  Cloned geofence group: {group.get('name', 'unnamed')} (ID: {old_id} -> {new_id})")

    return group_id_map


def clone_devices(executor, source_devices, target_user_id, device_group_map):
    """Clone devices and return mapping of old_id -> new_id."""
    device_id_map = {}

    for device in source_devices:
        old_id = device['id']

        # Prepare insert data (exclude id, update user_id and group_id)
        insert_data = {k: v for k, v in device.items() if k != 'id'}
        insert_data['user_id'] = target_user_id

        # Map group_id to new group
        if 'group_id' in insert_data and insert_data['group_id']:
            old_group_id = int(insert_data['group_id']) if insert_data['group_id'] else None
            insert_data['group_id'] = device_group_map.get(old_group_id, insert_data['group_id'])

        new_id = executor.insert('devices', insert_data)
        device_id_map[old_id] = new_id
        print(f"  Cloned device: {device.get('name', 'unnamed')} (ID: {old_id} -> {new_id})")

    return device_id_map


def clone_geofences(executor, source_geofences, target_user_id, geofence_group_map, color_generator):
    """Clone geofences with randomized colors and return mapping of old_id -> new_id."""
    geofence_id_map = {}

    for geofence in source_geofences:
        old_id = geofence['id']

        # Prepare insert data (exclude id, update user_id and group_id)
        insert_data = {k: v for k, v in geofence.items() if k != 'id'}
        insert_data['user_id'] = target_user_id

        # Assign new random eye-friendly color
        new_color = color_generator.get_next_color()
        if 'polygon_color' in insert_data:
            insert_data['polygon_color'] = new_color
        elif 'color' in insert_data:
            insert_data['color'] = new_color

        # Map group_id to new group
        if 'group_id' in insert_data and insert_data['group_id']:
            old_group_id = int(insert_data['group_id']) if insert_data['group_id'] else None
            insert_data['group_id'] = geofence_group_map.get(old_group_id, insert_data['group_id'])

        new_id = executor.insert('geofences', insert_data)
        geofence_id_map[old_id] = new_id
        print(f"  Cloned geofence: {geofence.get('name', 'unnamed')} with color {new_color} (ID: {old_id} -> {new_id})")

    return geofence_id_map


def clone_alerts(executor, source_alerts, target_user_id, geofence_id_map):
    """Clone alerts and return mapping of old_id -> new_id."""
    alert_id_map = {}

    for alert in source_alerts:
        old_id = alert['id']

        # Prepare insert data (exclude id, update user_id)
        insert_data = {k: v for k, v in alert.items() if k != 'id'}
        insert_data['user_id'] = target_user_id

        # Map geofence_id if present
        if 'geofence_id' in insert_data and insert_data['geofence_id']:
            old_geofence_id = int(insert_data['geofence_id']) if insert_data['geofence_id'] else None
            insert_data['geofence_id'] = geofence_id_map.get(old_geofence_id, insert_data['geofence_id'])

        new_id = executor.insert('alerts', insert_data)
        alert_id_map[old_id] = new_id
        print(f"  Cloned alert: {alert.get('name', 'unnamed')} (ID: {old_id} -> {new_id})")

    return alert_id_map


def clone_alert_device_assignments(executor, alert_id_map, device_id_map):
    """Clone alert-device assignments."""
    for old_alert_id, new_alert_id in alert_id_map.items():
        # Get original alert device assignments
        columns = executor.get_columns('alert_device')
        rows = executor.fetchall("SELECT * FROM alert_device WHERE alert_id = %s", (old_alert_id,))
        assignments = [dict(zip(columns, row)) for row in rows]

        for assignment in assignments:
            old_device_id = assignment.get('device_id')
            # Convert to int for comparison since data comes as strings
            old_device_id_int = int(old_device_id) if old_device_id else None
            if old_device_id_int and old_device_id_int in device_id_map:
                new_device_id = device_id_map[old_device_id_int]
                executor.insert('alert_device', {'alert_id': new_alert_id, 'device_id': new_device_id})
                print(f"  Linked alert {new_alert_id} to device {new_device_id}")


def clone_alert_geofence_assignments(executor, alert_id_map, geofence_id_map):
    """Clone alert-geofence assignments."""
    for old_alert_id, new_alert_id in alert_id_map.items():
        # Get original alert geofence assignments
        columns = executor.get_columns('alert_geofence')
        rows = executor.fetchall("SELECT * FROM alert_geofence WHERE alert_id = %s", (old_alert_id,))
        assignments = [dict(zip(columns, row)) for row in rows]

        for assignment in assignments:
            old_geofence_id = assignment.get('geofence_id')
            # Convert to int for comparison since data comes as strings
            old_geofence_id_int = int(old_geofence_id) if old_geofence_id else None
            if old_geofence_id_int and old_geofence_id_int in geofence_id_map:
                new_geofence_id = geofence_id_map[old_geofence_id_int]
                executor.insert('alert_geofence', {'alert_id': new_alert_id, 'geofence_id': new_geofence_id})
                print(f"  Linked alert {new_alert_id} to geofence {new_geofence_id}")


def discover_table_structure(executor, table_name):
    """Discover the structure of a table."""
    try:
        output = executor.execute(f"DESCRIBE {table_name}")
        print(f"\n  Table '{table_name}' structure:")
        for line in output.strip().split('\n'):
            if line:
                parts = line.split('\t')
                print(f"    - {parts[0]}: {parts[1] if len(parts) > 1 else ''}")
        return True
    except Exception as e:
        print(f"\n  Table '{table_name}' not found or error: {e}")
        return False


def clone_user_data(source_email, target_email, dry_run=False, discover=False):
    """Main function to clone user data from source to target."""
    config = load_config()
    color_generator = ColorGenerator()

    with get_db_connection(config) as executor:
        if discover:
            print("\n=== Discovering Database Structure ===")
            tables = [
                'users', 'devices', 'device_groups', 'geofences', 'geofence_groups',
                'alerts', 'alert_device', 'alert_geofence', 'device_sensors',
                'user_devices', 'objects', 'object_groups'
            ]
            for table in tables:
                discover_table_structure(executor, table)
            return

        # Fetch source user
        print(f"\nLooking up source user: {source_email}")
        source_user = get_user_by_email(executor, source_email)
        if not source_user:
            print(f"ERROR: Source user '{source_email}' not found!")
            return False
        print(f"  Found source user ID: {source_user['id']}")

        # Fetch target user
        print(f"\nLooking up target user: {target_email}")
        target_user = get_user_by_email(executor, target_email)
        if not target_user:
            print(f"ERROR: Target user '{target_email}' not found!")
            return False
        print(f"  Found target user ID: {target_user['id']}")

        source_user_id = source_user['id']
        target_user_id = target_user['id']

        # Fetch all source data
        print("\n=== Fetching Source User Data ===")

        device_groups = get_user_device_groups(executor, source_user_id)
        print(f"  Device groups: {len(device_groups)}")

        geofence_groups = get_user_geofence_groups(executor, source_user_id)
        print(f"  Geofence groups: {len(geofence_groups)}")

        devices = get_user_devices(executor, source_user_id)
        print(f"  Devices/Objects: {len(devices)}")

        geofences = get_user_geofences(executor, source_user_id)
        print(f"  Geofences: {len(geofences)}")

        alerts = get_user_alerts(executor, source_user_id)
        print(f"  Alerts: {len(alerts)}")

        if dry_run:
            print("\n=== DRY RUN - No changes will be made ===")
            print(f"Would clone {len(device_groups)} device groups")
            print(f"Would clone {len(geofence_groups)} geofence groups")
            print(f"Would clone {len(devices)} devices")
            print(f"Would clone {len(geofences)} geofences (with randomized colors)")
            print(f"Would clone {len(alerts)} alerts")
            return True

        # Clone in order (dependencies first)
        print("\n=== Cloning Device Groups ===")
        device_group_map = clone_device_groups(executor, device_groups, target_user_id)

        print("\n=== Cloning Geofence Groups ===")
        geofence_group_map = clone_geofence_groups(executor, geofence_groups, target_user_id)

        print("\n=== Cloning Devices ===")
        device_id_map = clone_devices(executor, devices, target_user_id, device_group_map)

        print("\n=== Cloning Geofences (with randomized colors) ===")
        geofence_id_map = clone_geofences(executor, geofences, target_user_id, geofence_group_map, color_generator)

        print("\n=== Cloning Alerts ===")
        alert_id_map = clone_alerts(executor, alerts, target_user_id, geofence_id_map)

        print("\n=== Cloning Alert-Device Assignments ===")
        clone_alert_device_assignments(executor, alert_id_map, device_id_map)

        print("\n=== Cloning Alert-Geofence Assignments ===")
        clone_alert_geofence_assignments(executor, alert_id_map, geofence_id_map)

        # No explicit commit needed - MySQL autocommit is enabled by default via CLI

        print("\n=== Clone Complete ===")
        print(f"Successfully cloned data from {source_email} to {target_email}")
        print(f"  - {len(device_group_map)} device groups")
        print(f"  - {len(geofence_group_map)} geofence groups")
        print(f"  - {len(device_id_map)} devices")
        print(f"  - {len(geofence_id_map)} geofences")
        print(f"  - {len(alert_id_map)} alerts")

        return True


def main():
    parser = argparse.ArgumentParser(
        description="Clone GpsWox user data from source to target user",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python clone_user.py source@example.com target@example.com
    python clone_user.py source@example.com target@example.com --dry-run
    python clone_user.py --discover  # Discover database structure
        """
    )

    parser.add_argument(
        "source_email",
        nargs="?",
        help="Email of the source user to clone from"
    )
    parser.add_argument(
        "target_email",
        nargs="?",
        help="Email of the target user to clone to"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be cloned without making changes"
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover and display database table structure"
    )

    args = parser.parse_args()

    if args.discover:
        clone_user_data(None, None, discover=True)
        return

    if not args.source_email or not args.target_email:
        parser.print_help()
        print("\nERROR: Both source_email and target_email are required")
        sys.exit(1)

    success = clone_user_data(args.source_email, args.target_email, args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
