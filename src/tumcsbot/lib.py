#!/usr/bin/env python3

# See LICENSE file for copyright and license details.
# TUM CS Bot - https://github.com/ro-i/tumcsbot

"""Collection of useful classes and functions.

Classes:
--------
Regex        Some widely used regular expressions.
MessageType  Enum describing the type of a message.
DB           Simple sqlite wrapper.
Helper       Collect docs of interactive commands.
Response     Provide Response building methods.

Functions:
----------
new_private_message  Construct a new private message.
new_stream_message   Construct a new stream message.
"""

import re
import sqlite3 as sqlite

from enum import Enum
from inspect import cleandoc
from typing import Any, Dict, List, Optional, Tuple, Union


class StrEnum(str, Enum):
    """Construct a string enum.

    See https://docs.python.org/3/library/enum.html#others.
    """


class Regex(StrEnum):
    """Some widely used regular expressions.

    OPT_ASTERISKS  Match optional asterisks enclosing autocompleted
                   stream or user names.
    STREAM         Match a stream name.
    """

    OPT_ASTERISKS: str = r'(?:\*\*|)'
    STREAM: str = r'[^*#]*'


class MessageType(StrEnum):
    """Represent the type of a message.

    MESSAGE  Normal message as written by a human user.
    EMOJI    Emoji reaction on a message.
    NONE     No message.
    """

    MESSAGE: str = 'message'
    EMOJI: str = 'emoji'
    NONE: str = 'none'


class DB:
    """Simple wrapper class to conveniently access a sqlite database.

    Currently not threadsafe.
    """

    path: Optional[str] = None

    def __init__(self) -> None:
        if not DB.path:
            raise ValueError('no path to database given')
        self.connection = sqlite.connect(DB.path)
        self.cursor = self.connection.cursor()

    def checkout_table(self, table: str, schema: str) -> None:
        """Create table if it does not already exist.

        Arguments:
        ----------
        table   name of the table
        schema  schema of the table in the form of
                    '(Name Type, ...)' --> valid SQL!
        """
        result: List[Tuple[Any, ...]] = self.execute(
            ('select * from sqlite_master where type = "table" and '
             'name = "{}";'.format(table))
        )
        if not result:
            self.execute(
                'create table {} {};'.format(table, schema),
                commit = True
            )

    def checkout_row(
        self,
        table: str,
        key_column: str,
        key: str,
        default_values: str
    ) -> None:
        """Create row in table if it does not already exist.

        Arguments:
        ----------
        table           name of the table
        key_column      name of the column of the primary key
        key             key to identify the row
        default_values  default value to insert if row does not yet
                        exist
                        - must be in the form of
                          '(Integer, "String", ...)' --> valid SQL!
        """
        result: List[Tuple[Any, ...]] = self.execute(
            'select * from {} where {} = "{}";'.format(table, key_column, key)
        )
        if not result:
            self.execute(
                'insert into {} values {}'.format(table, default_values),
                commit = True
            )

    def execute(
        self,
        command: str,
        *args: Any,
        commit: bool = False
    ) -> List[Tuple[Any, ...]]:
        """Execute an sql command.

        Execute an sql command, save the new database state
        (if commit == True) and return the result of the command.
        Forward 'args' to cursor.execute()
        """
        result: sqlite.Cursor = self.cursor.execute(command, args)
        if commit:
            self.connection.commit()
        return result.fetchall()


class Helper:
    """Get the docs of the interactive commands for the users.

    Collect all usage documentation from the command classes during
    their import by TumCSBot.
    """

    help: str = cleandoc(
        """
        Hi {}!
        Currently, I understand the following commands:

        {}

        Have a nice day! :-)
        """
    )
    command_docs: str = ''

    @classmethod
    def get_help(cls, user: str) -> str:
        """Return help string."""
        return cls.help.format(user, cls.command_docs)

    @classmethod
    def extend_command_docs(cls, docs: List[Tuple[str, str]]) -> None:
        """Add further docs to the internal documentation.

        Format the syntax and description received from the command.
        """
        processed: List[str] = []

        # sort by syntax string
        docs = sorted(docs, key = lambda tuple: tuple[0])

        # format
        for (syntax, desc) in docs:
            syntax = '- `' + syntax.replace('\n', '') + '`'
            # replace multiple whitespaces by a single one
            for space in [ '\n', ' ', '\t' ]:
                desc = re.sub(space + r'{2,}', space, desc)
            if not desc.endswith('\n'):
                desc += '\n'
            # ensure one (!) joining newline
            if not desc.startswith('\n'):
                syntax += '\n'
            processed.append(syntax + desc)

        cls.command_docs += '\n'.join(processed)


