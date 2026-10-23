"""
Stripe Service

A thin, SDK-agnostic seam over the Stripe Python SDK so the billing routes
never import ``stripe`` directly. The real implementation imports the SDK
**lazily** (inside each method) and is guarded by ``StripeUnavailableError``
— the package is only required when ``STRIPE_LIVE_PAYMENTS`` is enabled on a
host that has it installed. Tests inject a fake module via ``_load_sdk`` /
the ``sdk`` argument, so no network or real SDK is needed to exercise the
billing routes.

Scope:
  * ``create_subscription`` — create/look up a Customer, attach the
    payment method, create a Subscription on the for-profit price (applying
    a coupon if present). Returns a small dict the route consumes.
  * ``construct_webhook_event`` — verify the ``Stripe-Signature`` header
    against the webhook secret and return the parsed event. Raises
    ``StripeSignatureError`` on a bad/missing signature so the webhook
    fails closed.

Everything here is pure orchestration over the SDK; the subscription-state
metadata writes live in ``SubscriptionStateService.apply_stripe_status``.
"""

from __future__ import annotations

import logging


class StripeError(Exception):
    """Base for Stripe-service errors."""


class StripeUnavailableError(StripeError):
    """The ``stripe`` SDK is not importable (not installed)."""


class StripeSignatureError(StripeError):
    """A webhook payload failed signature verification."""


def _load_sdk():
    """Import the Stripe SDK lazily. Raises StripeUnavailableError when the
    package is absent so callers can degrade to a 503 rather than 500."""
    try:
        import stripe  # noqa: PLC0415 — intentional lazy import
    except ImportError as exc:  # pragma: no cover - exercised via injected fake
        raise StripeUnavailableError("the 'stripe' SDK is not installed") from exc
    return stripe


def _field(obj, key, default=""):
    """Read ``key`` from a Stripe SDK return value, which may be a
    ``StripeObject`` (subscriptable, but NO ``.get`` method) or a plain dict
    (test fakes). ``obj.get(...)`` raises ``AttributeError: get`` on a
    StripeObject, so never use ``.get`` on SDK returns — go through here."""
    try:
        return obj[key]
    except (KeyError, TypeError, AttributeError):
        return default


def _coupon_is_usable(coupon) -> bool:
    """True if a Coupon object is still applicable — ``valid`` is Stripe's own
    rolled-up flag (false once a coupon is expired or hit its redemption cap).
    Defaults to False when the field is absent so we never apply a coupon we
    can't confirm is good."""
    return bool(_field(coupon, "valid", False))


def _item_discount(discount_id: str) -> dict:
    """Shape a subscription-ITEM discount entry from a discount id.
    A ``promo_...`` id is a Promotion Code (enforces its own restrictions); any
    other id is treated as a bare Coupon. Used for the add-on item's mission
    discount so it stays item-level (the base plan keeps full price)."""
    if discount_id.startswith("promo_"):
        return {"promotion_code": discount_id}
    return {"coupon": discount_id}


def _coupon_restricted_products(coupon) -> list | None:
    """Return the coupon's ``applies_to.products`` list, or None when the coupon
    is UNRESTRICTED (no applies_to / empty products). Reads robustly (
    2026-06-10 hardening): the SDK StripeObject first (attr/subscript), then a
    JSON round-trip of ``str(coupon)`` as a fallback — so the restriction is
    found whether or not the SDK serializes ``applies_to`` into ``str()``.

    Returns:
      * None  → unrestricted (applies to everything);
      * list  → the restricted product ids (possibly empty list ≡ unrestricted →
                normalized to None);
      * "unknown" sentinel string → applies_to is PRESENT but its products can't
                be read (a restricted coupon we can't introspect) — the caller
                decides the fail policy."""
    # 1) SDK object path — applies_to is a nested StripeObject/dict.
    applies_to = _field(coupon, "applies_to", None)
    if applies_to is not None:
        products = _field(applies_to, "products", None)
        if products is None and isinstance(applies_to, dict):
            products = applies_to.get("products")
        if products:
            try:
                return list(products)
            except TypeError:
                return "unknown"  # present-but-unreadable
        # applies_to present but no products → unrestricted.
        # (fall through to confirm via JSON in case the object hid products)
    # 2) JSON round-trip fallback.
    try:
        import json as _json

        data = _json.loads(str(coupon))
    except Exception:  # noqa: BLE001
        # Couldn't read applies_to either way. If the SDK object DID expose an
        # applies_to (step 1) we already know it's present; signal unknown so a
        # restricted coupon isn't waved through. Otherwise treat as unrestricted.
        return "unknown" if applies_to is not None else None
    at = data.get("applies_to")
    if not isinstance(at, dict):
        return None  # unrestricted
    products = at.get("products")
    return list(products) if products else None


def _coupon_applies_to_product(coupon, product_id: str, *, fail_closed: bool = False) -> bool:
    """True if ``coupon`` can apply to ``product_id``.

    Unrestricted coupon → True. Restricted → True only if ``product_id`` is in
    its product list. ``product_id`` empty → True (caller skipped the check).

    ``fail_closed``: when a coupon IS restricted but its
    product list can't be read (the "unknown" case), return False (reject)
    instead of True. Callers validating a code on a SPECIFIC product (e.g. the
    base-plan or add-on boxes) pass fail_closed=True so a restricted-but-
    unreadable coupon can't slip through — the leak this hardens against."""
    if not product_id:
        return True
    products = _coupon_restricted_products(coupon)
    if products is None:
        return True  # unrestricted
    if products == "unknown":
        return not fail_closed
    return product_id in products


def _coupon_label(coupon) -> str:
    """Short human label for a coupon: ``"20% off"`` or ``"$10.00 off"``.
    Falls back to the coupon name/id when neither percent nor amount is set."""
    percent = _field(coupon, "percent_off", 0)
    if percent:
        # Stripe sends e.g. 20.0; show 20 when whole, 12.5 otherwise.
        pct = int(percent) if float(percent).is_integer() else percent
        return f"{pct}% off"
    amount = _field(coupon, "amount_off", 0)
    if amount:
        currency = (_field(coupon, "currency", "") or "").upper()
        sym = "$" if currency in ("", "USD") else f"{currency} "
        return f"{sym}{int(amount) / 100:.2f} off"
    return _field(coupon, "name", "") or _field(coupon, "id", "") or "discount"


