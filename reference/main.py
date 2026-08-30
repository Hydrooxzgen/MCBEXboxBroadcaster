"""Standalone entry point (root copy).

Run from the repository root with::

    python main.py config.yml

This mirrors ``python/mcxboxbroadcast/__main__.py`` but adds a sys.path
bootstrap so the ``mcxboxbroadcast`` package under ``python/`` can be
imported when running from the root directory. Config, cache and
screenshot files are all relative to the current working directory.
"""

from __future__ import annotations

import os
import sys

# Make the package under python/ importable when running from the repo root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PYTHON_DIR = os.path.join(_THIS_DIR, "python")
if os.path.isdir(_PYTHON_DIR) and _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

import threading
import time

from mcxboxbroadcast import __version__
from mcxboxbroadcast.config.config_loader import load_config
from mcxboxbroadcast.constants import BEDROCK_PROTOCOL_VERSION, BEDROCK_VERSION
from mcxboxbroadcast.logger import Logger, setup_logging
from mcxboxbroadcast.notifications.slack_notification_manager import SlackNotificationManager
from mcxboxbroadcast.ping.ping_util import ping
from mcxboxbroadcast.session_info import SessionInfo
from mcxboxbroadcast.session_manager import SessionManager
from mcxboxbroadcast.storage.file_storage_manager import FileStorageManager


def _ensure_config(config_file_name: str) -> None:
    """Create ``config.yml`` from the example template if it doesn't exist."""
    if os.path.exists(config_file_name):
        return
    example = os.path.join(_PYTHON_DIR, "config.yml.example")
    if os.path.exists(example):
        try:
            with open(example, "r", encoding="utf-8") as f:
                content = f.read()
            with open(config_file_name, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[MCXboxBroadcast] No config found, created a default one at:")
            print(f"[MCXboxBroadcast]   {os.path.abspath(config_file_name)}")
            print(f"[MCXboxBroadcast] Edit it to set your server address, then run again.")
        except OSError as e:
            print(f"[MCXboxBroadcast] Failed to create default config: {e}")


def main(argv: list | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    logger = Logger("MCXboxBroadcast")
    setup_logging(False)

    logger.info(
        f"Starting MCXboxBroadcast Standalone {__version__} "
        f"for Bedrock {BEDROCK_VERSION} ({BEDROCK_PROTOCOL_VERSION})"
    )

    config_file_name = argv[0] if argv else "config.yml"
    _ensure_config(config_file_name)
    try:
        config = load_config(config_file_name, "Standalone")
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return 1

    logger.set_debug(config.debug_mode)

    # TODO Support multiple notification types
    notification_manager = SlackNotificationManager(logger, config.notifications)

    session_manager = SessionManager(
        FileStorageManager("./cache", "./screenshot.jpg"),
        notification_manager,
        logger,
    )
    session_manager.set_nether_net_port_range(
        config.session.ice_port_range.min, config.session.ice_port_range.max
    )

    session_info = SessionInfo.from_config(config.session.session_info)

    # Fallback to the gamertag if the host name is empty
    if not session_info.get_host_name():
        session_info.set_host_name(session_manager.get_gamertag())

    # Sync the session info from the server if needed
    update_session_info(session_info, config, session_manager)

    create_session(session_info, config, session_manager, logger)

    logger.info("Type 'help' for a list of commands, 'quit' to exit")

    # Command input loop on a background thread so the main thread can be
    # interrupted cleanly by Ctrl+C.
    stop = threading.Event()

    def _command_loop() -> None:
        while not stop.is_set():
            try:
                line = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("quit", "exit"):
                logger.info("Shutting down...")
                session_manager.shutdown()
                stop.set()
            elif line == "restart":
                logger.info("Restarting session...")
                restart(session_info, config, session_manager, notification_manager, logger)
            elif line == "list":
                session_manager.list_sessions()
            elif line == "dump":
                session_manager.dump_session()
            elif line == "help":
                logger.info("Commands: quit, restart, list, dump, help")
            else:
                logger.info(f"Unknown command: {line} (try 'help')")

    threading.Thread(target=_command_loop, name="Console", daemon=True).start()

    try:
        while not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        session_manager.shutdown()
    return 0


def restart(
    session_info: SessionInfo,
    config,
    session_manager: SessionManager,
    notification_manager,
    logger: Logger,
) -> None:
    try:
        session_manager.shutdown()

        # Create a new session manager, but reuse the notification manager as
        # config hasn't been reloaded
        new_manager = SessionManager(
            FileStorageManager("./cache", "./screenshot.jpg"),
            notification_manager,
            logger,
        )
        new_manager.set_nether_net_port_range(
            config.session.ice_port_range.min, config.session.ice_port_range.max
        )
        create_session(session_info, config, new_manager, logger)
        globals()["session_manager"] = new_manager
    except Exception as e:
        logger.error(f"Failed to restart session: {e}")


def create_session(
    session_info: SessionInfo,
    config,
    session_manager: SessionManager,
    logger: Logger,
) -> None:
    session_manager.restart_callback(
        lambda: restart(session_info, config, session_manager, session_manager.notification_manager(), logger)
    )
    initialized = session_manager.init(
        session_info, config.friend_sync, config.session.visibility
    )

    # If the session failed to initialize, don't start the update loop
    if not initialized:
        return

    session_manager.scheduled_thread().schedule_with_fixed_delay(
        lambda: _update_loop(session_info, config, session_manager),
        config.session.update_interval,
        config.session.update_interval,
    )


def _update_loop(session_info: SessionInfo, config, session_manager: SessionManager) -> None:
    update_session_info(session_info, config, session_manager)

    try:
        # Update the session
        session_manager.update_session(session_info)
        if config.suppress_session_update_message:
            session_manager.logger().debug("Updated session!")
        else:
            session_manager.logger().info("Updated session!")
    except Exception as e:
        session_manager.logger().error(f"Failed to update session: {e}")


def update_session_info(session_info: SessionInfo, config, session_manager: SessionManager) -> None:
    if not config.session.query_server:
        return

    try:
        pong = ping(
            session_info.get_ip(),
            session_info.get_port(),
            timeout=1.5,
            web_fallback=config.session.web_query_fallback,
        )

        # Update the session information
        session_info.set_host_name(pong.motd2)
        session_info.set_world_name(pong.motd1)
        session_info.set_players(pong.player_count)
        session_info.set_max_players(pong.max_player_count)

        # Fallback to the gamertag if the host name is empty
        if not session_info.get_host_name():
            session_info.set_host_name(session_manager.get_gamertag())
    except Exception as e:
        if config.session.config_fallback:
            session_manager.logger().error(
                "Failed to ping server, falling back to config values", exc_info=e
            )
            session_info.set_host_name(config.session.session_info.host_name)
            session_info.set_world_name(config.session.session_info.world_name)
            session_info.set_players(config.session.session_info.players)
            session_info.set_max_players(config.session.session_info.max_players)

            # Fallback to the gamertag if the host name is empty
            if not session_info.get_host_name():
                session_info.set_host_name(session_manager.get_gamertag())
        else:
            session_manager.logger().error(f"Failed to ping server: {e}")


if __name__ == "__main__":
    sys.exit(main())
