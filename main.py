"""Standalone entry point, ported from Java StandaloneMain.java."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from mcxboxbroadcast import constants
from mcxboxbroadcast.config import load_config
from mcxboxbroadcast.logger import Logger, setup_console_logging
from mcxboxbroadcast.notifications import SlackNotificationManager
from mcxboxbroadcast.ping import ping as ping_server, set_web_ping_enabled
from mcxboxbroadcast.session_info import SessionInfo
from mcxboxbroadcast.session_manager import SessionManager
from mcxboxbroadcast.storage import FileStorageManager

logger = Logger("Standalone")

config = None
session_manager: SessionManager = None
session_info: SessionInfo = None
notification_manager = None
_update_lock = threading.Lock()


def main() -> None:
    global config, session_manager, session_info, notification_manager

    setup_console_logging()
    logging.getLogger().setLevel(logging.INFO)

    logger.info(
        f"Starting MCXboxBroadcast Standalone for Bedrock {constants.MINECRAFT_VERSION} "
        f"({constants.PROTOCOL_VERSION})"
    )

    config_file_name = "config.yml"

    try:
        config = load_config(config_file_name)
    except Exception as ex:
        logger.error("Failed to load config", ex)
        return

    logger.set_debug(config.debug_mode)
    logging.getLogger().setLevel(logging.DEBUG if config.debug_mode else logging.INFO)

    # TODO Support multiple notification types
    notification_manager = SlackNotificationManager(logger, config.notifications)

    session_manager = SessionManager(
        FileStorageManager("./cache", "./screenshot.jpg"), notification_manager, logger
    )
    session_manager.set_nether_net_port_range(
        config.session.ice_port_range.min, config.session.ice_port_range.max
    )

    session_info = SessionInfo.from_config(config.session.session_info)

    # Fallback to the gamertag if the host name is empty
    if not session_info.host_name:
        session_info.host_name = session_manager.get_gamertag()

    set_web_ping_enabled(config.session.web_query_fallback)

    # Sync the session info from the server if needed
    update_session_info(session_info)

    create_session()

    # Start the interactive console command loop
    console = threading.Thread(target=console_loop, daemon=True, name="Console")
    console.start()

    # Block until interrupted
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError:
        # Not on the main thread (or Windows); fall back to joining forever
        pass
    console.join()


def console_loop() -> None:
    for line in sys.stdin:
        command = line.strip()
        if not command:
            continue
        try:
            run_command(command)
        except Exception as ex:
            logger.error("Failed to execute command", ex)


def run_command(command: str) -> None:
    parts = command.split(" ")
    offset = 1 if parts[0].lower() == "mcxboxbroadcast" else 0
    command_node = parts[offset].lower()
    args = parts[offset + 1 :]

    if command_node in ("stop", "exit", "quit"):
        logger.info("Shutting down...")
        try:
            session_manager.shutdown()
        finally:
            os._exit(0)
    elif command_node == "restart":
        restart()
    elif command_node == "dumpsession":
        logger.info(
            "Dumping session responses to 'lastSessionResponse.json' and "
            "'currentSessionResponse.json'"
        )
        session_manager.dump_session()
    elif command_node == "accounts":
        if not args:
            logger.warn("Usage:")
            logger.warn("accounts list")
            logger.warn("accounts add/remove <sub-session-id>")
            return
        sub = args[0].lower()
        if sub == "list":
            session_manager.list_sessions()
        elif sub == "add":
            session_manager.add_sub_session(args[1])
        elif sub == "remove":
            session_manager.remove_sub_session(args[1])
        else:
            logger.warn(f"Unknown accounts command: {args[0]}")
    elif command_node == "version":
        logger.info("MCXboxBroadcast Standalone (Python port)")
    elif command_node == "help":
        logger.info("Available commands:")
        logger.info("exit - Exit the application")
        logger.info("restart - Restart the application")
        logger.info("dumpsession - Dump the current session to json files")
        logger.info("accounts list - List sub-accounts")
        logger.info("accounts add <sub-session-id> - Add a sub-account")
        logger.info("accounts remove <sub-session-id> - Remove a sub-account")
        logger.info("version - Display the version")
    else:
        logger.warn(f"Unknown command: {command_node}")


def _signal_handler(signum, frame) -> None:
    logger.info("Shutting down...")
    try:
        session_manager.shutdown()
    finally:
        os._exit(0)


def create_session() -> None:
    session_manager.restart_callback_(restart)
    try:
        initialized = session_manager.init(session_info, config.friend_sync)
    except Exception as ex:
        logger.error("Failed to initialize session", ex)
        return

    # If the session failed to initialize, don't start the update loop
    # We assume an error has already been logged
    if not initialized:
        return

    def update_loop() -> None:
        update_session_info(session_info)
        try:
            # Update the session
            session_manager.update_session_with(session_info)
            if config.suppress_session_update_message:
                session_manager.logger.debug("Updated session!")
            else:
                session_manager.logger.info("Updated session!")
        except Exception as ex:
            session_manager.logger.error("Failed to update session", ex)

    session_manager.scheduled_thread().schedule_with_fixed_delay(
        update_loop, config.session.update_interval, config.session.update_interval
    )


def restart() -> None:
    global session_manager
    try:
        session_manager.shutdown()

        # Create a new session manager, but reuse the notification manager
        # as config hasn't been reloaded
        session_manager = SessionManager(
            FileStorageManager("./cache", "./screenshot.jpg"), notification_manager, logger
        )
        session_manager.set_nether_net_port_range(
            config.session.ice_port_range.min, config.session.ice_port_range.max
        )

        create_session()
    except Exception as ex:
        logger.error("Failed to restart session", ex)


def update_session_info(session_info: SessionInfo) -> None:
    if not config.session.query_server:
        return
    try:
        pong = ping_server(session_info.ip, session_info.port, 1500).result(timeout=3)

        # Update the session information
        session_info.host_name = pong.sub_motd
        session_info.world_name = pong.motd
        session_info.players = pong.player_count
        session_info.max_players = pong.maximum_player_count

        # Fallback to the gamertag if the host name is empty
        if not session_info.host_name:
            session_info.host_name = session_manager.get_gamertag()
    except Exception as ex:
        if config.session.config_fallback:
            session_manager.logger.error(
                "Failed to ping server, falling back to config values", ex
            )
            session_info.host_name = config.session.session_info.host_name
            session_info.world_name = config.session.session_info.world_name
            session_info.players = config.session.session_info.players
            session_info.max_players = config.session.session_info.max_players

            # Fallback to the gamertag if the host name is empty
            if not session_info.host_name:
                session_info.host_name = session_manager.get_gamertag()
        else:
            session_manager.logger.error("Failed to ping server", ex)


if __name__ == "__main__":
    main()
