import requests
import re
import base64
import binascii
import subprocess
import os
import json
import time
from datetime import datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from pywidevine import Cdm, Device, PSSH
from rich.console import Console
from rich.rule import Rule
from rich.text import Text
from colors import bcolors
import icons
from filename_utils import safe_windows_filename
from services.proxy import append_downloader_proxy, mask_proxy_command


ABC_SERIES_URL = "https://api.iview.abc.net.au/v3/series/{slug}"
TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "temp"))
EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "export"))
console = Console()

def get_video_id(url):
    match = re.search(r'video/([A-Z0-9]+)', url)
    return match.group(1) if match else None

def parse_series_input_url(series_url):
    parts = urlsplit(series_url.strip())
    path_parts = [part for part in parts.path.split("/") if part]

    if "show" in path_parts:
        show_index = path_parts.index("show")
        if show_index + 1 < len(path_parts):
            return path_parts[show_index + 1]

    if path_parts:
        return path_parts[-1]

    return ""

def get_series_data(slug):
    response = requests.get(ABC_SERIES_URL.format(slug=slug), timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to retrieve ABC iView series data, status code: {response.status_code}")

    try:
        return response.json()
    except Exception as e:
        raise RuntimeError(f"ABC iView series endpoint did not return valid JSON: {e}")

def extract_season_number(season_data, fallback=0):
    for value in (
        season_data.get("displaySubtitle"),
        season_data.get("title"),
        season_data.get("id"),
        season_data.get("_links", {}).get("deeplink", {}).get("href"),
    ):
        match = re.search(r"(?:Season|Series|S)[\s/-]*(\d+)", str(value or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))

    return fallback

def extract_episode_number(episode_data):
    for value in (
        episode_data.get("displaySubtitle"),
        episode_data.get("title"),
        episode_data.get("id"),
    ):
        match = re.search(r"(?:Episode|Ep)[\s/-]*(\d+)", str(value or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))

        match = re.search(r"H(\d+)S\d+", str(value or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))

    return 0

def clean_episode_title(episode_data):
    title = episode_data.get("title") or episode_data.get("displaySubtitle") or "Unknown Title"
    title = re.sub(r"^S\d+\s+Episode\s+\d+\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^S\d{2,4}\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^(?:Series|Season)\s+\d+\s+Episode\s+\d+\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^Episode\s+\d+\s*", "", title, flags=re.IGNORECASE)
    return title.strip(" -") or "Unknown Title"

def pick_thumbnail(images):
    for preferred_name in ("episodeThumbnail", "thumbnail", "seriesThumbnail", "titledThumbnail"):
        for image in images or []:
            if image.get("name") == preferred_name and image.get("url"):
                return image["url"]

    for image in images or []:
        if image.get("url"):
            return image["url"]

    return "Not Available"

def collect_episode_details(series_slug, series_data):
    seasons_data = series_data if isinstance(series_data, list) else [series_data]
    episode_details = []
    episode_summary = []
    seen_ids = set()

    for season_index, season_data in enumerate(seasons_data, start=1):
        season_number = extract_season_number(season_data, fallback=season_index)
        episodes = season_data.get("_embedded", {}).get("videoEpisodes", {}).get("items", []) or []
        for episode_data in episodes:
            video_id = episode_data.get("id")
            if not video_id or video_id in seen_ids:
                continue

            show_title = episode_data.get("showTitle") or season_data.get("showTitle") or series_slug.replace("-", " ").title()
            episode_number = extract_episode_number(episode_data)
            title = clean_episode_title(episode_data)
            video_url = f"https://iview.abc.net.au/video/{video_id}"

            episode_details.append({
                "Video URL": video_url,
                "Video ID": video_id,
                "Show Title": show_title,
                "Title": title,
                "Season": season_number,
                "Episode": episode_number,
                "Date Aired": episode_data.get("pubDate", "Not Available"),
                "Description": episode_data.get("description") or "Not Available",
                "Thumbnail": pick_thumbnail(episode_data.get("images") or season_data.get("images") or []),
            })
            episode_summary.append(f"{show_title} Season {season_number} Episode {episode_number} - {title} ID: {video_id}")
            seen_ids.add(video_id)

    episode_details.sort(key=lambda item: (item.get("Season") or 0, item.get("Episode") or 0))
    episode_summary.sort()

    return {
        "Episode Summary": episode_summary,
        "Episode Details": episode_details,
    }

def save_episode_list_json(series_slug, episode_data):
    os.makedirs(TEMP_DIR, exist_ok=True)
    output_path = os.path.join(TEMP_DIR, f"abc_{safe_windows_filename(series_slug)}_episodes.json")

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(episode_data, file, ensure_ascii=False, indent=4)

    return output_path

def export_episode_list_text(series_slug, episodes):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(EXPORT_DIR, f"abc_{safe_windows_filename(series_slug)}_export_{timestamp}.txt")

    with open(output_path, "w", encoding="utf-8") as file:
        for episode in episodes:
            label = episode.get("Season Label") or "Episodes"
            episode_number = episode.get("Episode Label") or episode.get("Episode") or "-"
            title = episode.get("Title") or "-"
            url = episode.get("Video URL") or "-"
            file.write(f"{label} Episode {episode_number} - {title}\n")
            file.write(f"{url}\n")

    return output_path

def print_episode_list(series_title, episodes):
    if not episodes:
        print(f"{bcolors.WARNING}No playable ABC iView episodes found.{bcolors.ENDC}")
        return

    tree_style = "grey70"
    label_style = "bold grey70"
    header_style = "bright_blue"

    seasons = {}
    for episode in episodes:
        season_number = episode.get("Season") or 0
        seasons.setdefault(season_number, []).append(episode)

    season_numbers = sorted(seasons)
    for season_episodes in seasons.values():
        season_episodes.sort(key=lambda item: item.get("Episode") or 0)

    season_summary = ",  ".join(
        f"S{season_number}({len(seasons[season_number])})"
        for season_number in season_numbers
    )

    console.print(Rule(Text.assemble(("ABC iView Series: ", f"bold {header_style}"), (series_title, "bold white")), style=header_style))
    console.print()
    console.print(
        Text.assemble(
            (f"{len(season_numbers)} seasons", label_style),
            (f",  {season_summary}" if season_summary else "", "white"),
        )
    )

    for season_index, season_number in enumerate(season_numbers):
        if season_index > 0:
            console.print(Text("│", style=tree_style))

        season_is_last = season_index == len(season_numbers) - 1
        season_branch = "└─" if season_is_last else "├─"
        season_child_prefix = "   " if season_is_last else "│  "
        season_episodes = seasons[season_number]
        console.print(
            Text.assemble(
                (f"{season_branch} ", tree_style),
                (f"Season {season_number}: ", label_style),
                (f"{len(season_episodes)} episodes", "white"),
            )
        )

        for index, episode in enumerate(season_episodes):
            is_last = index == len(season_episodes) - 1
            branch = "└─" if is_last else "├─"
            url_branch = "  " if is_last else "│ "

            console.print(
                Text.assemble(
                    (season_child_prefix, tree_style),
                    (f"{branch} ", tree_style),
                    (f"{episode.get('Episode') or '-'}. ", label_style),
                    (episode.get("Title") or "-", "white"),
                )
            )
            console.print(
                Text.assemble(
                    (season_child_prefix, tree_style),
                    (f"{url_branch} ", tree_style),
                    (episode.get("Video URL") or "-", "bright_blue"),
                )
            )

def list_show_episodes(series_url, export_list=False):
    series_slug = parse_series_input_url(series_url)
    if not series_slug:
        raise ValueError("Could not determine ABC iView show slug from the URL.")

    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    series_data = get_series_data(series_slug)
    episode_data = collect_episode_details(series_slug, series_data)
    episodes = episode_data["Episode Details"]
    series_title = episodes[0].get("Show Title") if episodes else series_slug.replace("-", " ").title()
    output_path = save_episode_list_json(series_slug, episode_data)

    try:
        console.print()
        print_episode_list(series_title, episodes)
        print(f"\n{bcolors.OKGREEN}{icons.ICON_SUCCESS} Found {len(episodes)} episode(s){bcolors.ENDC}")
        if export_list:
            export_path = export_episode_list_text(series_slug, episodes)
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Exported list: {export_path}{bcolors.ENDC}")
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)

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
        raise ValueError(
            "Download range must include both start and end selectors. "
            "Examples: s01e03-s02e02 or s01-s03"
        )

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
        matched_start = (int(selected[0].get("Season") or 0), int(selected[0].get("Episode") or 0))
        matched_end = (int(selected[-1].get("Season") or 0), int(selected[-1].get("Episode") or 0))
        if matched_start > requested_start or matched_end < requested_end:
            matched_label = f"{format_queue_selector(*matched_start)}-{format_queue_selector(*matched_end)}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched {matched_label}.{bcolors.ENDC}")

    if parsed_selector["type"] == "season_range":
        requested_start = parsed_selector["start"]["season"]
        requested_end = parsed_selector["end"]["season"]
        matched_seasons = sorted({int(item.get("Season") or 0) for item in selected})
        if matched_seasons[0] > requested_start or matched_seasons[-1] < requested_end:
            matched_label = f"{format_queue_selector(matched_seasons[0])}-{format_queue_selector(matched_seasons[-1])}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched seasons {matched_label}.{bcolors.ENDC}")

