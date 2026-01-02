import html
import re
import secrets
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.ext import (
    CallbackContext,
    InlineQueryHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    Filters,
)
from sqlalchemy import func, desc

# Импортируем ваши модели. Убедитесь, что в database.py в классе Message есть поле reactions_total
from database import Chat, DBSession, Message, User

# --- Конфигурация ---
INLINE_PAGE_SIZE = 25
MAX_QUERY_LEN = 120
DEFAULT_MIN_REACTIONS = 1

# --- Вспомогательные функции ---

def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _split_page(raw: str) -> Tuple[str, int]:
    raw = _norm(raw)
    if not raw:
        return "", 1
    parts = raw.split(" ")
    page = 1
    if parts and parts[-1].isdigit():
        try:
            page = max(1, int(parts[-1]))
            parts = parts[:-1]
        except ValueError:
            pass
    q = " ".join(parts).strip()
    if len(q) > MAX_QUERY_LEN:
        q = q[:MAX_QUERY_LEN].strip()
    return q, page

def _parse_uname(raw: str) -> Tuple[Optional[str], str]:
    """Выделяет @username из начала запроса."""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    parts = raw.split(" ", 1)
    first = parts[0]
    if first.startswith("@") and len(first) > 1:
        rest = parts[1] if len(parts) > 1 else ""
        return first, rest.strip()
    return None, raw

def _parse_tokens(raw: str) -> Tuple[List[str], int]:
    """
    Парсит ключевые слова и фильтр реакций (например, 'test r:5' или 'likes:10').
    Возвращает (список_слов, минимальное_кол-во_реакций).
    """
    toks = [t for t in (raw or "").split() if t]
    kw: List[str] = []
    min_r = DEFAULT_MIN_REACTIONS
    
    for t in toks:
        # Поддержка форматов: r:5, likes:5, reactions>=5
        m = re.match(r"^(?:r|likes|reactions)(?::|>=)(\d+)$", t.lower())
        if m:
            try:
                min_r = max(0, int(m.group(1)))
            except Exception:
                pass
        else:
            kw.append(t)
    return kw, min_r

def _format_date(d) -> str:
    if d is None:
        return ""
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, str):
        return d[:10]
    return ""

def get_filter_chats(session, user_id: Optional[int] = None) -> List[int]:
    """
    Получает список ID чатов, в которых включен поиск.
    Можно добавить логику проверки прав доступа user_id, если нужно.
    """
    rows = session.query(Chat).filter(Chat.enable == 1).all()
    return [c.id for c in rows]

def _lookup_user_ids(session, uname: str) -> List[int]:
    if not uname:
        return []
    # Убираем @ если есть
    clean_name = uname[1:] if uname.startswith("@") else uname
    like = f"%{clean_name.lower()}%"
    rows = (
        session.query(User)
        .filter(User.username.isnot(None))
        .filter(User.username.ilike(like))
        .limit(50)
        .all()
    )
    return [u.id for u in rows]

# --- Основная логика поиска ---

def search_messages_orm(
    session,
    keywords: List[str],
    chat_ids: List[int],
    user_ids: List[int],
    min_reactions: int,
    page: int,
    page_size: int
) -> List[Dict]:
    """
    Поиск сообщений с использованием SQLAlchemy ORM.
    Сортировка: Сначала по количеству реакций (убыв.), потом по дате (убыв.).
    """
    if not chat_ids:
        return []

    # Базовый запрос
    q = session.query(Message).filter(Message.from_chat.in_(chat_ids))
    
    # Фильтр по типу (обычно ищем только текст)
    # Если в вашей базе старые записи имеют тип 'message', а новые 'text', можно использовать .in_
    # Но обычно достаточно проверить наличие текста
    q = q.filter(Message.text.isnot(None)).filter(Message.text != "")

    # Фильтр по пользователю
    if user_ids:
        q = q.filter(Message.from_id.in_(user_ids))

    # Фильтр по ключевым словам (AND)
    for kw in keywords:
        # ilike для регистронезависимого поиска
        q = q.filter(Message.text.ilike(f"%{kw}%"))

    # Фильтр и Сортировка по реакциям
    # Проверяем, есть ли атрибут reactions_total в модели Message
    has_reactions_col = hasattr(Message, 'reactions_total')
    
    if has_reactions_col:
        # Если фильтр задан явно (r:5), применяем его
        if min_reactions > 0:
            q = q.filter(Message.reactions_total >= min_reactions)
        
        # СОРТИРОВКА: Сначала лайки (null считаем как 0), потом дата
        # coalesce превращает NULL в 0 для корректной сортировки
        q = q.order_by(
            desc(func.coalesce(Message.reactions_total, 0)),
            Message.date.desc()
        )
    else:
        # Если колонки нет, просто сортируем по дате
        q = q.order_by(Message.date.desc())

    # Пагинация
    offset = (page - 1) * page_size
    msgs = q.limit(page_size).offset(offset).all()

    results = []
    for m in msgs:
        # Безопасное получение полей
        r_total = getattr(m, 'reactions_total', 0) or 0
        
        results.append({
            "id": m.id,
            "text": m.text,
            "link": m.link,
            "date": m.date,
            "user": m.from_id,
            "chat": m.from_chat,
            "reactions_total": r_total
        })
    
    return results