def _coupon_valid(coupon, promotion_code: str = "") -> dict:
    """Build the 'valid' validate_coupon verdict from a usable Coupon object,
    surfacing the numeric discount so the caller can compute a live total.

    ``promotion_code`` is the Stripe Promotion Code id (``promo_...``) the
    customer-facing code resolved to. The apply paths discount via this
    promotion_code (NOT the bare coupon id) so the promo's own restrictions —
    redemption caps, expiry, first-time-only, customer eligibility — are
    enforced by Stripe (decision 2026-06-09). ``coupon`` is still returned for
    display/identity (and as a fallback)."""
    percent = _field(coupon, "percent_off", 0) or 0
    amount = _field(coupon, "amount_off", 0) or 0
    currency = (_field(coupon, "currency", "") or "").lower()
    return {
        "valid": True,
        "coupon": _field(coupon, "id"),
        "promotion_code": promotion_code or "",
        "description": _coupon_label(coupon),
        "percent_off": float(percent) if percent else 0.0,
        "amount_off": int(amount) if amount else 0,
        "currency": currency if amount else "",
        "reason": "",
    }


def _coupon_id_from_promo(promo) -> str:
    """Extract the coupon ID from a PromotionCode object.

    The nested ``coupon`` is awkward through the SDK's StripeObject layer
    (subscript/attr access can raise; the str()-form may render it null even
    though the REST payload includes it). Try, in order: a dict-shaped coupon
    (``coupon["id"]``), a bare string id, then a JSON round-trip of the whole
    promo (the version-proof path, same trick the webhook normalization uses).
    Returns "" if no id can be recovered."""
    cp = _field(promo, "coupon", None)
    if isinstance(cp, str) and cp:
        return cp
    cid = _field(cp, "id", "") if cp is not None else ""
    if cid:
        return cid
    # JSON round-trip the whole promo — robust where the object layer hides it.
    # Two shapes seen across API versions:
    #   * legacy: top-level ``"coupon": {...}`` or a coupon id string
    #   * newer:  ``"promotion": {"coupon": "<id>", "type": "coupon"}``
    try:
        import json as _json

        data = _json.loads(str(promo))
    except Exception:  # noqa: BLE001
        return ""

    def _coerce(c):
        if isinstance(c, str):
            return c
        if isinstance(c, dict):
            return c.get("id", "") or ""
        return ""

    cid = _coerce(data.get("coupon"))
    if cid:
        return cid
    promotion = data.get("promotion")
    if isinstance(promotion, dict):
        return _coerce(promotion.get("coupon"))
    return ""


def _discount_coupon_from_sub(sub_dict: dict, stripe) -> dict:
    """Return the coupon dict ({percent_off, amount_off, ...}) of the active
    discount on a JSON-normalized subscription, or {} if none.

    A subscription's discount carries the coupon id under
    ``discounts[0].source.coupon`` (newer API) or ``discounts[0].coupon``
    (older). We read that id and retrieve the coupon for its percent/amount.
    Best-effort: any miss returns {} (no discount applied)."""
    discounts = sub_dict.get("discounts") or []
    if not discounts:
        return {}
    first = discounts[0]
    coupon_id = ""
    if isinstance(first, dict):
        src = first.get("source")
        if isinstance(src, dict):
            coupon_id = src.get("coupon") or ""
        if not coupon_id:
            cp = first.get("coupon")
            coupon_id = cp if isinstance(cp, str) else (cp or {}).get("id", "") if isinstance(cp, dict) else ""
    elif isinstance(first, str):
        # A bare discount id — can't resolve to a coupon without a fetch we
        # don't have an endpoint for here; treat as no computable discount.
        coupon_id = ""
    if not coupon_id:
        return {}
    try:
        coupon = stripe.Coupon.retrieve(coupon_id)
    except Exception:  # noqa: BLE001
        return {}
    return {
        "id": _field(coupon, "id", "") or coupon_id,
        "percent_off": _field(coupon, "percent_off", 0) or 0,
        "amount_off": _field(coupon, "amount_off", 0) or 0,
    }


def _coupon_invalid(reason: str) -> dict:
    """The 'invalid' validate_coupon verdict — same keys as the valid one so
    callers can read the shape uniformly."""
    return {
        "valid": False,
        "coupon": "",
        "promotion_code": "",
        "description": "",
        "percent_off": 0.0,
        "amount_off": 0,
        "currency": "",
        "reason": reason,
    }


