# TikTok collector

This directory contains the TikTok AdsPower/CDP collector. It connects to an
already-running AdsPower browser, searches the keywords in `keywords.txt`,
and exports video and comment data under a timestamped `data/` directory.

## Run on Windows

1. Start an AdsPower profile and open TikTok in a browser tab.
2. Replace the commented examples in `keywords.txt` with one search keyword
   per line. The tracked file intentionally contains comments only, so a fresh
   checkout does not start a crawl until you add your own keywords.
3. Run `start_tiktok.bat`, or use the repository launcher:

```powershell
py -3 unified_scraper.py --platform tiktok
```

The collector can auto-detect an active AdsPower TikTok page when launched by
Python. The standalone BAT first finds the page's CDP debug port and passes the
same defaults used by the original script (`10` videos, `20` search scrolls,
`80` comment scrolls, and reply expansion).

Optional `ADSPOWER_API_KEY` is read from the process environment only; never
save an API key, cookie, browser profile, or captured output in this directory.

Install dependencies with `pip install -r requirements.txt`.
