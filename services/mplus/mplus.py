import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from colors import bcolors
import icons
from download_confirm import confirm_download
from filename_utils import safe_windows_filename
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import append_downloader_proxy, mask_proxy_command


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVICE_NAME = "Maori+"
SERVICE_TAG = "MPLUS"
BASE_URL = "https://www.maoriplus.co.nz"
BRIGHTCOVE_ACCOUNT = "1614493167001"
BRIGHTCOVE_POLICY = (
    "BCpkADawqM1udjJrOfsrwbRGnCtsbzTuIG7vwdI0uE1-lF_MQrHzkfE6KOqZA0jO_"
    "JK8DXOHBffQJVqt4sPTqtyOdPP5jRtTejf-wIsus0mvKR8nkuCgrvjmFLooMBrVlyi"
    "Cm1M4FHwZr037FeR1ExlEFGE8Ch55K88adA"
)
TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "temp"))
EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "export"))

DOWNLOAD_DIR = None
WVD_DEVICE_PATH = None
N_M3U8DL = "N_m3u8DL-RE"

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-NZ,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}

HTML_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BRIGHTCOVE_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/json;pk=" + BRIGHTCOVE_POLICY,
    "BCOV-Policy": BRIGHTCOVE_POLICY,
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}

session = requests.Session()
session.headers.update(DEFAULT_HEADERS)
session.verify = False
console = Console()


@dataclass
class Metadata:
    title: str = "Unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    video_id: Optional[str] = None
    date_aired: str = ""
    description: str = ""
    thumbnail: str = ""
    drm_enabled: bool = False
    media_type: str = "episode"


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    pssh: Optional[str] = None
    metadata: Metadata = field(default_factory=Metadata)
    subtitles: list = field(default_factory=list)


@dataclass
class EpisodeItem:
    url: str
    title: str = "Unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    video_id: Optional[str] = None
    date_aired: str = ""
    description: str = ""
    thumbnail: str = ""
    drm_enabled: bool = False


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def absolute_url(value):
    if not value:
        return ""
    return urljoin(BASE_URL, value)


def fetch_html(url):
    response = session.get(url, headers=HTML_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def normalise_url(input_url):
    input_url = input_url.strip()
    if input_url.startswith("/"):
        return BASE_URL + input_url

    parsed = urlparse(input_url)
    if parsed.netloc.lower() not in {"maoriplus.co.nz", "www.maoriplus.co.nz"}:
        raise ValueError("Expected a maoriplus.co.nz URL.")

    return input_url


def decode_next_flight_payload(script_text):
    match = re.fullmatch(
        r"\s*self\.__next_f\.push\((\[.*\])\)\s*",
        script_text or "",
        re.DOTALL,
    )
    if not match:
        return None

    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
        return value[1]
    return None


def next_flight_payloads(html_text):
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html_text, re.DOTALL | re.IGNORECASE):
        payload = decode_next_flight_payload(match.group(1))
        if payload:
            yield payload


def raw_decode_object_after(payload, marker_pattern):
    decoder = json.JSONDecoder()
    marker = re.search(marker_pattern, payload)
    if not marker:
        return None

    try:
        value, _ = decoder.raw_decode(payload[marker.end():])
    except json.JSONDecodeError:
        return None

    return value if isinstance(value, dict) else None


def extract_show_data(html_text):
    for payload in next_flight_payloads(html_text):
        if '"brightcoveId"' not in payload or '"seasons"' not in payload:
            continue
        show_data = raw_decode_object_after(payload, r'"show"\s*:\s*')
        if show_data and show_data.get("seasons"):
            return show_data
    raise ValueError("Could not locate the Maori+ show catalogue in the page data.")


def fetch_show_data(show_url):
    return extract_show_data(fetch_html(show_url))


