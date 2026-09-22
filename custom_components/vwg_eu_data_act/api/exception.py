"""Exceptions raised by the EU Data Act portal client.

Messages never contain the VIN, the data request identifier or a delivery file
name (which embeds the VIN): they end up in Home Assistant's UI and logs, and
from there in bug reports.
"""

from __future__ import annotations


class EudaError(Exception):
    """Base class for every failure talking to the portal.

    ``status`` carries the HTTP status when the failure came from a response,
    so callers can tell a transient 5xx apart from anything else without
    parsing the message.
    """

    def __init__(
        self, message: str, *, status: int | None = None, transient: bool = False
    ) -> None:
        """Store the message, the HTTP status and whether a retry may help.

        :param transient: For failures without a status, such as a dropped
            connection, whether retrying later has a reasonable chance of
            succeeding. A status of 5xx or 429 always counts as transient.
        """
        super().__init__(message)
        self.status = status
        self._transient = transient

    @property
    def is_transient(self) -> bool:
        """Whether retrying later has a reasonable chance of succeeding."""
        if self.status is not None and (self.status >= 500 or self.status == 429):
            return True
        return self._transient


class EudaAuthError(EudaError):
    """The identity provider rejected the credentials.

    Raised only when the rejection is unambiguous, because it makes Home
    Assistant ask the user for the password again.
    """


class EudaActionRequiredError(EudaError):
    """The credentials work, but the account needs attention in a browser.

    For example new terms of use that have to be accepted before the identity
    provider lets the sign-in complete.
    """


class EudaNoDataError(EudaError):
    """The account is fine, but the portal has nothing to deliver yet.

    Typically the continuous data request has not been set up for the vehicle,
    or it has been set up but no dataset has been produced so far.
    """
