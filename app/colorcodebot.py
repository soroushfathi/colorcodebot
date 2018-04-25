#!/usr/bin/env python3
import io
from pathlib import Path
from time import time

from joblib import Parallel, delayed
import strictyaml
import structlog
from peewee import IntegerField, CharField
from playhouse.kv import KeyValue
from playhouse.apsw_ext import APSWDatabase
from pygments import formatters, lexers, highlight
from telebot import TeleBot
from telebot.apihelper import ApiException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, InputMediaPhoto


def yload(yamltxt: str) -> dict:
    return strictyaml.load(yamltxt).data


def ydump(data: dict) -> str:
    return strictyaml.as_document(data).as_yaml()


home = Path(__file__).parent
with open(home / 'vault.yml', 'r') as y:
    data = yload(y.read())
TG_API_KEY = data['TG_API_KEY']
ADMIN_CHAT_ID = data.get('ADMIN_CHAT_ID')
del data
with open(home / 'theme_previews.yml', 'r') as y:
    THEME_PREVIEWS = yload(y.read())
with open(home / 'english.yml', 'r') as y:
    LANG = yload(y.read())
with open(home / 'syntaxes.yml', 'r') as y:
    SYNTAXES = yload(y.read())
del home


user_themes = KeyValue(
    key_field=IntegerField(primary_key=True),
    value_field=CharField(),
    database=APSWDatabase('user_themes.sqlite')
)
log = structlog.get_logger()
bot = TeleBot(TG_API_KEY)


def mk_html(code: str, ext: str, theme: str='native') -> str:
    """Return HTML content"""
    return highlight(
        code,
        lexers.get_lexer_by_name(ext),
        formatters.HtmlFormatter(
            linenos='table',
            full=True,
            style=theme
        )
    )


def mk_png(code: str, ext: str, theme: str='native') -> str:
    """Return path of generated png"""
    return highlight(
        code,
        lexers.get_lexer_by_name(ext),
        formatters.ImageFormatter(
            font_name='Iosevka Custom',
            font_size=35,
            line_number_chars=3,
            style=theme
        )
    )


@bot.inline_handler(lambda q: True)
def switch_from_inline(inline_query):
    log.msg(
        "receiving inline query",
        user_id=inline_query.from_user.id,
        user_first_name=inline_query.from_user.first_name,
        query=inline_query.query
    )
    bot.answer_inline_query(
        inline_query.id, [],
        switch_pm_text=LANG['switch to direct'], switch_pm_parameter='x'
    )


@bot.message_handler(commands=['start', 'help'])
def welcome(message):
    log.msg(
        "introducing myself",
        user_id=message.from_user.id,
        user_first_name=message.from_user.first_name
    )
    bot.reply_to(message, LANG['welcome'])


@bot.message_handler(commands=['theme', 'themes'])
def browse_themes(message):
    log.msg(
        "browsing themes",
        user_id=message.from_user.id,
        user_first_name=message.from_user.first_name
    )
    bot.send_media_group(
        message.chat.id,
        map(InputMediaPhoto, THEME_PREVIEWS.values()),
        reply_to_message_id=message.message_id
    )
    kb = InlineKeyboardMarkup()
    kb.add(*(
        InlineKeyboardButton(
            name, callback_data=ydump({'action': 'set theme', 'theme': name})
        ) for name in THEME_PREVIEWS.keys()
    ))
    bot.reply_to(message, LANG['select theme'], reply_markup=kb)


@bot.callback_query_handler(lambda q: yload(q.data)['action'] == 'set theme')
def set_theme(cb_query):
    data = yload(cb_query.data)
    log.msg(
        "setting theme",
        user_id=cb_query.message.reply_to_message.from_user.id,
        user_first_name=cb_query.message.reply_to_message.from_user.first_name,
        theme=data['theme']
    )
    user_themes[cb_query.message.reply_to_message.from_user.id] = data['theme']
    bot.reply_to(cb_query.message, LANG['acknowledge theme'].format(data['theme']))
    if ADMIN_CHAT_ID:
        with open('user_themes.sqlite', 'rb') as doc:
            bot.send_document(ADMIN_CHAT_ID, doc)


@bot.message_handler(func=lambda m: m.content_type == 'text')
def intake_snippet(message):
    log.msg(
        "receiving code",
        user_id=message.from_user.id,
        user_first_name=message.from_user.first_name
    )
    kb = InlineKeyboardMarkup()
    kb.add(*(
        InlineKeyboardButton(
            name, callback_data=ydump({'action': 'set ext', 'ext': ext})
        ) for name, ext in SYNTAXES.items()
    ))
    bot.reply_to(message, LANG['query ext'], reply_markup=kb)


def send_html(snippet: Message, ext: str, theme: str='native'):
    bot.send_chat_action(snippet.chat.id, 'upload_document')
    start = time()
    html = mk_html(snippet.text, ext, theme)
    log.msg('completed mk_html', seconds=time() - start)
    with io.StringIO(html) as doc:
        doc.name = 'code.html'
        bot.send_document(snippet.chat.id, doc, reply_to_message_id=snippet.message_id)


def send_image(snippet: Message, ext: str, theme: str='native', max_lines_for_compressed: int=80):
    bot.send_chat_action(snippet.chat.id, 'upload_photo')
    start = time()
    png = mk_png(snippet.text, ext, theme)
    log.msg('completed mk_png', seconds=time() - start)
    with io.BytesIO(png) as doc:
        doc.name = 'code.png'
        if snippet.text.count('\n') <= max_lines_for_compressed:
            try:
                bot.send_photo(snippet.chat.id, doc, reply_to_message_id=snippet.message_id)
            except ApiException as e:
                log.error("failed to send compressed image", exc_info=e)
                bot.send_document(snippet.chat.id, doc, reply_to_message_id=snippet.message_id)
        else:
            bot.send_document(snippet.chat.id, doc, reply_to_message_id=snippet.message_id)


@bot.callback_query_handler(lambda q: yload(q.data)['action'] == 'set ext')
def set_snippet_filetype(cb_query):
    data = yload(cb_query.data)
    log.msg(
        "colorizing code",
        user_id=cb_query.message.reply_to_message.from_user.id,
        user_first_name=cb_query.message.reply_to_message.from_user.first_name,
        syntax=data['ext']
    )
    snippet = cb_query.message.reply_to_message
    theme = user_themes.get(cb_query.message.reply_to_message.from_user.id, 'native')
    Parallel(n_jobs=2, backend="threading")(
        delayed(snd)(snippet, data['ext'], theme) for snd in (send_html, send_image)
    )


@bot.message_handler(content_types=['photo'])
def recv_photo(message):
    log.msg('received photo', file_id=message.photo[0].file_id)


if __name__ == '__main__':
    bot.polling()
