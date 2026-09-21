from sqlalchemy.orm import Session

from shared.content_core.models import MarketingBlock

# Real placeholder copy (not lorem ipsum) so the homepage looks finished on
# first deploy — edit via /admin/content, not by changing this dict, since
# these are only ever used to backfill rows that don't exist yet.
DEFAULT_HOMEPAGE_CONTENT: dict[str, str] = {
    "brand_name": "Cloudscope",
    "hero_headline": "One dashboard for every cloud drive you use.",
    "hero_subheadline": (
        "Connect Google Drive, OneDrive, and Dropbox to see storage, sharing risk, "
        "and security posture in one place — for yourself or your whole organization."
    ),
    "hero_cta_primary": "Get started free",
    "hero_cta_secondary": "Log in",
    "connects_label": "Works with the storage you already use",
    "feature_1_title": "Storage analytics",
    "feature_1_body": "See what's taking up space, how fast it's growing, and where to clean up — across every connected account.",
    "feature_2_title": "Sharing & security insights",
    "feature_2_body": "Find files shared with “anyone with the link,” catch oversharing, and check security posture before it's a problem.",
    "feature_3_title": "Organization-wide reporting",
    "feature_3_body": "Admins get tenant-wide visibility into Google Workspace, Microsoft 365, and Dropbox Business — users, licenses, MFA coverage, and more.",
    "feature_4_title": "Cross-cloud migration",
    "feature_4_body": "Move files between Google Drive, OneDrive, and Dropbox directly, with hash-verified integrity checks on every transfer.",
    "pricing_teaser_headline": "Simple plans that grow with you",
    "pricing_teaser_body": "Start free. Upgrade when you need more storage connections or team features.",
    "pricing_teaser_cta": "See plans",
    "footer_tagline": "Cloudscope — clarity across every cloud drive.",
}


def get_all(db: Session) -> dict[str, str]:
    rows = db.query(MarketingBlock).all()
    return {row.key: row.value for row in rows}


def upsert_many(db: Session, updates: dict[str, str]) -> dict[str, str]:
    existing = {row.key: row for row in db.query(MarketingBlock).filter(MarketingBlock.key.in_(updates.keys())).all()}
    for key, value in updates.items():
        if key in existing:
            existing[key].value = value
        else:
            db.add(MarketingBlock(key=key, value=value))
    db.commit()
    return get_all(db)


def seed_defaults(db: Session) -> None:
    """Idempotent — only inserts keys that don't exist yet, so re-running
    on every startup never clobbers an admin's edits."""
    existing_keys = {row.key for row in db.query(MarketingBlock.key).all()}
    missing = {k: v for k, v in DEFAULT_HOMEPAGE_CONTENT.items() if k not in existing_keys}
    if not missing:
        return
    for key, value in missing.items():
        db.add(MarketingBlock(key=key, value=value))
    db.commit()
