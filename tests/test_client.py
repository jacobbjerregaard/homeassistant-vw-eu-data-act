"""Tests for the portal client, against a scripted fake HTTP session."""

import io
import json
import zipfile
from dataclasses import dataclass, field

import aiohttp
import pytest
from euda_api.brands import BRANDS
from euda_api.client import BASE_URL, EudaClient, find_vins, unzip_json
from euda_api.dataset import DatasetFile
from euda_api.exception import (
    EudaActionRequiredError,
    EudaAuthError,
    EudaError,
    EudaNoDataError,
)

from tests.test_login import EMAIL_PAGE, PASSWORD_PAGE

VIN = "WVWZZZE1ZTEST0001"
SIGNIN_URL = "https://identity.vwgroup.io/signin-service/v1/abc@apps/login"


@dataclass
class FakeResponse:
    status: int = 200
    body: bytes | str = b""
    url: str = BASE_URL

    async def __aenter__(self):  # noqa: D105
        return self

    async def __aexit__(self, *exc):  # noqa: D105
        return False

    async def text(self):
        return self.body.decode() if isinstance(self.body, bytes) else self.body

    async def read(self):
        return self.body.encode() if isinstance(self.body, str) else self.body


def json_response(payload, status=200):
    return FakeResponse(status=status, body=json.dumps(payload))


@dataclass
class FakeSession:
    """Answers requests from per-path queues; the last answer repeats."""

    routes: dict[str, list] = field(default_factory=dict)
    calls: list[tuple[str, str, dict]] = field(default_factory=list)

    def add(self, key, *responses):
        self.routes.setdefault(key, []).extend(responses)

    def _answer(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for key, queue in self.routes.items():
            if key in url:
                answer = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kwargs):
        return self._answer("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, **kwargs)


def logged_in_session(landing=f"{BASE_URL}/de/en/home.html"):
    session = FakeSession()
    session.add("oidc/v1/authorize", FakeResponse(body=EMAIL_PAGE, url=SIGNIN_URL))
    session.add(
        "login/identifier",
        FakeResponse(body=PASSWORD_PAGE, url=f"{SIGNIN_URL}/authenticate?relayState=r"),
    )
    session.add("login/authenticate", FakeResponse(url=landing))
    return session


def make_client(session):
    return EudaClient(session, BRANDS["skoda"], "owner@example.com", "secret")


def zipped(document, name="dataset.json"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, json.dumps(document))
    return buffer.getvalue()


async def test_login_posts_credentials_and_lands_on_portal():
    session = logged_in_session()
    session.add(f"{BASE_URL}/", FakeResponse())
    client = make_client(session)

    await client.login()

    posts = [call for call in session.calls if call[0] == "POST"]
    assert posts[0][2]["data"]["email"] == "owner@example.com"
    # The password step posts to the clean URL, without the relayState query.
    assert posts[1][1] == f"{SIGNIN_URL}/authenticate"
    assert posts[1][2]["data"]["password"] == "secret"
    assert posts[1][2]["data"]["hmac"] == "hmac-2"


def test_authorize_url_carries_brand():
    url = make_client(FakeSession()).authorize_url()
    assert "client_id=3ea88bf9" in url
    assert "state=de__en__SKODA" in url


async def test_wrong_password_raises_auth_error():
    session = logged_in_session(landing=f"{SIGNIN_URL}/authenticate?error=x")
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaAuthError):
        await make_client(session).login()


async def test_identity_outage_is_transient_not_auth():
    session = FakeSession()
    session.add(f"{BASE_URL}/", FakeResponse())
    session.add("oidc/v1/authorize", FakeResponse(status=503, url=SIGNIN_URL))
    with pytest.raises(EudaError) as info:
        await make_client(session).login()
    assert not isinstance(info.value, EudaAuthError)
    assert info.value.is_transient


async def test_network_error_during_login():
    session = FakeSession()
    session.add(f"{BASE_URL}/", FakeResponse())
    session.add("oidc/v1/authorize", aiohttp.ClientConnectionError("boom"))
    with pytest.raises(EudaError):
        await make_client(session).login()


