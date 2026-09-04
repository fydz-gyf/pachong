# Reddit HTTP Scraper V1.2

Two modes are included:

1. **Post detail + comment tree** — existing Shreddit comment crawler.
2. **Keyword search -> post list** — logged-in Reddit search listing with cursor pagination.

## Search mode

Start `start.bat`, select mode `2`, then enter keywords directly or put one phrase per line in `input/keywords.txt`.

Supported search filters:

- Sort: `relevance`, `hot`, `top`, `new`, `comments`
- Time: `hour`, `day`, `week`, `month`, `year`, `all`
- Optional subreddit restriction
- Max posts per keyword, with checkpoint/resume

Search output columns include keyword, rank, post ID, subreddit, title, author, created time, score, upvote ratio, comment count, flair, URLs, self text, NSFW/spoiler/locked/stickied flags, etc.

The search client uses the current logged-in AdsPower/SunBrowser Reddit cookies. Authentication values are kept in memory and are not written to Excel/checkpoint files.

If Reddit changes or disables cookie-authenticated `search.json` in a specific session, the program will stop rather than trying to bypass verification. Capture the safe console error so the search transport can be switched to the current Shreddit search endpoint.
