#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
YouCube Server
"""

# built-in modules
from os.path import join
from os import getenv
from asyncio import get_event_loop
from typing import (
    Union,
    Tuple,
    Type,
    List,
    Any
)
from base64 import b64encode
from shutil import which

try:
    from ujson import (
        JSONDecodeError,
        dumps,
        loads as load_json
    )
except ModuleNotFoundError:
    from json.decoder import JSONDecodeError
    from json import dumps, loads as load_json

try:
    from types import UnionType
except ImportError:
    UnionType = Union[int, str]


# pip modules
from sanic import (
    Sanic,
    Request,
    Websocket
)
from sanic.response import text
from sanic.handlers import ErrorHandler
from spotipy import SpotifyClientCredentials, MemoryCacheHandler
from spotipy.client import Spotify

# local modules
from yc_utils import (
    is_save,
    cap_width_and_height,
    get_video_name,
    get_audio_name
)
from yc_colours import Foreground, RESET
from yc_download import download, DATA_FOLDER, FFMPEG_PATH, SANJUUNI_PATH
from yc_magic import run_function_in_thread_from_async_function
from yc_logging import setup_logging, NO_COLOR
from yc_spotify import SpotifyURLProcessor

VERSION = "0.0.0-poc.1.0.2"
API_VERSION = "0.0.0-poc.1.0.0"  # https://commandcracker.github.io/YouCube/

# one dfpwm chunk is 16 bits
CHUNK_SIZE = 16

"""
CHUNKS_AT_ONCE should not be too big, [CHUNK_SIZE * 1024]
because then the CC Computer cant decode the string fast enough!
Also, it should not be too small because then the client
would need to send thousands of WS messages
and that would also slow everything down! [CHUNK_SIZE * 1]
"""
CHUNKS_AT_ONCE = CHUNK_SIZE * 256


FRAMES_AT_ONCE = 10

# pylint settings
# pylint: disable=pointless-string-statement
# pylint: disable=fixme
# pylint: disable=multiple-statements


def get_vid(vid_file: str, tracker: int) -> List[str]:
    """
    Returns given line of 32vid file
    """
    with open(vid_file, "r", encoding="utf-8") as file:
        file.seek(tracker)
        lines = []
        for _unused in range(FRAMES_AT_ONCE):
            lines.append(file.readline()[:-1])  # remove \n
        file.close()

    return lines


def get_chunk(media_file: str, chunkindex: int) -> bytes:
    """
    Returns a chunk of the given media file
    """
    with open(media_file, "rb") as file:
        file.seek(chunkindex * CHUNKS_AT_ONCE)
        chunk = file.read(CHUNKS_AT_ONCE)
        file.close()

    return chunk


class UntrustedProxy(Exception):
    """
    Occurs when someone connects through an untrusted proxy
    """

    def __str__(self) -> str:
        return "A client is not using a trusted proxy!"

# pylint: disable=redefined-outer-name


def get_client_ip(request: Request, trusted_proxies: list) -> str:
    """
    Returns the real client IP
    """
    peername_host = request.ip

    if trusted_proxies is None:
        return peername_host

    if peername_host in trusted_proxies:
        x_forwarded_for = request.headers.get('X-Forwarded-For')

        if x_forwarded_for is not None:
            x_forwarded_for = x_forwarded_for.split(",")[0]

        return x_forwarded_for or request.headers.get('True-Client-Ip')

    raise UntrustedProxy

# pylint: enable=redefined-outer-name


def assert_resp(
    __obj_name: str,
    __obj: Any,
    __class_or_tuple: Union[
        Type, UnionType,
        Tuple[
            Union[
                Type,
                UnionType,
                Tuple[Any, ...]
            ],
            ...
        ]
    ]
) -> Union[dict, None]:
    """
    "assert" / isinstance that returns a dict that can be send as a ws response
    """
    if not isinstance(__obj, __class_or_tuple):
        return {
            "action": "error",
            "message": f"{__obj_name} must be a {__class_or_tuple.__name__}"
        }
    return None


logger = setup_logging()

# pylint: disable=duplicate-code
spotify_client_id = getenv("SPOTIPY_CLIENT_ID")
spotify_client_secret = getenv("SPOTIPY_CLIENT_SECRET")
# pylint: disable-next=invalid-name
spotipy = None

# TODO: only print once

if spotify_client_id and spotify_client_secret:
    logger.info("Spotipy Enabled")
    spotipy = Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=spotify_client_id,
            client_secret=spotify_client_secret,
            cache_handler=MemoryCacheHandler()
        )
    )
else:
    logger.info("Spotipy Disabled")

# pylint: disable-next=invalid-name
spotify_url_processor = None
if spotipy:
    spotify_url_processor = SpotifyURLProcessor(spotipy)

# pylint: enable=duplicate-code


class Actions:
    """
    Default set of actions
    Every action needs to be called with a message and needs to return a dict response
    """

    # pylint: disable=missing-function-docstring

    @staticmethod
    async def request_media(message: dict, resp: Websocket):
        loop = get_event_loop()
        # get "url"
        url = message.get("url")
        if error := assert_resp("url", url, str): return error
        # TODO: assert_resp width and height
        return await run_function_in_thread_from_async_function(
            download,
            url,
            resp,
            loop,
            message.get("width"),
            message.get("height"),
            spotify_url_processor
        )

    @staticmethod
    async def get_chunk(message: dict, _unused):
        # get "chunkindex"
        chunkindex = message.get("chunkindex")
        if error := assert_resp("chunkindex", chunkindex, int): return error

        # get "id"
        media_id = message.get("id")
        if error := assert_resp("media_id", media_id, str): return error

        if is_save(media_id):
            file = join(
                DATA_FOLDER,
                get_audio_name(message.get("id"))
            )

            chunk = get_chunk(file, chunkindex)

            return {
                "action": "chunk",
                "chunk": b64encode(chunk).decode("ascii")
            }
        logger.warning("User tried to use special Characters")
        return {
            "action": "error",
            "message": "You dare not use special Characters"
        }

    @staticmethod
    async def get_vid(message: dict, _unused):
        # get "line"
        tracker = message.get("tracker")
        if error := assert_resp("tracker", tracker, int): return error

        # get "id"
        media_id = message.get("id")
        if error := assert_resp("id", media_id, str): return error

        # get "width"
        width = message.get('width')
        if error := assert_resp("width", width, int): return error

        # get "height"
        height = message.get('height')
        if error := assert_resp("height", height, int): return error

        # cap height and width
        width, height = cap_width_and_height(width, height)

        if is_save(media_id):
            file = join(
                DATA_FOLDER,
                get_video_name(message.get('id'), width, height)
            )

            return {
                "action": "vid",
                "lines": get_vid(file, tracker)
            }

        return {
            "action": "error",
            "message": "You dare not use special Characters"
        }

    @staticmethod
    async def handshake(*_unused):
        return {
            "action": "handshake",
            "server": {
                "version": VERSION
            },
            "api": {
                "version": API_VERSION
            },
            "capabilities": {
                "video": [
                    "32vid"
                ],
                "audio": [
                    "dfpwm"
                ]
            }
        }

    # pylint: enable=missing-function-docstring


class CustomErrorHandler(ErrorHandler):
    """
    Error handler for sanic
    """

    def default(self, request: Request, exception: Exception):
        ''' handles errors that have no error handlers assigned '''
        # You custom error handling logic...

        msg = \
            "Failed to open a WebSocket connection." + \
            "\nSee server log for more information.\n"
        if request.method == "GET" and request.path == "/" and str(exception) == msg:
            return text(
                "You cannot access a WebSocket server directly. "
                "You need a WebSocket client."
            )

        return super().default(request, exception)


app = Sanic(__name__)
app.error_handler = CustomErrorHandler()
# FIXME: The Client is not Responsing to Websocket pings
app.config.WEBSOCKET_PING_INTERVAL = 0
# FIXME: Add UVLOOP support for alpine pypy
if getenv("SANIC_NO_UVLOOP"):
    app.config.USE_UVLOOP = False

actions = {}

# add all actions from default action set
for method in dir(Actions):
    if not method.startswith('__'):
        actions[method] = getattr(Actions, method)
logger = setup_logging()

trusted_proxies = getenv("TRUSTED_PROXIES")

# pylint: disable-next=invalid-name
proxies = None

if trusted_proxies is not None:
    proxies = []
    for proxy in trusted_proxies.split(","):
        proxies.append(proxy)

# TODO: only print once

if which(FFMPEG_PATH) is None:
    logger.warning("FFmpeg not found.")

if which(SANJUUNI_PATH) is None:
    logger.warning("Sanjuuni not found.")


@app.websocket("/")
# pylint: disable-next=invalid-name
async def wshandler(request: Request, ws: Websocket):
    """Handels web-socket requests"""
    client_ip = get_client_ip(request, proxies)
    if NO_COLOR:
        prefix = f"[{client_ip}] "
    else:
        prefix = f"{Foreground.BLUE}[{client_ip}]{RESET} "

    logger.info("%sConnected!", prefix)

    logger.debug(
        "%sMy headers are: %s",
        prefix,
        request.headers
    )

    while True:
        message = await ws.recv()
        logger.debug("%sMessage: %s", prefix, message)

        try:
            message: dict = load_json(message)
        except JSONDecodeError:
            logger.debug("%sFaild to parse Json", prefix)
            await ws.send(dumps({
                "action": "error",
                "message": "Faild to parse Json"
            }))

        if message.get("action") in actions:
            response = await actions[message.get("action")](message, ws)
            await ws.send(dumps(response))


def main() -> None:
    """
    Run all needed services
    """
    port = int(getenv("PORT", "5000"))
    host = getenv("HOST", "0.0.0.0")
    fast = not getenv("NO_FAST")

    app.run(host=host, port=port, fast=fast)


if __name__ == "__main__":
    main()
