"""
Shared infrastructure for chamber_session.py (Rows 1-5, the chamber day) and
gym_session.py (Row 6, the separate real-gym visit): logging, config
loading, btmon/driver process control, the pre-flight device sanity check,
run execution, and capture parsing.

Split into submodules by concern; this package re-exports the names the
session scripts import, so `from session_lib import ...` keeps working.
Import anything else directly from its submodule:

    errors          exception types
    logs            setup_logging and the shared `log` logger
    session_files   config, session dirs, manifest.csv, resume/rerun
    capture         btmon, rosbag and nRF sniffer processes
    wifi            QBSS polling, internal-WiFi rfkill, channel occupancy
    ble_radio       BLE density, AFH/RSSI polling, forced channel map
    drivers         driver processes, BlueZ healing, pairing agent
    preflight       pre-flight device/infrastructure/throughput checks
    run             do_run (one recorded run)
    run_checks      pass thresholds, data checks, evaluate_run_result
    btmon_patterns  btmon output regexes
    btmon_parse     parse_btmon_text / parse_capture_file
    polar           Polar H10 ACC/ECG/HR completeness
    corsano         Corsano 287-2B completeness
    session_parse   do_parse (manifest.csv -> results.csv)
"""

from .errors import (  # noqa: F401
    DeviceCheckError,
    InfrastructureCheckError,
    RunAbortedError,
    StrayProcessError,
)
from .logs import (  # noqa: F401
    log,
    setup_logging,
)
from .session_files import (  # noqa: F401
    find_latest_session_ts,
    get_output_dir,
    get_session_dir,
    load_completed_keys,
    load_config,
    prepare_rerun_failed,
    run_key,
)
from .capture import (  # noqa: F401
    start_ble_sniffer,
    start_btmon,
    stop_ble_sniffer,
    stop_btmon,
)
from .wifi import (  # noqa: F401
    disable_internal_wifi_for_session,
    log_wifi_rfkill_state,
    restore_internal_wifi,
    start_channel_occupancy_poller,
    stop_channel_occupancy_poller,
)
from .ble_radio import (  # noqa: F401
    FORCED_CHANNEL_RANGE,
    reset_host_channel_classification,
    start_ble_density_poller,
    stop_ble_density_poller,
)
from .drivers import (  # noqa: F401
    _kernel_has_link,
    check_ros_packages,
    disconnect_ble_device,
    heal_stale_bluez_connection,
    start_driver,
    start_pairing_agent,
    stop_all_drivers,
    stop_driver,
    stop_pairing_agent,
)
from .preflight import (  # noqa: F401
    check_ble_sniffer_ready,
    check_bt_controller_ready,
    check_devices_ready,
    check_devices_unbonded,
    check_pi_reachable,
    check_pi_wifi_link,
    check_wifi_scan_ready,
    check_no_foreign_wifi,
    kill_stray_driver_processes,
    resolve_duty_bitrates,
)
from .run import do_run  # noqa: F401
from .environment import snapshot_environment  # noqa: F401
from .btmon_parse import STARTUP_GRACE_S  # noqa: F401
from .session_parse import do_parse  # noqa: F401
