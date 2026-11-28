"""Storage behind «📁 Папка заявника» (25.11.2026).

The answers of the Mieterselbstauskunft questionnaire are one JSON per person.
A key present means "this question was asked" - even when the answer is empty
(skipped), so a skipped question is not asked again and the PDF gets a blank
line to fill in by hand. Nothing else is kept: no scans, no finished PDFs.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from database import DBSession, HousingLetterProfile, HousingMappeLog, HousingMappeProfile

logger = logging.getLogger(__name__)

MAX_VALUE_LEN = 400


def utc_now() -> datetime:
    return datetime.utcnow()


def get_answers(user_id: int) -> Optional[Dict[str, str]]:
    session = DBSession()
    try:
        row = session.query(HousingMappeProfile).get(int(user_id))
        if row is None:
            return None
        try:
            data = json.loads(row.data or "{}")
        except ValueError:
            return None
        return {str(k): str(v or "") for k, v in data.items()} if isinstance(data, dict) else None
    finally:
        session.close()


def save_answers(user_id: int, answers: Dict[str, str], sync: bool = True) -> None:
    clean = {str(k): str(v or "").strip()[:MAX_VALUE_LEN] for k, v in answers.items()}
    old = get_answers(user_id) if sync else None
    session = DBSession()
    try:
        row = session.query(HousingMappeProfile).get(int(user_id))
        if row is None:
            row = HousingMappeProfile(user_id=int(user_id))
            session.add(row)
        row.data = json.dumps(clean, ensure_ascii=False)
        row.updated_at = utc_now()
        session.commit()
    finally:
        session.close()
    if sync:        # спільні поля -> «Про мене» (одне правило для чату, сайту й мосту)
        try:
            from user_jobs import housing_profile_sync

            housing_profile_sync.after_mappe_save(int(user_id), old, clean)
        except Exception:
            logger.exception("Could not sync the mappe answers of %s to the letter profile", user_id)


def delete_all(user_id: int) -> None:
    """«🗑 Видалити мої дані»: анкета папки і анкета листів (вона спільна)."""
    session = DBSession()
    try:
        session.query(HousingMappeProfile).filter(HousingMappeProfile.user_id == int(user_id)).delete()
        session.query(HousingLetterProfile).filter(HousingLetterProfile.user_id == int(user_id)).delete()
        session.commit()
    finally:
        session.close()


def log_generation(user_id: int, source: Optional[str] = None, listing_key: Optional[str] = None) -> None:
    session = DBSession()
    try:
        session.add(HousingMappeLog(
            user_id=int(user_id), source=source, listing_key=listing_key, created_at=utc_now(),
        ))
        session.commit()
    finally:
        session.close()


def count_today(user_id: int) -> int:
    """Скільки папок зібрано за останню добу (ковзне вікно, не календарна доба)."""
    session = DBSession()
    try:
        return int(
            session.query(HousingMappeLog)
            .filter(HousingMappeLog.user_id == int(user_id), HousingMappeLog.created_at >= utc_now() - timedelta(days=1))
            .count()
        )
    finally:
        session.close()
