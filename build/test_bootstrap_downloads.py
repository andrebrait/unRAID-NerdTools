#!/usr/bin/env python3
"""Run with python3 -m unittest discover -s build -p 'test_*.py'."""
import hashlib
import io
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).with_name("unraid-build-root")))
import bootstrap  # noqa: E402

PUBLIC = "93.184.216.34"
PUBLIC_B = "1.1.1.1"
PUBLIC_V6 = "2606:4700:4700::1111"
PRIVATE = "10.0.0.5"


def response(status, headers=(), body=b""):
    head = "".join(f"{name}: {value}\r\n" for name, value in headers)
    return f"HTTP/1.1 {status}\r\n{head}Connection: close\r\n\r\n".encode() + body


def ok(body):
    return response("200 OK", [("Content-Length", str(len(body)))], body)


def redirect(location):
    return response("302 Found", [("Location", location), ("Content-Length", "0")])


class FakeSocket:
    """In-memory socket: records bytes sent and serves the canned peer bytes."""

    def __init__(self, address, wire):
        self.address = address
        self.wire = wire
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode="rb", *args, **kwargs):
        return io.BytesIO(self.wire)

    def close(self):
        self.closed = True


class FakeInternet:
    """Fake public DNS and TCP/TLS transport; the real PinnedHTTPSConnection and http.client run on top."""

    def __init__(self, dns, servers):
        # host -> list of answer lists; each lookup consumes one, the last one repeats.
        self.dns = {host: list(answers) for host, answers in dns.items()}
        self.servers = servers  # pinned IP -> bytes the peer sends
        self.lookups = []
        self.connects = []  # (ip, port) passed to create_connection
        self.handshakes = []  # (ip, SNI) passed to wrap_socket
        self.sockets = []
        self.tls_failures = set()
        self.connect_failures = 0

    def getaddrinfo(self, host, port, *args, **kwargs):
        self.lookups.append(host)
        answers = self.dns.get(host)
        if not answers:
            raise socket.gaierror(f"no such host: {host}")
        addresses = answers.pop(0) if len(answers) > 1 else answers[0]
        return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                for address in addresses]

    def create_connection(self, address, timeout=None, source_address=None):
        ip, port = address
        self.connects.append((ip, port))
        if self.connect_failures:
            self.connect_failures -= 1
            raise ConnectionResetError("reset before handshake")
        if ip not in self.servers:
            raise AssertionError(f"unexpected connection to {ip}")
        sock = FakeSocket(ip, self.servers[ip])
        self.sockets.append(sock)
        return sock

    def wrap_socket(self, sock, server_hostname=None, **kwargs):
        self.handshakes.append((sock.address, server_hostname))
        if sock.address in self.tls_failures:
            raise ssl.SSLError("certificate verify failed")
        return sock


class PinnedDownloadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name) / "cache"

    def fetch(self, url, net, expected=b"artifact"):
        item = {"url": url, "file": "source.zip", "sha256": hashlib.sha256(expected).hexdigest()}
        with patch.object(socket, "getaddrinfo", net.getaddrinfo), \
                patch.object(socket, "create_connection", net.create_connection), \
                patch.object(ssl.SSLContext, "wrap_socket", lambda context, sock, **kwargs: net.wrap_socket(sock, **kwargs)):
            return bootstrap.fetch(item, self.cache)

    def test_public_https_is_pinned_to_validated_address_with_hostname_sni(self):
        net = FakeInternet({"cdn.example": [[PUBLIC]]}, {PUBLIC: ok(b"artifact")})
        path = self.fetch("https://cdn.example/source.zip", net)
        self.assertEqual(path.read_bytes(), b"artifact")
        self.assertEqual(net.lookups, ["cdn.example"])
        self.assertEqual(net.connects, [(PUBLIC, 443)])
        self.assertEqual(net.handshakes, [(PUBLIC, "cdn.example")])
        self.assertIn(b"GET /source.zip HTTP/1.1", net.sockets[0].sent)
        self.assertEqual(list(self.cache.iterdir()), [path])
        self.assertEqual(path.name, hashlib.sha256(b"artifact").hexdigest())

    def test_explicit_https_port_is_kept_for_connection(self):
        net = FakeInternet({"cdn.example": [[PUBLIC]]}, {PUBLIC: ok(b"artifact")})
        self.fetch("https://cdn.example:8443/source.zip", net)
        self.assertEqual(net.connects, [(PUBLIC, 8443)])
        self.assertEqual(net.handshakes, [(PUBLIC, "cdn.example")])

    def test_public_ipv6_answer_is_pinned(self):
        net = FakeInternet({"v6.example": [[PUBLIC_V6]]}, {PUBLIC_V6: ok(b"artifact")})
        self.fetch("https://v6.example/source.zip", net)
        self.assertEqual(net.connects, [(PUBLIC_V6, 443)])

    def test_non_public_answers_are_refused_before_any_connection(self):
        answers = ("127.0.0.1", "10.0.0.5", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
                   "::1", "fe80::1", "fc00::1", "ff02::1", "::ffff:10.0.0.5", "::ffff:127.0.0.1",
                   "2001:db8::1", "64:ff9b::a00:5")
        for answer in answers:
            with self.subTest(answer=answer):
                net = FakeInternet({"cdn.example": [[answer]]}, {})
                with self.assertRaisesRegex(ValueError, "non-public"):
                    self.fetch("https://cdn.example/source.zip", net)
                self.assertEqual(net.connects, [])

    def test_mixed_public_and_private_answers_are_refused(self):
        for answers in ([PUBLIC, PRIVATE], [PRIVATE, PUBLIC], [PUBLIC, "::ffff:127.0.0.1"]):
            with self.subTest(answers=answers):
                net = FakeInternet({"cdn.example": [answers]}, {PUBLIC: ok(b"artifact")})
                with self.assertRaisesRegex(ValueError, "non-public"):
                    self.fetch("https://cdn.example/source.zip", net)
                self.assertEqual(net.connects, [])

    def test_literal_non_public_hosts_fail_closed_without_dns_or_connection(self):
        net = FakeInternet({}, {})
        with self.assertRaisesRegex(ValueError, "non-public"):
            self.fetch("https://127.0.0.1/source.zip", net)
        self.assertEqual((net.lookups, net.connects), ([], []))

    def test_numeric_host_forms_are_validated_as_their_resolved_address(self):
        net = FakeInternet({"2130706433": [["127.0.0.1"]]}, {})
        with self.assertRaisesRegex(ValueError, "non-public"):
            self.fetch("https://2130706433/source.zip", net)
        self.assertEqual(net.connects, [])

    def test_unresolvable_host_fails_closed(self):
        net = FakeInternet({}, {})
        with self.assertRaisesRegex(ValueError, "does not resolve"):
            self.fetch("https://missing.example/source.zip", net)
        self.assertEqual(net.connects, [])

    def test_dns_is_resolved_once_and_connection_uses_only_the_validated_address(self):
        net = FakeInternet({"cdn.example": [[PUBLIC], [PRIVATE]]}, {PUBLIC: ok(b"artifact")})
        self.fetch("https://cdn.example/source.zip", net)
        self.assertEqual(net.lookups, ["cdn.example"])
        self.assertEqual(net.connects, [(PUBLIC, 443)])

    def test_public_to_private_redirect_is_refused_and_never_connects_privately(self):
        net = FakeInternet({"public.example": [[PUBLIC]], "internal.example": [[PRIVATE]]},
                           {PUBLIC: redirect("https://internal.example/secret")})
        with self.assertRaisesRegex(ValueError, "non-public"):
            self.fetch("https://public.example/source.zip", net)
        self.assertEqual(set(net.connects), {(PUBLIC, 443)})
        self.assertNotIn("internal.example", [host for _, host in net.handshakes])
        self.assertEqual(net.lookups.count("internal.example"), 3)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_public_to_public_redirect_is_validated_and_pinned_for_the_new_hop(self):
        net = FakeInternet({"public.example": [[PUBLIC]], "mirror.example": [[PUBLIC_B]]},
                           {PUBLIC: redirect("https://mirror.example/source.zip"), PUBLIC_B: ok(b"artifact")})
        path = self.fetch("https://public.example/source.zip", net)
        self.assertEqual(path.read_bytes(), b"artifact")
        self.assertEqual(net.lookups, ["public.example", "mirror.example"])
        self.assertEqual(net.connects, [(PUBLIC, 443), (PUBLIC_B, 443)])
        self.assertEqual(net.handshakes, [(PUBLIC, "public.example"), (PUBLIC_B, "mirror.example")])

    def test_redirect_to_plain_http_is_refused(self):
        net = FakeInternet({"public.example": [[PUBLIC]]}, {PUBLIC: redirect("http://public.example/source.zip")})
        with self.assertRaisesRegex(ValueError, "HTTPS URL required"):
            self.fetch("https://public.example/source.zip", net)
        self.assertEqual(net.connects, [(PUBLIC, 443)] * 3)

    def test_ambient_proxy_environment_is_ignored(self):
        net = FakeInternet({"cdn.example": [[PUBLIC]]}, {PUBLIC: ok(b"artifact")})
        with patch.dict(os.environ, {"https_proxy": "http://proxy.invalid:3128", "HTTPS_PROXY": "http://proxy.invalid:3128"}):
            self.fetch("https://cdn.example/source.zip", net)
        self.assertEqual(net.connects, [(PUBLIC, 443)])

    def test_valid_cached_input_is_used_without_any_network_lookup(self):
        self.cache.mkdir()
        path = self.cache / hashlib.sha256(b"artifact").hexdigest()
        path.write_bytes(b"artifact")
        net = FakeInternet({}, {})
        self.assertEqual(self.fetch("https://cdn.example/source.zip", net), path)
        self.assertEqual((net.lookups, net.connects), ([], []))

    def test_transient_connect_failure_is_retried_then_cached_with_correct_sha(self):
        net = FakeInternet({"cdn.example": [[PUBLIC]]}, {PUBLIC: ok(b"artifact")})
        net.connect_failures = 1
        path = self.fetch("https://cdn.example/source.zip", net)
        self.assertEqual(path.read_bytes(), b"artifact")
        self.assertEqual(net.connects, [(PUBLIC, 443), (PUBLIC, 443)])
        self.assertEqual(list(self.cache.iterdir()), [path])

    def test_wrong_bytes_are_retried_and_leave_no_cache_or_temporary_files(self):
        net = FakeInternet({"cdn.example": [[PUBLIC]]}, {PUBLIC: ok(b"tampered")})
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.fetch("https://cdn.example/source.zip", net)
        self.assertEqual(len(net.connects), 3)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_truncated_download_is_retried_and_leaves_no_partial_data(self):
        short = response("200 OK", [("Content-Length", "100")], b"partial")
        net = FakeInternet({"cdn.example": [[PUBLIC]]}, {PUBLIC: short})
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.fetch("https://cdn.example/source.zip", net)
        self.assertEqual(len(net.connects), 3)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_tls_failure_closes_the_socket_and_is_retried(self):
        net = FakeInternet({"cdn.example": [[PUBLIC]]}, {PUBLIC: ok(b"artifact")})
        net.tls_failures.add(PUBLIC)
        with self.assertRaises(OSError):
            self.fetch("https://cdn.example/source.zip", net)
        self.assertEqual(len(net.sockets), 3)
        self.assertTrue(all(sock.closed for sock in net.sockets))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_tls_failure_on_one_validated_address_falls_back_to_the_next_validated_address(self):
        net = FakeInternet({"cdn.example": [[PUBLIC, PUBLIC_V6]]},
                           {PUBLIC: ok(b"artifact"), PUBLIC_V6: ok(b"artifact")})
        net.tls_failures.add(PUBLIC)
        path = self.fetch("https://cdn.example/source.zip", net)
        self.assertEqual(path.read_bytes(), b"artifact")
        self.assertEqual(net.connects, [(PUBLIC, 443), (PUBLIC_V6, 443)])
        self.assertTrue(net.sockets[0].closed)


if __name__ == "__main__":
    unittest.main()