def get_series_episodes(series_url):
    series_slug = parse_series_input_url(series_url)
    if not series_slug:
        raise ValueError("Could not determine ABC iView show slug from the URL.")

    series_data = get_series_data(series_slug)
    episode_data = collect_episode_details(series_slug, series_data)
    return series_slug, episode_data["Episode Details"]

def select_episodes(series_url, selector):
    parsed_selector = parse_download_selector(selector)
    series_slug, episodes = get_series_episodes(series_url)
    selected = []
    for item in episodes:
        season = int(item.get("Season") or 0)
        episode = int(item.get("Episode") or 0)

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
        normalized = format_download_selector(parsed_selector)
        series_title = episodes[0].get("Show Title") if episodes else series_slug.replace("-", " ").title()
        raise LookupError(f"No ABC iView episodes found for selector {normalized} in {series_title}.")

    selected.sort(key=lambda item: (int(item.get("Season") or 0), int(item.get("Episode") or 0)))
    warn_if_partial_range_match(parsed_selector, selected)
    return selected

def print_download_queue(episodes):
    console.print()
    console.print(Text("Download queue:", style="bold bright_blue"))
    for episode in episodes:
        season = int(episode.get("Season") or 0)
        episode_number = int(episode.get("Episode") or 0)
        season_label = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
        console.print(
            Text.assemble(
                (f"{season_label}E{episode_number:02d} ", "bold grey70"),
                (episode.get("Title") or "-", "white"),
            )
        )

