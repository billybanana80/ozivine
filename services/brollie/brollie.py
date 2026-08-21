import html
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
import urllib3
import icons
from colors import bcolors
from download_confirm import confirm_download
from quality_utils import apply_quality_to_filename, video_selector
from services.proxy import append_downloader_proxy, mask_proxy_command


for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


SERVICE_NAME = "brollie"
BASE_URL = "https://watch.brollie.com.au"
MAZ_APP_ID = 845
MAZ_API_KEY = "ec4f1fb57daf7d4e57aafcd8b8bdc9d2"
MAZ_LOCALE_ID = 542
MAZ_LANGUAGE = "en"
MAZ_PLATFORM = "web"

SCRIPT_DIR = Path(__file__).resolve().parent
SAVE_PATH = Path(".")
EXPORT_DIR = Path(__file__).resolve().parents[2] / "export"
N_M3U8DL = "N_m3u8DL-RE"


session = requests.Session()

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}
session.headers.update(DEFAULT_HEADERS)


@dataclass
class Metadata:
    title: str = "Unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    video_id: Optional[str] = None
    slug: Optional[str] = None
    cid: Optional[str] = None
    item: dict = field(default_factory=dict)


@dataclass
class PlaybackInfo:
    manifest_url: str
    manifest_type: str
    license_url: Optional[str] = None
    pssh: Optional[str] = None
    keys: list = field(default_factory=list)
    metadata: Metadata = field(default_factory=Metadata)


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def format_info_date(value):
    value = clean_text(value)
    return value if value else "Unknown"


def format_bitrate(value):
    try:
        bitrate = int(float(value))
    except (TypeError, ValueError):
        return "-"
    return f"{bitrate / 1000000:.2f} Mbps" if bitrate >= 1000000 else f"{bitrate // 1000} Kbps"


def parse_attribute_list(value):
    return {
        match.group(1): match.group(2).strip().strip('"')
        for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value)
    }


def stream_sort_key(stream):
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    height_match = re.search(r"x(\d+)", stream.get("resolution") or "")
    height = int(height_match.group(1)) if height_match else 0
    bitrate_text = stream.get("bitrate") or ""
    bitrate_match = re.search(r"[\d.]+", bitrate_text)
    bitrate = float(bitrate_match.group()) if bitrate_match else 0
    if "Mbps" in bitrate_text:
        bitrate *= 1000
    return (type_order.get(stream.get("type"), 9), -height, -bitrate, stream.get("lang") or "")