async def test_list_vehicles_with_nicknames():
    session = logged_in_session()
    session.add("consent/me/vehicles", json_response({"vehicles": [{"vin": VIN}]}))
    session.add("relations/", json_response({"relation": {"vehicleNickname": "Enyaq"}}))
    session.add(f"{BASE_URL}/", FakeResponse())

    assert await make_client(session).list_vehicles() == {VIN: "Enyaq"}
    relation_call = next(c for c in session.calls if "relations/" in c[1])
    assert relation_call[2]["headers"]["traceid"].startswith("vehicle-relation-fetch-")


async def test_expired_session_signs_in_again():
    session = logged_in_session()
    session.add(
        "metadata/partial",
        FakeResponse(status=401),
        json_response({"Identifier": "req-1"}),
    )
    session.add(f"{BASE_URL}/", FakeResponse())
    client = make_client(session)

    assert await client.get_request_identifier(VIN) == "req-1"
    authorizations = [c for c in session.calls if "oidc/v1/authorize" in c[1]]
    assert len(authorizations) == 2


async def test_missing_data_request():
    session = logged_in_session()
    session.add("metadata/partial", FakeResponse(status=404))
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaNoDataError):
        await make_client(session).get_request_identifier(VIN)


async def test_list_and_download_dataset():
    session = logged_in_session()
    session.add(
        "/list",
        json_response([{"name": "a.zip", "createdOn": "2026-09-20T10:00:00Z"}]),
    )
    session.add(
        "/download",
        FakeResponse(body=zipped({"vin": VIN, "Data": [{"key": "k", "value": "1"}]})),
    )
    session.add(f"{BASE_URL}/", FakeResponse())
    client = make_client(session)

    files = await client.list_datasets(VIN, "req-1")
    dataset = await client.download_dataset(VIN, "req-1", files[0])

    assert dataset.vin == VIN
    download = next(c for c in session.calls if "/download" in c[1])
    assert download[2]["headers"]["filename"] == "a.zip"
    assert download[2]["headers"]["type"] == "partial"


async def test_listing_before_first_delivery_is_empty():
    session = logged_in_session()
    session.add("/list", FakeResponse(status=404))
    session.add(f"{BASE_URL}/", FakeResponse())
    assert await make_client(session).list_datasets(VIN, "req-1") == []


async def test_listing_server_error_is_raised():
    session = logged_in_session()
    session.add("/list", FakeResponse(status=500))
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaError) as info:
        await make_client(session).list_datasets(VIN, "req-1")
    assert info.value.status == 500


async def test_placeholder_file_is_not_downloaded():
    client = make_client(FakeSession())
    with pytest.raises(EudaNoDataError):
        await client.download_dataset(
            VIN, "req-1", DatasetFile("x_no_content_found.zip", None)
        )


def test_unzip_json_errors():
    for raw in (b"not a zip", zipped({}, name="readme.txt"), zipped([1, 2])):
        with pytest.raises(EudaError) as info:
            unzip_json(raw)
        # A file the portal cannot serve properly is not fixed by retrying it.
        assert not info.value.is_transient


def test_find_vins_walks_any_shape():
    payload = {"data": [{"vehicle": {"vin": VIN.lower()}}, {"vin": "short"}]}
    assert find_vins(payload) == [VIN]


def _session_landing_on(landing, *, status=200, body=""):
    session = logged_in_session()
    session.routes["login/authenticate"] = [
        FakeResponse(status=status, body=body, url=landing)
    ]
    session.add(f"{BASE_URL}/", FakeResponse())
    return session


async def test_changed_signin_page_is_not_an_auth_error():
    session = FakeSession()
    session.add(f"{BASE_URL}/", FakeResponse())
    session.add(
        "oidc/v1/authorize",
        FakeResponse(body="<html>Maintenance</html>", url=SIGNIN_URL),
    )
    with pytest.raises(EudaError) as info:
        await make_client(session).login()
    assert not isinstance(info.value, EudaAuthError)


async def test_missing_password_form():
    session = logged_in_session()
    session.routes["login/identifier"] = [FakeResponse(body="<html></html>")]
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaError) as info:
        await make_client(session).login()
    assert not isinstance(info.value, EudaAuthError)

    session = logged_in_session()
    session.routes["login/identifier"] = [
        FakeResponse(
            body='<script>templateModel: {"error": "account.unknown"}</script>'
        )
    ]
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaAuthError, match="account.unknown"):
        await make_client(session).login()


