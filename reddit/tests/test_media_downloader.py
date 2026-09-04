from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from reddit_scraper.models import MediaItem
from reddit_scraper.services.media import MediaDownloader


class FakeClient:
    def __init__(self):
        self.calls = []

    def fetch_media_to_file(self, url, target, referer='', max_bytes=0, retries=0):
        self.calls.append(url)
        target = Path(target)
        if '404' in url:
            return 404, 'text/html', 'HTTP_404', 0
        if 'DASH_AUDIO_128' in url or 'DASH_audio' in url:
            data = b'A' * 2048
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return 200, 'audio/mp4', 'OK', len(data)
        if url.endswith('.jpg'):
            data = b'J' * 2048
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return 200, 'image/jpeg', 'OK', len(data)
        if 'DASH_' in url or url.endswith('.mp4'):
            data = b'V' * 4096
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return 200, 'video/mp4', 'OK', len(data)
        return 404, 'text/plain', 'HTTP_404', 0


class MediaDownloaderTests(unittest.TestCase):
    def test_image_existing_file_is_success_and_not_refetched(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            dl = MediaDownloader(client, Path(tmp), {'ffmpeg_mux': False})
            item = MediaItem(kind='image', url='https://i.redd.it/x.jpg', source='test')
            rows, _ = dl.download_post('abc', [item])
            self.assertEqual('成功', rows[0]['status'])
            rows2, _ = dl.download_post('abc', [item])
            self.assertEqual('成功', rows2[0]['status'])
            self.assertEqual('已存在', rows2[0]['note'])
            self.assertEqual(1, len(client.calls))

    def test_vreddit_video_keeps_safe_link_without_download(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            dl = MediaDownloader(client, Path(tmp), {'ffmpeg_mux': False, 'max_requests_per_post': 10})
            item = MediaItem(kind='video', url='https://v.redd.it/vid123/DASH_720.mp4', source='test')
            rows, _ = dl.download_post('vid', [item])
            self.assertEqual('链接', rows[0]['status'])
            self.assertEqual('video', rows[0]['kind'])
            self.assertEqual('https://v.redd.it/vid123', rows[0]['url'])
            self.assertEqual('', rows[0]['path'])
            self.assertIn('视频仅保留链接', rows[0]['note'])
            self.assertEqual([], client.calls)

    def test_vreddit_video_failure_is_not_retried_or_written(self):
        class PosterClient(FakeClient):
            def fetch_media_to_file(self, url, target, referer='', max_bytes=0, retries=0):
                if 'poster.jpg' in url:
                    return super().fetch_media_to_file(url, target, referer, max_bytes, retries)
                self.calls.append(url)
                return 404, 'text/plain', 'HTTP_404', 0

        client = PosterClient()
        with tempfile.TemporaryDirectory() as tmp:
            dl = MediaDownloader(client, Path(tmp), {'ffmpeg_mux': False, 'max_requests_per_post': 10})
            item = MediaItem(kind='video', url='https://v.redd.it/404bad', source='test', poster='https://i.redd.it/poster.jpg')
            rows, _ = dl.download_post('x', [item])
            self.assertEqual('链接', rows[0]['status'])
            self.assertEqual('video', rows[0]['kind'])
            self.assertEqual('https://v.redd.it/404bad', rows[0]['url'])
            self.assertEqual('', rows[0]['path'])
            self.assertIn('视频仅保留链接', rows[0]['note'])
            self.assertEqual([], client.calls)

    def test_media_request_budget_stops_extra_requests(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            dl = MediaDownloader(client, Path(tmp), {'ffmpeg_mux': False, 'max_requests_per_post': 1})
            items = [
                MediaItem(kind='image', url='https://i.redd.it/1.jpg', source='test'),
                MediaItem(kind='image', url='https://i.redd.it/2.jpg', source='test'),
            ]
            rows, _ = dl.download_post('b', items)
            self.assertEqual('成功', rows[0]['status'])
            self.assertEqual('失败', rows[1]['status'])
            self.assertIn('预算', rows[1]['note'])
            self.assertEqual(1, len(client.calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
