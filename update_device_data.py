#!/usr/bin/env python3
"""
Update device data by merging:
1. Latest FOTA export (device status, config, firmware)
2. Existing SIM data from HTML cross-reference (ICCID, SIM provider, etc.)
3. Project mapping from GPSWox

Outputs: unified_devices.json
"""

import csv
import json
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup


def load_fota_csv(csv_path):
    """Load device data from FOTA CSV export."""
    devices = {}
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            imei = row.get('imei', '').strip()
            if imei:
                devices[imei] = {
                    'imei': imei,
                    'model': row.get('model', ''),
                    'config': row.get('current_configuration', ''),
                    'firmware': row.get('current_firmware', ''),
                    'description': row.get('description', ''),
                    'seen_at': row.get('seen_at', ''),
                    'activity_status': row.get('activity_status', ''),
                    'task_queue': row.get('task_queue', ''),
                }
    return devices


def load_sim_data_from_html(html_path):
    """Extract SIM data from existing HTML cross-reference."""
    sim_data = {}

    if not html_path.exists():
        print(f"Warning: HTML file not found at {html_path}")
        return sim_data

    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')
    table = soup.find('table', id='deviceTable')

    if not table:
        print("Warning: Device table not found in HTML")
        return sim_data

    tbody = table.find('tbody')
    if not tbody:
        print("Warning: Table body not found")
        return sim_data

    for row in tbody.find_all('tr'):
        cells = row.find_all('td')
        if len(cells) >= 12:
            imei = cells[1].get_text(strip=True)
            sim_data[imei] = {
                'sim_provider': cells[7].get_text(strip=True),
                'sim_status': cells[8].get_text(strip=True),
                'iccid': cells[9].get_text(strip=True),
                'msisdn': cells[10].get_text(strip=True),
            }

    return sim_data