class Response:
    """Some useful methods for building a response message."""

    admin_err_msg: str = cleandoc(
        """
        Hi {}!
        You need to be administrator of this organization in order to execute \
        this command.
        """
    )
    command_not_found_msg: str = cleandoc(
        """
        Hi {}!
        Unfortunately, I currently cannot understand what you wrote to me.
        Try "help" to get a glimpse of what I am capable of. :-)
        """
    )
    exception_msg: str = cleandoc(
        """
        Hi {}!
        An exception occurred while executing your request.
        Did you try to hack me? ;-)
        """
    )
    error_msg: str = cleandoc(
        """
        Sorry, {}, an error occurred while executing your request.
        """
    )
    greet_msg: str = 'Hi {}! :-)'
    ok_emoji: str = 'ok'
    no_emoji: str = 'cross_mark'

    @classmethod
    def build_message(
        cls,
        message: Dict[str, Any],
        response: str,
        msg_type: Optional[str] = None,
        to: Optional[str] = None,
        subject: Optional[str] = None
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """Build a message.

        Arguments:
        ----------
        message    Message to respond to.
        response   Content of the response.
        msg_type   Determine if the response should be a stream or a
                   private message.
        to         The recipients (private message) or the stream.
        subject    The topic the message should be added to (only for
                   stream messages.
        """
        if msg_type is None:
            msg_type = message['type']
        private: bool = msg_type == 'private'

        if to is None:
            to = message['sender_email'] if private else message['stream_id']

        if subject is None:
            subject = message['subject'] if not private else ''

        if private:
            return new_private_message(
                to = to,
                content = response
            )

        return new_stream_message(
            stream = to,
            subject = subject,
            content = response
        )

    @classmethod
    def build_reaction(
        cls,
        message: Dict[str, Any],
        emoji: str
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """Build a reaction response.

        Arguments:
        ----------
        message   The message to react on.
        emoji     The emoji to react with.
        """
        return (
            MessageType.EMOJI,
            dict(message_id = message['id'], emoji_name = emoji)
        )

    @classmethod
    def admin_err(
        cls, message: Dict[str, Any]
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """The user has not sufficient rights.

        Tell the user that they have not administrator rights. Relevant
        for some commands intended to be exclusively used by admins.
        """
        return cls.build_message(
            message,
            cls.admin_err_msg.format(message['sender_full_name'])
        )

    @classmethod
    def command_not_found(
        cls, message: Dict[str, Any]
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """Tell the user that his command could not be found."""
        return cls.build_message(
            message,
            cls.command_not_found_msg.format(message['sender_full_name'])
        )

    @classmethod
    def error(
        cls, message: Dict[str, Any]
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """Tell the user that an error occurred."""
        return cls.build_message(
            message, cls.error_msg.format(message['sender_full_name'])
        )

    @classmethod
    def exception(
        cls, message: Dict[str, Any]
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """Tell the user that an exception occurred."""
        return cls.build_message(
            message, cls.exception_msg.format(message['sender_full_name'])
        )

    @classmethod
    def greet(
        cls, message: Dict[str, Any]
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """Greet the user."""
        return cls.build_message(
            message, cls.greet_msg.format(message['sender_full_name'])
        )

    @classmethod
    def ok(
        cls, message: Dict[str, Any]
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """Return an "ok"-reaction."""
        return cls.build_reaction(message, cls.ok_emoji)

    @classmethod
    def no(
        cls, message: Dict[str, Any]
    ) -> Tuple[MessageType, Dict[str, Any]]:
        """Return a "no"-reaction."""
        return cls.build_reaction(message, cls.no_emoji)

    @classmethod
    def none(cls) -> Tuple[MessageType, Dict[str, Any]]:
        """No response."""
        return (MessageType.NONE, {})


def new_private_message(
    to: Union[str, int],
    content: str
) -> Tuple[MessageType, Dict[str, Any]]:
    """Send a private message.

    Arguments:
    ----------
    to        Either a list containing integer user IDs
              or a list containing string email addresses.
    content   The content of the message.
    """
    return (
        MessageType.MESSAGE,
        dict(
            type = 'private',
            to = to,
            content = content
        )
    )


def new_stream_message(
    stream: Union[str, int],
    subject: str,
    content: str
) -> Tuple[MessageType, Dict[str, Any]]:
    """Send a stream message.

    Arguments:
    ----------
    stream    Either the name or the integer ID of a stream.
    subject   The topic to add the message to.
    content   The content of the message.
    """
    return (
        MessageType.MESSAGE,
        dict(**{
            'type': 'stream',
            'to': stream,
            'subject': subject,
            'content': content
        })
    )
