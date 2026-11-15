"""Offline transfer deadlines, including reads hidden inside HTTP chunk framing."""
import contextlib
import http.client
import importlib.util
import json
from pathlib import Path
import signal
import socket
import threading
import time
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('transfer_agent', Path(__file__).resolve().parents[1] / 'scripts/upstream-agent.py')
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


@contextlib.contextmanager
def drip_response(chunked=False):
    reader, writer = socket.socketpair()
    reader.settimeout(1)
    stop = threading.Event()
    headers = b'Transfer-Encoding: chunked' if chunked else b'Content-Length: 100000'
    writer.sendall(b'HTTP/1.1 200 OK\r\n' + headers + b'\r\n\r\n')
    response = http.client.HTTPResponse(reader)
    response.begin()

    def send():
        try:
            # An unterminated chunk extension keeps readline busy across raw reads.
            if chunked:
                writer.sendall(b'1;')
            while not stop.wait(.02):
                writer.sendall(b'x')
        except OSError:
            pass

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    try:
        yield response
    finally:
        stop.set()
        response.close()
        reader.close()
        writer.close()
        sender.join(1)


def session():
    value = agent.Session.__new__(agent.Session)
    value.deadline = time.monotonic() + 10
    value.done = False
    value.calls = value.output = value.downloaded = 0
    value.actions, value.hash_cache = [], {}
    return value


@unittest.skipUnless(hasattr(signal, 'setitimer'), 'requires POSIX interval timers')
class TransferDeadlineTests(unittest.TestCase):
    def test_response_header_drip_is_interrupted_and_connection_closed(self):
        reader, writer = socket.socketpair()
        reader.settimeout(1)
        writer.sendall(b'HTTP/1.1 200 OK\r\nX-Test: ')
        response = http.client.HTTPResponse(reader)
        connection = mock.Mock()
        connection.getresponse.side_effect = response.begin
        stop = threading.Event()

        def send():
            try:
                while not stop.wait(.02):
                    writer.sendall(b'x')
            except OSError:
                pass

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        try:
            start = time.monotonic()
            with mock.patch.object(agent, 'PinnedHTTPSConnection', return_value=connection), self.assertRaisesRegex(
                    agent.ToolError, 'timed out'):
                agent._request(agent.check_url('https://example.org/a'), ['93.184.216.34'], start + .2)
            self.assertLess(time.monotonic() - start, 1.5)
            connection.close.assert_called_once()
        finally:
            stop.set()
            response.close()
            reader.close()
            writer.close()
            sender.join(1)

    def test_session_deadline_cannot_extend_download_limit(self):
        with drip_response() as response:
            connection = mock.Mock()
            start = time.monotonic()
            with mock.patch.object(agent, 'FETCH_SECONDS', .1), mock.patch.object(
                    agent, 'resolve_public', return_value=['93.184.216.34']), mock.patch.object(
                    agent, '_request', return_value=(connection, response)), self.assertRaisesRegex(
                    agent.ToolError, 'timed out'):
                agent.fetch('https://example.org/a', 'text', deadline=start + 10)
            self.assertLess(time.monotonic() - start, 1.5)
            connection.close.assert_called_once()

    def test_raw_body_and_chunk_headers_cannot_drip_past_deadline(self):
        for chunked in (False, True):
            with self.subTest(chunked=chunked), drip_response(chunked) as response:
                start = time.monotonic()
                seen = []
                with self.assertRaisesRegex(agent.ToolError, 'timed out'):
                    agent._body(response, 'sha256', start + .2, on_bytes=seen.append)
                self.assertLess(time.monotonic() - start, 1.5)
                if not chunked:
                    self.assertGreater(sum(seen), 0)

    def test_session_deadline_bounds_hash_and_text_and_closes_connection(self):
        for mode in ('sha256', 'text'):
            with self.subTest(mode=mode), drip_response() as response:
                value = session()
                value.deadline = time.monotonic() + .2
                connection = mock.Mock()
                start = time.monotonic()
                with mock.patch.object(agent, 'resolve_public', return_value=['93.184.216.34']), mock.patch.object(
                        agent, '_request', return_value=(connection, response)):
                    text, error = value.run('fetch_url', {'url': 'https://example.org/a', 'mode': mode})
                self.assertTrue(error)
                self.assertIn('timed out', json.loads(text)['error'])
                self.assertLess(time.monotonic() - start, 1.5)
                connection.close.assert_called_once()
                if mode == 'sha256':
                    self.assertGreater(value.downloaded, 0)

    def test_transfer_errors_are_tool_results_and_keep_charged_bytes(self):
        for failure in (OSError('reset'), http.client.IncompleteRead(b'')):
            with self.subTest(failure=type(failure).__name__):
                value = session()
                response, connection = mock.Mock(status=200), mock.Mock()
                response.getheader.return_value = None
                response.read1.side_effect = [b'abc', failure]
                with mock.patch.object(agent, 'resolve_public', return_value=['93.184.216.34']), mock.patch.object(
                        agent, '_request', return_value=(connection, response)):
                    text, error = value.run('fetch_url', {'url': 'https://example.org/a', 'mode': 'sha256'})
                self.assertTrue(error)
                self.assertIn('could not fetch', json.loads(text)['error'])
                self.assertEqual(value.downloaded, 3)
                connection.close.assert_called_once()

    def test_alarm_handler_is_restored_after_timeout(self):
        previous = signal.getsignal(signal.SIGALRM)
        handler = lambda *args: None
        signal.signal(signal.SIGALRM, handler)
        try:
            with drip_response(True) as response, self.assertRaises(agent.ToolError):
                agent._body(response, 'text', time.monotonic() + .1)
            self.assertIs(signal.getsignal(signal.SIGALRM), handler)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        finally:
            signal.signal(signal.SIGALRM, previous)

    def test_active_alarm_is_preserved_and_transfer_refused(self):
        previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, lambda *args: None)
        signal.setitimer(signal.ITIMER_REAL, 10, 2)
        response = mock.Mock()
        response.getheader.return_value = None
        try:
            with self.assertRaises(agent.ToolError):
                agent._body(response, 'text', time.monotonic() + 1)
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(remaining, 8)
            self.assertEqual(interval, 2)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_worker_thread_fails_closed_without_reading(self):
        response = mock.Mock()
        response.getheader.return_value = None
        errors = []

        def fetch():
            try:
                agent._body(response, 'text', time.monotonic() + 1)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=fetch)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], agent.ToolError)
        response.read1.assert_not_called()


if __name__ == '__main__':
    unittest.main()
