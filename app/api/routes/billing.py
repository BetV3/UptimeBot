"""
Stripe billing routes — checkout, customer portal, and webhook handler.

Webhook events handled:
    - checkout.session.completed       → record subscription on user
    - customer.subscription.updated    → sync plan + status + period end
    - customer.subscription.deleted    → downgrade to FREE
    - invoice.payment_failed           → mark subscription past_due
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.models import PlanType, SubscriptionStatus, User
from app.services.auth import decode_token

log = logging.getLogger(__name__)

router = APIRouter()
settings = get_settings()


PLAN_PRICE_MAP = {
    PlanType.STARTER: lambda: settings.stripe_starter_price_id,
    PlanType.PRO: lambda: settings.stripe_pro_price_id,
}

# Reverse lookup (price id -> plan) populated lazily so callers pick up the
# latest settings values even if they're loaded from env after import time.
def _plan_for_price(price_id: str) -> PlanType | None:
    if not price_id:
        return None
    if price_id == settings.stripe_starter_price_id:
        return PlanType.STARTER
    if price_id == settings.stripe_pro_price_id:
        return PlanType.PRO
    return None


async def _require_user(request: Request, db: AsyncSession) -> User:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    try:
        user_id = decode_token(token, expected_type="access")
    except HTTPException:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def _ensure_configured() -> None:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payments are not configured on this instance.")
    stripe.api_key = settings.stripe_secret_key


async def _ensure_stripe_customer(user: User, db: AsyncSession) -> str:
    """Return the user's stripe customer id, creating one if absent."""
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer = stripe.Customer.create(
        email=user.email,
        metadata={"user_id": str(user.id)},
    )
    user.stripe_customer_id = customer.id
    await db.commit()
    return customer.id


def _parse_plan(plan_value: str) -> PlanType:
    try:
        plan = PlanType(plan_value.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan. Choose 'starter' or 'pro'.")
    if plan == PlanType.FREE:
        raise HTTPException(status_code=400, detail="Cannot checkout the free plan.")
    return plan


def _period_end_from_sub(subscription: dict) -> datetime | None:
    epoch = subscription.get("current_period_end")
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def _status_from_sub(sub_status: str | None) -> SubscriptionStatus | None:
    if not sub_status:
        return None
    try:
        return SubscriptionStatus(sub_status)
    except ValueError:
        return None


async def _apply_subscription_to_user(
    db: AsyncSession,
    user: User,
    subscription: dict,
) -> None:
    """Sync plan + subscription fields on the user from a Stripe subscription object."""
    items = subscription.get("items", {}).get("data", [])
    price_id = items[0]["price"]["id"] if items else None
    plan = _plan_for_price(price_id)
    sub_status = _status_from_sub(subscription.get("status"))

    user.stripe_subscription_id = subscription.get("id")
    user.current_period_end = _period_end_from_sub(subscription)

    # Don't let a stale "incomplete" event overwrite an already-active status.
    # Stripe fires customer.subscription.created (incomplete) before the payment
    # is confirmed, and it can arrive after checkout.session.completed has already
    # set the status to active.
    active_states = {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING}
    if sub_status == SubscriptionStatus.INCOMPLETE and user.subscription_status in active_states:
        pass  # keep the existing active/trialing status
    else:
        user.subscription_status = sub_status

    if plan and sub_status in active_states:
        user.plan = plan
    elif sub_status in {SubscriptionStatus.CANCELED, SubscriptionStatus.INCOMPLETE_EXPIRED, SubscriptionStatus.UNPAID}:
        user.plan = PlanType.FREE

    await db.commit()


# --- Routes ---


@router.post("/checkout")
async def create_checkout_session(
    request: Request,
    plan: str,
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe Checkout session for the given plan and redirect to it."""
    _ensure_configured()
    user = await _require_user(request, db)
    plan_enum = _parse_plan(plan)

    price_id = PLAN_PRICE_MAP[plan_enum]()
    if not price_id:
        raise HTTPException(status_code=503, detail=f"Price id for {plan_enum.value} is not configured.")

    customer_id = await _ensure_stripe_customer(user, db)

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=str(user.id),
        metadata={"user_id": str(user.id), "plan": plan_enum.value},
        subscription_data={"metadata": {"user_id": str(user.id), "plan": plan_enum.value}},
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{settings.app_url}/dashboard/billing?upgraded=1",
        cancel_url=f"{settings.app_url}/dashboard/billing?cancelled=1",
        allow_promotion_codes=True,
    )
    return RedirectResponse(session.url, status_code=303)


@router.post("/portal")
async def create_portal_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe billing portal session for managing/cancelling subscription."""
    _ensure_configured()
    user = await _require_user(request, db)
    if not user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer on file. Subscribe first.")

    portal = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{settings.app_url}/dashboard/billing",
    )
    return RedirectResponse(portal.url, status_code=303)


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Stripe webhook events and keep user plan state in sync."""
    _ensure_configured()

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured.")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        log.warning("Invalid Stripe webhook signature: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        # Session gives us user_id via metadata and the subscription id.
        user_id = obj.get("metadata", {}).get("user_id") or obj.get("client_reference_id")
        subscription_id = obj.get("subscription")
        customer_id = obj.get("customer")
        if not user_id or not subscription_id:
            log.warning("checkout.session.completed missing user_id or subscription")
            return {"status": "ignored"}

        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            log.warning("Unknown user_id in checkout session: %s", user_id)
            return {"status": "ignored"}

        if customer_id and not user.stripe_customer_id:
            user.stripe_customer_id = customer_id

        subscription = stripe.Subscription.retrieve(subscription_id)
        await _apply_subscription_to_user(db, user, subscription)

    elif event_type in {"customer.subscription.updated", "customer.subscription.created"}:
        customer_id = obj.get("customer")
        if not customer_id:
            return {"status": "ignored"}
        result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
        user = result.scalar_one_or_none()
        if not user:
            log.warning("subscription.updated for unknown customer: %s", customer_id)
            return {"status": "ignored"}
        await _apply_subscription_to_user(db, user, obj)

    elif event_type == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        if not customer_id:
            return {"status": "ignored"}
        result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
        user = result.scalar_one_or_none()
        if not user:
            return {"status": "ignored"}
        user.plan = PlanType.FREE
        user.subscription_status = SubscriptionStatus.CANCELED
        user.stripe_subscription_id = None
        user.current_period_end = None
        await db.commit()

    elif event_type == "invoice.payment_failed":
        customer_id = obj.get("customer")
        if not customer_id:
            return {"status": "ignored"}
        result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
        user = result.scalar_one_or_none()
        if user:
            user.subscription_status = SubscriptionStatus.PAST_DUE
            await db.commit()

    return {"status": "ok"}
