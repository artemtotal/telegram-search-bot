# coding: utf-8
import os

from sqlalchemy import Column, INTEGER, TEXT, BOOLEAN, DATETIME, FLOAT, create_engine, UniqueConstraint
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.pool import StaticPool

# Local test and first-run environments may not have the mounted directory yet.
os.makedirs('./config', exist_ok=True)

engine = create_engine('sqlite:///./config/bot.db',
                       connect_args={'check_same_thread': False},
                       poolclass=StaticPool,
                       echo=False)
# WAL mode allows concurrent readers while a writer is active,
# which is essential because robot.py and json_receive.py both
# import this module at startup.
engine.execute('PRAGMA journal_mode=WAL')
engine.execute('PRAGMA busy_timeout=5000')
DBSession = sessionmaker(bind=engine)
Base = declarative_base()


class Message(Base):
    __tablename__ = 'message'

    _id = Column(INTEGER, primary_key=True)
    id = Column(INTEGER)
    link = Column(TEXT)
    type = Column(TEXT)  # 文本、图像、视频、音频、语音
    category = Column(TEXT)  # 分类
    text = Column(TEXT)
    # Python-lowercased shadow copy of `text`.
    # SQLite's built-in lower()/LIKE only folds ASCII, so ILIKE on Cyrillic
    # is effectively case-SENSITIVE. All keyword search must go through this column.
    text_lower = Column(TEXT)
    # Telegram message_id this message replies to (None if not a reply).
    # Needed to reconstruct question->answer pairs for search context.
    reply_to_msg_id = Column(INTEGER)
    video = Column(TEXT)
    photo = Column(TEXT)
    audio = Column(TEXT)
    voice = Column(TEXT)
    date = Column(DATETIME)
    from_id = Column(INTEGER)
    from_chat = Column(INTEGER)


class User(Base):
    __tablename__ = 'user'

    id = Column(INTEGER, primary_key=True)
    fullname = Column(TEXT)
    username = Column(TEXT)


class Chat(Base):
    __tablename__ = 'chat'

    id = Column(INTEGER, primary_key=True)
    title = Column(TEXT)
    enable = Column(BOOLEAN)


class AnonymousTopic(Base):
    __tablename__ = 'anonymous_topic'
    __table_args__ = (
        UniqueConstraint('chat_id', 'message_thread_id', name='uq_anonymous_topic_chat_thread'),
    )

    id = Column(INTEGER, primary_key=True)
    chat_id = Column(INTEGER, nullable=False)
    message_thread_id = Column(INTEGER, nullable=False, default=0)
    name = Column(TEXT, nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)
    updated_at = Column(DATETIME, nullable=False)


class AnonymousUser(Base):
    __tablename__ = 'anonymous_user'

    user_id = Column(INTEGER, primary_key=True)
    username = Column(TEXT)
    display_name = Column(TEXT)
    is_blocked = Column(BOOLEAN, nullable=False, default=False)
    captcha_failures = Column(INTEGER, nullable=False, default=0)
    captcha_locked_until = Column(DATETIME)
    captcha_passed_at = Column(DATETIME)
    last_submission_at = Column(DATETIME)
    created_at = Column(DATETIME, nullable=False)
    updated_at = Column(DATETIME, nullable=False)


