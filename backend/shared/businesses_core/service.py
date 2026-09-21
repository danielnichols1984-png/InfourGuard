from sqlalchemy.orm import Session

from shared.businesses_core.models import Business, BusinessMembership


def get_membership(db: Session, user_id: int) -> BusinessMembership | None:
    return db.query(BusinessMembership).filter(BusinessMembership.user_id == user_id).first()


def is_business_admin(db: Session, user_id: int) -> BusinessMembership | None:
    membership = get_membership(db, user_id)
    if membership and membership.role == "business_admin":
        return membership
    return None


def list_members(db: Session, business_id: int) -> list[BusinessMembership]:
    return (
        db.query(BusinessMembership)
        .filter(BusinessMembership.business_id == business_id)
        .order_by(BusinessMembership.id)
        .all()
    )


def create_business(db: Session, name: str, admin_user_id: int) -> Business:
    business = Business(name=name)
    db.add(business)
    db.commit()
    db.refresh(business)
    membership = BusinessMembership(
        business_id=business.id, user_id=admin_user_id, role="business_admin"
    )
    db.add(membership)
    db.commit()
    return business


def add_member(db: Session, business_id: int, user_id: int, role: str = "member") -> BusinessMembership:
    membership = BusinessMembership(business_id=business_id, user_id=user_id, role=role)
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return membership


def remove_member(db: Session, membership: BusinessMembership) -> None:
    db.delete(membership)
    db.commit()


def set_member_role(db: Session, membership: BusinessMembership, new_role: str) -> str | None:
    """Returns None on success, or an error message describing why not.
    Refuses to demote the last remaining business_admin in a business —
    every business must keep at least one admin."""
    if membership.role == "business_admin" and new_role == "member":
        other_admins = (
            db.query(BusinessMembership)
            .filter(
                BusinessMembership.business_id == membership.business_id,
                BusinessMembership.role == "business_admin",
                BusinessMembership.id != membership.id,
            )
            .count()
        )
        if other_admins == 0:
            return "This is the only admin left in this business — promote someone else first"

    membership.role = new_role
    db.commit()
    return None


def user_has_org_access(db: Session, user) -> bool:
    """Plain (non-dependency) equivalent of require_org_access, for the
    one call site that isn't cookie-based: the tenant OAuth callback,
    which resolves identity from the OAuth `state` param instead."""
    if user.is_admin:
        return True
    membership = get_membership(db, user.id)
    return bool(membership and membership.role == "business_admin")


def list_businesses(db: Session) -> list[Business]:
    return db.query(Business).order_by(Business.id).all()


def delete_business(db: Session, business_id: int) -> str | None:
    """Returns None on success, or an error message describing why not."""
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        return "Business not found"

    if list_members(db, business_id):
        return "Business still has members; remove them first"

    db.delete(business)
    db.commit()
    return None
