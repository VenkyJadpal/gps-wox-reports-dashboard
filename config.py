# Project and Report Configuration

PROJECTS = {
    "riyas-ngl@wakecap.com": {
        "id": 8,
        "name": "Aramco - Riyas NGL",
        "app_id": "app_8_Aramco___Riyas_NGL"
    },
    "jafurah-pkg3@wakecap.com": {
        "id": 9,
        "name": "Aramco Jafurah JFGP PKG3",
        "app_id": "app_9_Aramco_Jafurah_JFGP_PKG3"
    },
    "jc3c4@wakecap.com": {
        "id": 10,
        "name": "Aramco JC3C4",
        "app_id": "app_10_Aramco__JC3C4"
    },
    "habwah@wakecap.com": {
        "id": 11,
        "name": "Habwah",
        "app_id": "app_11_Habwah"
    },
    "enppi-sulfur@wakecap.com": {
        "id": 18,
        "name": "Enppi Sulfur-Ras-Tanura-Refinery",
        "app_id": "app_18_Enppi_Sulfur_Ras_Tanura_Refinery"
    },
    "alyamama@wakecap.com": {
        "id": 22,
        "name": "Aramco - Alyamama",
        "app_id": "app_22_Aramco___Alyamama"
    },
    "lt-phase2-pkg1@wakecap.com": {
        "id": 23,
        "name": "Aramco Jafurah L&T Phase2-Pkg1",
        "app_id": "app_23_Aramco_Jafurah_L&T_Phase2_Pkg1"
    },
    "hyundai-phase2-pkg2@wakecap.com": {
        "id": 27,
        "name": "Aramco Jafurah Hyundai Phase 2 - Pkg 2",
        "app_id": "app_27_Aramco_Jafurah_Hyundai_Phase_2___Pkg_2"
    },
    "phase3-pkg8@wakecap.com": {
        "id": 36,
        "name": "Aramco - Phase 3 Package 8",
        "app_id": "app_36_Aramco___Phase_3_Package_8"
    }
}

# Standard reports for all projects (Device List + Overspeeding by category + Vehicle Status)
STANDARD_REPORTS = [
    {"id": 1, "name": "Device List", "description": "Current list of all devices with location"},
    {"id": 2, "name": "Bus Overspeeding Report", "description": "Overspeeding events for Bus vehicles"},
    {"id": 3, "name": "Heavy Overspeeding Report", "description": "Overspeeding events for Heavy vehicles"},
    {"id": 4, "name": "Light Overspeeding Report", "description": "Overspeeding events for Light vehicles"},
    {"id": 10, "name": "Trip Report", "description": "Vehicle trip status with Idle, Run & Parked segments"},
]

# Phase 3 Package 8 has additional custom event reports
PHASE3_PKG8_REPORTS = STANDARD_REPORTS + [
    {"id": 6, "name": "Seatbelt Violation Report", "description": "Seatbelt off while moving (>20 km/h)"},
    {"id": 7, "name": "SOS Alert Report", "description": "SOS emergency alerts"},
    {"id": 8, "name": "Harsh Braking Report", "description": "Harsh braking events"},
    {"id": 9, "name": "Harsh Acceleration Report", "description": "Harsh acceleration events"},
    {"id": 11, "name": "Fleet Summary Report", "description": "Daily fleet summary with vehicle times, distances, and event counts"},
]

# All projects use standard reports, except phase3-pkg8 which has seatbelt
REPORTS = {
    "riyas-ngl@wakecap.com": STANDARD_REPORTS,
    "jafurah-pkg3@wakecap.com": STANDARD_REPORTS,
    "jc3c4@wakecap.com": STANDARD_REPORTS,
    "habwah@wakecap.com": STANDARD_REPORTS,
    "enppi-sulfur@wakecap.com": STANDARD_REPORTS,
    "alyamama@wakecap.com": STANDARD_REPORTS,
    "lt-phase2-pkg1@wakecap.com": STANDARD_REPORTS,
    "hyundai-phase2-pkg2@wakecap.com": STANDARD_REPORTS,
    "phase3-pkg8@wakecap.com": PHASE3_PKG8_REPORTS,
}