def load_project_mapping(mapping_path):
    """Load device-to-project mapping."""
    if not mapping_path.exists():
        print(f"Warning: Project mapping not found at {mapping_path}")
        return {}

    with open(mapping_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def calculate_status(seen_at, activity_status):
    """Calculate device status based on last seen time."""
    if activity_status == 'Inactive' or not seen_at:
        return 'Inactive'

    try:
        seen_dt = datetime.strptime(seen_at[:19], '%Y-%m-%d %H:%M:%S')
        now = datetime.now()
        diff = now - seen_dt

        if diff < timedelta(hours=24):
            return 'Online'
        elif diff < timedelta(days=30):
            return 'Recent'
        else:
            return 'Offline'
    except (ValueError, TypeError):
        return activity_status or 'Unknown'


def get_config_status(config):
    """Determine if config is correct, wrong, or none."""
    if not config:
        return 'None'
    config_lower = config.lower()
    if 'primary' in config_lower or 'gpswox' in config_lower:
        return 'Correct'
    return 'Wrong'


def merge_device_data(fota_devices, sim_data, project_mapping):
    """Merge all data sources into unified device list."""
    unified = []

    for imei, fota in fota_devices.items():
        sim = sim_data.get(imei, {})
        project = project_mapping.get(imei, {})

        status = calculate_status(fota.get('seen_at'), fota.get('activity_status'))
        config_status = get_config_status(fota.get('config'))

        # Determine if device is in GPSWox (has project mapping)
        in_gpswox = 'Yes' if project.get('project_email') else 'No'

        device = {
            'imei': imei,
            'model': fota.get('model', ''),
            'status': status,
            'activity_status': fota.get('activity_status', ''),
            'in_gpswox': in_gpswox,
            'gpswox_name': project.get('device_name', ''),
            'config': fota.get('config', '') or 'None',
            'config_status': config_status,
            'firmware': fota.get('firmware', ''),
            'sim_provider': sim.get('sim_provider', '') or 'Unknown',
            'sim_status': sim.get('sim_status', ''),
            'iccid': sim.get('iccid', ''),
            'msisdn': sim.get('msisdn', ''),
            'last_seen': fota.get('seen_at', ''),
            'project_email': project.get('project_email', ''),
            'project_name': project.get('project_name', ''),
            'device_group': project.get('device_group', ''),
            'task_queue': fota.get('task_queue', ''),
        }
        unified.append(device)

    # Sort by project name, then IMEI
    unified.sort(key=lambda d: (d.get('project_name') or 'ZZZ', d['imei']))

    return unified


def generate_statistics(devices):
    """Generate summary statistics."""
    stats = {
        'total': len(devices),
        'by_status': {},
        'by_project': {},
        'by_sim_provider': {},
        'by_config': {},
        'in_gpswox': 0,
        'not_in_gpswox': 0,
    }

    for d in devices:
        # Status counts
        status = d.get('status', 'Unknown')
        stats['by_status'][status] = stats['by_status'].get(status, 0) + 1

        # Project counts
        project = d.get('project_name') or 'Unassigned'
        stats['by_project'][project] = stats['by_project'].get(project, 0) + 1

        # SIM provider counts
        provider = d.get('sim_provider') or 'Unknown'
        stats['by_sim_provider'][provider] = stats['by_sim_provider'].get(provider, 0) + 1

        # Config counts
        config_status = d.get('config_status', 'None')
        stats['by_config'][config_status] = stats['by_config'].get(config_status, 0) + 1

        # GPSWox counts
        if d.get('in_gpswox') == 'Yes':
            stats['in_gpswox'] += 1
        else:
            stats['not_in_gpswox'] += 1

    return stats


def main():
    base_path = Path(__file__).parent

    # File paths
    fota_csv_path = Path('/Users/venky/Downloads/Exported_devices1768818682.csv')
    html_path = base_path / 'Unified Device Cross-Reference (1).html'
    project_mapping_path = base_path / 'device_project_mapping.json'
    output_path = base_path / 'unified_devices.json'

    print("=" * 60)
    print("Updating Device Data")
    print("=" * 60)

    # Load data sources
    print(f"\n1. Loading FOTA CSV: {fota_csv_path}")
    fota_devices = load_fota_csv(fota_csv_path)
    print(f"   Loaded {len(fota_devices)} devices from FOTA")

    print(f"\n2. Loading SIM data from HTML: {html_path}")
    sim_data = load_sim_data_from_html(html_path)
    print(f"   Loaded SIM data for {len(sim_data)} devices")

    print(f"\n3. Loading project mapping: {project_mapping_path}")
    project_mapping = load_project_mapping(project_mapping_path)
    print(f"   Loaded project mapping for {len(project_mapping)} devices")

    # Merge data
    print("\n4. Merging data sources...")
    unified_devices = merge_device_data(fota_devices, sim_data, project_mapping)
    print(f"   Merged {len(unified_devices)} devices")

    # Generate statistics
    stats = generate_statistics(unified_devices)

    # Save output
    output_data = {
        'generated_at': datetime.now().isoformat(),
        'source_files': {
            'fota_csv': str(fota_csv_path),
            'html_crossref': str(html_path),
            'project_mapping': str(project_mapping_path),
        },
        'statistics': stats,
        'devices': unified_devices,
    }

    print(f"\n5. Saving to: {output_path}")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2)

    # Print summary
    print("\n" + "=" * 60)
    print("Summary Statistics")
    print("=" * 60)
    print(f"\nTotal Devices: {stats['total']}")

    print("\nBy Status:")
    for status, count in sorted(stats['by_status'].items(), key=lambda x: -x[1]):
        print(f"  {status}: {count}")

    print("\nBy Project:")
    for project, count in sorted(stats['by_project'].items(), key=lambda x: -x[1])[:10]:
        print(f"  {project}: {count}")

    print("\nBy SIM Provider:")
    for provider, count in sorted(stats['by_sim_provider'].items(), key=lambda x: -x[1]):
        print(f"  {provider}: {count}")

    print("\nConfig Status:")
    for config, count in sorted(stats['by_config'].items(), key=lambda x: -x[1]):
        print(f"  {config}: {count}")

    print(f"\nIn GPSWox: {stats['in_gpswox']}")
    print(f"Not in GPSWox: {stats['not_in_gpswox']}")

    print(f"\nDone! Output saved to: {output_path}")


if __name__ == "__main__":
    main()
