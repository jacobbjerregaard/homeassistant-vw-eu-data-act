"""Tests for parsing the identity provider's sign-in pages."""

from euda_api.login import extract_template_model, login_error, parse_login_form

EMAIL_PAGE = """
<html><body>
<form id="emailPasswordForm" action="/signin-service/v1/abc@apps/login/identifier"
      method="POST">
  <input type="hidden" name="_csrf" value="csrf-from-input"/>
  <input type="hidden" name="relayState" value="relay-1"/>
  <input type="hidden" name="hmac" value="hmac-1"/>
  <input type="email" name="email"/>
</form>
<form action="/other"><input name="ignored" value="x"/></form>
</body></html>
"""

PASSWORD_PAGE = """
<html><head><script>
window._IDK = {
  templateModel: {"hmac":"hmac-2","relayState":"relay-2",
                  "emailPasswordForm":{"email":"owner@example.com"},
                  "postAction":"login/authenticate","note":"a } in a string"},
  csrf_token: 'csrf-from-js'
};
</script></head><body><div id="root"></div></body></html>
"""

ERROR_PAGE = """
<script>window._IDK = { templateModel: {"error": "login.errors.password_invalid"} };
</script>
"""


def test_email_step_uses_html_inputs_of_first_form_only():
    form = parse_login_form(EMAIL_PAGE)

    assert form.action == "/signin-service/v1/abc@apps/login/identifier"
    assert form.fields == {
        "_csrf": "csrf-from-input",
        "relayState": "relay-1",
        "hmac": "hmac-1",
        "email": "",
    }
    assert form.is_complete


def test_password_step_reads_javascript_state():
    form = parse_login_form(PASSWORD_PAGE)

    assert form.action is None
    assert form.fields == {
        "hmac": "hmac-2",
        "relayState": "relay-2",
        "email": "owner@example.com",
        "_csrf": "csrf-from-js",
    }
    assert form.is_complete


def test_template_model_survives_braces_in_strings():
    assert extract_template_model(PASSWORD_PAGE)["note"] == "a } in a string"


def test_page_without_form_is_incomplete():
    form = parse_login_form("<html>Service unavailable</html>")
    assert not form.is_complete
    assert extract_template_model("templateModel: {broken") == {}


def test_login_error():
    assert login_error(ERROR_PAGE) == "login.errors.password_invalid"
    assert login_error(PASSWORD_PAGE) is None
