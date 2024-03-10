#!/usr/bin/env python3

# See LICENSE file for copyright and license details.
# TUM CS Bot - https://github.com/ro-i/tumcsbot

"""TUM CS Bot - a generic Zulip bot.

This bot is currently especially intended for administrative tasks.
It supports several commands which can be written to the bot using
a private message or a message starting with @mentioning the bot.
"""

from __future__ import annotations
import asyncio
import logging
import signal
from graphlib import TopologicalSorter
import sys
from typing import Any, Callable, Iterable, Type, cast
from sqlalchemy import Boolean, Column, String

from zulip import Client as ZulipClient
from tumcsbot import lib
from tumcsbot.db import DB, TableBase
from tumcsbot.client import AsyncClient
from tumcsbot.plugin import (
    Event,
    Plugin,
    EventType,
    PluginContext,
    get_zulip_events_from_plugins,
)


class EventQueue(asyncio.Queue[Event]):
    pass


class PlublicStreams(TableBase):
    __tablename__ = "PublicStreams"

    StreamName = Column(String, primary_key=True)
    Subscribed = Column(Boolean, nullable=False)

class _RootClient(AsyncClient):
    """Enhanced Client class with additional functionality.

    Particularly, this client initializes the Client's database tables.
    """

    @staticmethod
    async def from_sync_client(*args: Any, **kwargs: Any) -> _RootClient:
        client = ZulipClient(*args, **kwargs)
        profile = client.get_profile()
        client = _RootClient(profile["user_id"], f"@**{profile['full_name']}**", client)
        await client.init_db()
        return client

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


    async def init_db(self) -> None:
        """Initialize some tables of the database."""

        stream_names = await self.get_public_stream_names(use_db=False)
        with DB.session() as session:
            for entry in session.query(PlublicStreams).all():
                if not str(entry.StreamName) in stream_names:
                    session.delete(entry)
            session.commit()


