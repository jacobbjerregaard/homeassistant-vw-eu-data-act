"""The VW Group brands the portal serves, and their identity clients.

Every brand signs in through the shared VW Group identity provider, but with
its own OIDC client. The ``state`` value is echoed back to the portal's
callback, which uses it to pick the brand the session belongs to.

The client ids are not published anywhere; they are what the portal's own web
front end sends, as collected by the evcc project and the other community
integrations for this portal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Brand:
    """One brand's identity settings."""

    key: str
    name: str
    client_id: str
    state: str


BRANDS: dict[str, Brand] = {
    brand.key: brand
    for brand in (
        Brand(
            "volkswagen",
            "Volkswagen",
            "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com",
            "VOLKSWAGEN_PASSENGER_CARS",
        ),
        Brand(
            "audi",
            "Audi",
            "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com",
            "AUDI",
        ),
        Brand(
            "skoda",
            "Škoda",
            "3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com",
            "SKODA",
        ),
        # SEAT and CUPRA share one identity client and differ only in state.
        Brand(
            "seat",
            "SEAT",
            "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
            "SEAT",
        ),
        Brand(
            "cupra",
            "CUPRA",
            "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
            "CUPRA",
        ),
    )
}

DEFAULT_BRAND = "volkswagen"


def get_brand(key: str) -> Brand:
    """Return the brand for ``key``, raising ``KeyError`` when unknown."""
    return BRANDS[key]
