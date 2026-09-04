from __future__ import annotations

import unittest
from bs4 import BeautifulSoup

from reddit_scraper.parser.media import extract_comment_media, extract_media, kind_from_url, normalize_url, video_id


class MediaParserTests(unittest.TestCase):
    def test_target_post_scope_excludes_comment_and_recommendation_media(self):
        html = '''
        <html><body>
          <shreddit-post id="t3_target" view-context="CommentsPage" post-type="image"
            post-title="Target" content-href="https://i.redd.it/target.jpg">
            <faceplate-img src="https://preview.redd.it/target.jpg?width=640&crop=smart&auto=webp"></faceplate-img>
          </shreddit-post>
          <shreddit-comment thingId="t1_c1"><div slot="comment"><img src="https://i.redd.it/comment.gif?format=mp4"></div></shreddit-comment>
          <shreddit-post id="t3_rec" view-context="Feed" post-type="image" content-href="https://i.redd.it/recommendation.jpg"></shreddit-post>
        </body></html>
        '''
        items = extract_media(html)
        urls = [x.url for x in items]
        self.assertIn('https://i.redd.it/target.jpg', urls)
        self.assertFalse(any('comment.gif' in x for x in urls), urls)
        self.assertFalse(any('recommendation.jpg' in x for x in urls), urls)
        self.assertEqual(1, len(items), urls)

    def test_gallery_reads_only_images_inside_target_post(self):
        html = '''
        <html><body>
          <shreddit-post id="t3_g" view-context="CommentsPage" post-type="gallery" content-href="https://www.reddit.com/gallery/abc">
            <faceplate-img src="https://preview.redd.it/abc-v0-1.jpg?width=320" srcset="https://preview.redd.it/abc-v0-1.jpg?width=320 320w, https://preview.redd.it/abc-v0-1.jpg?width=1080 1080w"></faceplate-img>
            <faceplate-img src="https://preview.redd.it/abc-v0-2.jpg?width=1080"></faceplate-img>
          </shreddit-post>
          <img src="https://i.redd.it/outside.jpg">
        </body></html>
        '''
        items = extract_media(html)
        urls = [x.url for x in items]
        self.assertEqual(2, len(items), urls)
        self.assertTrue(any('abc-v0-1.jpg' in x for x in urls))
        self.assertTrue(any('abc-v0-2.jpg' in x for x in urls))
        self.assertFalse(any('outside.jpg' in x for x in urls))
        self.assertFalse(any('/gallery/' in x for x in urls))

    def test_video_manifest_gets_poster_and_dedupes_dash_variants(self):
        html = '''
        <html><head><meta property="og:image" content="https://preview.redd.it/poster.jpg"></head><body>
          <shreddit-post id="t3_v" view-context="CommentsPage" post-type="video" content-href="https://v.redd.it/vid123/DASHPlaylist.mpd">
            <shreddit-player poster="https://preview.redd.it/poster.jpg">
              <video><source src="https://v.redd.it/vid123/DASH_1080.mp4"></video>
            </shreddit-player>
          </shreddit-post>
        </body></html>
        '''
        items = extract_media(html)
        self.assertEqual(1, len(items), [x.to_dict() for x in items])
        self.assertEqual('video', items[0].kind)
        self.assertIn('DASHPlaylist.mpd', items[0].url)
        self.assertEqual('https://preview.redd.it/poster.jpg', items[0].poster)


    def test_vreddit_base_and_dash_source_collapse_to_one_video(self):
        html = '''
        <html><body>
          <shreddit-post id="t3_v2" view-context="CommentsPage" post-type="video" content-href="https://v.redd.it/vid999">
            <shreddit-player><video><source src="https://v.redd.it/vid999/DASH_1080.mp4"></video></shreddit-player>
          </shreddit-post>
        </body></html>
        '''
        items = extract_media(html)
        self.assertEqual(1, len(items), [x.to_dict() for x in items])
        self.assertEqual('video', items[0].kind)
        self.assertEqual('vid999', video_id(items[0].url))

    def test_self_post_does_not_take_page_meta_image(self):
        html = '''<html><head><meta property="og:image" content="https://preview.redd.it/noise.jpg"></head><body>
        <shreddit-post id="t3_s" view-context="CommentsPage" post-type="self"></shreddit-post></body></html>'''
        self.assertEqual([], extract_media(html))

    def test_comment_media_is_scoped_to_comment_body(self):
        tag = BeautifulSoup('''
        <shreddit-comment thingId="t1_c">
          <img src="https://styles.redditmedia.com/avatar.png">
          <div slot="comment">
            <img src="https://i.redd.it/a.gif?format=mp4">
            <faceplate-img src="https://i.imgur.com/b.png"></faceplate-img>
          </div>
        </shreddit-comment>
        ''', 'html.parser').find('shreddit-comment')
        items = extract_comment_media(tag)
        self.assertEqual(2, len(items))
        kinds = {x.url: x.kind for x in items}
        self.assertEqual('video', kinds['https://i.redd.it/a.gif?format=mp4'])
        self.assertEqual('image', kinds['https://i.imgur.com/b.png'])

    def test_helpers(self):
        self.assertEqual('video', kind_from_url('https://i.redd.it/a.gif?format=mp4'))
        self.assertEqual('image', kind_from_url('https://i.redd.it/a.gif'))
        self.assertEqual('video', kind_from_url('https://v.redd.it/abc123'))
        self.assertEqual('abc123', video_id('https://v.redd.it/abc123/DASH_720.mp4'))
        self.assertEqual('https://i.redd.it/a.jpg?x=1&y=2', normalize_url('https://i.redd.it/a.jpg?x=1&amp;y=2#frag'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
