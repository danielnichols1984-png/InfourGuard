from sqlalchemy import Column, Integer, String, Text

from shared.content_core.db import Base


class MarketingBlock(Base):
    """One editable piece of copy on a public page (e.g. the homepage
    hero headline). Admin edits these via /admin/content; the page that
    renders them reads the current value server-side on every request —
    no deploy needed to change copy."""

    __tablename__ = "marketing_blocks"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False, default="")
