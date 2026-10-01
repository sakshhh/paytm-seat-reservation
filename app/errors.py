class DomainError(Exception):
    """An expected business outcome (decline, bad input) — always a 4xx."""

    def __init__(self, status: int, code: str, message: str, **details):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.details}