def download_selected_episodes(series_url, selector, downloads_path, wvd_device_path):
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    try:
        episodes = select_episodes(series_url, selector)
    except LookupError as error:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} {error}{bcolors.ENDC}")
        return
    print_download_queue(episodes)

    user_input = input(f"\nDownload {len(episodes)} episode(s)? Y or N: ").strip().lower()
    if user_input != "y":
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
        return

    for index, episode in enumerate(episodes, start=1):
        print(f"\n{bcolors.LIGHTBLUE}{icons.ICON_INFO} Downloading {index}/{len(episodes)}: {episode.get('Title') or episode.get('Video URL')}{bcolors.ENDC}")
        main(episode["Video URL"], downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, auto_download=True)

def get_jwt_token(client_id, jwt_url):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    response = requests.post(jwt_url, data={"clientId": client_id}, headers=headers)
    if response.status_code == 200:
        return response.json().get("token")
    else:
        return None

def get_license_data(video_id, drm_url, jwt_token):
    headers = {
        'Authorization': f"Bearer {jwt_token}"
    }
    response = requests.get(drm_url.format(video_id=video_id), headers=headers)
    if response.status_code == 200:
        data = response.json()
        if data["status"] == "ok":
            custom_data = data["license"]
            license_url = "https://wv-keyos.licensekeyserver.com/"
            return license_url, custom_data
        else:
            return None, None
    else:
        return None, None

