from fastapi import APIRouter, Depends, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import User
from app.schemas.auth import UserCreate, UserResponse, Token, TokenRefresh
from app.services.auth import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
)
from app.services.email import generate_verification_token, send_verification_email

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("5/minute")
async def register(request: Request, body: UserCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    token, expires_at = generate_verification_token()
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        email_verified=False,
        verification_token=token,
        verification_token_expires_at=expires_at,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await send_verification_email(user.email, token)

    return {"status": "verification_email_sent", "email": user.email}


@router.post("/login", response_model=Token)
@limiter.limit("10/minute")
async def login(request: Request, body: UserCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_not_verified",
        )

    return Token(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.post("/refresh", response_model=Token)
@limiter.limit("10/minute")
async def refresh(request: Request, body: TokenRefresh, db: AsyncSession = Depends(get_db)):
    user_id = decode_token(body.refresh_token, expected_type="refresh")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return Token(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return UserResponse(
        id=str(current_user.id),
        email=current_user.email,
        plan=current_user.plan.value,
        created_at=current_user.created_at.isoformat(),
    )