class AnonymousPost(Base):
    __tablename__ = 'anonymous_post'

    id = Column(INTEGER, primary_key=True)
    submit_token = Column(TEXT, nullable=False, unique=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    topic_id = Column(INTEGER, nullable=False)
    chat_id = Column(INTEGER, nullable=False)
    message_thread_id = Column(INTEGER, nullable=False, default=0)
    target_message_id = Column(INTEGER)
    message_link = Column(TEXT)
    text = Column(TEXT, nullable=False)
    text_fingerprint = Column(TEXT, nullable=False, index=True)
    status = Column(TEXT, nullable=False, default='pending')
    can_delete_until = Column(DATETIME)
    created_at = Column(DATETIME, nullable=False)
    updated_at = Column(DATETIME, nullable=False)
    deleted_at = Column(DATETIME)


class EqueueSubscription(Base):
    __tablename__ = 'equeue_subscription'
    __table_args__ = (
        UniqueConstraint('user_id', 'service', name='uq_equeue_subscription_user_service'),
    )

    id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    username = Column(TEXT)
    display_name = Column(TEXT)
    service = Column(TEXT, nullable=False, default='dp_document_berlin')
    active = Column(BOOLEAN, nullable=False, default=True)
    last_status = Column(TEXT)
    last_checked_at = Column(DATETIME)
    last_notified_at = Column(DATETIME)
    created_at = Column(DATETIME, nullable=False)
    updated_at = Column(DATETIME, nullable=False)


class EqueueStatus(Base):
    """Останній браузерний результат сервісу, окремо від підписок.

    Перевірку робить Chrome один раз на всіх, тому її відмітка не належить
    жодній підписці. Поки вона писалась лише в активні рядки, вимкнена
    підписка морозила час у меню на моменті останнього вмикання.
    """

    __tablename__ = 'equeue_status'

    service = Column(TEXT, primary_key=True)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_reason = Column(TEXT)


class HousingAccessUser(Base):
    __tablename__ = 'housing_access_user'

    user_id = Column(INTEGER, primary_key=True)
    display_name = Column(TEXT, nullable=False, default='')
    active = Column(BOOLEAN, nullable=False, default=True)
    expires_at = Column(DATETIME)
    expiry_notice_sent = Column(BOOLEAN, nullable=False, default=False)
    created_at = Column(DATETIME, nullable=False)
    updated_at = Column(DATETIME, nullable=False)


class ProPotsdamListing(Base):
    __tablename__ = 'propotsdam_listing'

    listing_key = Column(TEXT, primary_key=True)
    title = Column(TEXT, nullable=False)
    address = Column(TEXT)
    district = Column(TEXT)
    rooms = Column(FLOAT)
    area_m2 = Column(FLOAT)
    total_rent_eur = Column(FLOAT)
    available_from = Column(TEXT)
    detail_url = Column(TEXT)
    image_url = Column(TEXT)
    raw_json = Column(TEXT)
    first_seen_at = Column(DATETIME, nullable=False)
    last_seen_at = Column(DATETIME, nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)


class ProPotsdamFilter(Base):
    __tablename__ = 'propotsdam_filter'

    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    title = Column(TEXT, nullable=False)
    districts = Column(TEXT)
    min_rooms = Column(FLOAT)
    max_rooms = Column(FLOAT)
    min_area_m2 = Column(FLOAT)
    max_area_m2 = Column(FLOAT)
    min_total_rent_eur = Column(FLOAT)
    max_total_rent_eur = Column(FLOAT)
    active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)


class ProPotsdamStatus(Base):
    __tablename__ = 'propotsdam_status'

    key = Column(TEXT, primary_key=True)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_error = Column(TEXT)
    listings_count = Column(INTEGER)


class ProPotsdamDelivery(Base):
    __tablename__ = 'propotsdam_delivery'
    __table_args__ = (
        UniqueConstraint('filter_id', 'listing_key', name='uq_propot_delivery_filter_listing'),
    )

    id = Column(INTEGER, primary_key=True)
    filter_id = Column(INTEGER, nullable=False, index=True)
    listing_key = Column(TEXT, nullable=False, index=True)
    sent_at = Column(DATETIME, nullable=False)


class SemmelhaackListing(Base):
    """SEMMELHAACK-квартири в Потсдамі: без районів — сайт цього не показує."""

    __tablename__ = 'semmelhaack_listing'

    listing_key = Column(TEXT, primary_key=True)
    title = Column(TEXT, nullable=False)
    address = Column(TEXT)
    rooms = Column(FLOAT)
    area_m2 = Column(FLOAT)
    price_eur = Column(FLOAT)
    detail_url = Column(TEXT)
    image_url = Column(TEXT)
    first_seen_at = Column(DATETIME, nullable=False)
    last_seen_at = Column(DATETIME, nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)


class SemmelhaackFilter(Base):
    __tablename__ = 'semmelhaack_filter'

    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    title = Column(TEXT, nullable=False)
    min_rooms = Column(FLOAT)
    max_rooms = Column(FLOAT)
    min_area_m2 = Column(FLOAT)
    max_area_m2 = Column(FLOAT)
    min_price_eur = Column(FLOAT)
    max_price_eur = Column(FLOAT)
    active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)


class SemmelhaackStatus(Base):
    __tablename__ = 'semmelhaack_status'

    key = Column(TEXT, primary_key=True)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_error = Column(TEXT)
    listings_count = Column(INTEGER)