# Function to get 1080p MPD URL
def get_video_data(video_id):
    api_url = f"https://api.iview.abc.net.au/v3/video/{video_id}"
    response = requests.get(api_url)
    if response.status_code == 200:
        return response.json()
    return None

def get_mpd_candidates(video_id):
    data = get_video_data(video_id)
    candidates = []
    seen_urls = set()

    if data and '_embedded' in data and 'playlist' in data['_embedded']:
        for playlist in data['_embedded']['playlist']:
            if 'streams' not in playlist or 'mpegdash' not in playlist['streams']:
                continue

            mpegdash_streams = playlist['streams']['mpegdash']
            source_url = None
            for quality in ['1080', '720', 'sd']:
                if quality in mpegdash_streams and video_id in mpegdash_streams[quality]:
                    source_url = mpegdash_streams[quality]
                    break

            if not source_url:
                continue

            upgraded_url = source_url.replace('720.mpd', '1080.mpd')
            for label, url in [('1080', upgraded_url), ('source', source_url)]:
                if url not in seen_urls:
                    candidates.append({'label': label, 'url': url})
                    seen_urls.add(url)

            break

    return candidates

def get_mpd_url(video_id):
    candidates = get_mpd_candidates(video_id)
    return candidates[0]['url'] if candidates else None

