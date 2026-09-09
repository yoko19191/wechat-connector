class ConnectorError(ValueError):
    """Public, sanitized errors safe to return through a tool or CLI."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
