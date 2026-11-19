"""Stripe Checkout helpers for the Siteless subscription plans."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


PAID_PLANS = {"starter", "agency"}


class BillingUnavailable(RuntimeError):
    """Raised when Stripe is not configured or the SDK is unavailable."""


class InvalidStripeWebhook(RuntimeError):
    """Raised when a Stripe webhook payload cannot be verified."""


def stripe_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
    return getattr(obj, key, default)


def stripe_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(stripe_value(value, "id", "") or "")


def plan_for_price(price_id: str, price_ids: dict[str, str]) -> str | None:
    if not price_id:
        return None
    return next((plan for plan, configured_id in price_ids.items()
                 if configured_id and configured_id == price_id and plan in PAID_PLANS), None)


def subscription_price_id(subscription: Any) -> str:
    items = stripe_value(stripe_value(subscription, "items", {}), "data", []) or []
    for item in items:
        price_id = stripe_id(stripe_value(item, "price"))
        if price_id:
            return price_id
    return ""


def plan_for_subscription(subscription: Any, price_ids: dict[str, str]) -> str | None:
    plan = plan_for_price(subscription_price_id(subscription), price_ids)
    if plan:
        return plan
    metadata = stripe_value(subscription, "metadata", {}) or {}
    metadata_plan = str(stripe_value(metadata, "plan", "") or "").casefold()
    return metadata_plan if metadata_plan in PAID_PLANS else None


def price_matches_expectation(
    price: Any,
    *,
    currency: str,
    unit_amount: int,
    interval: str = "month",
) -> bool:
    recurring = stripe_value(price, "recurring", {}) or {}
    return (
        str(stripe_value(price, "currency", "")).casefold() == currency.casefold()
        and stripe_value(price, "unit_amount") == unit_amount
        and stripe_value(recurring, "interval") == interval
        and bool(stripe_value(price, "active", True))
    )


def subscription_period_end(subscription: Any) -> str:
    timestamp = stripe_value(subscription, "current_period_end")
    if timestamp in (None, ""):
        items = stripe_value(stripe_value(subscription, "items", {}), "data", []) or []
        timestamp = max(
            (stripe_value(item, "current_period_end") or 0 for item in items),
            default=0,
        )
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def invoice_subscription_id(invoice: Any) -> str:
    direct = stripe_id(stripe_value(invoice, "subscription"))
    if direct:
        return direct
    parent = stripe_value(invoice, "parent", {}) or {}
    details = stripe_value(parent, "subscription_details", {}) or {}
    return stripe_id(stripe_value(details, "subscription"))


def _client(secret_key: str) -> tuple[Any, Any]:
    if not secret_key:
        raise BillingUnavailable("Configure STRIPE_SECRET_KEY para habilitar pagamentos.")
    try:
        import stripe
    except ImportError as exc:
        raise BillingUnavailable("Instale as dependências para habilitar o Stripe.") from exc
    return stripe, stripe.StripeClient(secret_key)


def create_checkout_session(
    *,
    secret_key: str,
    price_id: str,
    plan: str,
    currency: str,
    unit_amount: int,
    user_id: int,
    email: str,
    customer_id: str,
    base_url: str,
) -> Any:
    if plan not in PAID_PLANS or not price_id:
        raise BillingUnavailable("O preço recorrente deste plano ainda não foi configurado.")
    _stripe, client = _client(secret_key)
    configured_price = client.v1.prices.retrieve(price_id)
    if not price_matches_expectation(configured_price, currency=currency, unit_amount=unit_amount):
        raise BillingUnavailable(
            f"O preço Stripe configurado para {plan.title()} não corresponde a "
            f"{currency.upper()} {unit_amount / 100:.2f}/mês. Atualize o Price ID no arquivo .env."
        )
    metadata = {"user_id": str(user_id), "plan": plan}
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{base_url}/?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/?billing=cancelled",
        "client_reference_id": str(user_id),
        "metadata": metadata,
        "subscription_data": {"metadata": metadata},
    }
    if customer_id:
        params["customer"] = customer_id
    else:
        params["customer_email"] = email
    return client.v1.checkout.sessions.create(params)


def create_portal_session(*, secret_key: str, customer_id: str, return_url: str) -> Any:
    _stripe, client = _client(secret_key)
    return client.v1.billing_portal.sessions.create({
        "customer": customer_id,
        "return_url": return_url,
    })


def retrieve_subscription(*, secret_key: str, subscription_id: str) -> Any:
    _stripe, client = _client(secret_key)
    return client.v1.subscriptions.retrieve(subscription_id)


def construct_webhook_event(*, payload: bytes, signature: str, secret: str) -> Any:
    if not secret:
        raise BillingUnavailable("Configure STRIPE_WEBHOOK_SECRET para validar webhooks.")
    try:
        import stripe
    except ImportError as exc:
        raise BillingUnavailable("Instale as dependências para habilitar o Stripe.") from exc
    try:
        return stripe.Webhook.construct_event(payload, signature, secret)
    except Exception as exc:
        if isinstance(exc, (ValueError, TypeError)) or exc.__class__.__name__ in {
            "SignatureVerificationError", "InvalidPayloadError",
        }:
            raise InvalidStripeWebhook("Assinatura ou payload Stripe inválido.") from exc
        raise
