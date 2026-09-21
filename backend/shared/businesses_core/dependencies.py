from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from shared.auth_core.dependencies import get_current_user
from shared.auth_core.models import User
from shared.businesses_core.db import get_db
from shared.businesses_core.models import BusinessMembership
from shared.businesses_core.service import get_membership


def require_business_admin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[User, BusinessMembership]:
    membership = get_membership(db, user.id)
    if not membership or membership.role != "business_admin":
        raise HTTPException(status_code=403, detail="Business admin access required")
    return user, membership
