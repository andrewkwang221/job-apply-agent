"""Trust the OS certificate store for outbound HTTPS.

requests/urllib3 verify against Mozilla's certifi bundle. Antivirus HTTPS
scanning (e.g. Avast Web/Mail Shield) presents a local CA that is in the
Windows store but not in certifi, which causes CERTIFICATE_VERIFY_FAILED.
"""
import ssl

import requests
from requests.adapters import HTTPAdapter

_PATCHED = False


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


class SystemCertAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _ssl_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = _ssl_context()
        return super().proxy_manager_for(*args, **kwargs)


def install() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _orig_init = requests.Session.__init__

    def _session_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        adapter = SystemCertAdapter()
        self.mount("https://", adapter)

    requests.Session.__init__ = _session_init  # type: ignore[method-assign]
    _PATCHED = True


install()
