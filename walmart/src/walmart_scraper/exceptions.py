class WalmartScraperError(RuntimeError):
    """Base application error."""


class WalmartBlockError(WalmartScraperError):
    """Walmart returned robot / captcha verification."""


class NetworkTransportError(WalmartScraperError):
    """HTTP/TLS/network request failed repeatedly."""


class ParseStructureError(WalmartScraperError):
    """Page loaded but expected product data structure was not found."""
