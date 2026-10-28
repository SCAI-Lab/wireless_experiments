"""Exception types raised across session_lib."""




class DriverExitedError(Exception):
    """Raised when a device driver process exits on its own during a run."""

    def __init__(self, launch_cmd, returncode):
        self.launch_cmd = launch_cmd
        self.returncode = returncode
        super().__init__(
            f"driver process exited early (returncode={returncode}): {launch_cmd}"
        )


class ChannelClassificationError(Exception):
    """Raised when forcing the BLE channel map fails. A full run failure:
    without the map the run would be an AFH-free run labelled as the
    channel-restricted one."""


class DeviceCheckError(Exception):
    """Raised when one or more devices fail the pre-flight connectivity check."""


class StrayProcessError(Exception):
    """Raised when a device driver process is already running before this
    session starts one of its own for the same device."""


class InfrastructureCheckError(Exception):
    """Raised when required infrastructure (BT controller, Pi, etc.) isn't reachable."""


class RunAbortedError(Exception):
    """Raised when a test fails and the session should stop: the user chose
    to stop when asked, or the terminal is non-interactive."""