def get_meta_content(html_text, name):
    patterns = [
        rf'<meta\s+name="{re.escape(name)}"\s+content="([^"]*)"',
        rf'<meta\s+property="{re.escape(name)}"\s+content="([^"]*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.IGNORECASE)
        if match:
            return clean_text(match.group(1).replace("&#x27;", "'").replace("&amp;", "&"))
    return ""


def format_air_date(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%d %B %Y %I:%M %p")
    except ValueError:
        return str(value)


def select_thumbnail(video):
    thumbnail = video.get("thumbnail") or {}
    sizes = thumbnail.get("sizes") or {}
    selected = sizes.get("small") or sizes.get("large") or thumbnail
    return absolute_url(selected.get("url"))


def season_number(season):
    episodes = season.get("episodes") or []
    for episode_wrapper in episodes:
        video = episode_wrapper.get("video") or {}
        number = video.get("seasonNumber")
        if isinstance(number, int):
            return number

    text = season.get("displayTitle") or season.get("title") or season.get("slug") or ""
    match = re.search(r"season[- ]+(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def parse_show_url(video_url):
    parsed = urlparse(normalise_url(video_url))
    match = re.search(r"/show/([^/?#]+)", parsed.path, re.IGNORECASE)
    if not match:
        raise ValueError("Could not extract the Maori+ show slug from the URL.")
    return f"{BASE_URL}/show/{match.group(1)}", match.group(1)


def build_episode_item(show_title, show_slug, season, episode_wrapper):
    video = episode_wrapper.get("video") or episode_wrapper
    video_id = str(video.get("brightcoveId") or "").strip()
    ep_number = video.get("episodeNumber")
    ep_number = ep_number if isinstance(ep_number, int) else None
    fallback_season = season_number(season)
    current_season = video.get("seasonNumber")
    current_season = current_season if isinstance(current_season, int) else fallback_season
    season_slug = season.get("slug") or f"season-{current_season}"
    episode_slug = video.get("slug") or (f"episode-{ep_number}" if ep_number is not None else video_id)
    episode_name = clean_text(video.get("title")) or (f"Episode {ep_number}" if ep_number is not None else "Episode")

    return EpisodeItem(
        url=f"{BASE_URL}/show/{show_slug}/{season_slug}/{episode_slug}/play",
        title=show_title,
        season=current_season or None,
        episode=ep_number,
        episode_title=episode_name,
        video_id=video_id,
        date_aired=format_air_date(video.get("publishDate")),
        description=clean_text(video.get("synopsis")),
        thumbnail=select_thumbnail(video),
        drm_enabled=bool(video.get("drmEnabled")),
    )


def collect_episode_items(series_url, show_progress=True):
    show_url, show_slug = parse_show_url(series_url)
    show_data = fetch_show_data(show_url)
    show_title = clean_text(show_data.get("title")) or show_slug.replace("-", " ").title()

    items = []
    seen_video_ids = set()
    for season_wrapper in show_data.get("seasons") or []:
        season = season_wrapper.get("season") or season_wrapper
        for episode_wrapper in season.get("episodes") or []:
            item = build_episode_item(show_title, show_slug, season, episode_wrapper)
            if not item.video_id or item.video_id in seen_video_ids:
                continue
            seen_video_ids.add(item.video_id)
            items.append(item)

    items.sort(key=episode_sort_key)
    return items


def collect_movie_item(video_url):
    html_text = fetch_html(video_url)
    video_id_match = re.search(r'brightcoveId\\?":\\?"?(\d+)', html_text)
    if not video_id_match:
        raise ValueError("Could not locate a Brightcove ID in the Maori+ page data.")

    title = ""
    title_match = re.search(r'"title\\?":\\?"([^"\\]+)"[^{}]{0,400}"type\\?":\\?"movie', html_text)
    if title_match:
        title = title_match.group(1)
    if not title:
        page_title = get_meta_content(html_text, "twitter:title") or get_meta_content(html_text, "og:title")
        title = re.sub(r"^Watch\s+|\s+\|\s+.*$", "", page_title).strip()

    description = get_meta_content(html_text, "description") or get_meta_content(html_text, "og:description")
    drm_match = re.search(r'drmEnabled\\?":(true|false)', html_text)

    return EpisodeItem(
        url=normalise_url(video_url),
        title=clean_text(title) or "Movie",
        episode_title=None,
        video_id=video_id_match.group(1),
        description=description,
        drm_enabled=bool(drm_match and drm_match.group(1) == "true"),
    )


def collect_episode_item(video_url):
    if is_movie_url(video_url):
        return collect_movie_item(video_url)

    show_url, show_slug = parse_show_url(video_url)
    path = urlparse(video_url).path.rstrip("/")
    items = collect_episode_items(show_url, show_progress=False)
    for item in items:
        if urlparse(item.url).path.rstrip("/") == path:
            return item

    match = re.search(r"/season-(\d+)/episode-(\d+)", path, re.IGNORECASE)
    if match:
        wanted = (int(match.group(1)), int(match.group(2)))
        for item in items:
            if (item.season, item.episode) == wanted:
                return item

    raise ValueError(f"Could not find this episode in the Maori+ show catalogue: {show_slug}")


def metadata_from_item(item, media_type="episode"):
    return Metadata(
        title=item.title,
        season=item.season,
        episode=item.episode,
        episode_title=item.episode_title,
        video_id=item.video_id,
        date_aired=item.date_aired,
        description=item.description,
        thumbnail=item.thumbnail,
        drm_enabled=item.drm_enabled,
        media_type=media_type,
    )


def extract_video_id(video_url):
    return collect_episode_item(video_url).video_id


def search_metadata(video_url, video_id=None):
    item = collect_episode_item(video_url)
    metadata = metadata_from_item(item, "movie" if is_movie_url(video_url) else "episode")
    if video_id and not metadata.video_id:
        metadata.video_id = video_id
    return metadata


def is_movie_url(video_url):
    return re.search(r"/movie/[^/?#]+", urlparse(normalise_url(video_url)).path, re.IGNORECASE) is not None


def is_episode_url(video_url):
    path = urlparse(normalise_url(video_url)).path.rstrip("/")
    return bool(
        re.search(r"/movie/[^/]+$", path, re.IGNORECASE)
        or re.search(r"/show/[^/]+/season-[^/]+/episode-[^/]+/play$", path, re.IGNORECASE)
    )


def get_playback_json(video_id):
    playback_url = f"https://edge.api.brightcove.com/playback/v1/accounts/{BRIGHTCOVE_ACCOUNT}/videos/{video_id}"
    response = session.get(playback_url, headers=BRIGHTCOVE_HEADERS, timeout=30)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list) and data and data[0].get("error_code"):
        raise ConnectionError(f"Brightcove playback error: {data[0].get('error_subcode') or data[0].get('message')}")
    return data


def source_src(source):
    return source.get("src") or source.get("streaming_src")


def get_widevine_license(source):
    key_systems = source.get("key_systems") or {}
    widevine = key_systems.get("com.widevine.alpha") or {}
    return widevine.get("license_url")


def subtitle_extension(track, content):
    mime_type = (track.get("mime_type") or "").lower()
    path = urlparse(track.get("src") or "").path.lower()
    text = content.lstrip("\ufeff").lstrip()

    if text.startswith("WEBVTT") or "webvtt" in mime_type or path.endswith(".vtt"):
        return "srt"
    if "ttml" in mime_type or path.endswith((".ttml", ".dfxp")) or text.startswith("<tt"):
        return "ttml"
    if path.endswith(".srt") or re.search(r"(?m)^\d+\s*\n\d{2}:\d{2}:\d{2},\d{3}\s+-->", text):
        return "srt"
    return "srt"


def subtitle_has_real_cues(content):
    text = content.lstrip("\ufeff").strip()
    if not text:
        return False

    if text.startswith("WEBVTT"):
        return "-->" in text
    if text.startswith("<") and "<tt" in text[:300].lower():
        return bool(re.search(r"\bbegin\s*=", text, re.IGNORECASE))
    return bool(re.search(r"(?m)^\d+\s*\n\d{2}:\d{2}:\d{2},\d{3}\s+-->", text))


def get_valid_external_subtitles(brightcove_response):
    subtitles = []
    seen_urls = set()

    for track in brightcove_response.get("text_tracks") or []:
        src = track.get("src")
        kind = (track.get("kind") or "").lower()
        label = (track.get("label") or "").lower()
        language = (track.get("srclang") or "").strip().lower()

        if not src or src in seen_urls:
            continue
        if kind not in {"captions", "subtitles"}:
            continue
        if label in {"thumbnail", "thumbnails"}:
            continue

        try:
            response = session.get(src, headers=BRIGHTCOVE_HEADERS, timeout=20)
            response.raise_for_status()
            content = response.text
        except Exception:
            continue

        if not subtitle_has_real_cues(content):
            continue

        seen_urls.add(src)
        subtitles.append({
            "url": src,
            "language": language or "und",
            "label": track.get("label") or language or "Subtitle",
            "kind": kind,
            "extension": subtitle_extension(track, content),
            "content": content,
        })

    return subtitles


def get_playback_info(video_url, metadata):
    playback_json = get_playback_json(metadata.video_id)
    sources = playback_json.get("sources") or []
    subtitles = get_valid_external_subtitles(playback_json)
    hls_source = None
    dash_source = None

    for source in sources:
        src = source_src(source)
        if not src:
            continue

        source_type = (source.get("type") or "").lower()
        container = (source.get("container") or "").lower()
        if "mpegurl" in source_type or src.split("?", 1)[0].lower().endswith(".m3u8"):
            hls_source = hls_source or source
        elif "dash" in source_type or container == "mpd" or src.split("?", 1)[0].lower().endswith(".mpd"):
            if "playready" not in src.lower():
                dash_source = dash_source or source

    if metadata.drm_enabled and dash_source:
        manifest_url = source_src(dash_source)
        return PlaybackInfo(
            manifest_url=manifest_url,
            manifest_type="mpd",
            license_url=get_widevine_license(dash_source),
            metadata=metadata,
            subtitles=subtitles,
        )

    if hls_source:
        return PlaybackInfo(
            manifest_url=source_src(hls_source),
            manifest_type="m3u8",
            metadata=metadata,
            subtitles=subtitles,
        )

    if dash_source:
        manifest_url = source_src(dash_source)
        return PlaybackInfo(
            manifest_url=manifest_url,
            manifest_type="mpd",
            license_url=get_widevine_license(dash_source),
            metadata=metadata,
            subtitles=subtitles,
        )

    raise ValueError("No DASH or HLS manifest was found in the Brightcove playback response.")


def episode_sort_key(item):
    return (
        item.season if item.season is not None else 9999,
        item.episode if item.episode is not None else 9999,
        item.video_id or item.url,
    )


def episode_series_number(item):
    return item.season


def episode_number(item):
    return item.episode


def episode_tree_label(item):
    number = episode_number(item)
    title = item.episode_title or item.title or item.url
    return str(number).zfill(2) if number is not None else "-", title


def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in sorted(episode_items, key=episode_sort_key):
        season = episode_series_number(item)
        series_label = f"Series {season}" if season is not None else "Episodes"
        grouped.setdefault(series_label, []).append(item)
    return grouped


def series_group_sort_key(label):
    match = re.search(r"\d+", label)
    return int(match.group(0)) if match else 0


def print_series_rule(service_label, series_title):
    terminal_width = shutil.get_terminal_size((88, 20)).columns
    title = f" {service_label}: {series_title} "
    rule_width = max(terminal_width, len(title) + 4)
    left_width = max((rule_width - len(title)) // 2, 0)
    right_width = max(rule_width - len(title) - left_width, 0)
    print(
        f"{bcolors.LIGHTBLUE}{'-' * left_width}{bcolors.ENDC}"
        f" {bcolors.LIGHTBLUE}{service_label}: {bcolors.ENDC}{series_title} "
        f"{bcolors.LIGHTBLUE}{'-' * right_width}{bcolors.ENDC}"
    )


def parse_selector_part(selector_part):
    match = re.fullmatch(r"s(?P<season>\d{2}|\d{4})(?:e(?P<episode>\d{2}))?", selector_part)
    if not match:
        raise ValueError(
            "Download selector must be sXXeXX, sXXXXeXX, sXX, sXXXX, or a matching range. "
            "Examples: s01e01, s2026e01, s01, s2026, s01e03-s02e02, s01-s03"
        )
    return {
        "season": int(match.group("season")),
        "episode": int(match.group("episode")) if match.group("episode") else None,
    }


def parse_download_selector(selector):
    selector = str(selector or "").strip().lower()
    if "-" not in selector:
        part = parse_selector_part(selector)
        return {
            "type": "single_episode" if part["episode"] is not None else "single_season",
            "start": part,
            "end": part,
        }

    range_parts = selector.split("-", 1)
    if not range_parts[0] or not range_parts[1]:
        raise ValueError("Download range must include both start and end selectors.")

    start = parse_selector_part(range_parts[0])
    end = parse_selector_part(range_parts[1])
    start_has_episode = start["episode"] is not None
    end_has_episode = end["episode"] is not None
    if start_has_episode != end_has_episode:
        raise ValueError("Download range must use two episode selectors or two season selectors.")

    if start_has_episode:
        if (start["season"], start["episode"]) > (end["season"], end["episode"]):
            raise ValueError("Download episode range start must be before the end selector.")
        return {"type": "episode_range", "start": start, "end": end}

    if start["season"] > end["season"]:
        raise ValueError("Download season range start must be before the end selector.")
    return {"type": "season_range", "start": start, "end": end}


def format_selector_part(part):
    season = part["season"]
    season_label = f"s{season:04d}" if season >= 1000 else f"s{season:02d}"
    if part["episode"] is not None:
        return f"{season_label}e{part['episode']:02d}"
    return season_label


def format_download_selector(parsed_selector):
    if parsed_selector["start"] == parsed_selector["end"]:
        return format_selector_part(parsed_selector["start"])
    return f"{format_selector_part(parsed_selector['start'])}-{format_selector_part(parsed_selector['end'])}"


def format_queue_selector(season, episode=None):
    season_label = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
    if episode is not None:
        return f"{season_label}E{episode:02d}"
    return season_label


def warn_if_partial_range_match(parsed_selector, selected):
    if parsed_selector["type"] == "episode_range":
        requested_start = (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
        requested_end = (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
        matched_start = (episode_series_number(selected[0]), episode_number(selected[0]))
        matched_end = (episode_series_number(selected[-1]), episode_number(selected[-1]))
        if matched_start > requested_start or matched_end < requested_end:
            matched_label = f"{format_queue_selector(*matched_start)}-{format_queue_selector(*matched_end)}"
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Requested range {format_download_selector(parsed_selector)} only matched {matched_label}.{bcolors.ENDC}")

    if parsed_selector["type"] == "season_range":
        requested_start = parsed_selector["start"]["season"]
        requested_end = parsed_selector["end"]["season"]
        matched_seasons = sorted({episode_series_number(item) for item in selected if episode_series_number(item) is not None})
        if matched_seasons and (matched_seasons[0] > requested_start or matched_seasons[-1] < requested_end):
            matched_label = f"{format_queue_selector(matched_seasons[0])}-{format_queue_selector(matched_seasons[-1])}"
            print(f"{icons.ICON_WARNING} {bcolors.WARNING}Requested range {format_download_selector(parsed_selector)} only matched seasons {matched_label}.{bcolors.ENDC}")


def select_episode_items(series_url, selector):
    parsed_selector = parse_download_selector(selector)
    episode_items = collect_episode_items(series_url, show_progress=False)
    selected = []
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_number(item)
        if season is None or episode is None:
            continue

        if parsed_selector["type"] == "single_episode":
            keep = season == parsed_selector["start"]["season"] and episode == parsed_selector["start"]["episode"]
        elif parsed_selector["type"] == "single_season":
            keep = season == parsed_selector["start"]["season"]
        elif parsed_selector["type"] == "episode_range":
            keep = (
                (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
                <= (season, episode)
                <= (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
            )
        else:
            keep = parsed_selector["start"]["season"] <= season <= parsed_selector["end"]["season"]

        if keep:
            selected.append(item)

    if not selected:
        raise ValueError(f"No Maori+ episodes found for selector {format_download_selector(parsed_selector)}.")

    selected.sort(key=episode_sort_key)
    warn_if_partial_range_match(parsed_selector, selected)
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.LIGHTBLUE}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_number(item)
        selector = format_queue_selector(season, episode) if season is not None and episode is not None else item.video_id or item.url
        _, title = episode_tree_label(item)
        print(f"{selector} {title}")


def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}No Maori+ episodes found.{bcolors.ENDC}")
        return

    tree_style = "grey70"
    label_style = "bold grey70"
    header_style = "bright_blue"
    series_title = episode_items[0].title or SERVICE_NAME
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)

    console.print(Rule(Text.assemble(("Maori+ Series: ", f"bold {header_style}"), (series_title, "bold white")), style=header_style))
    console.print()
    console.print(
        Text.assemble(
            (f"{len(group_labels)} Seasons", label_style),
            (f",  {series_summary}" if series_summary else "", "white"),
        )
    )

    for group_index, label in enumerate(group_labels):
        if group_index > 0:
            console.print(Text("│", style=tree_style))

        group_is_last = group_index == len(group_labels) - 1
        group_branch = "└─" if group_is_last else "├─"
        group_child_prefix = "   " if group_is_last else "│  "
        group_episodes = grouped_items[label]
        console.print(Text.assemble((f"{group_branch} ", tree_style), (f"{label}: ", label_style), (f"{len(group_episodes)} episodes", "white")))

        for index, item in enumerate(group_episodes):
            is_last = index == len(group_episodes) - 1
            branch = "└─" if is_last else "├─"
            url_branch = "  " if is_last else "│ "
            number_label, title = episode_tree_label(item)
            console.print(
                Text.assemble(
                    (group_child_prefix, tree_style),
                    (f"{branch} ", tree_style),
                    (f"{number_label}. ", label_style),
                    (title or "-", "white"),
                )
            )
            console.print(
                Text.assemble(
                    (group_child_prefix, tree_style),
                    (f"{url_branch} ", tree_style),
                    (item.url or "-", "bright_blue"),
                )
            )


def save_episode_list_json(show_slug, episode_items):
    os.makedirs(TEMP_DIR, exist_ok=True)
    output_path = os.path.join(TEMP_DIR, f"mplus_{safe_windows_filename(show_slug)}_episodes.json")
    episode_details = []
    episode_summary = []

    for item in sorted(episode_items, key=episode_sort_key):
        title_label = f"Season {item.season} Episode {item.episode}: {item.episode_title}"
        row = {
            "Video URL": item.url,
            "Video ID": item.video_id,
            "Show Title": item.title,
            "Title": title_label,
            "Date Aired": item.date_aired,
            "Description": item.description,
            "Thumbnail": item.thumbnail,
            "Season": item.season,
            "Episode": item.episode,
        }
        episode_details.append(row)
        episode_summary.append(f"{item.title} {title_label} ID: {item.video_id}")

    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(
            {"Episode Summary": episode_summary, "Episode Details": episode_details},
            output_file,
            ensure_ascii=False,
            indent=4,
        )
    return output_path


def export_episode_list_text(show_slug, episode_items):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(EXPORT_DIR, f"mplus_{safe_windows_filename(show_slug)}_export_{timestamp}.txt")

    with open(output_path, "w", encoding="utf-8") as file:
        for item in sorted(episode_items, key=episode_sort_key):
            selector = format_queue_selector(item.season, item.episode) if item.season is not None and item.episode is not None else "-"
            file.write(f"{selector} {item.episode_title or item.title}\n{item.url}\n\n")

    return output_path


def list_show_episodes(series_url, export_list=False):
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    show_url, show_slug = parse_show_url(series_url)
    episode_items = collect_episode_items(show_url, show_progress=False)
    output_path = save_episode_list_json(show_slug, episode_items)

    try:
        console.print()
        list_episode_items(episode_items)
        print(f"\n{bcolors.OKGREEN}{icons.ICON_SUCCESS} Found {len(episode_items)} episode(s){bcolors.ENDC}")
        if export_list:
            export_path = export_episode_list_text(show_slug, episode_items)
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Exported list: {export_path}{bcolors.ENDC}")
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)


def get_pssh_from_manifest(manifest_url):
    response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    ns_mpd = "{urn:mpeg:dash:schema:mpd:2011}"
    ns_cenc = "{urn:mpeg:cenc:2013}"
    widevine_uuid = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"

    for content_protection in root.findall(".//" + ns_mpd + "ContentProtection"):
        scheme = (content_protection.attrib.get("schemeIdUri") or "").lower()
        if widevine_uuid in scheme:
            pssh_el = content_protection.find(ns_cenc + "pssh")
            if pssh_el is not None and pssh_el.text:
                pssh_data = pssh_el.text.strip()
                base64.b64decode(pssh_data)
                return pssh_data

    for pssh_el in root.findall(".//" + ns_cenc + "pssh"):
        if pssh_el.text:
            pssh_data = pssh_el.text.strip()
            base64.b64decode(pssh_data)
            return pssh_data

    raise ValueError("PSSH not found in the manifest.")


def build_license_headers(metadata):
    return {
        "Content-Type": "application/octet-stream",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
    }


def post_license_challenge(license_url, challenge, metadata):
    response = session.post(license_url, headers=build_license_headers(metadata), data=challenge, timeout=30)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}HTTPError: {exc}{bcolors.ENDC}")
        print(f"{icons.ICON_INFO} Response Headers: {response.headers}")
        print(f"{icons.ICON_INFO} Response Text: {response.text[:2000]}")
        raise
    return response.content


def get_keys(pssh, license_url, metadata):
    try:
        pssh = PSSH(pssh)
    except (binascii.Error, ValueError) as exc:
        print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Could not parse PSSH: {exc}{bcolors.ENDC}")
        return []

    device = Device.load(WVD_DEVICE_PATH)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    try:
        challenge = cdm.get_license_challenge(session_id, pssh)
        licence = post_license_challenge(license_url, challenge, metadata)
        cdm.parse_license(session_id, licence)
        return [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == "CONTENT"]
    finally:
        cdm.close(session_id)


def parse_m3u8_attributes(value):
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value):
        attrs[match.group(1)] = match.group(2).strip().strip('"')
    return attrs


def get_m3u8_streams(m3u8_url):
    response = session.get(m3u8_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    streams = []
    last_attrs = None

    for line in response.text.splitlines():
        value = line.strip()
        if value.startswith("#EXT-X-STREAM-INF:"):
            last_attrs = parse_m3u8_attributes(value.split(":", 1)[1])
            continue

        if not last_attrs or not value or value.startswith("#"):
            continue

        bandwidth = last_attrs.get("BANDWIDTH") or last_attrs.get("AVERAGE-BANDWIDTH")
        resolution = last_attrs.get("RESOLUTION") or "-"
        codecs = last_attrs.get("CODECS") or "unknown codecs"
        bitrate = f"{int(bandwidth) // 1000} Kbps" if bandwidth and bandwidth.isdigit() else "unknown bitrate"
        streams.append({"type": "Vid", "resolution": resolution, "bitrate": bitrate, "codec": codecs, "lang": "-"})
        last_attrs = None

    return sorted(streams, key=stream_sort_key)


def get_mpd_streams(mpd_url):
    response = session.get(mpd_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    streams = []

    for adaptation_set in root.iter():
        if not adaptation_set.tag.endswith("AdaptationSet"):
            continue

        content_type = (adaptation_set.attrib.get("contentType") or "").lower()
        mime_type = (adaptation_set.attrib.get("mimeType") or "").lower()
        lang = adaptation_set.attrib.get("lang") or "-"

        for representation in adaptation_set:
            if not representation.tag.endswith("Representation"):
                continue

            rep_mime_type = (representation.attrib.get("mimeType") or "").lower()
            rep_content = f"{content_type} {mime_type} {rep_mime_type}"
            codecs = representation.attrib.get("codecs") or adaptation_set.attrib.get("codecs") or "unknown codecs"
            bandwidth = representation.attrib.get("bandwidth")
            bitrate = f"{int(bandwidth) // 1000} Kbps" if bandwidth and bandwidth.isdigit() else "unknown bitrate"
            width = representation.attrib.get("width")
            height = representation.attrib.get("height")

            if "video" in rep_content or width or height:
                stream_type = "Vid"
                resolution = f"{width or '?'}x{height or '?'}"
            elif "audio" in rep_content:
                stream_type = "Aud"
                resolution = "-"
            elif "text" in rep_content or "subtitle" in rep_content or codecs.lower() in {"stpp", "wvtt"}:
                stream_type = "Sub"
                resolution = "-"
            else:
                continue

            streams.append({"type": stream_type, "resolution": resolution, "bitrate": bitrate, "codec": codecs, "lang": lang})

    return sorted(streams, key=stream_sort_key)


def stream_sort_key(stream):
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    height = 0
    bitrate = 0

    resolution_match = re.search(r"x(\d+)", stream["resolution"])
    if resolution_match:
        height = int(resolution_match.group(1))

    bitrate_match = re.search(r"(\d+)", stream["bitrate"])
    if bitrate_match:
        bitrate = int(bitrate_match.group(1))

    return (type_order.get(stream["type"], 9), -height, -bitrate, stream["codec"], stream["lang"])


def print_streams(streams):
    if not streams:
        print(f"\n{bcolors.WARNING}No stream variants found.{bcolors.ENDC}")
        return

    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    header = f"  {'#':>2}  {'Type':<4} {'Resolution':<10} {'Bitrate':<16} {'Codec':<18} {'Lang':<5}"
    divider = f"  {'-' * 2}  {'-' * 4} {'-' * 10} {'-' * 16} {'-' * 18} {'-' * 5}"
    print(header)
    print(divider)
    for index, stream in enumerate(streams, start=1):
        print(
            f"  {index:>2}  "
            f"{stream['type']:<4} "
            f"{stream['resolution']:<10} "
            f"{stream['bitrate']:<16} "
            f"{stream['codec']:<18} "
            f"{stream['lang']:<5}"
        )


def print_external_subtitles(subtitles):
    if not subtitles:
        return

    print(f"\n{bcolors.YELLOW}External subtitles:{bcolors.ENDC}")
    header = f"  {'#':>2}  {'Lang':<5} {'Kind':<10} {'Format':<6} {'Label':<20}"
    divider = f"  {'-' * 2}  {'-' * 5} {'-' * 10} {'-' * 6} {'-' * 20}"
    print(header)
    print(divider)
    for idx, subtitle in enumerate(subtitles, start=1):
        print(
            f"  {idx:>2}  "
            f"{subtitle.get('language', '-'):<5} "
            f"{subtitle.get('kind', '-'):<10} "
            f"{subtitle.get('extension', '-'):<6} "
            f"{subtitle.get('label', '-'):<20}"
        )


def clean_info_episode_title(value):
    title = str(value or "").strip()
    title = re.sub(r"^Ep(?:isode)?\s+\d+\s*", "", title, flags=re.IGNORECASE)
    return title.strip(" -:") or None


def format_info_date(value):
    value = str(value or "").strip()
    if not value:
        return value

    for fmt in ("%d %B %Y %I:%M %p", "%d %B %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.strftime("%d %B %Y").lstrip("0")
        except ValueError:
            pass

    match = re.match(r"(\d{1,2} \w+ \d{4})", value)
    return match.group(1).lstrip("0") if match else value


def print_info_metadata(metadata):
    fields = [
        ("Show", metadata.title),
        ("Title", clean_info_episode_title(metadata.episode_title or metadata.title)),
        ("Date Aired", format_info_date(metadata.date_aired)),
        ("Description", metadata.description),
    ]
    visible_fields = [
        (label, str(value).strip())
        for label, value in fields
        if value and str(value).strip() and str(value).strip() != "Not Available"
    ]
    if not visible_fields:
        return

    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in visible_fields:
        print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")


def get_resolution(playback):
    try:
        if playback.manifest_type == "mpd":
            streams = get_mpd_streams(playback.manifest_url)
        else:
            streams = get_m3u8_streams(playback.manifest_url)
    except Exception:
        return "Unknown"

    heights = []
    for stream in streams:
        match = re.search(r"x(\d+)", stream["resolution"])
        if match:
            heights.append(int(match.group(1)))
    return f"{max(heights)}p" if heights else "Unknown"


def safe_name(value):
    value = clean_text(value).replace("'", "")
    value = re.sub(r"[\\/:*?\"<>|]", " ", value)
    value = re.sub(r"\s+", ".", value)
    return value.strip(".") or "Unknown"


def format_filename(metadata, resolution):
    title = safe_name(metadata.title)
    season_episode = ""
    if metadata.season is not None and metadata.episode is not None:
        season_episode = f"S{int(metadata.season):02}E{int(metadata.episode):02}"
    elif metadata.season is not None:
        season_episode = f"S{int(metadata.season):02}"

    parts = [title]
    if season_episode:
        parts.append(season_episode)
    elif metadata.episode_title:
        parts.append(safe_name(metadata.episode_title))
    parts.extend([resolution, SERVICE_TAG, "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, keys=None, mode="auto", quality=None, save_subs=False):
    subtitle_selector = "--select-subtitle all" if save_subs else "--drop-subtitle all"
    selectors = "" if mode == "interactive" else f"{video_selector(quality)} --select-audio best {subtitle_selector} "
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{DOWNLOAD_DIR}" --save-name "{filename}"'
    )

    if keys:
        command += " " + " ".join(f"--key {key}" for key in keys)

    return append_downloader_proxy(command)


def print_playback_details(playback, keys, command):
    label = "MPD URL" if playback.manifest_type == "mpd" else "M3U8 URL"
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")

    if playback.license_url:
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
    if playback.pssh:
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
    if keys:
        for key in keys:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
    elif playback.manifest_type == "mpd":
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys available - content may be encrypted{bcolors.ENDC}")

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(command))


def print_mplus_info(playback, filename, keys=None):
    label = "MPD URL" if playback.manifest_type == "mpd" else "M3U8 URL"
    print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{playback.manifest_url}")
    if playback.manifest_type == "mpd":
        if playback.license_url:
            print(f"{bcolors.RED}License URL: {bcolors.ENDC}{playback.license_url}")
        if playback.pssh:
            print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{playback.pssh}")
        for key in keys or []:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
        print_streams(get_mpd_streams(playback.manifest_url))
    else:
        print_streams(get_m3u8_streams(playback.manifest_url))
    print_external_subtitles(playback.subtitles or [])
    print_info_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{filename}.mkv")


def maybe_download(command, auto_download=False, auto_confirm=False):
    if confirm_download("Do you wish to download? Y or N: ", auto_confirm=auto_confirm, auto_download=auto_download):
        print(f"{icons.ICON_WAITING} {bcolors.OKBLUE}Downloading video...{bcolors.ENDC}")
        subprocess.run(command, shell=True)
        return
    print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


def process_video(video_url, mode="auto", auto_download=False, info=False, quality=None, auto_confirm=False, save_subs=False):
    if not info:
        print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    metadata = search_metadata(video_url)

    if not info:
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Fetching playback info...{bcolors.ENDC}")
    playback = get_playback_info(video_url, metadata)

    keys = []
    if playback.manifest_type == "mpd":
        if not playback.pssh:
            if not info:
                print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Extracting PSSH from manifest...{bcolors.ENDC}")
            try:
                playback.pssh = get_pssh_from_manifest(playback.manifest_url)
                if not info:
                    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}PSSH extracted{bcolors.ENDC}")
            except Exception as exc:
                if not info:
                    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Could not extract PSSH: {exc}{bcolors.ENDC}")
        if playback.license_url and playback.pssh:
            if not info:
                print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Getting decryption keys...{bcolors.ENDC}")
            keys = get_keys(playback.pssh, playback.license_url, metadata)
            if not info and keys:
                print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Retrieved {len(keys)} keys{bcolors.ENDC}")
            elif not info:
                print(f"{icons.ICON_WARNING} {bcolors.WARNING}No keys retrieved{bcolors.ENDC}")
    elif playback.manifest_type != "m3u8":
        raise ValueError(f"Unsupported manifest type: {playback.manifest_type}")

    resolution = get_resolution(playback)
    filename = format_filename(metadata, resolution)
    if info:
        print_mplus_info(playback, filename, keys)
        return

    filename = apply_quality_to_filename(filename, quality)
    command = build_download_command(playback, filename, keys, mode=mode, quality=quality, save_subs=save_subs)
    print_playback_details(playback, keys, command)

    maybe_download(command, auto_download=auto_download, auto_confirm=auto_confirm)


def info(video_url):
    if not is_episode_url(video_url):
        raise ValueError("Info mode requires a Maori+ episode/video URL, not a series URL.")
    process_video(video_url, info=True)


def download_selected_episodes(series_url, selector, downloads_path, wvd_device_path, quality=None, auto_confirm=False, save_subs=False):
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)

    if not confirm_download(f"\nDownload {len(episode_items)} episode(s)? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, start=1):
        _, title = episode_tree_label(item)
        print(f"\n{icons.ICON_INFO} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {title}{bcolors.ENDC}")
        main(item.url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, auto_download=True, quality=quality, auto_confirm=auto_confirm, save_subs=save_subs)


def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, auto_download=False, quality=None, auto_confirm=False, save_subs=False):
    global DOWNLOAD_DIR, WVD_DEVICE_PATH
    DOWNLOAD_DIR = downloads_path
    WVD_DEVICE_PATH = wvd_device_path

    if mode == "list":
        if is_episode_url(video_url):
            list_episode_items([collect_episode_item(video_url)])
        else:
            list_show_episodes(video_url, export_list)
        return

    if mode == "download":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.FAIL}Download selector mode requires a Maori+ series URL, not an episode/movie URL.{bcolors.ENDC}")
            return
        download_selected_episodes(video_url, download_selector, downloads_path, wvd_device_path, quality, auto_confirm, save_subs)
        return

    if mode == "info":
        info(video_url)
        return

    if is_episode_url(video_url):
        process_video(video_url, mode=mode, auto_download=auto_download, quality=quality, auto_confirm=auto_confirm, save_subs=save_subs)
        return

    print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
