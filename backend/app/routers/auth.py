from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..config import LOCATION_BY_NAME, settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..repositories import UserRepository
from ..schemas import AuthOut, LoginIn, RegisterIn, UserOut, UserUpdateIn
from ..security import create_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def to_user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        avatar=user.avatar or "",
        location_name=user.location_name,
        lat=user.lat,
        lng=user.lng,
        pickup_range_enabled=user.pickup_range_enabled,
        auto_notify=bool(user.auto_notify),
        pickup_range_km=user.pickup_range_km,
        onboarded=bool(user.location_name),
    )


@router.post("/register", response_model=AuthOut)
def register(payload: RegisterIn, db: Session = Depends(get_db)) -> AuthOut:
    repo = UserRepository(db)
    if repo.get_by_email(payload.email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这个邮箱已经注册过了")
    user = repo.create(
        email=payload.email,
        password_hash=hash_password(payload.password),
        avatar=f"avatar-{abs(hash(payload.email)) % 8}",
        pickup_range_enabled=False,
        pickup_range_km=settings.default_pickup_range_km,
    )
    db.commit()
    return AuthOut(token=create_token(user.id), user=to_user_out(user))


@router.post("/login", response_model=AuthOut)
def login(payload: LoginIn, db: Session = Depends(get_db)) -> AuthOut:
    user = UserRepository(db).get_by_email(payload.email)
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码不正确")
    return AuthOut(token=create_token(user.id), user=to_user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return to_user_out(user)


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: UserUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    """Shared by first-login onboarding and the profile settings page:
    change location / pickup range."""
    if payload.location_name is not None:
        location = LOCATION_BY_NAME.get(payload.location_name)
        if not location:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "请从预设地点中选择一个")
        user.location_name = location["name"]
        user.lat = location["lat"]
        user.lng = location["lng"]
    if payload.pickup_range_enabled is not None:
        user.pickup_range_enabled = payload.pickup_range_enabled
    if payload.auto_notify is not None:
        user.auto_notify = bool(payload.auto_notify)
    if payload.pickup_range_km is not None:
        if payload.pickup_range_km <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "自提范围必须大于 0")
        user.pickup_range_km = payload.pickup_range_km
    UserRepository(db).save(user)
    db.commit()
    return to_user_out(user)
