from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys
import types

try:
    import curl_cffi  # noqa: F401
except ModuleNotFoundError:
    fake_requests = types.SimpleNamespace(Session=lambda *a, **k: None)
    fake_pkg = types.ModuleType("curl_cffi")
    fake_pkg.requests = fake_requests
    sys.modules["curl_cffi"] = fake_pkg

from reddit_scraper.http.client import RedditHTTPClient
from reddit_scraper.models import BootstrapData, BrowserInfo
from reddit_scraper.safety.guard import GuardConfig, RequestGuard


class FakeResp:
    def __init__(self, status, ctype, chunks, headers=None):
        self.status_code = status
        self._headers = {'content-type': ctype}
        self._headers.update(headers or {})
        self._chunks = chunks
        self.closed = False

    @property
    def headers(self):
        return self._headers

    def iter_content(self, n):
        yield from self._chunks

    def close(self):
        self.closed = True


class SeqSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def make_client(session):
    browser = BrowserInfo(name='x', pid=0, user_data_dir='', port=0, browser='chrome', browser_ws='')
    boot = BootstrapData(browser=browser, cookies=[], csrf_token='', client_version=None, csrf_source_path=None)
    guard = RequestGuard(GuardConfig(max_requests=999, hard_stop_status_codes=(403,429,503), rate_remaining_pause_threshold=0, rate_reset_padding_seconds=0))
    client = RedditHTTPClient(boot, {'timeout_seconds': 5, 'min_interval_seconds': 0, 'media_network_retries': 1}, guard)
    client.session = session
    return client


class MediaClientTests(unittest.TestCase):
    def test_streams_to_file(self):
        session = SeqSession([FakeResp(200, 'image/jpeg', [b'a' * 700, b'b' * 700])])
        client = make_client(session)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'x.part'
            code, ctype, flag, size = client.fetch_media_to_file('https://i.redd.it/x.jpg', target, retries=0)
            self.assertEqual(200, code)
            self.assertEqual('OK', flag)
            self.assertEqual(1400, size)
            self.assertTrue(target.exists())
            self.assertEqual(1400, target.stat().st_size)

    def test_rejects_html_wrapper_and_deletes_part(self):
        session = SeqSession([FakeResp(200, 'text/html', [b'<!DOCTYPE html><html>login</html>'])])
        client = make_client(session)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'x.part'
            code, _, flag, _ = client.fetch_media_to_file('https://reddit.com/media', target, retries=0)
            self.assertEqual(200, code)
            self.assertEqual('HTML_WRAPPER', flag)
            self.assertFalse(target.exists())

    def test_403_is_not_retried(self):
        session = SeqSession([FakeResp(403, 'text/plain', [b'no'])])
        client = make_client(session)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'x.part'
            code, _, flag, _ = client.fetch_media_to_file('https://i.redd.it/x.jpg', target, retries=3)
            self.assertEqual(403, code)
            self.assertEqual('HTTP_403', flag)
            self.assertEqual(1, session.calls)

    def test_too_large_uses_content_length_without_buffering(self):
        session = SeqSession([FakeResp(200, 'video/mp4', [b'x'], {'content-length': '999999'})])
        client = make_client(session)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'x.part'
            code, _, flag, size = client.fetch_media_to_file('https://v.redd.it/x/DASH_720.mp4', target, max_bytes=1000, retries=0)
            self.assertEqual(200, code)
            self.assertEqual('TOO_LARGE', flag)
            self.assertEqual(0, size)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
