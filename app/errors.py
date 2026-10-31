class ConfigError(Exception):
    """Bad or missing configuration."""


class MediaError(Exception):
    """Upload rejected: missing, empty, wrong type, too big, corrupt."""


class RateLimited(Exception):
    """Gemini said 429. Retry later."""


class Transient(Exception):
    """Temporary failure (5xx, network). Retry later."""


class Permanent(Exception):
    """Retrying will not help (bad input, blocked content, auth)."""
