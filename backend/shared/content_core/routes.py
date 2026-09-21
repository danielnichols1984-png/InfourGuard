from fastapi import APIRouter, Depends
from pydantic import RootModel
from sqlalchemy.orm import Session

from shared.auth_core.dependencies import require_site_admin
from shared.auth_core.models import User as AuthUser
from shared.content_core.db import get_db
from shared.content_core.service import get_all, upsert_many

router = APIRouter()


class ContentUpdateRequest(RootModel[dict[str, str]]):
    pass


@router.get("/homepage")
def get_homepage_content(db: Session = Depends(get_db)):
    return get_all(db)


@router.put("/admin/homepage")
def update_homepage_content(
    body: ContentUpdateRequest, admin: AuthUser = Depends(require_site_admin), db: Session = Depends(get_db)
):
    return upsert_many(db, body.root)