async def test_terms_to_accept_need_a_browser():
    session = _session_landing_on(
        "https://identity.vwgroup.io/signin-service/v1/abc@apps/terms-and-conditions"
        "?relayState=secret-token"
    )
    with pytest.raises(EudaActionRequiredError) as info:
        await make_client(session).login()
    # The query can carry one-time tokens and is never repeated.
    assert "secret-token" not in str(info.value)


async def test_identity_error_page_is_not_an_auth_error():
    session = _session_landing_on("https://identity.vwgroup.io/error?code=x")
    with pytest.raises(EudaError) as info:
        await make_client(session).login()
    assert not isinstance(info.value, EudaAuthError)


async def test_error_on_landing_page_is_an_auth_error():
    session = _session_landing_on(
        f"{SIGNIN_URL}/authenticate",
        body='<script>templateModel: {"error": "login.errors.password_invalid"}'
        "</script>",
    )
    with pytest.raises(EudaAuthError, match="password_invalid"):
        await make_client(session).login()


async def test_rejected_password_status_is_an_auth_error():
    session = _session_landing_on(f"{SIGNIN_URL}/authenticate", status=401)
    with pytest.raises(EudaAuthError):
        await make_client(session).login()


async def test_identity_outage_at_password_step_is_transient():
    session = _session_landing_on(f"{SIGNIN_URL}/authenticate", status=502)
    with pytest.raises(EudaError) as info:
        await make_client(session).login()
    assert info.value.is_transient
    assert not isinstance(info.value, EudaAuthError)


async def test_credentials_only_go_to_the_identity_provider():
    session = logged_in_session()
    session.routes["oidc/v1/authorize"] = [
        FakeResponse(
            body=EMAIL_PAGE.replace(
                "/signin-service/v1/abc@apps/login/identifier",
                "https://attacker.example/collect",
            ),
            url=SIGNIN_URL,
        )
    ]
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaError, match="attacker.example"):
        await make_client(session).login()
    assert not any(call[0] == "POST" for call in session.calls)


async def test_priming_timeout_does_not_stop_the_login():
    session = logged_in_session()
    session.add(f"{BASE_URL}/", TimeoutError())
    await make_client(session).login()


async def test_forbidden_after_fresh_login_is_not_an_auth_error():
    session = logged_in_session()
    session.add("metadata/partial", FakeResponse(status=403))
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaError) as info:
        await make_client(session).get_request_identifier(VIN)
    assert not isinstance(info.value, EudaAuthError)
    assert info.value.status == 403


@pytest.mark.parametrize("status", [500, 400])
async def test_errors_do_not_reveal_the_vin_or_request(status):
    session = logged_in_session()
    session.add("/list", FakeResponse(status=status))
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaError) as info:
        await make_client(session).list_datasets(VIN, "req-secret")
    assert VIN not in str(info.value)
    assert "req-secret" not in str(info.value)
    assert str(info.value).startswith("Delivery list failed")


async def test_download_errors_do_not_reveal_the_vin():
    session = logged_in_session()
    session.add("/download", FakeResponse(status=500))
    session.add(f"{BASE_URL}/", FakeResponse())
    file = DatasetFile(f"20260920100000_{VIN}.zip", None)
    with pytest.raises(EudaError) as info:
        await make_client(session).download_dataset(VIN, "req-1", file)
    assert VIN not in str(info.value)


async def test_which_errors_are_transient():
    session = logged_in_session()
    session.add("metadata/partial", FakeResponse(body="<html>Maintenance</html>"))
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaError) as info:
        await make_client(session).get_request_identifier(VIN)
    assert info.value.is_transient

    session = logged_in_session()
    session.add("metadata/partial", aiohttp.ClientConnectionError("reset"))
    session.add(f"{BASE_URL}/", FakeResponse())
    with pytest.raises(EudaError) as info:
        await make_client(session).get_request_identifier(VIN)
    assert info.value.is_transient

    assert EudaError("x", status=429).is_transient
    assert not EudaError("x", status=404).is_transient