class TumCSBot:
    """Main Bot class.

    Use run() to run the bot.

    Arguments:
    ----------
    zuliprc       zuliprc file containing the bot's configuration
    db_path       path to the bot's database
    debug         debugging mode switch
    logfile       use LOGFILE for logging output
    """

    async def from_sync_client(
        zuliprc: str,
        db_path: str,
        debug: bool = False,
        logfile: str | None = None,
    ) -> TumCSBot:
        client = await _RootClient.from_sync_client(config_file=zuliprc)
        return TumCSBot(
            zuliprc=zuliprc,
            db_path=db_path,
            client=client,
            debug=debug,
            logfile=logfile,
        )

    def __init__(
        self,
        zuliprc: str,
        db_path: str,
        client: _RootClient,
        debug: bool = False,
        logfile: str | None = None,
    ) -> None:
        self.events: list[str]
        self.plugins: dict[str, Plugin] = {}
        self.plugins_stopped: dict[str, Plugin] = {}
        self.restart: bool = False
        self.stopped: bool = False

        # Init logging.
        logging_level: int = logging.WARNING
        if debug:
            logging_level = logging.DEBUG

        logging.basicConfig(
            # todo: change sys.stdout
            format=lib.LOGGING_FORMAT,
            level=logging_level,
            stream=sys.stdout,  # filename=logfile if logfile else sys.stdout
        )

        # Init database handler.
        DB.set_path(db_path)
        # Ensure presence of Plugins table.
        DB.create_tables()

        # Init own Zulip client which also inits the global DB tables for all
        # Zulip client objects.
        self.client = client

        # Init the event queue. It is not a multiprocessing queue because the
        # communication with the process plugins goes over their queues and a
        # separate loopback queue. The loopback queue for the thread plugins
        # simply is the central event queue.
        # In order to deliver the events from the process loopback queue to the
        # central event queue, too, we additionally need a small worker thread.

        self.event_queue: EventQueue = asyncio.Queue()
        logging.debug("start queue forward worker")

        # Cleanup properly on SIGTERM and SIGINT.
        # signal.signal(signal.SIGTERM, self.sigterm_handler)
        # signal.signal(signal.SIGINT, self.sigterm_handler)

        # Get the plugin classes and start the plugins in correct dependency order.
        plugin_classes: Iterable[Type[Plugin]] = lib.get_classes_from_path(
            "tumcsbot.plugins", Plugin  # type: ignore
        )
        self.startPlugins(plugin_classes, zuliprc, logging_level)

        # Get events to listen for.
        self.events = get_zulip_events_from_plugins(plugin_classes)

    # def exit_handler(self) -> None:
    #     """Stop the main loop if necessary."""
    #     logging.debug("exit handler")
    #
    #     if not self.stopped:
    #         self.stopped = True
    #         await self.event_queue.put(Event._empty_event("", ""))

    #     def restartPlugin(self, name: str) -> None:
    #         """Restart a plugin given its name."""
    #         logging.debug("restart plugin %s ...", name)
    #         plugin: Plugin = self.plugins_stopped[name]
    #         plugin.start()
    #         self.plugins[name] = plugin
    #         del self.plugins_stopped[name]

    async def run(self) -> None:
        """Run the central event queue.

        This queue does not only get the events from the event listener,
        but also loopback data from the plugins.
        """

        logging.debug("start event listener, listening on events: %s", str(self.events))
        logging.debug("start central queue")

        async def _event_listener() -> None:
            logging.debug("waiting for events ...")
            async for event_data in self.client.events():
                logging.debug("received event data %s", str(event_data))
                await self.event_queue.put(Event(sender="_root", type=EventType.ZULIP, data=event_data))
                logging.debug("waiting for events ...")

        _ = asyncio.create_task(_event_listener())
        
        tasks: list[asyncio.Task] = []
        while True:
            logging.debug("waiting for event ...")
            event = await self.event_queue.get()
            logging.debug("received event (%s) %s", id(event), str(event))
            tasks = [task for task in tasks if not task.done()]

            if self.stopped or event.type == EventType._EMPTY:
                if event.type == EventType._EMPTY and event.sender == "restart":
                    self.restart = True
                self.stopped = True
                break

            # todo: handle other event types
            if event.type == EventType.ZULIP:
                if event.data["type"] == "heartbeat":
                    continue
                try:
                    event.data = self.zulip_event_preprocess(event.data)
                except Exception as exc:
                    logging.exception(exc)
                    continue
                
                for plugin in self.plugins.values():
                    if plugin.is_responsible(event):
                        logging.debug("push event to plugin %s", plugin.plugin_name())
                        tasks.append(asyncio.create_task(plugin.handle_event(event)))

        logging.debug("stopping plugins ...")
        for plugin_name in self.plugins:
            self.stopPlugin(plugin_name, updatePlugins_dicts=False)

    def sigterm_handler(self, *_: Any) -> None:
        self.exit_handler()

    def startPlugins(
        self, plugin_classes: Iterable[Type[Plugin]], zuliprc: str, logging_level: int
    ) -> None:
        # First, build the correct order using the dependency information.
        plugin_class_dict: dict[str, Type[Plugin]] = {
            plugin_class.plugin_name(): plugin_class for plugin_class in plugin_classes
        }
        plugin_graph: dict[str, set[str]] = {
            plugin_class.plugin_name(): set(plugin_class.dependencies)
            for plugin_class in plugin_classes
        }

        for plugin_name in TopologicalSorter(plugin_graph).static_order():
            logging.debug("start %s", plugin_name)
            plugin_class = plugin_class_dict[plugin_name]

            plugin: Plugin = plugin_class(
                PluginContext(zuliprc, self.event_queue.put, logging_level),
                self.client,
            )

            if plugin_name in self.plugins:
                raise ValueError(f"plugin {plugin.plugin_name()} appears twice")
            self.plugins[plugin_name] = plugin

    def zulip_event_preprocess(self, event: dict[str, Any]) -> dict[str, Any]:
        """Preprocess a Zulip event dictionary.

        Check if the event could be an interactive command (to be
        handled by a CommandPlugin instance).

        Check if one of the following requirements are met by the event:
          - It is a private message to the bot.
          - It is a message starting with mentioning the bot.
        The sender of the message must not be the bot itself.

        If this event may be a command, add two new fields to the
        message dict:
          command_name     The name of the command.
          command          The command without the name.
        """
        startswithping: bool = False

        if event["type"] == "message" and event["message"]["content"].startswith(
            self.client.ping
        ):
            startswithping = True

        if (
            event["type"] != "message"
            or event["message"]["sender_id"] == self.client.id
            or (event["message"]["type"] != "private" and not startswithping)
            or (
                event["message"]["type"] == "private"
                and (
                    startswithping
                    or not self.client.is_only_pm_recipient(event["message"])
                )
            )
        ):
            return event

        content: str
        message: dict[str, Any] = event["message"]

        if startswithping:
            content = message["content"][self.client.ping_len :]
        else:
            content = message["content"]

        cmd: list[str] = content.split(maxsplit=1)
        logging.debug("received command line %s", str(cmd))

        event["message"].update(
            command_name=cmd[0] if len(cmd) > 0 else "",
            command=cmd[1] if len(cmd) > 1 else "",
        )

        return event