def get_mpd_streams(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code != 200:
        return []

    root = ET.fromstring(response.content)
    streams = []
    for adaptation_set in root.iter():
        if not adaptation_set.tag.endswith('AdaptationSet'):
            continue

        content_type = adaptation_set.attrib.get('contentType', '').lower()
        mime_type = adaptation_set.attrib.get('mimeType', '').lower()
        lang = adaptation_set.attrib.get('lang', '-')

        for representation in adaptation_set:
            if not representation.tag.endswith('Representation'):
                continue

            rep_mime_type = representation.attrib.get('mimeType', '').lower()
            rep_content = f"{content_type} {mime_type} {rep_mime_type}"
            codecs = representation.attrib.get('codecs') or adaptation_set.attrib.get('codecs') or 'unknown codecs'
            bandwidth = representation.attrib.get('bandwidth')
            bitrate = f"{int(bandwidth) // 1000} Kbps" if bandwidth and bandwidth.isdigit() else "unknown bitrate"
            width = representation.attrib.get('width')
            height = representation.attrib.get('height')

            if 'video' in rep_content or width or height:
                stream_type = 'Vid'
                resolution = f"{width or '?'}x{height or '?'}"
            elif 'audio' in rep_content:
                stream_type = 'Aud'
                resolution = '-'
            elif 'text' in rep_content or 'subtitle' in rep_content or codecs.lower() in {'stpp', 'wvtt'}:
                stream_type = 'Sub'
                resolution = '-'
            else:
                continue

            streams.append({
                'type': stream_type,
                'resolution': resolution,
                'bitrate': bitrate,
                'codec': codecs,
                'lang': lang or '-'
            })

    return streams

def get_available_streams(candidates):
    streams = []
    seen = set()
    for candidate in candidates:
        for stream in get_mpd_streams(candidate['url']):
            stream_key = (
                stream['type'],
                stream['resolution'],
                stream['bitrate'],
                stream['codec'],
                stream['lang']
            )
            if stream_key not in seen:
                streams.append(stream)
                seen.add(stream_key)
    return sorted(streams, key=stream_sort_key)

def stream_sort_key(stream):
    type_order = {'Vid': 0, 'Aud': 1, 'Sub': 2}
    height = 0
    bitrate = 0

    resolution_match = re.search(r'x(\d+)', stream['resolution'])
    if resolution_match:
        height = int(resolution_match.group(1))

    bitrate_match = re.search(r'(\d+)', stream['bitrate'])
    if bitrate_match:
        bitrate = int(bitrate_match.group(1))

    return (type_order.get(stream['type'], 9), -height, -bitrate, stream['codec'], stream['lang'])

def print_streams(streams):
    if not streams:
        print(f"\n{bcolors.YELLOW}Available streams: {bcolors.ENDC}No streams found")
        return

    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    print(f"{'#':>3}  {'Type':<4} {'Resolution':<11} {'Bitrate':<16} {'Codec':<18} {'Lang':<6}")
    print(f"{'--':>3}  {'----':<4} {'----------':<11} {'----------------':<16} {'------------------':<18} {'------':<6}")
    for index, stream in enumerate(streams, start=1):
        print(
            f"{index:>3}  "
            f"{stream['type']:<4} "
            f"{stream['resolution']:<11} "
            f"{stream['bitrate']:<16} "
            f"{stream['codec']:<18} "
            f"{stream['lang']:<6}"
        )

def collect_subtitles(video_id):
    data = get_video_data(video_id)
    playlist = (data or {}).get("_embedded", {}).get("playlist", [])
    program = next((item for item in playlist if item.get("type") == "program"), None)
    if not program:
        return []

    captions = program.get("captions") or {}
    url = captions.get("src-vtt")
    if not url or str(captions.get("live", "0")) == "1":
        return []

    return [{
        "url": url,
        "language": "en",
        "name": "English",
        "kind": "captions",
        "extension": "srt",
    }]

def print_external_subtitles(subtitles):
    if not subtitles:
        return

    print(f"\n{bcolors.YELLOW}External subtitles:{bcolors.ENDC}")
    print(f"{'#':>3}  {'Lang':<6} {'Kind':<10} {'Format':<7} {'Name':<20}")
    print(f"{'--':>3}  {'------':<6} {'----------':<10} {'-------':<7} {'--------------------':<20}")
    for index, subtitle in enumerate(subtitles, start=1):
        print(
            f"{index:>3}  "
            f"{subtitle.get('language', '-'):<6} "
            f"{subtitle.get('kind', '-'):<10} "
            f"{subtitle.get('extension', '-'):<7} "
            f"{subtitle.get('name', '-'):<20}"
        )

def vtt_timestamp_to_srt(timestamp):
    return timestamp.replace(".", ",")

def vtt_to_srt(vtt_text):
    text = vtt_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    current = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            if current:
                cues.append(current)
                current = []
            continue
        if stripped == "WEBVTT" or stripped.startswith(("NOTE", "STYLE", "REGION", "X-TIMESTAMP-MAP")):
            continue
        current.append(stripped)

    if current:
        cues.append(current)

    srt_blocks = []
    for cue in cues:
        timing_index = next((idx for idx, line in enumerate(cue) if "-->" in line), None)
        if timing_index is None:
            continue

        timing = cue[timing_index]
        match = re.match(
            r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})",
            timing,
        )
        if not match:
            continue

        text_lines = cue[timing_index + 1:]
        if not text_lines:
            continue

        srt_blocks.append(
            f"{len(srt_blocks) + 1}\n"
            f"{vtt_timestamp_to_srt(match.group('start'))} --> {vtt_timestamp_to_srt(match.group('end'))}\n"
            f"{chr(10).join(text_lines)}"
        )

    if not srt_blocks:
        return None

    return "\n\n".join(srt_blocks) + "\n"

