"""Which Stripe webhook objects belong to Kavanah Analytics.

One Stripe account bills Kavanah Analytics (KA), Kavanah Chat and Kavanah
Social. All their events reach
KA's ``/billing/stripe/webhook``, and KA resolves its target by customer id.
Without a filter, a Chat or Social subscription on a customer that also holds
a KA subscription would rewrite the KA workspace (a Chat cancel would cancel
KA) and send KA emails.

The rule is an ALLOWLIST of KA price ids from config
(``STRIPE_PRICE_BUSINESS``, ``STRIPE_PRICE_NONPROFIT``,
``STRIPE_PRICE_ADDON_CONNECTION``):

* A subscription is KA when at least one of its items is on a KA price. A KA
  subscription can hold the base plan plus the add-on item, so "any" (not
  "every") keeps a KA subscription recognisable even on a host whose add-on
  price is not configured. Chat and Social subscriptions never carry a KA
  price.
* An invoice is KA when at least one of its lines is on a KA price.
* No items or lines on the payload, or no KA prices configured: NOT KA (fail
  closed).
* A subscription whose metadata carries ``km_product`` with any value other
  than ``analytics`` is owned by kavanah-billing (it stamps ``km_product`` =
  social|chat on every customer, checkout and subscription it creates), so it
  is NOT KA whatever its prices say. ``km_product=analytics`` is reserved for
  KA itself (comment 14442: every product's prices carry
  ``km_product``), so KA can adopt the marker later without silencing its
  own webhook.

These functions are pure: they read the payload dicts and the allowlist the
caller passes in, and never call Stripe or Elasticsearch.
"""

from __future__ import annotations

# Ignore reasons (returned to the route, logged, and echoed in the 200 body).
REASON_KA = "ka_price"
REASON_NO_KA_PRICES_CONFIGURED = "no_ka_prices_configured"
REASON_NO_PRICES_ON_PAYLOAD = "no_prices_on_payload"
REASON_NOT_KA_PRICE = "not_ka_price"
REASON_KA_PRICE_REMOVED = "ka_price_removed"
REASON_KAVANAH_BILLING_OWNED = "kavanah_billing_owned"

# kavanah-billing's ownership marker (kbilling/checkout.py, onboarding.py).
KAVANAH_BILLING_PRODUCT_KEY = "km_product"
# The one km_product value that means Kavanah Analytics itself.
KA_PRODUCT_VALUE = "analytics"


def ka_price_ids(*price_ids) -> frozenset:
    """The configured KA price ids, blanks dropped."""
    return frozenset(p.strip() for p in price_ids if isinstance(p, str) and p.strip())


def _id_of(value) -> str:
    """A Stripe reference is either an id string or an expanded object."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id") or ""
    return ""


def _list_data(value) -> list:
    """Stripe list objects are ``{"object": "list", "data": [...]}``."""
    if isinstance(value, dict):
        data = value.get("data")
        return data if isinstance(data, list) else []
    return []


def _price_of_entry(entry) -> str:
    """The price id on a subscription item or an invoice line.

    Covers both API shapes: ``price`` (object or id) with the legacy ``plan``
    fallback, and the 2025-03-31+ invoice-line ``pricing.price_details.price``.
    """
    if not isinstance(entry, dict):
        return ""
    pid = _id_of(entry.get("price")) or _id_of(entry.get("plan"))
    if pid:
        return pid
    pricing = entry.get("pricing")
    if isinstance(pricing, dict):
        return _id_of((pricing.get("price_details") or {}).get("price"))
    return ""


def subscription_price_ids(subscription) -> list:
    """Price ids on a subscription's items (order kept, blanks dropped)."""
    items = _list_data((subscription or {}).get("items"))
    return [p for p in (_price_of_entry(i) for i in items) if p]


def invoice_price_ids(invoice) -> list:
    """Price ids on an invoice's lines (order kept, blanks dropped)."""
    lines = _list_data((invoice or {}).get("lines"))
    return [p for p in (_price_of_entry(line) for line in lines) if p]


def _kavanah_billing_owned(metadata) -> bool:
    if not isinstance(metadata, dict):
        return False
    value = metadata.get(KAVANAH_BILLING_PRODUCT_KEY)
    return bool(value) and str(value).strip().lower() != KA_PRODUCT_VALUE


def _invoice_subscription_metadata(invoice) -> dict:
    """Subscription metadata copied onto the invoice, either API shape."""
    inv = invoice or {}
    for details in (
        inv.get("subscription_details"),
        ((inv.get("parent") or {}).get("subscription_details") if isinstance(inv.get("parent"), dict) else None),
    ):
        if isinstance(details, dict) and isinstance(details.get("metadata"), dict):
            return details["metadata"]
    return {}


def classify_subscription(subscription, ka_prices, previous_attributes=None) -> tuple:
    """Return ``(is_ka, reason)`` for a ``customer.subscription.*`` object.

    ``previous_attributes`` (from ``event.data.previous_attributes`` on an
    update) only refines the reason: a subscription that HAD a KA price and no
    longer does is reported as ``ka_price_removed`` so the caller can log it
    louder. It is still not KA, and is never applied.
    """
    sub = subscription or {}
    if not ka_prices:
        return False, REASON_NO_KA_PRICES_CONFIGURED
    if _kavanah_billing_owned(sub.get("metadata")):
        return False, REASON_KAVANAH_BILLING_OWNED
    prices = subscription_price_ids(sub)
    if not prices:
        return False, REASON_NO_PRICES_ON_PAYLOAD
    if any(p in ka_prices for p in prices):
        return True, REASON_KA
    previous = subscription_price_ids(previous_attributes or {})
    if any(p in ka_prices for p in previous):
        return False, REASON_KA_PRICE_REMOVED
    return False, REASON_NOT_KA_PRICE


def classify_invoice(invoice, ka_prices) -> tuple:
    """Return ``(is_ka, reason)`` for an ``invoice.*`` object."""
    inv = invoice or {}
    if not ka_prices:
        return False, REASON_NO_KA_PRICES_CONFIGURED
    if _kavanah_billing_owned(_invoice_subscription_metadata(inv)):
        return False, REASON_KAVANAH_BILLING_OWNED
    prices = invoice_price_ids(inv)
    if not prices:
        return False, REASON_NO_PRICES_ON_PAYLOAD
    if any(p in ka_prices for p in prices):
        return True, REASON_KA
    return False, REASON_NOT_KA_PRICE
