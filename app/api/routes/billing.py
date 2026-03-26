"""
Stripe billing routes — checkout sessions and webhook handler.
"""

import stripe
from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.models import PlanType, User
from app.services.auth import get_current_user

router = APIRouter()
settings = get_settings()


PLAN_PRICE_MAP = {
    "starter": settings.stripe_starter_price_id,
    "pro": settings.stripe_pro_price_id,
}


@router.post("/checkout")
async def create_checkout_session(
    plan: str,
    current_user: User = Depends(get_current_user),
):
    """Create a Stripe Checkout session for upgrading."""
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payments not configured yet")

    if plan not in PLAN_PRICE_MAP:
        raise HTTPException(status_code=400, detail="Invalid plan. Choose 'starter' or 'pro'.")

    price_id = PLAN_PRICE_MAP[plan]
    if not price_id:
        raise HTTPException(status_code=503, detail=f"Price ID for {plan} not configured")

    stripe.api_key = settings.stripe_secret_key

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=current_user.email,
        metadata={"user_id": str(current_user.id), "plan": plan},
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{settings.app_url}/dashboard?upgraded=1",
        cancel_url=f"{settings.app_url}/dashboard?cancelled=1",
    )

    return {"checkout_url": session.url}


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Stripe webhook events."""
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payments not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    stripe.api_key = settings.stripe_secret_key

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")
        plan = session.get("metadata", {}).get("plan")

        if user_id and plan:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                user.plan = PlanType(plan)
                await db.commit()

    elif event["type"] == "customer.subscription.deleted":
        # Downgrade to free on cancellation
        subscription = event["data"]["object"]
        customer_email = subscription.get("customer_email")
        if customer_email:
            result = await db.execute(select(User).where(User.email == customer_email))
            user = result.scalar_one_or_none()
            if user:
                user.plan = PlanType.FREE
                await db.commit()

    return {"status": "ok"}