class SemmelhaackDelivery(Base):
    __tablename__ = 'semmelhaack_delivery'
    __table_args__ = (
        UniqueConstraint('filter_id', 'listing_key', name='uq_semmelhaack_delivery_filter_listing'),
    )

    id = Column(INTEGER, primary_key=True)
    filter_id = Column(INTEGER, nullable=False, index=True)
    listing_key = Column(TEXT, nullable=False, index=True)
    sent_at = Column(DATETIME, nullable=False)


class SchobaListing(Base):
    """SCHOBA-квартири в Потсдамі: без надійного словника районів, як і SEMMELHAACK."""

    __tablename__ = 'schoba_listing'

    listing_key = Column(TEXT, primary_key=True)
    title = Column(TEXT, nullable=False)
    address = Column(TEXT)
    rooms = Column(FLOAT)
    area_m2 = Column(FLOAT)
    price_eur = Column(FLOAT)
    detail_url = Column(TEXT)
    first_seen_at = Column(DATETIME, nullable=False)
    last_seen_at = Column(DATETIME, nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)


class SchobaFilter(Base):
    __tablename__ = 'schoba_filter'

    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    title = Column(TEXT, nullable=False)
    min_rooms = Column(FLOAT)
    max_rooms = Column(FLOAT)
    min_area_m2 = Column(FLOAT)
    max_area_m2 = Column(FLOAT)
    min_price_eur = Column(FLOAT)
    max_price_eur = Column(FLOAT)
    active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)


class SchobaStatus(Base):
    __tablename__ = 'schoba_status'

    key = Column(TEXT, primary_key=True)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_error = Column(TEXT)
    listings_count = Column(INTEGER)


class SchobaDelivery(Base):
    __tablename__ = 'schoba_delivery'
    __table_args__ = (
        UniqueConstraint('filter_id', 'listing_key', name='uq_schoba_delivery_filter_listing'),
    )

    id = Column(INTEGER, primary_key=True)
    filter_id = Column(INTEGER, nullable=False, index=True)
    listing_key = Column(TEXT, nullable=False, index=True)
    sent_at = Column(DATETIME, nullable=False)


class RegiomaklerListing(Base):
    """Спільна стрічка ImmoTeam Potsdam + alpha Immobilien (плагін immomakler) —
    один і той самий Objekt-ID зустрічається на обох сайтах, тож зберігаємо
    по одному запису на listing_key, а не окремо для кожного сайту."""

    __tablename__ = 'regiomakler_listing'

    listing_key = Column(TEXT, primary_key=True)
    title = Column(TEXT, nullable=False)
    address = Column(TEXT)
    rooms = Column(FLOAT)
    area_m2 = Column(FLOAT)
    price_eur = Column(FLOAT)
    detail_url = Column(TEXT)
    source = Column(TEXT)
    first_seen_at = Column(DATETIME, nullable=False)
    last_seen_at = Column(DATETIME, nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)


class RegiomaklerFilter(Base):
    __tablename__ = 'regiomakler_filter'

    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    title = Column(TEXT, nullable=False)
    min_rooms = Column(FLOAT)
    max_rooms = Column(FLOAT)
    min_area_m2 = Column(FLOAT)
    max_area_m2 = Column(FLOAT)
    min_price_eur = Column(FLOAT)
    max_price_eur = Column(FLOAT)
    active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)


class RegiomaklerStatus(Base):
    __tablename__ = 'regiomakler_status'

    key = Column(TEXT, primary_key=True)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_error = Column(TEXT)
    listings_count = Column(INTEGER)


class RegiomaklerDelivery(Base):
    __tablename__ = 'regiomakler_delivery'
    __table_args__ = (
        UniqueConstraint('filter_id', 'listing_key', name='uq_regiomakler_delivery_filter_listing'),
    )

    id = Column(INTEGER, primary_key=True)
    filter_id = Column(INTEGER, nullable=False, index=True)
    listing_key = Column(TEXT, nullable=False, index=True)
    sent_at = Column(DATETIME, nullable=False)


class KleinanzeigenListing(Base):
    """Kleinanzeigen-оголошення в Потсдамі: без районів, без Kalt/Warm-мітки на ціні."""

    __tablename__ = 'kleinanzeigen_listing'

    listing_key = Column(TEXT, primary_key=True)
    title = Column(TEXT, nullable=False)
    address = Column(TEXT)
    rooms = Column(FLOAT)
    area_m2 = Column(FLOAT)
    price_eur = Column(FLOAT)
    detail_url = Column(TEXT)
    first_seen_at = Column(DATETIME, nullable=False)
    last_seen_at = Column(DATETIME, nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)


