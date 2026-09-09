"""Trust the OS certificate store for outbound HTTPS.

requests/urllib3 verify against Mozilla's certifi bundle. Antivirus HTTPS
scanning (e.g. Avast Web/Mail Shield) presents a local CA that is in the
Windows store but not in certifi, which causes CERTIFICATE_VERIFY_FAILED.

Avast also exposes a named device (\\\\.\\aswMonFltProxy\\...) via
SSL_CERT_FILE / REQUESTS_CA_BUNDLE, or intercepts CA file opens.
ssl.create_default_context() calls set_default_verify_paths() which tries
to open that device and raises PermissionError. On Windows, load the
system store only and cache the SSLContext so this work happens once.
"""
from __future__ import annotations

import os
import ssl
import sys
import threading

import requests
from requests.adapters import HTTPAdapter

_PATCHED = False
_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_CONTEXT_LOCK = threading.Lock()


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
    return cleared


def _load_windows_store(ctx: ssl.SSLContext) -> None:
    """Load Windows CA/ROOT stores without set_default_verify_paths()."""
    load_store = getattr(ctx, "_load_windows_store_certs", None)
    stores = getattr(ctx, "_windows_cert_stores", ("CA", "ROOT"))
    if load_store is not None:
        for storename in stores:
            try:
                load_store(storename, ssl.Purpose.SERVER_AUTH)
            except OSError:
                pass
        return
    for store in ("CA", "ROOT"):
        try:
            for der, encoding, trust in ssl.enum_certificates(store):
                if encoding != "x509_asn":
                    continue
                if trust is not True and ssl.Purpose.SERVER_AUTH.oid not in (trust or ()):
                    continue
                try:
                    ctx.load_verify_locations(cadata=der)
                except ssl.SSLError:
                    pass
        except OSError:
            pass


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if sys.platform == "win32":
        # create_default_context() -> load_default_certs() -> set_default_verify_paths()
        # opens Avast's aswMonFltProxy device. Use the Windows store only.
        _load_windows_store(ctx)
        return ctx
    try:
        ctx = ssl.create_default_context()
    except OSError:
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
    except Exception:
        pass
    return ctx


def _ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    with _SSL_CONTEXT_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = _build_ssl_context()
        return _SSL_CONTEXT


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
        use_os_store = verify is True or (
            isinstance(verify, str) and not _is_usable_cafile(verify)
        )
        if use_os_store:
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
