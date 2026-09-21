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


def require_org_access(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[User, BusinessMembership | None]:
    """Gate for organization/tenant-admin features (Google Workspace,
    Microsoft 365, Dropbox Business): a business admin of some org, or a
    global platform admin overriding into any org. A plain member or a
    user with no business at all (the individual/free tier) is refused —
    membership may be None here since a global admin doesn't need one."""
    if user.is_admin:
        return user, get_membership(db, user.id)
    membership = get_membership(db, user.id)
    if not membership or membership.role != "business_admin":
        raise HTTPException(status_code=403, detail="Organization admin access required")
    return user, membership