class KleinanzeigenFilter(Base):
    __tablename__ = 'kleinanzeigen_filter'

    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    title = Column(TEXT, nullable=False)
    min_rooms = Column(FLOAT)
    max_rooms = Column(FLOAT)
    min_area_m2 = Column(FLOAT)
    max_area_m2 = Column(FLOAT)
    min_price_eur = Column(FLOAT)
    max_price_eur = Column(FLOAT)
    active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)


class KleinanzeigenStatus(Base):
    __tablename__ = 'kleinanzeigen_status'

    key = Column(TEXT, primary_key=True)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_error = Column(TEXT)
    listings_count = Column(INTEGER)


class KleinanzeigenDelivery(Base):
    __tablename__ = 'kleinanzeigen_delivery'
    __table_args__ = (
        UniqueConstraint('filter_id', 'listing_key', name='uq_kleinanzeigen_delivery_filter_listing'),
    )

    id = Column(INTEGER, primary_key=True)
    filter_id = Column(INTEGER, nullable=False, index=True)
    listing_key = Column(TEXT, nullable=False, index=True)
    sent_at = Column(DATETIME, nullable=False)


class LocalsListing(Base):
    """locals®-квартири в Потсдамі: без районів, ціна — Kaltmiete."""

    __tablename__ = 'locals_listing'

    listing_key = Column(TEXT, primary_key=True)
    title = Column(TEXT, nullable=False)
    address = Column(TEXT)
    rooms = Column(FLOAT)
    area_m2 = Column(FLOAT)
    price_eur = Column(FLOAT)
    detail_url = Column(TEXT)
    first_seen_at = Column(DATETIME, nullable=False)
    last_seen_at = Column(DATETIME, nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)


class LocalsFilter(Base):
    __tablename__ = 'locals_filter'

    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    title = Column(TEXT, nullable=False)
    min_rooms = Column(FLOAT)
    max_rooms = Column(FLOAT)
    min_area_m2 = Column(FLOAT)
    max_area_m2 = Column(FLOAT)
    min_price_eur = Column(FLOAT)
    max_price_eur = Column(FLOAT)
    active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)


class LocalsStatus(Base):
    __tablename__ = 'locals_status'

    key = Column(TEXT, primary_key=True)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_error = Column(TEXT)
    listings_count = Column(INTEGER)


class LocalsDelivery(Base):
    __tablename__ = 'locals_delivery'
    __table_args__ = (
        UniqueConstraint('filter_id', 'listing_key', name='uq_locals_delivery_filter_listing'),
    )

    id = Column(INTEGER, primary_key=True)
    filter_id = Column(INTEGER, nullable=False, index=True)
    listing_key = Column(TEXT, nullable=False, index=True)
    sent_at = Column(DATETIME, nullable=False)


class CoopWatchdogStatus(Base):
    """Житлові кооперативи без жодного оголошення для парсингу (Gewoba, WBG 1903):
    замість повного скрейпера — лише стеження за текстом "немає вільного житла" на
    їхніх сторінках. `was_empty` фіксує попередній стан, щоб сповіщати адміна лише
    на переході "було порожньо → стало не порожньо", а не на кожному скані."""

    __tablename__ = 'coop_watchdog_status'

    key = Column(TEXT, primary_key=True)
    was_empty = Column(BOOLEAN)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_error = Column(TEXT)


class KarlmarxListing(Base):
    """Wohnungsgenossenschaft "Karl Marx": без районів, ціна — Warmmiete
    (тепла оренда), а не Kaltmiete, як у решти маклерів."""

    __tablename__ = 'karlmarx_listing'

    listing_key = Column(TEXT, primary_key=True)
    title = Column(TEXT, nullable=False)
    address = Column(TEXT)
    rooms = Column(FLOAT)
    area_m2 = Column(FLOAT)
    price_eur = Column(FLOAT)
    detail_url = Column(TEXT)
    first_seen_at = Column(DATETIME, nullable=False)
    last_seen_at = Column(DATETIME, nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)


