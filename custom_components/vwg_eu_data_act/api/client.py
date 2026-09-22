"""Client for the VW Group EU Data Act portal.

The portal has no public API. Its web front end talks to ``/proxy_api/...``
endpoints on the portal host, authenticated by the session cookies set when
the VW Group identity provider redirects back after sign-in. This client
replays that browser flow, so the ``aiohttp`` session it is given must have a
cookie jar of its own -- never share one between accounts.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
import zipfile
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import aiohttp

from .brands import Brand
from .dataset import Dataset, DatasetFile
from .exception import (
    EudaActionRequiredError,
    EudaAuthError,
    EudaError,
    EudaNoDataError,
)
from .login import LoginForm, login_error, parse_login_form

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_HOST = "identity.vwgroup.io"
IDENTITY_AUTHORIZE_URL = f"https://{IDENTITY_HOST}/oidc/v1/authorize"

_VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
_RELATION_PATH = "/proxy_api/vum/v2/users/me/relations/{vin}"
_METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial"
_LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
_DOWNLOAD_PATH = (
    "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"
)

#: The portal's servlets behave differently for clients that do not look like
#: a browser, so the whole flow presents itself as one.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

#: Statuses the identity provider answers with for reasons that have nothing
#: to do with the credentials: a deploy in progress, or rate limiting.
_TRANSIENT_LOGIN_STATUSES = frozenset({404, 429})

#: Path fragments of identity provider pages that interrupt a sign-in with
#: correct credentials, such as updated terms of use to accept.
_ACTION_REQUIRED_MARKERS = ("terms", "consent", "legal", "marketing")

_TIMEOUT = aiohttp.ClientTimeout(total=60)


class EudaClient:
    """An authenticated session with the portal for one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        brand: Brand,
        email: str,
        password: str,
    ) -> None:
        """Store the session and credentials; nothing is sent until needed."""
        self._session = session
        self._brand = brand
        self._email = email
        self._password = password
        self._logged_in = False

    # -- authentication ----------------------------------------------------

    async def login(self) -> None:
        """Sign in and leave the portal's session cookies in the jar."""
        try:
            await self._login()
        except aiohttp.ClientError as err:
            raise EudaError(
                f"Network error while signing in: {type(err).__name__}",
                transient=True,
            ) from err
        except TimeoutError as err:
            raise EudaError("Timed out while signing in", transient=True) from err
        self._logged_in = True

    async def _login(self) -> None:
        # Load the portal first, as a browser would: it sets the load balancer
        # and session cookies the login callback later relies on.
        try:
            async with self._get(f"{BASE_URL}/") as resp:
                await resp.read()
        except (aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("Priming request failed; continuing", exc_info=True)

        # The portal's own "start login" servlet fails for non-browser
        # clients, so the authorize request is built here instead.
        async with self._get(self.authorize_url()) as resp:
            signin_url = str(resp.url)
            signin_html = await resp.text()
            _raise_for_login_status(resp.status, "sign-in page")

        form = parse_login_form(signin_html)
        if not form.is_complete:
            # Nothing has been submitted yet, so this cannot be the
            # credentials: the page has changed or is not the sign-in page.
            raise EudaError(
                "The sign-in page did not contain the expected form "
                f"(fields: {sorted(form.fields)})"
            )
        form.fields["email"] = self._email
        async with self._post(
            urljoin(signin_url, form.action or ""), form, referer=signin_url
        ) as resp:
            password_url = str(resp.url)
            password_html = await resp.text()
            _raise_for_login_status(resp.status, "e-mail step")

        form = parse_login_form(password_html)
        if not form.is_complete:
            if error := login_error(password_html):
                raise EudaAuthError(f"The e-mail address was rejected: {error}")
            raise EudaError("The sign-in did not ask for a password")
        form.fields["email"] = self._email
        form.fields["password"] = self._password
        # relayState is already in the body; posting to a URL that repeats it
        # in the query is rejected with HTTP 400.
        target = (
            urljoin(password_url, form.action)
            if form.action
            else password_url.split("?", 1)[0]
        )
        async with self._post(target, form, referer=password_url) as resp:
            landing = str(resp.url)
            landing_html = await resp.text()
            status = resp.status
        _raise_for_login_status(status, "password step")
        _check_landing(landing, landing_html, status)

    def authorize_url(self) -> str:
        """Return the OIDC authorize URL for this client's brand."""
        params = {
            "client_id": self._brand.client_id,
            "response_type": "code",
            "scope": "openid cars profile",
            # country__language__brand, echoed back to the portal callback.
            "state": f"de__en__{self._brand.state}",
            "redirect_uri": f"{BASE_URL}/login",
            "prompt": "login",
        }
        return f"{IDENTITY_AUTHORIZE_URL}?{urlencode(params)}"

    # -- portal endpoints --------------------------------------------------

    async def list_vehicles(self) -> dict[str, str | None]:
        """Return the account's vehicles as ``{vin: nickname}``."""
        payload = await self._get_json(
            "Vehicle list", f"{_VEHICLES_PATH}?viewPosition=FRONT_LEFT"
        )
        vehicles: dict[str, str | None] = dict.fromkeys(find_vins(payload))
        for vin in vehicles:
            try:
                relation = await self._get_json(
                    "Vehicle details",
                    _RELATION_PATH.format(vin=vin),
                    # Answered with HTTP 400 unless a trace id is sent.
                    headers={"traceid": f"vehicle-relation-fetch-{uuid.uuid4()}"},
                )
            except EudaError:
                _LOGGER.debug("No nickname for a vehicle", exc_info=True)
                continue
            vehicles[vin] = (relation.get("relation") or {}).get("vehicleNickname")
        return vehicles

    async def get_request_metadata(self, vin: str) -> dict[str, Any]:
        """Return what the portal knows about the vehicle's data request.

        :raises EudaNoDataError: No data request has been set up.
        """
        try:
            metadata = await self._get_json(
                "Data request lookup", _METADATA_PATH.format(vin=vin)
            )
        except EudaError as err:
            if err.status == 404:
                raise EudaNoDataError(
                    "No continuous data request exists for this vehicle", status=404
                ) from err
            raise
        if not isinstance(metadata, dict):
            raise EudaError("The data request details were not understood")
        return metadata

    async def get_request_identifier(self, vin: str) -> str:
        """Return the identifier of the vehicle's continuous data request.

        :raises EudaNoDataError: No data request has been set up.
        """
        metadata = await self.get_request_metadata(vin)
        identifier = metadata.get("Identifier") or metadata.get("identifier")
        if not identifier:
            raise EudaNoDataError("No continuous data request exists for this vehicle")
        return str(identifier)

    async def list_datasets(self, vin: str, identifier: str) -> list[DatasetFile]:
        """Return the rolling list of delivery files for a data request.

        The list is empty until the request has delivered its first file.
        """
        try:
            payload = await self._get_json(
                "Delivery list",
                _LIST_PATH.format(vin=vin, identifier=identifier),
                # Answered with HTTP 500 unless the request type is sent.
                headers={"type": "partial"},
            )
        except EudaError as err:
            # The portal answers 404 until the first delivery exists. An
            # identifier that no longer resolves looks the same, which the
            # caller can rule out by looking the request up again.
            if err.status == 404:
                return []
            raise
        entries = payload if isinstance(payload, list) else payload.get("files") or []
        return [DatasetFile.from_json(e) for e in entries if isinstance(e, dict)]

    async def download_dataset(
        self, vin: str, identifier: str, file: DatasetFile
    ) -> Dataset:
        """Download one delivery file and parse the dataset inside it."""
        if not file.has_content:
            raise EudaNoDataError("The delivery file holds no data")
        raw = await self._request_bytes(
            "Dataset download",
            _DOWNLOAD_PATH.format(vin=vin, identifier=identifier),
            headers={"filename": file.name, "type": "partial"},
        )
        return Dataset.from_json(unzip_json(raw))

    # -- transport ---------------------------------------------------------

    def _get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        return self._session.get(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=_TIMEOUT
        )

    def _post(self, url: str, form: LoginForm, *, referer: str) -> Any:
        # The form's target comes from the page, so check where the
        # credentials are about to go.
        if urlparse(url).hostname != IDENTITY_HOST:
            raise EudaError(
                f"Refusing to send credentials to {urlparse(url).hostname!r}"
            )
        return self._session.post(
            url,
            data=form.fields,
            headers={"User-Agent": USER_AGENT, "Referer": referer},
            timeout=_TIMEOUT,
        )

    async def _request_bytes(
        self, what: str, path: str, headers: dict[str, str] | None = None
    ) -> bytes:
        """GET a portal path, signing in first and once more on expiry.

        :param what: Names the endpoint in error messages, as a capitalised
            phrase such as "Delivery list". Messages must not contain the
            path: it holds the VIN and the request identifier.
        """
        if not self._logged_in:
            await self.login()
        url = f"{BASE_URL}{path}"
        try:
            for attempt in range(2):
                async with self._get(url, headers) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        _LOGGER.debug(
                            "Session expired (HTTP %s); signing in", resp.status
                        )
                        self._logged_in = False
                        await self.login()
                        continue
                    if resp.status >= 400:
                        # A 401/403 right after a successful sign-in is the
                        # portal refusing access, not a bad password.
                        raise EudaError(
                            f"{what} failed with HTTP {resp.status}",
                            status=resp.status,
                        )
                    return await resp.read()
        except aiohttp.ClientError as err:
            raise EudaError(
                f"{what} failed with a network error: {type(err).__name__}",
                transient=True,
            ) from err
        except TimeoutError as err:
            raise EudaError(f"{what} timed out", transient=True) from err
        raise AssertionError("unreachable")  # pragma: no cover

    async def _get_json(
        self, what: str, path: str, headers: dict[str, str] | None = None
    ) -> Any:
        raw = await self._request_bytes(what, path, headers)
        try:
            return json.loads(raw)
        except ValueError as err:
            # Typically a maintenance page served in place of the API.
            raise EudaError(f"{what} did not return JSON", transient=True) from err


def _raise_for_login_status(status: int, step: str) -> None:
    """Raise a retryable error when the identity provider itself is failing."""
    if status >= 500 or status in _TRANSIENT_LOGIN_STATUSES:
        raise EudaError(
            f"Identity provider failed at the {step} (HTTP {status})", status=status
        )


def _check_landing(landing: str, html: str, status: int) -> None:
    """Decide how a sign-in went from where the password step ended up.

    Only an unambiguous rejection raises :class:`EudaAuthError`, since that
    makes Home Assistant ask for the password again. Anything the password
    cannot fix is a plain :class:`EudaError`.
    """
    url = urlparse(landing)
    # Never include the query in a message: it can hold one-time tokens.
    where = f"{url.hostname}{url.path}"

    if url.hostname == urlparse(BASE_URL).hostname and status < 400:
        return  # Back on the portal: signed in.

    if error := login_error(html):
        raise EudaAuthError(f"Sign-in rejected: {error}")
    if url.hostname == IDENTITY_HOST:
        if any(marker in url.path.lower() for marker in _ACTION_REQUIRED_MARKERS):
            raise EudaActionRequiredError(
                "The identity provider wants something confirmed before "
                f"signing in (at {where}); sign in once in a browser"
            )
        if "/error" in url.path:
            # A generic error page says nothing about the credentials.
            raise EudaError(f"The identity provider showed an error page ({where})")
        if "signin-service" in url.path and status < 400:
            # The sign-in form rendered again: the password was not accepted.
            raise EudaAuthError("Sign-in failed; check the e-mail and password")
        if status in (400, 401, 403):
            raise EudaAuthError(f"Sign-in rejected (HTTP {status})")
    raise EudaError(f"Sign-in did not complete (ended at {where}, HTTP {status})")


def find_vins(payload: Any) -> list[str]:
    """Collect every VIN in the (undocumented) vehicles response.

    The response is walked rather than indexed so a change in the wrapping
    structure does not lose the vehicles.
    """
    found: dict[str, None] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            vin = node.get("vin") or node.get("vehicleIdentificationNumber")
            if isinstance(vin, str) and len(vin) == 17:
                found.setdefault(vin.upper(), None)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return list(found)


def unzip_json(raw: bytes) -> dict[str, Any]:
    """Return the JSON document inside a delivery ZIP."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".json")]
            if not members:
                raise EudaError("The delivery file contains no JSON document")
            document = json.loads(archive.read(members[0]))
    except (zipfile.BadZipFile, ValueError) as err:
        raise EudaError(
            f"The delivery file could not be read: {type(err).__name__}"
        ) from err
    if not isinstance(document, dict):
        raise EudaError("The delivery file does not contain a dataset")
    return document
