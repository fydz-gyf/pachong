from __future__ import annotations

from urllib.parse import urlparse

from reddit_scraper.cdp.browser import BrowserCDP, choose_browser, discover_browsers
from reddit_scraper.models import BootstrapData


def bootstrap_from_browser(post_url: str, timeout_seconds: int = 60) -> BootstrapData:
    browser = choose_browser(discover_browsers())
    cdp = BrowserCDP(browser.browser_ws)
    cdp.connect()
    target_id: str | None = None
    try:
        res = cdp.call("Target.createTarget", {"url": "about:blank"}, timeout=12)
        target_id = str(res.get("targetId") or "")
        if not target_id:
            raise RuntimeError("Target.createTarget did not return targetId")
        sid = cdp.wait_session(target_id, timeout=15)
        cdp.call("Page.enable", session_id=sid, timeout=10)
        cdp.call("Page.navigate", {"url": post_url}, session_id=sid, timeout=15)
        cdp.csrf_event.wait(timeout=max(10, int(timeout_seconds)))
        if not cdp.csrf:
            raise RuntimeError(
                "Could not observe Reddit CSRF automatically. Keep a logged-in Reddit AdsPower profile open and rerun."
            )
        cookies = cdp.get_cookies(sid)
        if not cookies:
            raise RuntimeError("No Reddit cookies returned from browser session")
        return BootstrapData(
            browser=browser, cookies=cookies, csrf_token=cdp.csrf,
            client_version=cdp.client_version,
            csrf_source_path=urlparse(cdp.csrf_source or "").path if cdp.csrf_source else None,
        )
    finally:
        if target_id:
            try:
                cdp.call("Target.closeTarget", {"targetId": target_id}, timeout=5)
            except Exception:
                pass
        cdp.close()
