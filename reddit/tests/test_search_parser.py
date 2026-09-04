from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reddit_scraper.parser.search import parse_search_listing

sample = {
    "kind": "Listing",
    "data": {
        "after": "t3_next123",
        "children": [
            {"kind": "t3", "data": {
                "id": "abc123", "name": "t3_abc123", "subreddit": "testsub",
                "subreddit_id": "t5_test", "title": "Office chair test", "author": "tester",
                "created_utc": 1700000000, "score": 42, "upvote_ratio": 0.91, "num_comments": 12,
                "permalink": "/r/testsub/comments/abc123/test/", "url": "https://example.com/item",
                "domain": "example.com", "selftext": "body", "link_flair_text": "Discussion",
                "over_18": False, "spoiler": False, "locked": False, "stickied": False,
                "is_self": False, "post_hint": "link", "thumbnail": "https://example.com/thumb.jpg",
            }}
        ]
    }
}
rows, after = parse_search_listing(
    json.dumps(sample), keyword="office chair", sort="new", time_filter="month", start_rank=1
)
assert after == "t3_next123"
assert len(rows) == 1
r = rows[0]
assert r["post_id"] == "t3_abc123"
assert r["title"] == "Office chair test"
assert r["result_rank"] == 1
assert r["full_url"].startswith("https://www.reddit.com/r/testsub/")
print("SEARCH PARSER TEST: OK")
