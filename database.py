# coding: utf-8
import os

from sqlalchemy import Column, INTEGER, TEXT, BOOLEAN, DATETIME, create_engine, UniqueConstraint
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


Base.metadata.create_all(engine)


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
