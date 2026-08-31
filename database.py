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
    # Коли адміну справді пішло попередження про несправну перевірку - окремо
    # від last_checked_at, який оновлюється щоразу незалежно від статусу і
    # тому не годиться як мітка кулдауна (див. _notify_admin_error).
    last_admin_alert_at = Column(DATETIME)
    # Кулдаун окремого попередження "давно не бачили вільних термінів"
    # (див. _notify_admin_stale). Тримається окремо від last_admin_alert_at,
    # бо це різні за суттю тривоги: та про зламану перевірку, ця про підозріло
    # довгу тишу за цілком справної перевірки.
    last_stale_alert_at = Column(DATETIME)


class EqueueAvailableSighting(Base):
    """Історія моментів, коли перевірка справді побачила вільні терміни.

    `EqueueStatus` зберігає лише останній результат, тож по ньому не видно
    ні коли терміни траплялися востаннє, ні як давно їх не було. Кожна
    знахідка лягає сюди окремим рядком - меню показує кілька останніх, а
    `check_equeue_stale` по них розуміє, чи не занадто довго тиша.
    """

    __tablename__ = 'equeue_available_sighting'

    id = Column(INTEGER, primary_key=True)
    service = Column(TEXT, nullable=False, index=True)
    found_at = Column(DATETIME, nullable=False, index=True)
    reason = Column(TEXT)


class HousingAccessUser(Base):
    __tablename__ = 'housing_access_user'

    user_id = Column(INTEGER, primary_key=True)
    display_name = Column(TEXT, nullable=False, default='')
    active = Column(BOOLEAN, nullable=False, default=True)
    expires_at = Column(DATETIME)
    expiry_notice_sent = Column(BOOLEAN, nullable=False, default=False)
    # True while this grant is the free self-service trial rather than an
    # admin-approved paid grant. Cleared (False) the moment an admin grants
    # real months, so the row falls back into the ordinary paid-expiry path.
    is_trial = Column(BOOLEAN, nullable=False, default=False)
    # Set when a trial's 7 days run out: monitoring stops immediately, but
    # the filters are kept until this timestamp so a same-day upgrade to
    # full access doesn't force the person to rebuild every filter.
    trial_grace_ends_at = Column(DATETIME)
    created_at = Column(DATETIME, nullable=False)
    updated_at = Column(DATETIME, nullable=False)


class HousingTrialUsed(Base):
    """Permanent record of Telegram IDs that already burned their one free
    7-day trial. Kept separate from HousingAccessUser because that row gets
    deleted once access closes (see `_close_access`) - this one must survive
    that deletion so nobody can re-trigger the trial by asking again."""

    __tablename__ = 'housing_trial_used'

    user_id = Column(INTEGER, primary_key=True)
    used_at = Column(DATETIME, nullable=False)


class ImmoweltListing(Base):
    """Immowelt-оголошення, зафіксовані в момент доставки людині — на відміну
    від решти джерел, Immowelt не має власного повного сканування в цьому
    боті (окремий сервіс лише пересилає збіги з чиїмось фільтром), тож це не
    "всі оголошення на сайті", а "всі, що колись комусь підійшли". Досить для
    статистики "скільки знайдено" за період."""

    __tablename__ = 'immowelt_listing'

    listing_key = Column(TEXT, primary_key=True)
    title = Column(TEXT)
    address = Column(TEXT)
    rooms = Column(FLOAT)
    area_m2 = Column(FLOAT)
    price_eur = Column(FLOAT)
    detail_url = Column(TEXT)
    first_seen_at = Column(DATETIME, nullable=False)


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
    cover_image_url = Column(TEXT)
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


class CoopWatchdogFilter(Base):
    """A person's "notify me about this cooperative" subscription.

    Not a real filter — the watchdog only knows empty vs. not-empty for the
    whole page (see CoopWatchdogStatus), no per-listing rooms/price/area to
    match against yet. This just tracks who wants the ping when a
    cooperative's page stops saying "no vacancies"; real per-listing
    filtering waits until there's an actual scraper for one of these."""

    __tablename__ = 'coop_watchdog_filter'
    __table_args__ = (
        UniqueConstraint('user_id', 'coop_key', name='uq_coop_watchdog_filter_user_coop'),
    )

    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    coop_key = Column(TEXT, nullable=False)
    title = Column(TEXT, nullable=False)
    active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False)


class UserSettings(Base):
    """Per-user bot preferences. Started as just the chosen UI language
    (uk/ru/de) — a dedicated table rather than reusing `User` (shared with
    message-archival/search/AI, and only lazily created on observed group
    messages) or `HousingAccessUser` (only exists for admin-granted housing
    users), since the language switcher must work for any bot user. Also
    doubles as the "known private-chat users" registry the admin broadcast
    (housing:broadcast) sends to: a row appears here for anyone who ever
    triggers a language lookup, which happens on basically every private
    screen — `news_subscribed` defaults to True so opt-in is automatic and
    users just opt back out via the notification-settings screen."""

    __tablename__ = 'user_settings'

    user_id = Column(INTEGER, primary_key=True)
    language = Column(TEXT, nullable=False, default='uk')
    news_subscribed = Column(BOOLEAN, nullable=False, default=True)
    updated_at = Column(DATETIME, nullable=False)


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
_ensure_column('kleinanzeigen_listing', 'cover_image_url', 'TEXT')
_ensure_column('housing_access_user', 'expires_at', 'DATETIME')
_ensure_column('housing_access_user', 'expiry_notice_sent', 'BOOLEAN')
_ensure_column('housing_access_user', 'is_trial', 'BOOLEAN NOT NULL DEFAULT 0')
_ensure_column('housing_access_user', 'trial_grace_ends_at', 'DATETIME')
_ensure_column('equeue_status', 'last_admin_alert_at', 'DATETIME')
_ensure_column('equeue_status', 'last_stale_alert_at', 'DATETIME')
_ensure_column('user_settings', 'news_subscribed', 'BOOLEAN NOT NULL DEFAULT 1')

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
