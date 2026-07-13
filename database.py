# coding: utf-8
from sqlalchemy import Column, INTEGER, TEXT, BOOLEAN, DATETIME, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.pool import StaticPool

engine = create_engine('sqlite:///./config/bot.db',
                       connect_args={'check_same_thread': False},
                       poolclass=StaticPool,
                       echo=False)
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


Base.metadata.create_all(engine)


def _migrate_text_lower():
    """Add and backfill the text_lower column on existing databases.

    Backfill must happen in Python because SQLite's lower() cannot fold
    Cyrillic. Runs on every startup; the WHERE clause makes repeat runs cheap.
    """
    fairy = engine.raw_connection()
    try:
        dbapi_con = getattr(fairy, "driver_connection", None) or fairy.connection
        cur = dbapi_con.cursor()
        cols = [row[1] for row in cur.execute("PRAGMA table_info(message)").fetchall()]
        if "text_lower" not in cols:
            cur.execute("ALTER TABLE message ADD COLUMN text_lower TEXT")
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


_migrate_text_lower()