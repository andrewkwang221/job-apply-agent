"""Tests for antivirus-safe HTTPS CA handling in utils.ssl_compat."""
import os
from types import SimpleNamespace
from unittest.mock import patch

from utils.ssl_compat import (
    SystemCertAdapter,
    _clear_unusable_ca_env,
    _is_usable_cafile,
)


class TestIsUsableCafile:
    def test_rejects_empty(self):
        assert _is_usable_cafile("") is False

    def test_rejects_avast_device_path(self):
        assert _is_usable_cafile(r"\\.\aswMonFltProxy\1b97150d49cb2bdf") is False

    def test_rejects_forward_slash_device_path(self):
        assert _is_usable_cafile("//./aswMonFltProxy/abc") is False

    def test_accepts_real_file(self, tmp_path):
        pem = tmp_path / "cacert.pem"
        pem.write_text("dummy", encoding="utf-8")
        assert _is_usable_cafile(str(pem)) is True


class TestClearUnusableCaEnv:
    def test_pops_asw_mon_flt_proxy(self):
        env = {
            "SSL_CERT_FILE": r"\\.\aswMonFltProxy\1b97150d49cb2bdf",
            "REQUESTS_CA_BUNDLE": r"\\.\aswMonFltProxy\1b97150d49cb2bdf",
            "CURL_CA_BUNDLE": r"\\.\aswMonFltProxy\1b97150d49cb2bdf",
        }
        with patch.dict(os.environ, env, clear=False):
            cleared = _clear_unusable_ca_env()
            assert set(cleared) == {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
            assert "SSL_CERT_FILE" not in os.environ
            assert "REQUESTS_CA_BUNDLE" not in os.environ
            assert "CURL_CA_BUNDLE" not in os.environ

    def test_keeps_readable_bundle(self, tmp_path):
        pem = tmp_path / "cacert.pem"
        pem.write_text("dummy", encoding="utf-8")
        with patch.dict(os.environ, {"SSL_CERT_FILE": str(pem)}, clear=False):
            assert _clear_unusable_ca_env() == []
            assert os.environ["SSL_CERT_FILE"] == str(pem)


class TestCertVerifySkipsCertifi:
    def test_verify_true_does_not_set_ca_certs(self):
        adapter = SystemCertAdapter()
        conn = SimpleNamespace(
            ca_certs="sentinel",
            ca_cert_dir="sentinel",
            ca_cert_data="sentinel",
            cert_reqs=None,
            cert_file="keep-me",
            key_file="keep-me",
        )
        adapter.cert_verify(conn, "https://remotive.com/api/remote-jobs", verify=True, cert=None)
        assert conn.ca_certs is None
        assert conn.ca_cert_dir is None
        assert conn.ca_cert_data is None
        assert conn.cert_reqs == "CERT_REQUIRED"
        assert conn.cert_file is None
        assert conn.key_file is None

    def test_verify_path_still_delegates(self, tmp_path):
        pem = tmp_path / "ca.pem"
        pem.write_text("dummy", encoding="utf-8")
        adapter = SystemCertAdapter()
        conn = SimpleNamespace(
            ca_certs=None,
            ca_cert_dir=None,
            cert_reqs=None,
            cert_file=None,
            key_file=None,
        )
        adapter.cert_verify(conn, "https://example.com", verify=str(pem), cert=None)
        assert conn.ca_certs == str(pem)
        assert conn.cert_reqs == "CERT_REQUIRED"
