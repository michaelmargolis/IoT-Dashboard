# Small client-side test helpers for backend reachability checks.
# Currently provides a cross-platform ping test used by the dashboard client.

mport platform
import subprocess


class ClientTests:
    def __init__(self, ip: str, timeout_ms: int) -> None:
        self.ip = ip
        self.timeout_ms = int(timeout_ms)

    def ping(self) -> bool:
        system = platform.system().lower()
        if system == "windows":
            cmd = ["ping", "-n", "1", "-w", str(self.timeout_ms), self.ip]
        else:
            timeout_s = max(1, int((self.timeout_ms + 999) / 1000))
            cmd = ["ping", "-c", "1", "-W", str(timeout_s), self.ip]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return result.returncode == 0
        except OSError:
            return False
