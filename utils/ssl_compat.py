"""Trust the OS certificate store for outbound HTTPS.

requests/urllib3 verify against Mozilla's certifi bundle. Antivirus HTTPS
scanning (e.g. Avast Web/Mail Shield) presents a local CA that is in the
Windows store but not in certifi, which causes CERTIFICATE_VERIFY_FAILED.

Avast also exposes a named device (\\\\.\\aswMonFltProxy\\...) via
SSL_CERT_FILE / REQUESTS_CA_BUNDLE, or intercepts the certifi file open.
That raises PermissionError before TLS starts. Ignore those unusable paths
and do not let requests pass certifi into urllib3 when the OS store is loaded.
"""
from __future__ import annotations

import logging
import os
import ssl

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

_PATCHED = False
_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def _is_usable_cafile(path: str) -> bool:
    if not path:
        return False
    normalized = path.replace("/", "\\")
    # Named devices such as \\.\aswMonFltProxy\<id> are not readable CA files.
    if normalized.startswith("\\\\.\\") or "aswMonFltProxy" in path:
        return False
    try:
        return os.path.isfile(path) and os.access(path, os.R_OK)
    except OSError:
        return False


def _clear_unusable_ca_env() -> list[str]:
    """Drop CA bundle env vars that point at AV device paths or missing files."""
    cleared: list[str] = []
    for key in _CA_ENV_VARS:
        val = os.environ.get(key)
        if val and not _is_usable_cafile(val):
            os.environ.pop(key, None)
            cleared.append(key)
            logger.warning(
                "Ignoring unusable %s (antivirus HTTPS scan device or unreadable path)",
                key,
            )
    return cleared


def _ssl_context() -> ssl.SSLContext:
    _clear_unusable_ca_env()
    try:
        ctx = ssl.create_default_context()
    except OSError as exc:
        logger.warning("ssl.create_default_context failed (%s); using TLS client context", exc)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        try:
            ctx.load_default_certs()
        except OSError:
            pass
    try:
        import certifi

        cafile = certifi.where()
        if _is_usable_cafile(cafile):
            ctx.load_verify_locations(cafile=cafile)
    except OSError as exc:
        logger.warning("Skipping certifi CA bundle (%s)", exc)
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

    def cert_verify(self, conn, url, verify, cert):
        # verify=True would open certifi's cacert.pem. Avast Web Shield can
        # redirect that open to \\.\aswMonFltProxy\... and raise PermissionError.
        # The SSLContext already trusts the OS (and Avast's local CA).
        if verify is True:
            conn.ca_certs = None
            conn.ca_cert_dir = None
            if hasattr(conn, "ca_cert_data"):
                conn.ca_cert_data = None
            conn.cert_reqs = "CERT_REQUIRED" if url.lower().startswith("https") else "CERT_NONE"
            if cert:
                if not isinstance(cert, str):
                    conn.cert_file = cert[0]
                    conn.key_file = cert[1]
                else:
                    conn.cert_file = cert
                    conn.key_file = None
            else:
                conn.cert_file = None
                conn.key_file = None
            return
        return super().cert_verify(conn, url, verify, cert)


def install() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _clear_unusable_ca_env()
    _orig_init = requests.Session.__init__

    def _session_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        adapter = SystemCertAdapter()
        self.mount("https://", adapter)

    requests.Session.__init__ = _session_init  # type: ignore[method-assign]
    _PATCHED = True


install()
