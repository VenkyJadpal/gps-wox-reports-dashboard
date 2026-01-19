#!/usr/bin/env python3
"""
Fetch device-to-project mapping from GPSWox database.
This creates a JSON file mapping each device IMEI to its project.
"""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

try:
    import paramiko
except ImportError:
    print("Missing paramiko. Install with: pip install paramiko")
    exit(1)

from config import PROJECTS


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


def fetch_device_project_mapping():
    """Fetch device-to-project mapping from GPSWox database."""
    config = load_config()

    if not config.get("ssh_server"):
        print("Error: SSH_HOST not configured in .env file")
        return None

    ssh_key_path = Path(__file__).parent / config["ssh_key"]
    if not ssh_key_path.exists():
        print(f"Error: SSH key not found at {ssh_key_path}")
        return None

    # Build reverse lookup: user_id -> project_email
    # First we need to get user IDs for each project email

    print("Connecting to GPSWox server via SSH...")
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh_client.connect(
            hostname=config["ssh_server"],
            username=config["ssh_user"],
            key_filename=str(ssh_key_path),
        )
        print("Connected successfully!")

        # Build list of project emails to filter by
        project_emails = list(PROJECTS.keys())
        email_list = "', '".join(project_emails)

        # Query to get all devices with their project assignment
        # Only include users that are actual projects (from config)
        query = f"""
            SELECT
                d.imei,
                d.name as device_name,
                u.email as project_email,
                u.id as user_id,
                dg.title as device_group
            FROM devices d
            JOIN user_device_pivot udp ON d.id = udp.device_id
            JOIN users u ON udp.user_id = u.id
            LEFT JOIN device_groups dg ON udp.group_id = dg.id
            WHERE d.deleted = 0
            AND u.email IN ('{email_list}')
            ORDER BY u.email, d.imei
        """

        cmd = f'sudo mysql -N -B {config["db_name"]} -e "{query}"'
        print("Executing query...")
        stdin, stdout, stderr = ssh_client.exec_command(cmd)
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')

        if error and "ERROR" in error:
            print(f"MySQL Error: {error}")
            return None

        # Parse results
        device_mapping = {}
        project_stats = {}

        for line in output.strip().split('\n'):
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) >= 4:
                imei = parts[0]
                device_name = parts[1] if parts[1] != 'NULL' else None
                project_email = parts[2]
                user_id = parts[3]
                device_group = parts[4] if len(parts) > 4 and parts[4] != 'NULL' else None

                # Get project info from config
                project_info = PROJECTS.get(project_email, {})
                project_name = project_info.get('name', project_email)

                device_mapping[imei] = {
                    'project_email': project_email,
                    'project_name': project_name,
                    'project_id': project_info.get('id'),
                    'device_name': device_name,
                    'device_group': device_group,
                    'user_id': int(user_id) if user_id else None
                }

                # Count devices per project
                if project_email not in project_stats:
                    project_stats[project_email] = {'name': project_name, 'count': 0}
                project_stats[project_email]['count'] += 1

        print(f"\nFound {len(device_mapping)} devices mapped to projects:")
        print("-" * 50)
        for email, stats in sorted(project_stats.items(), key=lambda x: -x[1]['count']):
            print(f"  {stats['name']}: {stats['count']} devices")

        return device_mapping

    except Exception as e:
        print(f"Error: {e}")
        return None
    finally:
        ssh_client.close()


def main():
    print("=" * 60)
    print("GPSWox Device-to-Project Mapping Generator")
    print("=" * 60)

    mapping = fetch_device_project_mapping()

    if mapping:
        output_path = Path(__file__).parent / "device_project_mapping.json"
        with open(output_path, 'w') as f:
            json.dump(mapping, f, indent=2)
        print(f"\nMapping saved to: {output_path}")
        print(f"Total devices mapped: {len(mapping)}")
    else:
        print("\nFailed to generate mapping.")
        exit(1)


if __name__ == "__main__":
    main()
