"""Пошуки житла: один рядок на фільтр, який людина створила майстром.

Див. докстрінг HousingSearch у database.py. Записи площадок живуть у своїх
сховищах як і раніше — тут лише те, що їх об'єднує в один фільтр.
"""

import json
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from database import DBSession, HousingSearch, engine

# Таблиці записів локальних площадок: звідти беремо час створення, щоб
# розкласти старі записи (зроблені до появи пошуків) по проходах майстра.
LOCAL_FILTER_TABLES = {
    "propotsdam": "propotsdam_filter",
    "semmelhaack": "semmelhaack_filter",
    "schoba": "schoba_filter",
    "regiomakler": "regiomakler_filter",
    "kleinanzeigen": "kleinanzeigen_filter",
    "locals": "locals_filter",
    "karlmarx": "karlmarx_filter",
    "vonovia": "vonovia_filter",
    "immoscout": "immoscout_filter",
}


def utc_now() -> datetime:
    return datetime.utcnow()


def _to_dict(row: HousingSearch) -> Dict:
    return {
        "search_id": int(row.search_id),
        "user_id": int(row.user_id),
        "criteria": json.loads(row.criteria or "{}"),
        "members": [(str(source), int(filter_id)) for source, filter_id in json.loads(row.members or "[]")],
        "coops": list(json.loads(row.coops or "[]")),
        "created_at": row.created_at,
    }


def create(
    user_id: int,
    criteria: Dict,
    members: Iterable[Tuple[str, int]],
    coops: Iterable[str] = (),
    created_at: Optional[datetime] = None,
) -> int:
    session = DBSession()
    try:
        row = HousingSearch(
            user_id=int(user_id),
            criteria=json.dumps(criteria, ensure_ascii=False),
            members=json.dumps([[str(source), int(filter_id)] for source, filter_id in members]),
            coops=json.dumps(list(coops)),
            created_at=created_at or utc_now(),
        )
        session.add(row)
        session.commit()
        return int(row.search_id)
    finally:
        session.close()


def list_for_user(user_id: int) -> List[Dict]:
    session = DBSession()
    try:
        rows = (
            session.query(HousingSearch)
            .filter(HousingSearch.user_id == int(user_id))
            .order_by(HousingSearch.created_at.asc(), HousingSearch.search_id.asc())
            .all()
        )
        return [_to_dict(row) for row in rows]
    finally:
        session.close()


def list_all() -> List[Dict]:
    session = DBSession()
    try:
        rows = session.query(HousingSearch).order_by(HousingSearch.search_id.asc()).all()
        return [_to_dict(row) for row in rows]
    finally:
        session.close()


def get(search_id: int, user_id: int) -> Optional[Dict]:
    session = DBSession()
    try:
        row = (
            session.query(HousingSearch)
            .filter(HousingSearch.search_id == int(search_id), HousingSearch.user_id == int(user_id))
            .first()
        )
        return _to_dict(row) if row else None
    finally:
        session.close()


def add_member(search_id: int, source: str, filter_id: int) -> None:
    session = DBSession()
    try:
        row = session.query(HousingSearch).filter(HousingSearch.search_id == int(search_id)).first()
        if row is None:
            return
        members = json.loads(row.members or "[]")
        if [source, int(filter_id)] not in members:
            members.append([source, int(filter_id)])
            row.members = json.dumps(members)
            session.commit()
    finally:
        session.close()


def delete(search_id: int, user_id: int) -> bool:
    session = DBSession()
    try:
        deleted = (
            session.query(HousingSearch)
            .filter(HousingSearch.search_id == int(search_id), HousingSearch.user_id == int(user_id))
            .delete()
        )
        session.commit()
        return bool(deleted)
    finally:
        session.close()


def local_created_at(user_id: int) -> Dict[Tuple[str, int], datetime]:
    """(джерело, filter_id) → час створення запису локальної площадки."""
    result: Dict[Tuple[str, int], datetime] = {}
    for source, table in LOCAL_FILTER_TABLES.items():
        rows = engine.execute(
            f"SELECT filter_id, created_at FROM {table} WHERE user_id = ?", (int(user_id),)
        )
        for filter_id, created_at in rows:
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at)
                except ValueError:
                    created_at = None
            if created_at is not None:
                result[(source, int(filter_id))] = created_at
    return result