def subtitle_filename(base_name, subtitle, index, used_names):
    base_name = safe_windows_filename(base_name)
    language = re.sub(r"[^A-Za-z0-9]+", "", subtitle.get("language") or "und") or "und"
    extension = subtitle.get("extension") or "srt"
    name = f"{base_name}.{language}.{extension}"
    if name in used_names:
        name = f"{base_name}.{language}.{index}.{extension}"
    used_names.add(name)
    return name

def save_external_subtitles(subtitles, downloads_path, formatted_file_name):
    if not subtitles:
        return

    os.makedirs(downloads_path, exist_ok=True)
    used_names = set()
    for index, subtitle in enumerate(subtitles, start=1):
        print(f"{bcolors.OKCYAN}{icons.ICON_WAITING} Processing subtitle:{bcolors.ENDC} {subtitle.get('language', 'und')} {subtitle.get('name', 'Subtitle')}")
        try:
            response = requests.get(subtitle["url"], timeout=20)
            response.raise_for_status()
            content = vtt_to_srt(response.text)
        except Exception:
            content = None

        if not content:
            print(f"{bcolors.WARNING}Subtitle skipped: no usable cues found{bcolors.ENDC}")
            continue

        filename = subtitle_filename(formatted_file_name, subtitle, index, used_names)
        path = os.path.join(downloads_path, filename)
        with open(path, "w", encoding="utf-8-sig", newline="") as file:
            file.write(content)
        print(f"{bcolors.GREEN}{icons.ICON_SUCCESS} Subtitle saved:{bcolors.ENDC} {path}")