def parse_hls_streams(manifest_text):
    streams = []
    pending_variant = None
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_attribute_list(line.split(":", 1)[1])
            continue
        if pending_variant is not None and line and not line.startswith("#"):
            attrs = pending_variant
            streams.append({
                "type": "Vid",
                "resolution": attrs.get("RESOLUTION") or "-",
                "bitrate": format_bitrate(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH")),
                "codec": attrs.get("CODECS") or "-",
                "lang": "-",
                "channels": "-",
            })
            pending_variant = None
            continue
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = parse_attribute_list(line.split(":", 1)[1])
        media_type = attrs.get("TYPE", "").upper()
        if media_type == "AUDIO":
            stream_type = "Aud"
        elif media_type in {"SUBTITLES", "CLOSED-CAPTIONS"}:
            stream_type = "Sub"
        else:
            continue
        streams.append({
            "type": stream_type,
            "resolution": "-",
            "bitrate": "-",
            "codec": "-",
            "lang": attrs.get("LANGUAGE") or "-",
            "channels": attrs.get("CHANNELS") or "-",
        })
    return sorted(streams, key=stream_sort_key)


def parse_dash_streams(manifest_text):
    try:
        root = ET.fromstring(manifest_text)
    except ET.ParseError as exc:
        raise ValueError(f"Unable to parse the DASH manifest: {exc}") from exc

    streams = []
    for adaptation in root.findall(".//{*}AdaptationSet"):
        adaptation_mime = clean_text(adaptation.get("mimeType"))
        adaptation_type = clean_text(adaptation.get("contentType"))
        adaptation_codec = clean_text(adaptation.get("codecs"))
        adaptation_lang = clean_text(adaptation.get("lang")) or "-"
        adaptation_channels = next(
            (
                clean_text(node.get("value"))
                for node in adaptation.findall("{*}AudioChannelConfiguration")
                if node.get("value")
            ),
            "",
        )
        for representation in adaptation.findall("{*}Representation"):
            mime_type = clean_text(representation.get("mimeType")) or adaptation_mime
            content_type = clean_text(representation.get("contentType")) or adaptation_type
            codec = clean_text(representation.get("codecs")) or adaptation_codec or "-"
            lang = clean_text(representation.get("lang")) or adaptation_lang
            width = clean_text(representation.get("width"))
            height = clean_text(representation.get("height"))
            channels = next(
                (
                    clean_text(node.get("value"))
                    for node in representation.findall("{*}AudioChannelConfiguration")
                    if node.get("value")
                ),
                adaptation_channels,
            )
            type_hint = f"{content_type} {mime_type} {codec}".lower()
            if "video" in type_hint:
                stream_type = "Vid"
            elif "audio" in type_hint:
                stream_type = "Aud"
            elif any(value in type_hint for value in ("text", "subtitle", "vtt", "ttml", "stpp", "wvtt")):
                stream_type = "Sub"
            else:
                continue
            streams.append({
                "type": stream_type,
                "resolution": f"{width}x{height}" if width and height else "-",
                "bitrate": format_bitrate(representation.get("bandwidth")),
                "codec": codec,
                "lang": lang,
                "channels": channels or "-",
            })
    return sorted(streams, key=stream_sort_key)


def parse_manifest_streams(manifest_text):
    if manifest_text.lstrip().startswith("#EXTM3U"):
        return parse_hls_streams(manifest_text), "HLS"
    return parse_dash_streams(manifest_text), "DASH"


def max_height_from_streams(streams, default="Unknown"):
    heights = []
    for stream in streams:
        if stream.get("type") != "Vid":
            continue
        match = re.search(r"x(\d+)", stream.get("resolution") or "")
        if match:
            heights.append(int(match.group(1)))
    return f"{max(heights)}p" if heights else default


def print_streams(streams):
    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    if not streams:
        print("No video, audio, or subtitle streams were found in the manifest.")
        return

    headings = ("#", "Type", "Resolution", "Bitrate", "Codec", "Lang", "Channels")
    rows = [
        (
            str(index),
            stream["type"],
            stream["resolution"],
            stream["bitrate"],
            stream["codec"],
            stream["lang"],
            stream["channels"],
        )
        for index, stream in enumerate(streams, start=1)
    ]
    widths = [
        min(max(len(headings[column]), *(len(row[column]) for row in rows)), 52)
        for column in range(len(headings))
    ]
    widths[0] = 3
    print("  ".join(f"{heading:<{widths[index]}}" for index, heading in enumerate(headings)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(f"{value[:widths[index]]:<{widths[index]}}" for index, value in enumerate(row)))


def print_episode_metadata(metadata):
    item = metadata.item or {}
    rows = [
        ("Show", clean_text(metadata.title)),
        ("Title", clean_text(metadata.episode_title or item.get("title"))),
        ("Date Aired", format_info_date(item.get("release_date") or item.get("air_date") or item.get("published_at"))),
        ("Description", clean_text(item.get("summary") or item.get("description") or item.get("overview"))),
    ]
    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        if value:
            print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")


def slug_from_url(video_url):
    parsed = urlparse(video_url.strip())
    source = parsed.fragment if parsed.fragment else parsed.path
    source = source.strip("/")
    match = re.search(r"^apps/\d+/(.+)$", source)
    if match:
        return match.group(1).strip("/")
    return source


def canonical_url(value):
    value = clean_text(value)
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return f"{BASE_URL}/apps/{MAZ_APP_ID}/{value.strip('/')}"


def extract_video_id(video_url):
    slug = slug_from_url(video_url)
    if slug:
        return slug.rsplit("/", 1)[-1]
    raise ValueError(f"{bcolors.RED}Could not extract video ID from URL.{bcolors.ENDC}")


def is_episode_url(video_url):
    slug = slug_from_url(canonical_url(video_url))
    return bool(re.search(r"/(?:season-\d+|ghost-season-[^/]+)/[^/]+$", slug, flags=re.IGNORECASE))


def is_series_url(video_url):
    slug = slug_from_url(canonical_url(video_url))
    if is_episode_url(video_url):
        return False
    try:
        feed = maz_item_feed(slug)
    except Exception:
        return False
    parent = feed.get("parent") or {}
    if is_playable_item(parent):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "menu"
        and re.search(r"/(?:season-\d+|ghost-season-[^/]+)(?:/)?$", clean_text(item.get("slug")), re.IGNORECASE)
        for item in feed.get("content") or []
    )


def parent_show_slug(episode_slug):
    match = re.match(r"(?P<show>.+?)/(?:season-\d+|ghost-season-[^/]+)/", episode_slug, flags=re.IGNORECASE)
    if not match:
        raise ValueError("Only Brollie TV episode URLs are supported for now.")
    return match.group("show").strip("/")


def parent_season_slug(episode_slug):
    match = re.match(r"(?P<season>.+?/(?:season-\d+|ghost-season-[^/]+))/", episode_slug, flags=re.IGNORECASE)
    if not match:
        raise ValueError("Only Brollie TV episode URLs are supported for now.")
    return match.group("season").strip("/")


def maz_item_feed(slug, locale_id=MAZ_LOCALE_ID, language=MAZ_LANGUAGE):
    content = []
    parent = {}
    page = 1

    while True:
        response = session.get(
            "https://api.maz.tv/v1/item_feeds/list",
            params={
                "device": "tv",
                "app_id": MAZ_APP_ID,
                "locale_id": locale_id,
                "language": language,
                "key": MAZ_API_KEY,
                "slug": slug,
                "page": page,
                "per_page": 100,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        content.extend(data.get("content") or [])
        parent = data.get("parent") or parent

        if data.get("last_page") or not data.get("content"):
            return {"content": content, "parent": parent}
        page += 1


def maz_episode_url(item):
    slug = clean_text(item.get("slug"))
    if slug:
        return f"{BASE_URL}/apps/{MAZ_APP_ID}/{slug.lstrip('/')}"
    return clean_text(item.get("shareLinkUrl"))


def maz_episode_number(item, fallback):
    if item.get("_feed_episode_number") not in (None, ""):
        return int(item["_feed_episode_number"]) if str(item["_feed_episode_number"]).isdigit() else item["_feed_episode_number"]
    for key in ("episodeNum", "episode_number", "episodeNumber"):
        value = item.get(key)
        if value not in (None, "", 0, "0"):
            return int(value) if str(value).isdigit() else value
    return fallback


def collect_episode_items(series_url):
    show_slug = slug_from_url(canonical_url(series_url))
    show_feed = maz_item_feed(show_slug)
    parent = show_feed.get("parent") or {}
    show_title = clean_text(parent.get("title") or parent.get("displayTitle"))
    if not show_title:
        show_title = show_slug.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()

    season_menus = [
        item for item in show_feed.get("content") or []
        if isinstance(item, dict)
        and item.get("type") == "menu"
        and re.search(r"/(?:season-\d+|ghost-season-[^/]+)(?:/)?$", clean_text(item.get("slug")), re.IGNORECASE)
    ]
    if not season_menus:
        raise RuntimeError("No Brollie seasons found for this URL.")
    season_menus.sort(key=lambda item: int(maz_season_number(item, 9999)))

    episodes = []
    seen = set()
    for season_menu in season_menus:
        season_number = maz_season_number(season_menu, 1)
        season_feed = maz_item_feed(clean_text(season_menu.get("slug")))
        season_items = [item for item in season_feed.get("content") or [] if is_playable_item(item)]
        for fallback_episode, item in enumerate(season_items, 1):
            item = dict(item)
            item.setdefault("seasonNum", season_number)
            item["_feed_episode_number"] = fallback_episode
            item_id = clean_text(item.get("identifier") or item.get("cid") or item.get("slug"))
            if item_id and item_id not in seen:
                seen.add(item_id)
                episodes.append(
                    {
                        "show_title": show_title,
                        "season": int(maz_season_number(item, season_number)),
                        "episode": int(maz_episode_number(item, fallback_episode)),
                        "title": clean_text(item.get("title")) or f"Episode {fallback_episode}",
                        "url": maz_episode_url(item),
                    }
                )

    if not episodes:
        raise RuntimeError("No Brollie episodes found for this URL.")

    return sorted(
        episodes,
        key=lambda item: (
            int(item.get("season") or 9999),
            int(item.get("episode") or 9999),
            clean_text(item.get("title")).lower(),
        ),
    )


def maz_season_number(item, fallback=1):
    for key in ("seasonNum", "season_number", "seasonNumber"):
        value = item.get(key)
        if value not in (None, ""):
            return int(value) if str(value).isdigit() else value
    slug = clean_text(item.get("slug"))
    match = re.search(r"/season-(\d+)(?:/|$)", slug, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if re.search(r"/ghost-season-[^/]+(?:/|$)", slug, re.IGNORECASE):
        return fallback
    return fallback


def is_playable_item(item):
    return isinstance(item, dict) and item.get("type") == "vid" and bool(clean_text(item.get("cid")))


def metadata_from_direct_item(item, video_id):
    return Metadata(
        title=clean_text(item.get("title")) or video_id.replace("-", " ").title(),
        season=None,
        episode=None,
        episode_title=None,
        video_id=clean_text(item.get("identifier") or item.get("cid")) or video_id,
        slug=clean_text(item.get("slug")),
        cid=clean_text(item.get("cid")),
        item=item,
    )


def metadata_title_from(show_feed, season_feed, episode_item):
    for container in (show_feed.get("parent"), season_feed.get("parent")):
        title = clean_text(container.get("title") or container.get("displayTitle")) if isinstance(container, dict) else ""
        if title and not re.match(r"season\s+\d+$|episodes$", title, flags=re.IGNORECASE):
            return title

    parent_titles = episode_item.get("parent_titles") or []
    if isinstance(parent_titles, list) and parent_titles:
        for parent_title in reversed(parent_titles):
            if isinstance(parent_title, dict):
                title = clean_text(parent_title.get("title") or parent_title.get("displayTitle"))
            else:
                title = clean_text(parent_title)
            if title and not re.match(
                r"season\s+\d+$|episodes$|tv shows$|home$|miniseries$",
                title,
                flags=re.IGNORECASE,
            ):
                return title

    show_slug = parent_show_slug(clean_text(episode_item.get("slug")))
    return show_slug.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()


def search_metadata(video_url, video_id):
    episode_slug = slug_from_url(video_url)
    if re.search(r"/(?:season-\d+|ghost-season-[^/]+)/", episode_slug, flags=re.IGNORECASE):
        season_slug = parent_season_slug(episode_slug)
        show_slug = parent_show_slug(episode_slug)

        show_feed = maz_item_feed(show_slug)
        season_feed = maz_item_feed(season_slug)
        season_items = [item for item in season_feed["content"] if item.get("type") == "vid"]

        for index, item in enumerate(season_items, 1):
            item_slug = clean_text(item.get("slug")).strip("/")
            if item_slug == episode_slug:
                season_number = maz_season_number(item, 1)
                show_title = metadata_title_from(show_feed, season_feed, item)
                return Metadata(
                    title=show_title,
                    season=season_number,
                    episode=index,
                    episode_title=clean_text(item.get("title")) or None,
                    video_id=clean_text(item.get("identifier") or item.get("cid")) or video_id,
                    slug=item_slug,
                    cid=clean_text(item.get("cid")),
                    item=item,
                )

        raise RuntimeError(f"Could not find Brollie episode metadata for slug: {episode_slug}")

    direct_feed = maz_item_feed(episode_slug)
    direct_parent = direct_feed.get("parent") or {}
    if is_playable_item(direct_parent):
        return metadata_from_direct_item(direct_parent, video_id)

    raise RuntimeError(f"Could not find Brollie playable metadata for slug: {episode_slug}")


def get_playback_info(video_url, metadata):
    if not metadata.cid:
        raise RuntimeError("Episode metadata did not include a Brollie content id.")

    payload = {
        "cid": metadata.cid,
        "progress": 0,
        "platform": MAZ_PLATFORM,
        "first_play": True,
        "key": MAZ_API_KEY,
        "app_id": MAZ_APP_ID,
        "language": MAZ_LANGUAGE,
        "locale_id": MAZ_LOCALE_ID,
    }
    response = session.post(
        "https://api.maz.tv/v1/streams/anonymous",
        json=payload,
        headers={**DEFAULT_HEADERS, "Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    manifest_url = data.get("url") or (data.get("files") or {}).get("m3u8")
    if not manifest_url:
        raise RuntimeError(f"Brollie did not return a playable stream for {metadata.episode_title or metadata.slug}.")

    manifest_type = clean_text(data.get("type")).lower()
    if not manifest_type:
        manifest_type = "m3u8" if ".m3u8" in manifest_url.lower() else "mp4"

    return PlaybackInfo(
        manifest_url=manifest_url,
        manifest_type=manifest_type,
        metadata=metadata,
    )


def get_hls_resolution(m3u8_url):
    response = session.get(m3u8_url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    resolutions = re.findall(r"RESOLUTION=\d+x(\d+)", response.text)
    if not resolutions:
        return "Unknown"
    return f"{max(int(height) for height in resolutions)}p"


def get_resolution(playback):
    if playback.manifest_type == "m3u8":
        return get_hls_resolution(playback.manifest_url)
    return "Unknown"


def fetch_manifest(manifest_url):
    try:
        response = session.get(manifest_url, headers=DEFAULT_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Brollie manifest: {exc}") from exc


def resolve_playback(video_url):
    video_url = canonical_url(video_url)
    video_id = extract_video_id(video_url)
    metadata = search_metadata(video_url, video_id)
    playback = get_playback_info(video_url, metadata)
    manifest_text = fetch_manifest(playback.manifest_url)
    streams, manifest_type = parse_manifest_streams(manifest_text)
    playback.manifest_type = manifest_type
    resolution = max_height_from_streams(streams)
    filename = format_filename(metadata, resolution)
    return {
        "playback": playback,
        "manifest_text": manifest_text,
        "manifest_type": manifest_type,
        "streams": streams,
        "resolution": resolution,
        "filename": filename,
    }


def episode_series_number(item):
    return int(item.get("season") or 1)


def episode_list_number(item):
    return int(item.get("episode") or 1)


def episode_tree_label(item):
    return str(episode_list_number(item)), clean_text(item.get("title")) or f"Episode {episode_list_number(item)}"


def group_episode_items_by_series(episode_items):
    grouped = {}
    for item in episode_items:
        label = f"Series {episode_series_number(item)}"
        grouped.setdefault(label, []).append(item)
    return grouped


def series_group_sort_key(label):
    match = re.search(r"\d+", label)
    return int(match.group(0)) if match else 9999


def print_series_rule(prefix, title, width=120):
    label = f" {prefix}: {title} "
    if len(label) >= width:
        print(f"{bcolors.GRAY}{label}{bcolors.ENDC}")
        return
    left = (width - len(label)) // 2
    right = width - len(label) - left
    print(f"{bcolors.GRAY}{'─' * left}{label}{'─' * right}{bcolors.ENDC}")


def list_episode_items(episode_items):
    if not episode_items:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}No Brollie episodes found.{bcolors.ENDC}")
        return
    show = episode_items[0].get("show_title", "Brollie")
    grouped_items = group_episode_items_by_series(episode_items)
    group_labels = sorted(grouped_items, key=series_group_sort_key)
    series_summary = ",  ".join(f"{label}({len(grouped_items[label])})" for label in group_labels)
    print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Found {len(episode_items)} Brollie episodes{bcolors.ENDC}")
    print()
    print_series_rule("Brollie Series", show)
    print()
    print(f"{bcolors.GRAY}{len(group_labels)} Series" + (f",  {series_summary}" if series_summary else "") + f"{bcolors.ENDC}")
    for series_index, series_label in enumerate(group_labels):
        series_items = grouped_items[series_label]
        if series_index > 0:
            print(f"{bcolors.GRAY}│{bcolors.ENDC}")
        group_is_last = series_index == len(group_labels) - 1
        group_branch = "└─" if group_is_last else "├─"
        group_child_prefix = "   " if group_is_last else "│  "
        print(f"{bcolors.GRAY}{group_branch} {series_label}: {bcolors.ENDC}{len(series_items)} episodes")
        for episode_index, item in enumerate(series_items):
            is_last = episode_index == len(series_items) - 1
            branch = "└─" if is_last else "├─"
            url_branch = "  " if is_last else "│ "
            episode_number_label, title = episode_tree_label(item)
            print(f"{bcolors.GRAY}{group_child_prefix}{branch} {episode_number_label}. {title}{bcolors.ENDC}")
            print(f"{bcolors.GRAY}{group_child_prefix}{url_branch} {bcolors.OKBLUE}{item['url']}{bcolors.ENDC}")


def export_episode_list_text(series_url, episode_items):
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    show_slug = slug_from_url(canonical_url(series_url)).rstrip("/").rsplit("/", 1)[-1] or "brollie"
    timestamp = __import__("time").strftime("%Y%m%d_%H%M%S")
    output_path = EXPORT_DIR / f"brollie_{safe_name(show_slug).lower()}_export_{timestamp}.txt"

    with output_path.open("w", encoding="utf-8") as file:
        for item in episode_items:
            file.write(f"Series {episode_series_number(item)} Episode {episode_list_number(item)} - {item.get('title') or '-'}\n")
            file.write(f"{item.get('url') or '-'}\n")

    return output_path


def parse_selector_part(value):
    match = re.fullmatch(r"s(\d{1,4})(?:e(\d{1,4}))?", clean_text(value).lower())
    if not match:
        raise ValueError(f"Invalid selector '{value}'. Use s01e01, s01, or a range like s01e01-s01e03.")
    return {
        "season": int(match.group(1)),
        "episode": int(match.group(2)) if match.group(2) is not None else None,
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
    start_text, end_text = selector.split("-", 1)
    if not start_text or not end_text:
        raise ValueError("Download range must include both start and end selectors.")
    start = parse_selector_part(start_text)
    end = parse_selector_part(end_text)
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
    if part["episode"] is None:
        return f"s{part['season']:02d}"
    return f"s{part['season']:02d}e{part['episode']:02d}"


def format_download_selector(parsed):
    if parsed["type"] in ("single_episode", "single_season"):
        return format_selector_part(parsed["start"])
    return f"{format_selector_part(parsed['start'])}-{format_selector_part(parsed['end'])}"


def format_queue_selector(item):
    return f"S{episode_series_number(item):02d}E{episode_list_number(item):02d}"


def select_episode_items(series_url, selector):
    parsed = parse_download_selector(selector)
    episode_items = collect_episode_items(series_url)
    selected = []
    for item in episode_items:
        season = episode_series_number(item)
        episode = episode_list_number(item)
        if parsed["type"] == "single_episode":
            keep = season == parsed["start"]["season"] and episode == parsed["start"]["episode"]
        elif parsed["type"] == "single_season":
            keep = season == parsed["start"]["season"]
        elif parsed["type"] == "episode_range":
            keep = (parsed["start"]["season"], parsed["start"]["episode"]) <= (season, episode) <= (
                parsed["end"]["season"],
                parsed["end"]["episode"],
            )
        else:
            keep = parsed["start"]["season"] <= season <= parsed["end"]["season"]
        if keep:
            selected.append(item)

    if not selected:
        raise ValueError(f"No Brollie episodes matched selector {format_download_selector(parsed)}.")
    return selected


def print_download_queue(episode_items):
    print()
    print(f"{bcolors.GRAY}Download queue:{bcolors.ENDC}")
    for item in episode_items:
        print(f"{bcolors.GRAY}{format_queue_selector(item)} {item.get('title') or ''}{bcolors.ENDC}".rstrip())


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
    parts.extend([resolution, "BROLLIE", "WEB-DL", "AAC2.0", "H.264"])
    return ".".join(part for part in parts if part and part != "Unknown")


def build_download_command(playback, filename, mode="auto", quality=None, save_subs=False):
    subtitle_selector = "--select-subtitle all" if save_subs else "--drop-subtitle all"
    selectors = "" if mode == "interactive" else f"{video_selector(quality)} --select-audio best {subtitle_selector} "
    command = (
        f'{N_M3U8DL} "{playback.manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{SAVE_PATH}" --save-name "{filename}"'
    )

    return append_downloader_proxy(command)


def print_playback_details(playback, command=None):
    metadata = playback.metadata
    manifest_label = "M3U8 URL" if playback.manifest_type == "HLS" else f"{playback.manifest_type} URL"
    print(f"{bcolors.LIGHTBLUE}{manifest_label}: {bcolors.ENDC}{playback.manifest_url}")
    if playback.keys:
        for key in playback.keys:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}{key}")
    if command:
        print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
        print(mask_proxy_command(command))


def maybe_download(command, auto_download=False, auto_confirm=False):
    if confirm_download("Do you wish to download? Y or N: ", auto_confirm=auto_confirm, auto_download=auto_download):
        subprocess.run(command, shell=True)
    else:
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")


def process_video(video_url, mode="auto", auto_download=False, quality=None, auto_confirm=False, save_subs=False):
    print(f"{icons.ICON_INFO} {bcolors.LIGHTBLUE}Processing: {bcolors.ENDC}{video_url}")
    print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving playback info...{bcolors.ENDC}")
    resolved = resolve_playback(video_url)

    playback = resolved["playback"]
    filename = apply_quality_to_filename(resolved["filename"], quality)
    command = build_download_command(playback, filename, mode=mode, quality=quality, save_subs=save_subs)
    print_playback_details(playback, command)
    maybe_download(command, auto_download=auto_download, auto_confirm=auto_confirm)


def info(video_url):
    if is_series_url(video_url):
        raise ValueError("Info mode requires a Brollie episode/video URL, not a series URL.")

    resolved = resolve_playback(video_url)

    playback = resolved["playback"]
    print(f"{bcolors.LIGHTBLUE}{resolved['manifest_type']} Manifest URL: {bcolors.ENDC}{playback.manifest_url}")
    if playback.keys:
        for key in playback.keys:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}{key}")
    print_streams(resolved["streams"])
    print_episode_metadata(playback.metadata)
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{resolved['filename']}.mkv")


def download_selected_episodes(series_url, selector, quality=None, auto_confirm=False, save_subs=False):
    episode_items = select_episode_items(series_url, selector)
    print_download_queue(episode_items)
    if not confirm_download("Do you wish to download these episodes? Y or N: ", auto_confirm=auto_confirm):
        print(f"{icons.ICON_FAILURE} {bcolors.RED}Download cancelled{bcolors.ENDC}")
        return

    for index, item in enumerate(episode_items, 1):
        print()
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Downloading {index}/{len(episode_items)}: {bcolors.ENDC}{item['url']}")
        process_video(item["url"], auto_download=True, quality=quality, auto_confirm=auto_confirm, save_subs=save_subs)


def main(video_url, downloads_path, wvd_device_path=None, mode="auto", export_list=False, download_selector=None, auto_download=False, quality=None, auto_confirm=False, save_subs=False):
    global SAVE_PATH
    SAVE_PATH = Path(downloads_path)
    video_url = canonical_url(video_url)

    if mode == "list":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.RED}List mode requires a Brollie series URL, not an episode URL.{bcolors.ENDC}")
            return
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
        episode_items = collect_episode_items(video_url)
        list_episode_items(episode_items)
        if export_list:
            export_path = export_episode_list_text(video_url, episode_items)
            print(f"{icons.ICON_SUCCESS} {bcolors.OKGREEN}Exported list: {export_path}{bcolors.ENDC}")
        return

    if mode == "download":
        if is_episode_url(video_url):
            print(f"{icons.ICON_FAILURE} {bcolors.RED}Download selector mode requires a Brollie series URL, not an episode URL.{bcolors.ENDC}")
            return
        print(f"{icons.ICON_WAITING} {bcolors.LIGHTBLUE}Retrieving series information.....{bcolors.ENDC}")
        download_selected_episodes(video_url, download_selector, quality, auto_confirm, save_subs)
        return

    if mode == "info":
        info(video_url)
        return

    if is_series_url(video_url):
        print(f"{icons.ICON_WARNING} {bcolors.WARNING}Series URLs require a flag. Use --list/-l to list episodes or --download/-d SELECTOR to download selected episodes.{bcolors.ENDC}")
        return

    process_video(video_url, mode=mode, auto_download=auto_download, quality=quality, auto_confirm=auto_confirm, save_subs=save_subs)
