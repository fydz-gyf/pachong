from __future__ import annotations

import logging

from ..config import Settings
from .adspower import AdsPowerAPIError, AdsPowerBrowserInfo, AdsPowerClient
from .cdp import CDPAuthProvider
from .process_discovery import BrowserProcessDiscoveryError, resolve_adspower_cdp


class BrowserAuthProvider:
    """Resolve one browser identity and expose a CDP-based auth interface.

    AdsPower resolution order:
    1. Explicit --adspower-cdp-port: attach directly to the running profile.
    2. AdsPower Local API when available.
    3. Windows process discovery fallback (no AdsPower Local API required).
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cdp = CDPAuthProvider(settings)
        self.adspower_info: AdsPowerBrowserInfo | None = None
        self.resolved_mode = "saved-state"
        self.proxy_url = ""
        self._ads_bind_attempted = False

    def load_state(self) -> dict | None:
        return self._cdp.load_state()

    def _bind_process_discovery(self) -> None:
        candidate = resolve_adspower_cdp(self.settings.adspower_cdp_port)
        self.settings.cdp_host = candidate.host
        self.settings.cdp_port = candidate.port
        self._cdp = CDPAuthProvider(self.settings)
        self.resolved_mode = "adspower-process"
        self.proxy_url = candidate.proxy_url if self.settings.adspower_sync_proxy else ""
        if self.proxy_url:
            logging.info("HTTP proxy discovered from running AdsPower Chromium process")
        logging.info("AdsPower browser attached without Local API: %s", candidate.label)

    def _bind_adspower(self) -> None:
        self._ads_bind_attempted = True

        # Explicit CDP port means: do not call the paid Local API at all.
        if self.settings.adspower_cdp_port:
            self._bind_process_discovery()
            return

        has_selector = bool(self.settings.adspower_profile_id or self.settings.adspower_profile_no)

        # For the common free-plan case (user manually opens one AdsPower profile),
        # prefer local process discovery. This avoids requiring Local API access.
        process_error: Exception | None = None
        if self.settings.adspower_process_discovery and not has_selector:
            try:
                self._bind_process_discovery()
                return
            except BrowserProcessDiscoveryError as e:
                process_error = e
                logging.info("AdsPower process discovery did not resolve a profile: %s", e)

        api_error: Exception | None = None
        try:
            info = AdsPowerClient(self.settings).attach_or_start()
            self.adspower_info = info
            self.settings.cdp_host = info.debug_host
            self.settings.cdp_port = info.debug_port
            self._cdp = CDPAuthProvider(self.settings)
            self.resolved_mode = "adspower-api"
            self.proxy_url = info.proxy_url
            logging.info(
                "AdsPower profile attached through Local API: profile=%s debug=%s:%s",
                info.profile_id,
                info.debug_host,
                info.debug_port,
            )
            return
        except AdsPowerAPIError as e:
            api_error = e
            if not self.settings.adspower_process_discovery:
                raise
            logging.warning("AdsPower Local API unavailable; trying local process discovery: %s", e)

        # Explicit profile selectors need Local API to map profile -> debug port,
        # but if that API is unavailable we can still attach when only one suitable
        # running profile exists (or the user opens Walmart in the intended one).
        try:
            self._bind_process_discovery()
            return
        except BrowserProcessDiscoveryError as e:
            process_error = e

        parts = []
        if api_error is not None:
            parts.append(f"Local API: {api_error}")
        if process_error is not None:
            parts.append(f"process discovery: {process_error}")
        raise AdsPowerAPIError("AdsPower browser could not be resolved. " + "; ".join(parts))

    def prepare_if_requested(self) -> None:
        mode = self.settings.browser_mode
        wants_ads = mode == "adspower" or bool(
            self.settings.adspower_profile_id
            or self.settings.adspower_profile_no
            or self.settings.adspower_cdp_port
        )
        if wants_ads:
            self._bind_adspower()
            return
        if mode == "chrome":
            self.resolved_mode = "chrome"
            return
        if mode == "auto":
            if self._cdp.available():
                self.resolved_mode = "chrome"
                return
            # Auto mode may reuse an already-running AdsPower profile without
            # requiring Local API. Failure is non-fatal because saved cookies can
            # still be enough for the first HTTP attempt.
            if self.settings.adspower_process_discovery:
                try:
                    self._bind_process_discovery()
                except Exception:
                    pass

    def available(self) -> bool:
        if self._cdp.available():
            return True

        wants_ads = (
            self.settings.browser_mode == "adspower"
            or bool(self.settings.adspower_profile_id)
            or bool(self.settings.adspower_profile_no)
            or bool(self.settings.adspower_cdp_port)
        )
        if wants_ads:
            try:
                self._bind_adspower()
                return self._cdp.available()
            except (AdsPowerAPIError, BrowserProcessDiscoveryError) as e:
                logging.warning("AdsPower browser is not available: %s", e)
                return False

        if self.settings.browser_mode == "auto" and self.settings.adspower_process_discovery:
            try:
                self._bind_process_discovery()
                return self._cdp.available()
            except Exception:
                return False
        return False

    def fetch_html_in_browser(self, url: str) -> dict:
        if not self.available():
            raise RuntimeError(
                f"Cannot connect to browser CDP at {self.settings.cdp_host}:{self.settings.cdp_port}"
            )
        return self._cdp.fetch_html_in_browser(url, timeout=self.settings.request_timeout)

    def save_state(self) -> dict:
        if not self._cdp.available():
            wants_ads = (
                self.settings.browser_mode == "adspower"
                or bool(self.settings.adspower_profile_id)
                or bool(self.settings.adspower_profile_no)
                or bool(self.settings.adspower_cdp_port)
            )
            if wants_ads:
                self._bind_adspower()
            if not self._cdp.available():
                raise RuntimeError(
                    f"Cannot connect to browser CDP at {self.settings.cdp_host}:{self.settings.cdp_port}"
                )

        state = self._cdp.save_state()
        state.setdefault("browser", {})
        state["browser"].update(
            {
                "mode": self.resolved_mode,
                "cdp_host": self.settings.cdp_host,
                "cdp_port": self.settings.cdp_port,
            }
        )
        if self.adspower_info:
            state["browser"]["adspower_profile_id"] = self.adspower_info.profile_id

        from ..utils import atomic_write_json

        atomic_write_json(self.settings.paths.auth_state, state)
        return state
