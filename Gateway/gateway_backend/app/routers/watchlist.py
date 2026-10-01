from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import resolve_owner_uid
from app.schemas import WatchlistItemCreate, WatchlistItemResponse
from db.engine import get_db
from db.models import WatchlistEntry

router = APIRouter()


@router.get("/api/watchlist", response_model=list[WatchlistItemResponse])
def get_watchlist(db: Session = Depends(get_db), owner_uid: str = Depends(resolve_owner_uid)):
    return (
        db.query(WatchlistEntry)
        .filter_by(owner_uid=owner_uid)
        .order_by(WatchlistEntry.sort_order, WatchlistEntry.id)
        .all()
    )


@router.post("/api/watchlist", response_model=WatchlistItemResponse, status_code=201)
def add_watchlist_item(req: WatchlistItemCreate, db: Session = Depends(get_db), owner_uid: str = Depends(resolve_owner_uid)):
    existing = db.query(WatchlistEntry).filter_by(owner_uid=owner_uid, exch=req.exch, token=req.token).first()
    if existing:
        return existing
    max_order = db.query(WatchlistEntry).filter_by(owner_uid=owner_uid).count()
    entry = WatchlistEntry(
        owner_uid=owner_uid,
        tsym=req.tsym,
        exch=req.exch,
        token=req.token,
        lotsize=req.lotsize,
        instrumenttype=req.instrumenttype,
        expd=req.expd,
        sym=req.sym,
        sort_order=max_order,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/api/watchlist/{exch}/{token}", status_code=204)
def remove_watchlist_item(exch: str, token: str, db: Session = Depends(get_db), owner_uid: str = Depends(resolve_owner_uid)):
    entry = db.query(WatchlistEntry).filter_by(owner_uid=owner_uid, exch=exch, token=token).first()
    if entry:
        db.delete(entry)
        db.commit()