class StripeService:
    """Thin wrapper over the Stripe SDK. All methods accept an optional
    ``sdk`` so tests can inject a fake stripe module."""

    @staticmethod
    def create_subscription(
        *,
        secret_key: str,
        price_id: str,
        payment_method_id: str,
        customer_email: str = "",
        customer_name: str = "",
        customer_address: dict | None = None,
        company: str = "",
        coupon: str = "",
        promotion_code: str = "",
        trial_end: int | None = None,
        sdk=None,
    ) -> dict:
        """Create a Customer (with the payment method attached as default)
        and a Subscription on ``price_id``. Applies a discount when given —
        ``promotion_code`` (a ``promo_...`` id) is PREFERRED over a bare
        ``coupon`` id so the promo's own restrictions are enforced by Stripe
        (decision 2026-06-09); ``coupon`` remains a fallback.

        Name/address/company collected by our own billing-details form are
        set on the Customer (``name``, ``address``); the company goes on the
        Customer ``metadata`` (Stripe has no first-class company field for a
        Customer). Empty values are omitted so we never send blank fields.

        Returns ``{"customer_id": str, "subscription_id": str, "status": str}``
        — ``status`` is the raw Stripe ``Subscription.status``. Raises
        ``StripeError`` on any SDK error (the route maps it to a clean
        decline/error response WITHOUT changing plan state).
        """
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            customer_params = {
                "email": customer_email or None,
            }
            # a 100%-off coupon makes the order $0 — there is nothing
            # to charge, so the page submits with no payment method. Only attach
            # one (and make it the default) when one was actually provided;
            # otherwise Stripe would reject an empty payment_method id.
            if payment_method_id:
                customer_params["payment_method"] = payment_method_id
                customer_params["invoice_settings"] = {"default_payment_method": payment_method_id}
            if customer_name:
                customer_params["name"] = customer_name
            # Only send a non-empty address (drop blank sub-fields).
            address = {k: v for k, v in (customer_address or {}).items() if v}
            if address:
                customer_params["address"] = address
            if company:
                customer_params["metadata"] = {"company": company}
            customer = stripe.Customer.create(**customer_params)
            sub_params = {
                "customer": customer["id"],
                "items": [{"price": price_id}],
                "expand": ["latest_invoice.payment_intent"],
            }
            # Prefer the promotion_code (enforces the promo's redemption
            # caps/expiry/eligibility); fall back to the bare coupon id. Newer
            # Stripe accounts run flexible billing, which REJECTS the legacy
            # top-level ``coupon`` param — the supported form is
            # ``discounts=[{...}]`` (works on classic + flexible).
            if promotion_code:
                sub_params["discounts"] = [{"promotion_code": promotion_code}]
            elif coupon:
                sub_params["discounts"] = [{"coupon": coupon}]
            # charge when the free trial ends, not today.
            if trial_end:
                sub_params["trial_end"] = int(trial_end)
            subscription = stripe.Subscription.create(**sub_params)
        except Exception as exc:  # noqa: BLE001 — wrap every SDK error uniformly
            logging.warning("StripeService.create_subscription failed: %s", exc)
            raise StripeError(str(exc)) from exc

        # Read fields via subscript, NOT ``.get(...)``: the SDK 15.x return is a
        # StripeObject, which supports ``obj["key"]`` but has no ``.get`` method
        # (``obj.get(...)`` raises ``AttributeError: get``). ``_field`` reads
        # safely from either a StripeObject or a plain dict (test fakes).
        return {
            "customer_id": _field(customer, "id"),
            "subscription_id": _field(subscription, "id"),
            "status": _field(subscription, "status"),
        }

    @staticmethod
    def create_customer(
        *,
        secret_key: str,
        email: str = "",
        name: str = "",
        address: dict | None = None,
        company: str = "",
        payment_method_id: str = "",
        sdk=None,
    ) -> str:
        """Create a Stripe Customer and return its id (Option B — the
        ORG customer that team subscriptions attach to). When ``payment_method_id``
        is given it's attached as the default. Empty fields are omitted. Raises
        ``StripeError`` on any SDK error."""
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            params: dict = {}
            if email:
                params["email"] = email
            if name:
                params["name"] = name
            addr = {k: v for k, v in (address or {}).items() if v}
            if addr:
                params["address"] = addr
            if company:
                params["metadata"] = {"company": company}
            if payment_method_id:
                params["payment_method"] = payment_method_id
                params["invoice_settings"] = {"default_payment_method": payment_method_id}
            customer = stripe.Customer.create(**params)
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.create_customer failed: %s", exc)
            raise StripeError(str(exc)) from exc
        return _field(customer, "id")

    @staticmethod
    def create_subscription_for_customer(
        *,
        secret_key: str,
        customer_id: str,
        price_id: str,
        coupon: str = "",
        promotion_code: str = "",
        metadata: dict | None = None,
        payment_method_id: str = "",
        trial_end: int | None = None,
        sdk=None,
    ) -> dict:
        """Add ONE subscription on ``price_id`` to an EXISTING customer (
        Option B — one subscription per team license under the org's customer).

        Unlike ``create_subscription`` this does NOT create a customer — it
        attaches another subscription to the org customer, so an org can carry
        N team subscriptions. ``metadata`` (e.g. ``{"team_esid": ...}``) is set on
        the subscription so the webhook can map an event back to the team.
        ``promotion_code`` is preferred over a bare ``coupon`` (flexible-billing
        safe). When ``payment_method_id`` is given it's the subscription's
        default. Returns ``{"subscription_id", "status"}``. Raises ``StripeError``
        on any SDK error (no state change on failure)."""
        if not (customer_id and price_id):
            raise StripeError("missing customer or price id")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            params: dict = {
                "customer": customer_id,
                "items": [{"price": price_id}],
                "expand": ["latest_invoice.payment_intent"],
            }
            if metadata:
                params["metadata"] = metadata
            if payment_method_id:
                params["default_payment_method"] = payment_method_id
            if promotion_code:
                params["discounts"] = [{"promotion_code": promotion_code}]
            elif coupon:
                params["discounts"] = [{"coupon": coupon}]
            # charge when the free trial ends, not today.
            if trial_end:
                params["trial_end"] = int(trial_end)
            subscription = stripe.Subscription.create(**params)
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.create_subscription_for_customer failed: %s", exc)
            raise StripeError(str(exc)) from exc
        return {
            "subscription_id": _field(subscription, "id"),
            "status": _field(subscription, "status"),
        }

    @staticmethod
    def validate_coupon(*, secret_key: str, code: str, price_id: str = "", sdk=None) -> dict:
        """Resolve a customer-entered promo/coupon ``code`` to an applicable
        Stripe discount BEFORE charging.

        Customers type a human code (e.g. ``LAUNCH20``). In Stripe that is a
        **Promotion Code** (customer-facing, case-insensitive lookup) that
        points at a **Coupon** (the actual discount). ONLY promotion codes are
        accepted (decision 2026-06-09) — a raw Coupon id is an internal Stripe
        identifier, not a customer-facing code, and validates as invalid.

        Returns a small verdict the route consumes::

            {
                "valid": bool,
                "coupon": str,         # the coupon id to pass to Subscription.create
                "description": str,    # short human label e.g. "20% off" / "$10 off"
                "percent_off": float,  # 0 when not a percentage coupon
                "amount_off": int,     # discount in minor units (cents); 0 if none
                "currency": str,       # lowercase ISO currency for amount_off, "" otherwise
                "reason": str,         # why invalid (for logging), "" when valid
            }

        ``percent_off`` / ``amount_off`` let the caller compute the discounted
        total to show the customer BEFORE charging (the discount Stripe will
        actually apply). Never raises for an unknown/expired/inactive code —
        that's a normal "invalid coupon" verdict. Raises ``StripeError`` only on
        an unexpected SDK/transport failure so the route can fail-soft.

        ``price_id``: when given, its Stripe product is
        resolved and a coupon whose ``applies_to.products`` does NOT include
        that product is rejected as ``wrong_product`` — otherwise a coupon
        scoped to another product (e.g. the add-on-only mission coupon entered
        at base-plan checkout) would VALIDATE (``coupon.valid`` is True
        regardless of ``applies_to``) and preview a discount Stripe then applies
        to nothing at charge time — a misleading preview. With no ``price_id``
        the check is skipped (unrestricted coupons are unaffected); a
        price-retrieve failure fails OPEN (skip the check) so a transient blip
        never blocks a good coupon."""
        code = (code or "").strip()
        if not code:
            return _coupon_invalid("empty")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key

        # 1) Promotion-code lookup (the customer-facing code). active=True so an
        #    archived/disabled promo doesn't validate.
        try:
            promos = stripe.PromotionCode.list(code=code, active=True, limit=1)
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.validate_coupon promo lookup failed: %s", exc)
            raise StripeError(str(exc)) from exc
        promo_data = _field(promos, "data", []) or []
        if not promo_data:
            # Promotion codes ONLY (decision 2026-06-09). A raw coupon ID is an
            # internal Stripe identifier, not a customer-facing code — it is
            # NOT accepted. No promo match → invalid.
            return _coupon_invalid("not_found")

        # The promo's nested ``coupon`` is unreliable through the SDK's
        # StripeObject layer (subscript/attr access can raise, and the
        # str()-form may show null) even though the REST payload carries it.
        # So extract just the coupon ID from the promo (via a JSON round-trip,
        # robust across SDK versions) and retrieve the coupon fresh.
        # The promotion code id (``promo_...``) is what the apply paths discount
        # WITH (not the bare coupon id), so the promo's own restrictions are
        # enforced by Stripe (decision 2026-06-09). Capture it from the matched
        # promo object.
        promotion_code_id = _field(promo_data[0], "id", "") or ""
        coupon_id = _coupon_id_from_promo(promo_data[0])
        if coupon_id:
            try:
                # expand=["applies_to"] is REQUIRED: Stripe
                # does NOT include the coupon's applies_to product restriction
                # in the retrieve response by default — without the expand the
                # coupon looks unrestricted and a product-restricted coupon
                # (e.g. the add-on-only mission coupon) validates on the WRONG
                # product. This was the real leak behind the staging bug.
                coupon = stripe.Coupon.retrieve(coupon_id, expand=["applies_to"])
            except Exception as exc:  # noqa: BLE001
                logging.warning("StripeService.validate_coupon: promo coupon retrieve failed: %s", exc)
                raise StripeError(str(exc)) from exc
            if _coupon_is_usable(coupon):
                # Product-scope check: a coupon restricted
                # via applies_to.products must include the product being
                # purchased, else it would preview a discount Stripe applies to
                # nothing at charge. Resolve the price's product; skip the check
                # when no price_id is given or the product can't be resolved
                # (fail-open — never block a good coupon on a transient blip).
                product_id = StripeService._product_id_for_price(stripe, price_id) if price_id else ""
                # fail_closed: a restricted coupon whose product list can't be
                # read is REJECTED here (we're validating against a specific
                # product), so a mis-introspected restricted coupon never leaks
                # onto the wrong product.
                if product_id and not _coupon_applies_to_product(coupon, product_id, fail_closed=True):
                    return _coupon_invalid("wrong_product")
                return _coupon_valid(coupon, promotion_code=promotion_code_id)
        return _coupon_invalid("coupon_inactive")

    @staticmethod
    def _product_id_for_price(stripe, price_id: str) -> str:
        """Resolve a price's product id. Returns "" on any
        miss so the caller skips the product-scope check (fail-open)."""
        if not price_id:
            return ""
        try:
            price = stripe.Price.retrieve(price_id)
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService._product_id_for_price retrieve failed: %s", exc)
            return ""
        product = _field(price, "product", "")
        # ``product`` may be an id string or an expanded object.
        if isinstance(product, str):
            return product
        return _field(product, "id", "") or ""

    @staticmethod
    def construct_webhook_event(*, payload: bytes, signature: str, secret: str, sdk=None) -> dict:
        """Verify + parse a webhook payload. Returns the event dict.

        Raises ``StripeSignatureError`` ONLY for an actual signature problem
        (empty secret, missing header, or the SDK's
        ``SignatureVerificationError``) — the webhook route turns that into a
        400 (fail closed, no state change). Any OTHER SDK error (e.g. a
        payload-shape/parse issue) is raised as a generic ``StripeError`` so
        the route's post-verify boundary logs it and acks, rather than
        mislabeling it "invalid signature" (which previously turned a parse
        bug into a misleading 400 — see the SDK 15.x ``object`` handling).
        """
        if not secret:
            raise StripeSignatureError("webhook secret not configured")
        if not signature:
            raise StripeSignatureError("missing Stripe-Signature header")
        stripe = sdk or _load_sdk()
        # Resolve the SDK's signature-verification error class when available
        # (real SDK); a test fake may not expose ``error``, so degrade safely.
        sig_error_cls = getattr(getattr(stripe, "error", None), "SignatureVerificationError", None)
        try:
            event = stripe.Webhook.construct_event(payload, signature, secret)
        except Exception as exc:  # noqa: BLE001 — classify: signature vs. other
            is_sig_error = sig_error_cls is not None and isinstance(exc, sig_error_cls)
            if is_sig_error:
                logging.warning("StripeService: webhook signature verification failed: %s", exc)
                raise StripeSignatureError(str(exc)) from exc
            # Not a signature problem — a real processing/parse error.
            logging.warning("StripeService: webhook construct error (non-signature): %r", exc)
            raise StripeError(str(exc)) from exc
        # Normalize the SDK Event (a StripeObject) into a plain, fully-nested
        # dict the caller can ``.get(...)`` at every level. Two traps on
        # SDK 15.x: ``dict(event)`` raises ``KeyError: 0`` (StripeObject is
        # not a plain mapping), and the bare StripeObject has no ``.get``
        # (``AttributeError: get``) — both previously 500'd the verified
        # webhook. ``to_dict_recursive`` does not exist on 15.x either. The
        # robust, version-proof normalization is a JSON round-trip of the
        # object's string form, which yields nested plain dicts. Fall back
        # through to_dict_recursive() / to_dict() / the raw object only if
        # the JSON path is somehow unavailable.
        import json as _json

        try:
            return _json.loads(str(event))
        except Exception:  # noqa: BLE001
            for attr in ("to_dict_recursive", "to_dict"):
                fn = getattr(event, attr, None)
                if callable(fn):
                    try:
                        return fn()
                    except Exception:  # noqa: BLE001
                        continue
            return event

    @staticmethod
    def get_subscription_summary(*, secret_key: str, customer_id: str, sdk=None) -> dict | None:
        """Return a small summary of the customer's most recent subscription,
        or ``None`` when they have none (Current Plan bill date +
        cancel).

        Shape::

            {
                "subscription_id": str,
                "status": str,                 # raw Stripe status
                "trial_end": str, # ISO-8601 UTC while trialing, "" otherwise
                "current_period_start": str,   # ISO-8601 UTC (cycle start), "" if absent
                "current_period_end": str,     # ISO-8601 UTC (next charge), "" if absent
                "cancel_at_period_end": bool,  # True once a cancel is scheduled
                "has_discount": bool,          # True iff a coupon discounts the sub
                "amount_total": int,           # actual charged amount, minor units (cents)
                "amount_subtotal": int,        # pre-discount amount, minor units (cents)
                "price_ids": [str], # item price ids
            }

        ``amount_total`` is what Stripe actually charges (list price minus any
        coupon), read off the latest invoice — the ground-truth discounted
        amount the billing pages display. ``has_discount``
        is True when total < subtotal. Both are 0 when no invoice is available.

        Reads via ``Subscription.list(customer=..., limit=1)`` so it works even
        for users created before we stamped a subscription id. Raises
        ``StripeError`` on an SDK error so the caller can fail-soft (omit the
        bill date / cancel control rather than 500 the page).
        """
        if not customer_id:
            return None
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            subs = stripe.Subscription.list(customer=customer_id, limit=1, status="all", expand=["data.discounts"])
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.get_subscription_summary failed: %s", exc)
            raise StripeError(str(exc)) from exc

        data = _field(subs, "data", []) or []
        if not data:
            return None
        sub = data[0]
        # SDK/API 15.x relocated current_period_end OFF the top-level
        # Subscription ONTO the subscription item (items.data[0]). Read the
        # item first, fall back to the top level for older API versions.
        period_end = _field(sub, "current_period_end", 0)
        if not period_end:
            items = _field(_field(sub, "items", {}) or {}, "data", []) or []
            if items:
                period_end = _field(items[0], "current_period_end", 0)
        # current_period_start rides the same 15.x relocation —
        # item first, top level as fallback. The AI-spend readout uses it
        # as the cycle window start for paid workspaces.
        period_start = _field(sub, "current_period_start", 0)
        if not period_start:
            items = _field(_field(sub, "items", {}) or {}, "data", []) or []
            if items:
                period_start = _field(items[0], "current_period_start", 0)

        def _epoch_iso(epoch):
            if not epoch:
                return ""
            try:
                from datetime import datetime, timezone

                return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
            except Exception:  # noqa: BLE001
                return ""

        iso = _epoch_iso(period_end)
        start_iso = _epoch_iso(period_start)
        # a card saved during the free trial leaves the subscription
        # ``trialing`` until ``trial_end``; the Current Plan page shows that date.
        trial_end_iso = _epoch_iso(_field(sub, "trial_end", 0))

        # Recurring charged amount = the item's unit price minus any active
        # subscription discount. We read this from the SUBSCRIPTION (durable),
        # NOT the latest invoice: Subscription.modify(discounts=...) only
        # discounts the NEXT invoice, so the already-paid latest invoice still
        # shows the full price and the discount would "vanish" on refresh
        #. JSON-normalize the sub once for reliable nested
        # reads (the SDK StripeObject layer is awkward with nested fields).
        amount_subtotal = 0
        amount_total = 0
        try:
            import json as _json

            sub_d = _json.loads(str(sub))
        except Exception:  # noqa: BLE001
            sub_d = {}
        items = ((sub_d.get("items") or {}).get("data")) or []
        # every item's price id, so the Current Plan page can name
        # the plan from the live subscription after a superuser plan change.
        price_ids = [str((it.get("price") or {}).get("id") or "") for it in items if (it.get("price") or {}).get("id")]
        if items:
            unit = (items[0].get("price") or {}).get("unit_amount")
            qty = items[0].get("quantity") or 1
            if unit:
                amount_subtotal = int(unit) * int(qty)
        # Active discount on the subscription → its coupon's percent/amount.
        coupon = _discount_coupon_from_sub(sub_d, stripe)
        discount_coupon_id = coupon.get("id", "") if coupon else ""
        amount_total = amount_subtotal
        if amount_subtotal and coupon:
            pct = coupon.get("percent_off") or 0
            amt = coupon.get("amount_off") or 0
            if pct:
                amount_total = int(round(amount_subtotal * (1 - pct / 100.0)))
            elif amt:
                amount_total = max(0, amount_subtotal - int(amt))
        # Fallback: if the item price wasn't available, use the latest invoice
        # (covers odd shapes) so we never show a blank price.
        if not amount_subtotal:
            inv = sub_d.get("latest_invoice") or {}
            if isinstance(inv, dict):
                amount_subtotal = int(inv.get("subtotal") or 0)
                amount_total = int(inv.get("total") or 0) or amount_subtotal
        # NOTE: compare totals directly — do NOT gate on ``amount_total`` being
        # truthy. A 100%-off coupon makes amount_total == 0, which is falsy, so
        # ``amount_total and ...`` would wrongly report has_discount=False for
        # the biggest discount.
        has_discount = bool(amount_subtotal) and amount_total < amount_subtotal
        return {
            "subscription_id": _field(sub, "id"),
            "status": _field(sub, "status"),
            "trial_end": trial_end_iso,
            "current_period_start": start_iso,
            "current_period_end": iso,
            "cancel_at_period_end": bool(_field(sub, "cancel_at_period_end", False)),
            "has_discount": has_discount,
            "amount_total": amount_total,
            "amount_subtotal": amount_subtotal,
            "discount_coupon": discount_coupon_id,
            "price_ids": price_ids,
        }

    @staticmethod
    def get_customer_billing_details(*, secret_key: str, customer_id: str, sdk=None) -> dict:
        """Return the Customer's stored billing details for pre-filling the
        update-card form — the same shape the form posts::

            {"name", "company", "address": {line1, city, state, postal_code, country}}

        Reads ``Customer.retrieve`` (name + address) plus ``metadata.company``
        (where create/update store the company, since Stripe has no first-class
        Customer company field). Returns empty strings for anything absent.
        Raises ``StripeError`` on an SDK error so the caller can fail-soft
        (render the form blank rather than 500 the page).
        """
        if not customer_id:
            return {"name": "", "company": "", "address": {}}
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            customer = stripe.Customer.retrieve(customer_id)
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.get_customer_billing_details failed: %s", exc)
            raise StripeError(str(exc)) from exc

        addr = _field(customer, "address", {}) or {}
        metadata = _field(customer, "metadata", {}) or {}
        return {
            "name": _field(customer, "name", "") or "",
            "company": _field(metadata, "company", "") or "",
            "address": {
                "line1": _field(addr, "line1", "") or "",
                "city": _field(addr, "city", "") or "",
                "state": _field(addr, "state", "") or "",
                "postal_code": _field(addr, "postal_code", "") or "",
                "country": _field(addr, "country", "") or "",
            },
        }

    @staticmethod
    def cancel_subscription(*, secret_key: str, subscription_id: str, at_period_end: bool = True, sdk=None) -> dict:
        """Cancel a subscription. Default ``at_period_end=True`` keeps
        access through the paid period (``modify(cancel_at_period_end=True)``);
        ``at_period_end=False`` cancels immediately (``Subscription.cancel``).

        Returns ``{"subscription_id", "status", "cancel_at_period_end"}``.
        Raises ``StripeError`` on any SDK error.
        """
        if not subscription_id:
            raise StripeError("missing subscription id")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            if at_period_end:
                sub = stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)
            else:
                sub = stripe.Subscription.cancel(subscription_id)
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.cancel_subscription failed: %s", exc)
            raise StripeError(str(exc)) from exc
        return {
            "subscription_id": _field(sub, "id"),
            "status": _field(sub, "status"),
            "cancel_at_period_end": bool(_field(sub, "cancel_at_period_end", False)),
        }

    @staticmethod
    def change_subscription_price(
        *,
        secret_key: str,
        subscription_id: str,
        new_price_id: str,
        base_price_ids,
        proration_behavior: str = "none",
        sdk=None,
    ) -> dict:
        """Move a subscription's BASE plan item onto another price (—
        the superuser Business ↔ Nonprofit change).

        The base item is the subscription item whose price is one of
        ``base_price_ids`` (the caller passes the configured Business and
        Nonprofit price ids — this module never reads config); the add-on
        connections item is left alone. ``proration_behavior``
        defaults to ``"none"``: the new price applies from the next invoice
        and nothing is charged or credited today (an administrative decision,
        not a customer purchase).

        Returns ``{"subscription_id", "status", "item_id", "old_price_id",
        "new_price_id", "changed"}``. ``changed`` is False — and nothing is
        sent to Stripe — when the base item already carries ``new_price_id``.
        Raises ``StripeError`` when an id is missing, ``new_price_id`` is not
        one of the base prices, the subscription has no base plan item, or
        the SDK fails.
        """
        if not subscription_id or not new_price_id:
            raise StripeError("missing subscription or price id")
        base_ids = {p for p in (base_price_ids or ()) if p}
        if new_price_id not in base_ids:
            raise StripeError("new price is not a base plan price")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            sub = stripe.Subscription.retrieve(subscription_id, expand=["items.data.price"])
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.change_subscription_price retrieve failed: %s", exc)
            raise StripeError(str(exc)) from exc
        items = _field(_field(sub, "items", {}) or {}, "data", []) or []
        item_id, old_price_id = "", ""
        for it in items:
            pid = _field(_field(it, "price", {}) or {}, "id", "")
            if pid in base_ids:
                item_id, old_price_id = _field(it, "id", ""), pid
                break
        if not item_id:
            raise StripeError("subscription has no base plan item")
        result = {
            "subscription_id": _field(sub, "id") or subscription_id,
            "status": _field(sub, "status"),
            "item_id": item_id,
            "old_price_id": old_price_id,
            "new_price_id": new_price_id,
            "changed": False,
        }
        if old_price_id == new_price_id:
            return result
        try:
            updated = stripe.Subscription.modify(
                subscription_id,
                items=[{"id": item_id, "price": new_price_id}],
                proration_behavior=proration_behavior,
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.change_subscription_price modify failed: %s", exc)
            raise StripeError(str(exc)) from exc
        result["status"] = _field(updated, "status") or result["status"]
        result["changed"] = True
        return result

    @staticmethod
    def apply_coupon_to_subscription(
        *, secret_key: str, subscription_id: str, coupon: str = "", promotion_code: str = "", sdk=None
    ) -> dict:
        """Attach a discount to an EXISTING subscription (coupon-for-
        current-plan 2026-06-08) via ``Subscription.modify(discounts=...)``.

        ``promotion_code`` (a ``promo_...`` id) is PREFERRED over a bare
        ``coupon`` id so the promo's own restrictions (redemption caps, expiry,
        eligibility) are enforced by Stripe (decision 2026-06-09); ``coupon``
        remains a fallback.

        Stripe applies the discount from the NEXT invoice onward — the current
        (already-paid) period is not refunded. A subscription holds at most ONE
        discount, so this REPLACES any existing one rather than stacking
        (Stripe does not stack coupons). Returns ``{"subscription_id",
        "status"}``. Raises ``StripeError`` on any SDK error (caller maps it to
        a clean message; the existing sub is unchanged).
        """
        if not subscription_id or not (coupon or promotion_code):
            raise StripeError("missing subscription id or discount")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            # ``discounts=[{...}]`` — NOT the legacy top-level ``coupon=`` param,
            # which flexible-billing accounts reject (see create_subscription).
            # Passing discounts REPLACES any existing discount (Stripe holds one
            # per subscription; coupons don't stack). Prefer promotion_code.
            discount = {"promotion_code": promotion_code} if promotion_code else {"coupon": coupon}
            sub = stripe.Subscription.modify(subscription_id, discounts=[discount])
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.apply_coupon_to_subscription failed: %s", exc)
            raise StripeError(str(exc)) from exc
        return {
            "subscription_id": _field(sub, "id"),
            "status": _field(sub, "status"),
        }

    @staticmethod
    def get_addon_quantity(*, secret_key: str, subscription_id: str, addon_price_id: str, sdk=None) -> int | None:
        """Return the LIVE add-on item quantity on the subscription — Stripe is
        the source of truth for what the org is actually paying for (
        reconciliation 2026-06-15). 0 when there's no add-on item.

        Returns ``None`` on any SDK/parse error so the caller can DISTINGUISH
        "couldn't read Stripe" (leave the stored metadata alone — don't zero a
        real entitlement on a transient blip) from a confirmed 0."""
        if not (subscription_id and addon_price_id):
            return None
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            _item_id, qty = StripeService._find_addon_item(stripe, subscription_id, addon_price_id)
            return int(qty)
        except Exception as exc:  # noqa: BLE001 — fail-soft: unknown, not zero
            logging.warning("StripeService.get_addon_quantity failed: %s", exc)
            return None

    @staticmethod
    def _find_addon_item(stripe, subscription_id: str, addon_price_id: str):
        """Return the existing add-on subscription ITEM (its id + quantity) for
        ``addon_price_id`` on the sub, or ``(None, 0)`` if not present. Raises
        ``StripeError`` on an SDK error."""
        try:
            sub = stripe.Subscription.retrieve(subscription_id, expand=["items.data.price"])
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService._find_addon_item retrieve failed: %s", exc)
            raise StripeError(str(exc)) from exc
        items = _field(_field(sub, "items", {}) or {}, "data", []) or []
        for it in items:
            price = _field(it, "price", {}) or {}
            if _field(price, "id", "") == addon_price_id:
                return _field(it, "id", ""), int(_field(it, "quantity", 0) or 0)
        return None, 0

    @staticmethod
    def get_addon_discount_ids(*, secret_key: str, subscription_id: str, addon_price_id: str, sdk=None) -> set:
        """Return the set of discount ids (coupon AND/OR promotion_code) currently
        on the add-on item (the "already applied" guard for the add-on
        coupon surface). Empty set when there's no add-on item or no discount.
        Best-effort: returns an empty set on any SDK/parse error (so the caller
        treats it as "not already applied" rather than blocking)."""
        if not (subscription_id and addon_price_id):
            return set()
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            sub = stripe.Subscription.retrieve(subscription_id, expand=["items.data.discounts"])
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.get_addon_discount_ids retrieve failed: %s", exc)
            return set()
        import json as _json

        try:
            sub_d = _json.loads(str(sub))
        except Exception:  # noqa: BLE001
            return set()
        out: set = set()
        for it in (sub_d.get("items") or {}).get("data") or []:
            price = it.get("price") or {}
            if (price.get("id") if isinstance(price, dict) else price) != addon_price_id:
                continue
            for d in it.get("discounts") or []:
                if isinstance(d, str):
                    out.add(d)
                elif isinstance(d, dict):
                    # A discount carries a coupon and/or a promotion_code.
                    cp = d.get("coupon")
                    out.add(cp.get("id") if isinstance(cp, dict) else cp) if cp else None
                    pc = d.get("promotion_code")
                    out.add(pc.get("id") if isinstance(pc, dict) else pc) if pc else None
        out.discard(None)
        out.discard("")
        return out

    @staticmethod
    def get_addon_discount_value(*, secret_key: str, subscription_id: str, addon_price_id: str, sdk=None) -> dict:
        """Return the add-on item's CURRENT discount as
        ``{"percent_off": float|None, "amount_off": int|None}`` (amount in
        minor units), or ``{}`` when the item carries no discount (
        2026-06-15 — the worse-value warning on the add-on coupon box compares
        the new code to this). Best-effort: ``{}`` on any SDK/parse error, so
        the caller simply skips the warning rather than blocking a legit apply."""
        if not (subscription_id and addon_price_id):
            return {}
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        import json as _json

        try:
            sub = stripe.Subscription.retrieve(subscription_id, expand=["items.data.discounts"])
            sub_d = _json.loads(str(sub))
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.get_addon_discount_value failed: %s", exc)
            return {}
        for it in (sub_d.get("items") or {}).get("data") or []:
            price = it.get("price") or {}
            if (price.get("id") if isinstance(price, dict) else price) != addon_price_id:
                continue
            for d in it.get("discounts") or []:
                cp = d.get("coupon") if isinstance(d, dict) else None
                if isinstance(cp, dict):
                    return {"percent_off": cp.get("percent_off"), "amount_off": cp.get("amount_off")}
        return {}

    @staticmethod
    def set_addon_connections(
        *,
        secret_key: str,
        subscription_id: str,
        quantity: int,
        addon_price_id: str,
        mission_coupon: str = "",
        proration_behavior: str = "always_invoice",
        preserve_discounts: bool = False,
        sdk=None,
    ) -> dict:
        """Set the number of add-on data connections on an EXISTING subscription
        . Adds/updates a SECOND subscription item on
        ``addon_price_id`` with ``quantity`` = connections beyond the included
        cap, and prorates the mid-cycle change (item 7) via
        ``proration_behavior`` (default ``always_invoice`` — charge the prorated
        delta now).

        ``mission_coupon`` (when given) is applied ITEM-LEVEL to the add-on item
        only (``item.discounts=[{"coupon": id}]``), so the base plan stays full
        price — the $5 mission rate. ``quantity=0`` removes the add-on item.

        Returns ``{"subscription_id", "quantity", "status"}``. Raises
        ``StripeError`` on any SDK error (caller maps to a clean response; the
        subscription is unchanged on failure).
        """
        if not subscription_id:
            raise StripeError("missing subscription id")
        if not addon_price_id:
            raise StripeError("missing add-on price id")
        if quantity < 0:
            raise StripeError("quantity must be >= 0")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key

        item_id, _current_qty = StripeService._find_addon_item(stripe, subscription_id, addon_price_id)

        # Build the single item-update entry. New item → {price, quantity};
        # existing → {id, quantity} (Stripe keys the change off the item id).
        # quantity=0 on an existing item → delete it (drop the add-on entirely).
        item: dict = {}
        if item_id:
            item["id"] = item_id
            if quantity == 0:
                item["deleted"] = True
            else:
                item["quantity"] = quantity
        else:
            if quantity == 0:
                # Nothing to add and nothing to remove — no-op.
                return {"subscription_id": subscription_id, "quantity": 0, "status": "noop"}
            item["price"] = addon_price_id
            item["quantity"] = quantity
        # Item-level discount on the add-on item (only when keeping the item).
        # The coupon is a code the user types per purchase (decision:
        # re-enter each time). So a code present → set it; NO code → explicitly
        # CLEAR any prior discount (``discounts: []``) so a blank field always
        # means full price — never a stale discount silently left on the item
        # (Stripe leaves an item's discount untouched if ``discounts`` is
        # omitted, which would make the rate disagree with the empty field).
        if quantity > 0:
            if mission_coupon:
                item["discounts"] = [_item_discount(mission_coupon)]
            elif not preserve_discounts:
                # Blank coupon field on a PURCHASE/adjust = full price (clear
                # any stale discount). The scheduled-reduction flow passes
                # preserve_discounts=True instead — its Remove box has no
                # coupon field, and dropping 1 of 3 must not strip the
                # discount from the remaining 2.
                item["discounts"] = []

        try:
            sub = stripe.Subscription.modify(
                subscription_id,
                items=[item],
                proration_behavior=proration_behavior,
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.set_addon_connections failed: %s", exc)
            raise StripeError(str(exc)) from exc
        return {
            "subscription_id": _field(sub, "id"),
            "quantity": quantity,
            "status": _field(sub, "status"),
        }

    @staticmethod
    def apply_coupon_to_addon(
        *,
        secret_key: str,
        subscription_id: str,
        addon_price_id: str,
        coupon: str = "",
        promotion_code: str = "",
        sdk=None,
    ) -> dict:
        """Apply a discount to an ALREADY-PURCHASED add-on item (the
        "Have a coupon for your add-ons?" surface on Current Plan).

        Unlike ``set_addon_connections`` this does NOT change the quantity and
        does NOT charge now: it attaches an ITEM-LEVEL discount with
        ``proration_behavior="none"``, so the discount takes effect on the NEXT
        bill (mirrors the base-plan "Have a coupon?" / apply_coupon_to_subscription
        UX). ``promotion_code`` is preferred over a bare ``coupon`` id (enforces
        the promo's restrictions). Returns ``{"subscription_id", "quantity",
        "status"}``. Raises ``StripeError`` when there is no add-on item to
        discount, or on any SDK error (the sub is unchanged).
        """
        if not subscription_id or not addon_price_id:
            raise StripeError("missing subscription or add-on price id")
        if not (coupon or promotion_code):
            raise StripeError("missing discount")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key

        item_id, qty = StripeService._find_addon_item(stripe, subscription_id, addon_price_id)
        if not item_id:
            # No add-on purchased — nothing to discount. The route guards this
            # too, but fail loud here so a bad call can't silently no-op.
            raise StripeError("no add-on connections to apply a coupon to")

        discount = {"promotion_code": promotion_code} if promotion_code else {"coupon": coupon}
        try:
            sub = stripe.Subscription.modify(
                subscription_id,
                items=[{"id": item_id, "discounts": [discount]}],
                proration_behavior="none",  # discount hits the NEXT bill, no charge now
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.apply_coupon_to_addon failed: %s", exc)
            raise StripeError(str(exc)) from exc
        return {
            "subscription_id": _field(sub, "id"),
            "quantity": qty,
            "status": _field(sub, "status"),
        }

    @staticmethod
    def preview_addon_proration(
        *,
        secret_key: str,
        customer_id: str,
        subscription_id: str,
        quantity: int,
        addon_price_id: str,
        mission_coupon: str = "",
        preserve_discounts: bool = False,
        sdk=None,
    ) -> dict:
        """Preview the immediate prorated charge for changing the add-on
        connection quantity, WITHOUT modifying the subscription (item 7
        UX — "you'll be charged $X now"). Uses the create-preview-invoice API
        with the same item change ``set_addon_connections`` would apply.

        Returns ``{"amount_due": int}`` (minor units, the prorated amount to be
        charged now) — ``0`` if it can't be previewed. Raises ``StripeError`` on
        an SDK error.
        """
        if not (subscription_id and customer_id and addon_price_id):
            raise StripeError("missing customer/subscription/price id")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key

        item_id, _ = StripeService._find_addon_item(stripe, subscription_id, addon_price_id)
        item: dict = {}
        if item_id:
            item["id"] = item_id
            item["quantity"] = quantity if quantity > 0 else 0
            if quantity == 0:
                item["deleted"] = True
        else:
            item["price"] = addon_price_id
            item["quantity"] = max(quantity, 0)
        # Mirror set_addon_connections: a code → set it; no code → either clear
        # (legacy purchase-with-field) or PRESERVE the item's existing discount
        # (final coupon form: purchases carry no field, so the preview must
        # reflect the applied coupon, not full price).
        if quantity > 0:
            if mission_coupon:
                item["discounts"] = [_item_discount(mission_coupon)]
            elif not preserve_discounts:
                item["discounts"] = []

        try:
            preview = stripe.Invoice.create_preview(
                customer=customer_id,
                subscription=subscription_id,
                subscription_details={"items": [item], "proration_behavior": "always_invoice"},
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.preview_addon_proration failed: %s", exc)
            raise StripeError(str(exc)) from exc
        return {"amount_due": int(_field(preview, "amount_due", 0) or 0)}

    @staticmethod
    def resume_subscription(*, secret_key: str, subscription_id: str, sdk=None) -> dict:
        """Undo a scheduled cancel — flip
        ``cancel_at_period_end`` back to ``False`` so the still-active
        subscription renews normally. Only meaningful while the sub is active
        with a pending period-end cancel (after it fully lapses there is
        nothing to resume — the user re-subscribes via the Payment form).

        Returns ``{"subscription_id", "status", "cancel_at_period_end"}``.
        Raises ``StripeError`` on any SDK error.
        """
        if not subscription_id:
            raise StripeError("missing subscription id")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            sub = stripe.Subscription.modify(subscription_id, cancel_at_period_end=False)
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.resume_subscription failed: %s", exc)
            raise StripeError(str(exc)) from exc
        return {
            "subscription_id": _field(sub, "id"),
            "status": _field(sub, "status"),
            "cancel_at_period_end": bool(_field(sub, "cancel_at_period_end", False)),
        }

    @staticmethod
    def update_payment_method(
        *,
        secret_key: str,
        customer_id: str,
        subscription_id: str,
        payment_method_id: str,
        customer_name: str = "",
        customer_address: dict | None = None,
        company: str = "",
        sdk=None,
    ) -> dict:
        """Swap the card on an existing subscription — used
        for "change card details". Does NOT create a new subscription.

        Steps so the new card is used on the NEXT invoice AND the customer is
        left with exactly ONE card on file:
          1. attach the new PaymentMethod to the Customer,
          2. set it as the Customer's default (``invoice_settings``),
          3. set it as the Subscription's ``default_payment_method`` (the
             subscription-level default wins for that sub's invoices),
          4. detach every OTHER card PaymentMethod on the Customer, so an
             "update" replaces the old card rather than stacking a second one
             (Stripe attaches, it does not auto-replace). Step 4 is
             best-effort: a detach failure is logged but does NOT fail the
             update — the new card is already the active default by step 3.

        Returns ``{"customer_id", "subscription_id", "payment_method_id"}``.
        Raises ``StripeError`` on an SDK error in steps 1–3 (the route maps it
        to a clean message; the existing card stays in place).
        """
        if not (customer_id and subscription_id and payment_method_id):
            raise StripeError("missing customer / subscription / payment method")
        stripe = sdk or _load_sdk()
        stripe.api_key = secret_key
        try:
            stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)
            # Set the new card as the Customer default AND refresh the stored
            # billing details (name/address/company) in the same modify call.
            # Empty values are omitted so a blank field never wipes existing data.
            customer_params = {"invoice_settings": {"default_payment_method": payment_method_id}}
            if customer_name:
                customer_params["name"] = customer_name
            address = {k: v for k, v in (customer_address or {}).items() if v}
            if address:
                customer_params["address"] = address
            if company:
                customer_params["metadata"] = {"company": company}
            stripe.Customer.modify(customer_id, **customer_params)
            stripe.Subscription.modify(
                subscription_id,
                default_payment_method=payment_method_id,
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("StripeService.update_payment_method failed: %s", exc)
            raise StripeError(str(exc)) from exc

        # Step 4 — replace, don't stack: detach the customer's OTHER cards so
        # only the new one remains. Best-effort; never raises past here (the
        # update already succeeded). Wrapped separately so a list/detach error
        # can't undo a successful card swap.
        try:
            existing = stripe.PaymentMethod.list(customer=customer_id, type="card")
            for pm in _field(existing, "data", []) or []:
                pm_id = _field(pm, "id")
                if pm_id and pm_id != payment_method_id:
                    try:
                        stripe.PaymentMethod.detach(pm_id)
                    except Exception as detach_exc:  # noqa: BLE001 — one bad detach must not abort the rest
                        logging.warning(
                            "StripeService.update_payment_method: detach of old card %s failed: %s",
                            pm_id,
                            detach_exc,
                        )
        except Exception as list_exc:  # noqa: BLE001 — cleanup is best-effort
            logging.warning(
                "StripeService.update_payment_method: could not list/detach old cards for %s: %s",
                customer_id,
                list_exc,
            )

        return {
            "customer_id": customer_id,
            "subscription_id": subscription_id,
            "payment_method_id": payment_method_id,
        }
