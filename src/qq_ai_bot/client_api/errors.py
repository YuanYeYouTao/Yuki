"""Public client errors do not disclose inaccessible objects or credentials."""


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
