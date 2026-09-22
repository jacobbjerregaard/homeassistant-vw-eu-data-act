"""Parsing of the VW Group identity provider's sign-in pages.

The sign-in is a two-step HTML form flow (e-mail, then password). Part of the
form state is rendered as ordinary hidden inputs and part of it lives in a
JavaScript object on the page::

    window._IDK = { templateModel: { hmac: ..., relayState: ... },
                    csrf_token: '...' }

Nothing here does I/O, so the fragile part of the login can be tested against
captured pages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

_CSRF_RE = re.compile(r"csrf_token\s*[:=]\s*['\"]([^'\"]+)['\"]")


@dataclass
class LoginForm:
    """The fields and target of one sign-in step."""

    action: str | None
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        """Whether the form carries the tokens the identity provider requires."""
        return "hmac" in self.fields and "_csrf" in self.fields


class _FirstFormParser(HTMLParser):
    """Collect the action and named inputs of the first ``<form>``."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._state = "before"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and self._state == "before":
            self.action = values.get("action")
            self._state = "inside"
        elif tag == "input" and self._state == "inside":
            name = values.get("name")
            if name:
                self.fields[name] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._state == "inside":
            self._state = "after"


def extract_template_model(html: str) -> dict[str, Any]:
    """Return the ``templateModel`` object embedded in a sign-in page.

    The object is JSON, but it sits inside a larger JavaScript literal, so it
    is cut out by matching braces rather than with a regular expression.
    """
    start = html.find("templateModel")
    if start == -1:
        return {}
    brace = html.find("{", start)
    if brace == -1:
        return {}

    depth = 0
    in_string = False
    escaped = False
    for index in range(brace, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    model = json.loads(html[brace : index + 1])
                except ValueError:
                    return {}
                return model if isinstance(model, dict) else {}
    return {}


def parse_login_form(html: str) -> LoginForm:
    """Merge a page's HTML inputs and JavaScript state into one form."""
    parser = _FirstFormParser()
    parser.feed(html)
    form = LoginForm(action=parser.action, fields=dict(parser.fields))

    model = extract_template_model(html)
    for key in ("hmac", "relayState"):
        if model.get(key):
            form.fields[key] = str(model[key])
    email = (model.get("emailPasswordForm") or {}).get("email")
    if email:
        form.fields.setdefault("email", str(email))

    if match := _CSRF_RE.search(html):
        form.fields.setdefault("_csrf", match.group(1))
    return form


def login_error(html: str) -> str | None:
    """Return the error a sign-in page reports, if it reports one."""
    model = extract_template_model(html)
    error = model.get("error") or model.get("errorCode")
    if isinstance(error, dict):
        return error.get("text") or error.get("errorCode") or str(error)
    return str(error) if error else None
