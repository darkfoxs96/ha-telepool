"""Public client exceptions; messages never include remote response bodies."""


class TelePoolError(Exception):
    """Base error for the undocumented TelePool website protocol."""


class AuthenticationError(TelePoolError):
    """Saved credentials no longer authenticate with TelePool."""


class ConnectionError(TelePoolError):
    """A request could not reach TelePool or timed out."""


class ProtocolError(TelePoolError):
    """TelePool returned an unexpected response or denied this operation."""


class WriteOutcomeUnknown(ProtocolError):
    """A settings POST may have been accepted; do not automatically resend it."""


class CommandNotSent(ProtocolError):
    """The final dispatch guard refused the command before any settings POST."""


class SettingsRejected(ProtocolError):
    """Definite settings rejection with one bounded, non-payload reason code."""

    _MESSAGES = {
        "http_400": "TelePool rejected the settings form with HTTP 400",
        "http_403": "TelePool rejected the settings form with HTTP 403",
        "access_denied": "TelePool denied access to the settings submission",
        "validation_error": "TelePool returned settings form validation errors",
    }

    def __init__(self, reason: str, *, invalid_fields: tuple[str, ...] = ()) -> None:
        if reason not in self._MESSAGES:
            raise ValueError("Unsupported settings rejection reason")
        self.reason = reason
        self.invalid_fields: tuple[str, ...] = ()
        if invalid_fields:
            # Controls is already loaded by the transport when errors are raised.
            # Reuse its schema to reject arbitrary response strings even if this
            # exception is manually constructed by a future caller.
            from .controls import _KNOWN_FORM_FIELDS

            self.invalid_fields = tuple(
                sorted({name for name in invalid_fields if isinstance(name, str) and name in _KNOWN_FORM_FIELDS})
            )[:8]
        super().__init__(self._MESSAGES[reason])
