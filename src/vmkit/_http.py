"""Internal HTTPS/TLS helpers shared by the SOAP and datastore-transfer paths."""

import threading

_warned_lock = threading.Lock()
_warnings_suppressed = False


def suppress_insecure_warnings() -> None:
    """Silence urllib3's InsecureRequestWarning, once, for unverified transfers.

    Called only when a request is actually made with ``verify=False``. This used
    to run as an import-time side effect in ``datastore.py``, which silenced the
    warning process-wide for the entire host application — including requests
    that had nothing to do with vmkit.
    """
    global _warnings_suppressed
    with _warned_lock:
        if _warnings_suppressed:
            return
        _warnings_suppressed = True
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
