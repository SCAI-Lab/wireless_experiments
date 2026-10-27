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
    """Raised when forcing the BLE channel map (Row 3's mechanism test)
    fails -- treated as a full run failure, not a degrade-gracefully case
    like QBSS/AFH-read, because silently NOT applying the forced channel
    map would invalidate the whole point of the condition: the run would
    look like a normal AFH-free test while being labeled as the
    channel-restricted one."""


class DeviceCheckError(Exception):
    """Raised when one or more devices fail the pre-flight connectivity check."""


class StrayProcessError(Exception):
    """Raised when a device driver process is already running before this
    session starts one of its own for the same device."""


class InfrastructureCheckError(Exception):
    """Raised when required infrastructure (BT controller, Pi, etc.) isn't reachable."""


class RunAbortedError(Exception):
    """Raised when a test fails and the session should stop -- either the
    user chose to stop when asked, or there was no one to ask (a
    non-interactive terminal) so the old automatic-abort behavior applies."""
