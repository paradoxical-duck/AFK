"""AFK backend entry point.

Launched by the Electron main process. Speaks newline-delimited JSON-RPC
over stdin/stdout. All human-readable logging goes to stderr so it never
corrupts the protocol stream.
"""

import sys
import os

# Ensure the package is importable whether launched from repo or bundle.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _suppress_macos_dock_icon() -> None:
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyProhibited

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyProhibited
        )
    except Exception:
        # Packaging also stamps LSUIElement on Python.app. Keep startup alive
        # if PyObjC is unavailable in a development environment.
        pass


_suppress_macos_dock_icon()

from afk_backend.app import AFKApp  # noqa: E402
from afk_backend.rpc import RpcServer  # noqa: E402
from afk_backend import logutil  # noqa: E402


def main() -> int:
    logutil.info("AFK backend booting (python %s)" % sys.version.split()[0])
    app = AFKApp()
    server = RpcServer(app.dispatch, on_started=app.on_started)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    logutil.info("AFK backend exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
