from telegram.ext import CommandHandler
import telegram.error

from database import Chat, DBSession
from utils import check_control_permission, is_userbot_mode, read_userbot_admin_id, get_text_func

_ = get_text_func()


def insert_chat_or_enable(chat_id, title):
    session = DBSession()
    target_chat = session.query(Chat).get(chat_id)
    if not target_chat:
        new_chat = Chat(id=chat_id, title=title, enable=True)
        session.add(new_chat)
        session.commit()
        msg_text = _('bot started!')
    else:
        if target_chat.enable:
            msg_text = _('already started, use /stop to stop bot in current group!')
        else:
            target_chat.enable = True
            session.commit()
            msg_text = _('bot restored!')
    session.close()
    return msg_text


def start(update, context):
    from_user_id = update.message.from_user.id

    if update.effective_chat.type == 'private':
        # «✍️ Лист орендодавцю» під квартирою - посилання t.me/<bot>?start=lt<id>.
        if context.args and str(context.args[0]).startswith('lt'):
            from user_handlers import housing_letter
            housing_letter.handle_start(update, context, str(context.args[0]))
            return
        # «✅ Подався» під квартирою - t.me/<bot>?start=ap<id> (трекер заявок).
        if context.args and str(context.args[0]).startswith('ap') and str(context.args[0])[2:].isdigit():
            from user_handlers import housing_tracker
            housing_tracker.handle_start(update, context, str(context.args[0]))
            return
        # «⚡ Підписка від N €» під квартирою безкоштовного: /start sub - одразу вибір пакета оплати.
        if context.args and str(context.args[0]) == 'sub':
            try:
                from user_jobs import housing_free_hints
                if housing_free_hints.start_param_payment(update, context):
                    return
            except Exception:
                import logging
                logging.getLogger(__name__).exception('start=sub failed, showing home')
        # 27.11.2026, «Увійти через Telegram» на сайті «Потсдам»: /start site - одноразове посилання входу (як /site).
        if context.args and str(context.args[0]) == 'site':
            try:
                from user_handlers import housing_site
                housing_site.send_link(context, int(from_user_id), with_old=False)
                return
            except Exception:
                import logging
                logging.getLogger(__name__).exception('start=site failed, showing home')
        # 27.11.2026, сайт каталогу: /start housing - одразу меню моніторингу житла, /start masters - як /masters.
        if context.args and str(context.args[0]) in ('housing', 'masters'):
            try:
                if str(context.args[0]) == 'housing':
                    from user_handlers import housing_monitor
                    housing_monitor.show_menu(update, context)
                else:
                    from user_handlers import masters_hint
                    masters_hint.masters_command(update, context)
                return
            except Exception:
                import logging
                logging.getLogger(__name__).exception('start=%s failed, showing home', context.args[0])
        # 27.11.2026, плитки сайту «Потсдам», яких там ще немає: /start equeue (ДП «Документ»), econsul (посольство), support
        # («Підтримати проєкт»), anon («Анонімні запитання») відкривають ті самі екрани бота, що й кнопки стартового меню.
        if context.args and str(context.args[0]) in ('equeue', 'econsul', 'support', 'anon'):
            try:
                param = str(context.args[0])
                if param == 'equeue':
                    from user_handlers import equeue_monitor
                    equeue_monitor.show_menu(update, context)
                elif param == 'econsul':
                    from user_handlers import econsul_monitor
                    econsul_monitor.show_menu(update, context)
                elif param == 'support':
                    from user_handlers import support
                    support.show_amounts(update, context)
                else:
                    from user_handlers import anonymous_posts
                    anonymous_posts.show_anon_submenu(update, context)
                return
            except Exception:
                import logging
                logging.getLogger(__name__).exception('start=%s failed, showing home', context.args[0])
        # «🎁 Запроси друга»: /start rf<код> - тихо привʼязуємо нову людину до запрошувача
        # (ДО show_home: він уже створює записи про людину, а привʼязка - лише для нових).
        if context.args and str(context.args[0]).startswith('rf'):
            from user_handlers import housing_referral
            housing_referral.handle_start(update, context, str(context.args[0]))
        from user_handlers.anonymous_posts import show_home
        show_home(update, context)
        return

    # Command with userbot mode
    if is_userbot_mode():
        admin_id = read_userbot_admin_id()
        if from_user_id == admin_id and len(context.args) == 1:
            command_text = context.args[0]
            if command_text.isdigit() or command_text.lstrip('-').isdigit():
                msg_text = insert_chat_or_enable(int(command_text), '')
                context.bot.send_message(chat_id=update.effective_chat.id, text=msg_text)
        return
    # Command with normal mode
    chat_id = update.effective_chat.id
    chat_title = update.message.chat.title
    
    # Can not get info if too much members in group
    try:
        chat_member = context.bot.get_chat_member(
            chat_id=chat_id, user_id=from_user_id)
    except telegram.error.BadRequest:
        context.bot.send_message(chat_id=update.effective_chat.id, 
            text=_('failed to read group member, grant bot administrator first (telegram limitation)'))
        return
    
    # Check control permission
    if check_control_permission(from_user_id) is True:
        pass
    elif check_control_permission(from_user_id) is False:
        return
    elif check_control_permission(from_user_id) is None:
        if chat_member.status != 'creator' and chat_member.status != 'administrator':
            return
    else:
        return
    if not update.message.chat.type == 'supergroup':
        msg_text = _('only work in supergroup!')
    else:
        msg_text = insert_chat_or_enable(chat_id, chat_title)
    context.bot.send_message(chat_id=update.effective_chat.id, text=msg_text)


handler = CommandHandler('start', start)
