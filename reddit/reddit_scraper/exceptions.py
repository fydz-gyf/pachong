class RedditScraperError(RuntimeError):
    pass


class VerificationRequired(RedditScraperError):
    pass


class HardStopHTTP(RedditScraperError):
    def __init__(self, status_code: int, message: str = ""):
        self.status_code = int(status_code)
        super().__init__(message or f"Hard-stop HTTP status: {status_code}")


class RequestBudgetReached(RedditScraperError):
    pass