class KarlmarxFilter(Base):
    __tablename__ = 'karlmarx_filter'

    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    title = Column(TEXT, nullable=False)
    min_rooms = Column(FLOAT)
    max_rooms = Column(FLOAT)
    min_area_m2 = Column(FLOAT)
    max_area_m2 = Column(FLOAT)
    min_price_eur = Column(FLOAT)
    max_price_eur = Column(FLOAT)
    active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)


class KarlmarxStatus(Base):
    __tablename__ = 'karlmarx_status'

    key = Column(TEXT, primary_key=True)
    last_checked_at = Column(DATETIME)
    last_status = Column(TEXT)
    last_error = Column(TEXT)
    listings_count = Column(INTEGER)


class KarlmarxDelivery(Base):
    __tablename__ = 'karlmarx_delivery'
    __table_args__ = (
        UniqueConstraint('filter_id', 'listing_key', name='uq_karlmarx_delivery_filter_listing'),
    )

    id = Column(INTEGER, primary_key=True)
    filter_id = Column(INTEGER, nullable=False, index=True)
    listing_key = Column(TEXT, nullable=False, index=True)
    sent_at = Column(DATETIME, nullable=False)


class HousingDelivery(Base):
    """Immowelt-объявления, уже отправленные конкретному человеку.

    Сеть до Telegram на этой машине рвётся так, что ответ теряется уже после
    доставки сообщения. Отправитель видит ошибку и присылает то же объявление
    снова — без этой записи человек получал бы одну квартиру по несколько раз.
    """

    __tablename__ = 'housing_delivery'
    __table_args__ = (
        UniqueConstraint('user_id', 'listing_id', name='uq_housing_delivery_user_listing'),
    )

    id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    listing_id = Column(TEXT, nullable=False, index=True)
    sent_at = Column(DATETIME, nullable=False)


Base.metadata.create_all(engine)


def _ensure_column(table_name: str, column_name: str, column_type: str) -> None:
    fairy = engine.raw_connection()
    try:
        dbapi_con = getattr(fairy, "driver_connection", None) or fairy.connection
        cur = dbapi_con.cursor()
        cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table_name})").fetchall()]
        if column_name not in cols:
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            dbapi_con.commit()
    finally:
        fairy.close()


_ensure_column('propotsdam_filter', 'min_total_rent_eur', 'FLOAT')
_ensure_column('housing_access_user', 'expires_at', 'DATETIME')
_ensure_column('housing_access_user', 'expiry_notice_sent', 'BOOLEAN')

# Keyword search (msg_ai._search_keyword_ids) runs up to ~30 per-keyword
# `text_lower LIKE '%word%'` queries per user message, each filtered by
# from_chat and sorted by date. Without this index SQLite full-scans the
# whole message table and materializes a temp B-tree for every one of
# those queries, which under concurrent scheduler load was hanging the
# whole process (query pipeline and cron jobs share one StaticPool
# connection) for minutes at a time.
engine.execute('CREATE INDEX IF NOT EXISTS idx_message_chat_date ON message(from_chat, date)')



def _migrate_text_lower(retries=5, delay=1):
    """Add and backfill the text_lower column on existing databases.

    Backfill must happen in Python because SQLite's lower() cannot fold
    Cyrillic. Runs on every startup; the WHERE clause makes repeat runs cheap.
    Retries with delay to handle concurrent startup (robot.py + json_receive.py).
    """
    import time
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            fairy = engine.raw_connection()
            try:
                dbapi_con = getattr(fairy, "driver_connection", None) or fairy.connection
                dbapi_con.execute("PRAGMA busy_timeout=5000")
                cur = dbapi_con.cursor()
                cols = [row[1] for row in cur.execute("PRAGMA table_info(message)").fetchall()]
                if "text_lower" not in cols:
                    cur.execute("ALTER TABLE message ADD COLUMN text_lower TEXT")
                if "reply_to_msg_id" not in cols:
                    cur.execute("ALTER TABLE message ADD COLUMN reply_to_msg_id INTEGER")
                dbapi_con.create_function(
                    "py_lower", 1,
                    lambda s: s.lower() if isinstance(s, str) else s,
                )
                cur.execute(
                    "UPDATE message SET text_lower = py_lower(text) "
                    "WHERE text_lower IS NULL AND text IS NOT NULL AND text != ''"
                )
                dbapi_con.commit()
            finally:
                fairy.close()
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(delay)
    raise RuntimeError(f"Migration failed after {retries} retries: {last_err}")


_migrate_text_lower()
