import logging

import telegram
from utils import get_text_func

_ = get_text_func()
logger = logging.getLogger(__name__)

def set_bot_commands(context: telegram.ext.CallbackContext):
    private_commands = [
        ('start', 'головне меню'),
        ('anonymous', 'поставити анонімне запитання'),
        ('dps_document', 'перевірка термінів ДП Документ'),
        ('housing', 'моніторинг житла'),
    ]
    commands = [
        ('start', _('start bot in current group ( userbot mode need `start <group_id>`)')),
        ('anonymous', 'поставити анонімне запитання'),
        ('dps_document', 'перевірка термінів ДП Документ'),
        ('housing', 'моніторинг житла'),
        ('help', 'довідка про команди бота'),
        ('chat_id', _('get current chat id (group or user)')),
        ('stop', _('stop bot in current group (userbot mode need `stop <group_id>`)')),
        ('delete', _('delete saved messages if stopped  (userbot mode need `stop <group_id>`)')),

    ]

    try:
        context.bot.set_my_commands(
            private_commands,
            scope=telegram.BotCommandScopeAllPrivateChats(),
        )
    except Exception:
        logger.exception("Could not set private bot commands")

    if hasattr(telegram, "MenuButtonCommands") and hasattr(context.bot, "set_chat_menu_button"):
        try:
            context.bot.set_chat_menu_button(menu_button=telegram.MenuButtonCommands())
        except Exception:
            logger.exception("Could not set bot menu button")

    context.bot.set_my_commands(commands)
