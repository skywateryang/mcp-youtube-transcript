#  __init__.py
#
#  Copyright (c) 2025 Junpei Kawamoto
#
#  This software is released under the MIT License.
#
#  http://opensource.org/licenses/mit-license.php
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache, partial
from itertools import islice
from typing import AsyncIterator, Tuple
from typing import Final
from urllib.parse import urlparse, parse_qs

import humanize
import requests
from bs4 import BeautifulSoup
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context
from pydantic import Field, BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig, GenericProxyConfig, ProxyConfig
from yt_dlp import YoutubeDL
from yt_dlp.extractor.youtube import YoutubeIE

# Disable SSL warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@dataclass(frozen=True)
class AppContext:
    http_client: requests.Session
    ytt_api: YouTubeTranscriptApi
    dlp: YoutubeDL


@asynccontextmanager
async def _app_lifespan(_server: FastMCP, proxy_config: ProxyConfig | None) -> AsyncIterator[AppContext]:
    with requests.Session() as http_client, YoutubeDL(params={"quiet": True}, auto_init=False) as dlp:
        http_client.verify = False
        ytt_api = YouTubeTranscriptApi(http_client=http_client, proxy_config=proxy_config)
        dlp.add_info_extractor(YoutubeIE())
        yield AppContext(http_client=http_client, ytt_api=ytt_api, dlp=dlp)


class Transcript(BaseModel):
    """Transcript of a YouTube video."""

    title: str = Field(description="Title of the video")
    transcript: str = Field(description="Transcript of the video")
    next_cursor: str | None = Field(description="Cursor to retrieve the next page of the transcript", default=None)


class VideoInfo(BaseModel):
    """Video information."""

    title: str = Field(description="Title of the video")
    description: str = Field(description="Description of the video")
    uploader: str = Field(description="Uploader of the video")
    upload_date: datetime = Field(description="Upload date of the video")
    duration: str = Field(description="Duration of the video")


def _parse_time_info(date: int, timestamp: int, duration: int) -> Tuple[datetime, str]:
    parsed_date = datetime.strptime(str(date), "%Y%m%d").date()
    parsed_time = datetime.strptime(str(timestamp), "%H%M%S%f").time()
    upload_date = datetime.combine(parsed_date, parsed_time)
    duration_str = humanize.naturaldelta(timedelta(seconds=duration))
    return upload_date, duration_str


@lru_cache
def _get_transcript(ctx: AppContext, video_id: str, lang: str) -> Tuple[str, list[str]]:
    if lang == "en":
        languages = ["en"]
    else:
        languages = [lang, "en"]

    page = ctx.http_client.get(
        f"https://www.youtube.com/watch?v={video_id}", headers={"Accept-Language": ",".join(languages)}
    )
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    title = soup.title.string if soup.title and soup.title.string else "Transcript"

    transcripts = ctx.ytt_api.fetch(video_id, languages=languages)
    return title, [item.text for item in transcripts]


@lru_cache
def _get_video_info(ctx: AppContext, video_url: str) -> VideoInfo:
    res = ctx.dlp.extract_info(video_url, download=False)
    upload_date, duration = _parse_time_info(res["upload_date"], res["timestamp"], res["duration"])
    return VideoInfo(
        title=res["title"],
        description=res["description"],
        uploader=res["uploader"],
        upload_date=upload_date,
        duration=duration,
    )


def server(
    response_limit: int | None = None,
    webshare_proxy_username: str | None = None,
    webshare_proxy_password: str | None = None,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
) -> FastMCP:
    """Initializes the MCP server."""

    proxy_config: ProxyConfig | None = None
    if webshare_proxy_username and webshare_proxy_password:
        proxy_config = WebshareProxyConfig(webshare_proxy_username, webshare_proxy_password)
    elif http_proxy or https_proxy:
        proxy_config = GenericProxyConfig(http_proxy, https_proxy)

    mcp = FastMCP("Youtube Transcript", lifespan=partial(_app_lifespan, proxy_config=proxy_config))

    @mcp.tool()
    async def get_transcript(
        ctx: Context[ServerSession, AppContext],
        url: str = Field(description="The URL of the YouTube video"),
        lang: str = Field(description="The preferred language for the transcript", default="en"),
        next_cursor: str | None = Field(description="Cursor to retrieve the next page of the transcript", default=None),
    ) -> Transcript:
        """Retrieves the transcript of a YouTube video."""
        parsed_url = urlparse(url)
        if parsed_url.hostname == "youtu.be":
            video_id = parsed_url.path.lstrip("/")
        else:
            q = parse_qs(parsed_url.query).get("v")
            if q is None:
                raise ValueError(f"couldn't find a video ID from the provided URL: {url}.")
            video_id = q[0]

        title, transcripts = _get_transcript(ctx.request_context.lifespan_context, video_id, lang)

        if response_limit is None or response_limit <= 0:
            return Transcript(title=title, transcript="\n".join(transcripts))

        res = ""
        cursor = None
        for i, line in islice(enumerate(transcripts), int(next_cursor or 0), None):
            if len(res) + len(line) + 1 > response_limit:
                cursor = str(i)
                break
            res += f"{line}\n"

        return Transcript(title=title, transcript=res[:-1], next_cursor=cursor)

    @mcp.tool()
    def get_video_info(
        ctx: Context[ServerSession, AppContext],
        url: str = Field(description="The URL of the YouTube video"),
    ) -> VideoInfo:
        """Retrieves the video information."""
        return _get_video_info(ctx.request_context.lifespan_context, url)

    return mcp


__all__: Final = ["server", "Transcript", "VideoInfo"]
