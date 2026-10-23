"""Public, actionable native-agent argument failures before dispatch."""


class ExternalAgentArgumentError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
