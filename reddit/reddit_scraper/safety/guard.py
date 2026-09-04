from __future__ import annotations

import time
from dataclasses import dataclass

from reddit_scraper.exceptions import HardStopHTTP, RequestBudgetReached, VerificationRequired

VERIFY_MARKERS = (
    "you've been blocked by network security",
    "you have been blocked by network security",
    "verify you are human",
    "challenge-form",
    "captcha required",
    "security check required",
)


@dataclass
class GuardConfig:
    max_requests: int
    hard_stop_status_codes: tuple[int, ...]
    rate_remaining_pause_threshold: float
    rate_reset_padding_seconds: float
    stop_on_verification: bool = True


class RequestGuard:
    def __init__(self, cfg: GuardConfig):
        self.cfg = cfg
        self.requests_made = 0
        self.last_rate_remaining: float | None = None
        self.last_rate_reset: float | None = None

    def before_request(self):
        if self.requests_made >= self.cfg.max_requests:
            raise RequestBudgetReached(
                f"HTTP request budget reached ({self.requests_made}/{self.cfg.max_requests})"
            )
        if (
            self.last_rate_remaining is not None
            and self.last_rate_remaining <= self.cfg.rate_remaining_pause_threshold
            and self.last_rate_reset is not None
            and self.last_rate_reset > 0
        ):
            wait = self.last_rate_reset + self.cfg.rate_reset_padding_seconds
            print(f"[RATE] remaining={self.last_rate_remaining:g}; waiting {wait:.1f}s for Reddit reset window")
            time.sleep(wait)
            self.last_rate_remaining = None
            self.last_rate_reset = None

    def after_response(self, status_code: int, text: str, rate_remaining: float | None, rate_reset: float | None):
        self.requests_made += 1
        self.last_rate_remaining = rate_remaining
        self.last_rate_reset = rate_reset
        if status_code in self.cfg.hard_stop_status_codes:
            raise HardStopHTTP(status_code)
        if self.cfg.stop_on_verification:
            low = (text or "").lower()
            if any(m in low for m in VERIFY_MARKERS):
                raise VerificationRequired("Reddit verification/CAPTCHA detected; complete it manually in AdsPower and rerun")