# Function to get PSSH from MPD URL
def extract_pssh(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code == 200:
        mpd_content = response.content
        root = ET.fromstring(mpd_content)
        for elem in root.iter():
            if 'ContentProtection' in elem.tag and 'urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed' in elem.attrib.values():
                pssh = elem.find('{urn:mpeg:cenc:2013}pssh').text
                return pssh
    return None

def get_video_metadata(video_id):
    show_info_url = f'https://api.iview.abc.net.au/v3/video/{video_id}'
    response = requests.get(show_info_url)
    if response.status_code == 200:
        return response.json()
    return {}

# Function to get the show information for file name formatting
def get_show_info(video_id, data=None):
    data = data or get_video_metadata(video_id)
    if data:
        show_title = data.get("showTitle", "UnknownShow").replace(" ", ".")
        title = data.get("title", "")
        status_title = data.get("status", {}).get("title", "")
        
        if status_title == "MOVIE":
            formatted_title = f"{show_title}.1080p.ABCiView.WEB-DL.AAC2.0.H.264"
        else:
            match = re.search(r'Series (\d+) Episode (\d+)', title)
            if match:
                season = match.group(1).zfill(2)
                episode = match.group(2).zfill(2)
                formatted_title = f"{show_title}.S{season}E{episode}.1080p.ABCiView.WEB-DL.AAC2.0.H.264"
            else:
                formatted_title = f"{show_title}.{title.replace(' ', '.')}.1080p.ABCiView.WEB-DL.AAC2.0.H.264"
        return safe_windows_filename(formatted_title)
    return "video"

def print_info_metadata(data):
    if not data:
        return

    def format_info_date(value):
        if not value:
            return value
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(str(value), fmt).strftime("%d %B %Y")
            except ValueError:
                pass
        return value

    fields = [
        ("Show", data.get("showTitle")),
        ("Title", clean_episode_title(data)),
        ("Date Aired", format_info_date(data.get("pubDate"))),
        ("Description", data.get("description")),
    ]
    visible_fields = [(label, str(value).strip()) for label, value in fields if value and str(value).strip() and str(value).strip() != "Not Available"]
    if not visible_fields:
        return

    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in visible_fields:
        print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

# Function to get keys using PSSH and license URL
def get_license(pssh, video_id, client_id, jwt_url, drm_url, wvd_device_path):
    jwt_token = get_jwt_token(client_id, jwt_url)
    if not jwt_token:
        return None

    license_url, custom_data = get_license_data(video_id, drm_url, jwt_token)
    if not license_url:
        return None

    # Headers for the license request
    headers = {
        'Accept': '*/*',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Content-Length': str(len(pssh)),
        'Host': 'wv-keyos.licensekeyserver.com',
        'Origin': 'https://iview.abc.net.au',
        'Referer': f'https://iview.abc.net.au/video/{video_id}',
        'sec-ch-ua': '"Not/A)Brand";v="8", "Chromium";v="126", "Microsoft Edge";v="126"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'cross-site',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
        'customdata': custom_data
    }

    # Make the license request
    device = Device.load(wvd_device_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    challenge = cdm.get_license_challenge(session_id, PSSH(pssh))

    response = requests.post(license_url, headers=headers, data=challenge)
    # Parse the license response
    if response.status_code == 200:
        cdm.parse_license(session_id, response.content)
        keys = cdm.get_keys(session_id)
        return keys
    else:
        return None

def format_keys(keys):
    formatted_keys = []
    for key in keys:
        formatted_keys.append(f"{key.kid.hex}:{key.key.hex()}")
    return formatted_keys

def build_download_command(mpd_url, downloads_path, formatted_file_name, formatted_keys, mode):
    selectors = "" if mode == "interactive" else '--select-video res=1080 --select-audio all -da role="alternate" --select-subtitle all '
    keys = " --key " + " --key ".join(formatted_keys)
    download_command = (
        f'N_m3u8DL-RE "{mpd_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}"'
        f'{keys}'
    )
    return append_downloader_proxy(download_command)

# Main execution flow
def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, auto_download=False):
    if mode == "list":
        list_show_episodes(video_url, export_list)
        return

    if mode == "download":
        download_selected_episodes(video_url, download_selector, downloads_path, wvd_device_path)
        return

    client_id = "1d4b5cba-42d2-403e-80e7-34565cdf772d"
    jwt_url = "https://api.iview.abc.net.au/v3/token/jwt"
    drm_url = "https://api.iview.abc.net.au/v3/token/drm/{video_id}"

    video_id = get_video_id(video_url)
    if video_id:
        candidates = get_mpd_candidates(video_id)
        mpd_url = candidates[0]['url'] if candidates else None
        if mpd_url:
            pssh = extract_pssh(mpd_url)
            if pssh:
                license_keys = get_license(pssh, video_id, client_id, jwt_url, drm_url, wvd_device_path)
                if license_keys:
                    formatted_keys = format_keys(license_keys)
                    video_metadata = get_video_metadata(video_id)
                    formatted_file_name = get_show_info(video_id, video_metadata)
                    subtitles = collect_subtitles(video_id)
                    
                    # Print the requested information
                    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
                    print(f"{bcolors.RED}License URL: {bcolors.ENDC}https://wv-keyos.licensekeyserver.com/")
                    print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
                    for key in formatted_keys:
                        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

                    if mode == "info":
                        print_streams(get_available_streams(candidates))
                        print_external_subtitles(subtitles)
                        print_info_metadata(video_metadata)
                        print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")
                        return

                    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                    download_command = build_download_command(mpd_url, downloads_path, formatted_file_name, formatted_keys, mode)
                    print(mask_proxy_command(download_command))
                    print_external_subtitles(subtitles)
                    
                    if download_command:
                        user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
                        if user_input == 'y':
                            print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
                            result = subprocess.run(download_command, shell=True)
                            if result.returncode == 0:
                                save_external_subtitles(subtitles, downloads_path, formatted_file_name)
                                print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
                        else:
                            print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
                else:
                    print("Failed to get license keys")
            else:
                print("Failed to extract PSSH")
        else:
            print("Failed to get MPD URL")
    else:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} ABC iView series URLs need a flag.{bcolors.ENDC}")
        print(f"{bcolors.YELLOW}{icons.ICON_INFO} Use -l to list episodes or -d with a selector to download from a series.{bcolors.ENDC}")
