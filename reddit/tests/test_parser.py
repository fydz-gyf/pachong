from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reddit_scraper.parser.shreddit import parse_comments, parse_post

POST = '''
<shreddit-post id="t3_abc" subreddit-name="test" subreddit-id="t5_x" post-title="Hello" author="op"
 created-timestamp="2026-01-01T00:00:00+0000" score="12" upvote-ratio="0.9" comment-count="2"
 post-type="self" domain="self.test" permalink="/r/test/comments/abc/hello/" user-logged-in>
 <div id="t3_abc-post-rtjson-content"><p>Post body</p></div>
</shreddit-post>
'''

COMMENTS = '''
<shreddit-comment thingId="t1_c1" postId="t3_abc" depth="0" author="a" created="2026-01-01" score="5" permalink="/r/test/comments/abc/comment/c1/">
 <div id="t1_c1-comment-rtjson-content"><p>First</p></div>
</shreddit-comment>
<shreddit-comment thingId="t1_c2" postId="t3_abc" parentId="t1_c1" depth="1" author="b" score="2">
 <div id="t1_c2-comment-rtjson-content"><p>Reply</p></div>
</shreddit-comment>
<faceplate-partial class="more-comments-partial" src="/svc/shreddit/more-comments/r/test/t3_abc?top-level=1" method="post">
 <input type="hidden" name="cursor" value="CURSOR123">
</faceplate-partial>
'''


def test_post():
    p = parse_post(POST)
    assert p["post_id"] == "t3_abc"
    assert p["title"] == "Hello"
    assert p["text"] == "Post body"
    assert p["logged_in_marker"] is True


def test_comments():
    page = parse_comments(COMMENTS)
    assert len(page.comments) == 2
    assert page.comments[1]["parent_id"] == "t1_c1"
    assert page.comments[1]["body"] == "Reply"
    assert len(page.loaders) == 1
    assert page.loaders[0].top_level is True
    assert page.loaders[0].cursor == "CURSOR123"


if __name__ == "__main__":
    test_post()
    test_comments()
    print("TESTS: OK")