# --- Обработчики Telegram ---

def inline_caps(update: Update, context: CallbackContext) -> None:
    iq = update.inline_query
    if not iq:
        return

    raw = (iq.query or "").strip()
    
    # Если запрос пустой, можно ничего не возвращать или вернуть кэш/хелп
    # Но для стабильности вернем пустой список
    if not raw:
        return

    # Разбор запроса
    # 1. Номер страницы
    query_text, page = _split_page(raw)
    
    # 2. Юзернейм (@user query)
    uname, query_text = _parse_uname(query_text)
    
    # 3. Ключевые слова и фильтр реакций (query r:5)
    keywords, min_reactions = _parse_tokens(query_text)

    # Если ничего не осталось для поиска, выходим (если не ищем чисто по юзеру)
    if not keywords and not uname:
        # Можно показать заглушку или последние сообщения, если хотите
        pass

    session = DBSession()
    try:
        # Получаем чаты для поиска
        chat_ids = get_filter_chats(session)
        
        # Если указан юзер, ищем его ID
        user_ids = []
        if uname:
            user_ids = _lookup_user_ids(session, uname)
            if not user_ids:
                # Юзер указан, но не найден в БД -> пустой результат
                context.bot.answer_inline_query(iq.id, [], cache_time=5)
                return

        # Выполняем поиск
        msgs = search_messages_orm(
            session,
            keywords=keywords,
            chat_ids=chat_ids,
            user_ids=user_ids,
            min_reactions=min_reactions,
            page=page,
            page_size=INLINE_PAGE_SIZE
        )

        results: List[InlineQueryResultArticle] = []
        for m in msgs:
            text_msg = _norm(m.get("text") or "")
            link = str(m.get("link") or "")
            likes = int(m.get("reactions_total") or 0)
            date_str = _format_date(m.get("date"))
            
            # Формируем заголовок
            title = text_msg[:60] if text_msg else "Message"
            
            # Формируем описание (Likes | Date | User | Chat)
            # Значок ?? добавлен, чтобы было видно сортировку
            desc_parts = []
            if likes > 0:
                desc_parts.append(f"❤️{likes}")
            desc_parts.append(date_str)
            desc_parts.append(f"u:{m.get('user')}")
            desc_parts.append(f"c:{m.get('chat')}")
            
            description = " | ".join(desc_parts)

            # Формируем текст, который отправится при клике
            # Отправляем текст сообщения + ссылку, или просто ссылку
            content_text = html.escape(text_msg[:1000])
            if link:
                content_text += f"\n\n<a href='{link}'>🔗 Ссылка на сообщение</a>"
            
            rid = f"{m.get('id')}-{secrets.token_hex(2)}"
            
            results.append(
                InlineQueryResultArticle(
                    id=rid,
                    title=title,
                    description=description,
                    input_message_content=InputTextMessageContent(
                        message_text=content_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    ),
                )
            )

        context.bot.answer_inline_query(iq.id, results, cache_time=2, is_personal=True)

    except Exception as e:
        # Логирование ошибки (опционально print(e))
        # Важно ответить API, чтобы клиент не висел
        try:
            context.bot.answer_inline_query(iq.id, [], cache_time=10)
        except Exception:
            pass
    finally:
        session.close()

# --- Обычные команды (заглушки или перенаправления) ---

def search_cmd(update: Update, context: CallbackContext) -> None:
    # Просто подсказка использовать инлайн
    try:
        update.message.reply_text("🔎 Используйте инлайн-поиск: введите @botname <запрос> в любом чате.")
    except Exception:
        pass

def search_text(update: Update, context: CallbackContext) -> None:
    # Игнорируем обычный текст, чтобы не спамить
    pass

# --- Сборка обработчиков ---

inline_handler = InlineQueryHandler(inline_caps)
search_cmd_handler = CommandHandler("search", search_cmd)
search_text_handler = MessageHandler(Filters.text & (~Filters.command), search_text)

# Основная переменная для импорта в robot.py
handler = inline_handler