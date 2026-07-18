import base64
import html
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import requests
import yaml
from rich.console import Console
from rich.rule import Rule
from rich.text import Text
from colors import bcolors
import icons
from filename_utils import safe_windows_filename
from services.proxy import append_downloader_proxy, mask_proxy_command


SBS_LOGIN_URL = "https://auth.sbs.com.au/login"
SBS_PLAYBACK_URL = "https://playback.pr.sbsod.com/stream/{video_id}"
SBS_CATALOGUE_URL = "https://catalogue.pr.sbsod.com/{series_type}/{slug}"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config.yaml")
CONFIG_PATH = os.path.abspath(CONFIG_PATH)
EXPORT_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "export")
console = Console()


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)

# Check for cached token
def ensure_sbs_cache(config):
    config.setdefault("credentials", {})
    config.setdefault("sbs", {})
    config["sbs"].setdefault("cache", {})
    config["sbs"]["cache"].setdefault("login", {})
    return config

# Obtain login credentials from config
def parse_sbs_credentials(credentials):
    creds = (credentials or "").strip()
    if not creds or ":" not in creds:
        raise ValueError("Missing SBS credentials. Expected username:password")

    username, password = creds.split(":", 1)
    username = username.strip()
    password = password.strip()

    if not username or not password:
        raise ValueError("Invalid SBS credentials. Expected username:password")

    return username, password


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

# Define token expiry
def jwt_expiry_utc(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        data = json.loads(decoded)
        exp = data.get("exp")
        if not exp:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        return None

# Check if token is valid
def token_is_valid(token, expiry, buffer_minutes=5):
    if not token or not expiry:
        return False

    expiry_dt = parse_iso_datetime(expiry)
    if not expiry_dt:
        return False

    now = datetime.now(timezone.utc)
    return expiry_dt > now + timedelta(minutes=buffer_minutes)


def mask_value(value):
    if not value:
        return "NONE"
    if len(value) <= 20:
        return value
    return f"{value[:10]}...{value[-10:]}"

# Login request
def sbs_login(username, password):
    headers = {
        "user-agent": (
            "okhttp/4.10.0"
        ),
    }

    payload = {
        "email": username,
        "password": password,
        "deviceName": "Android TV",
    }

    response = requests.post(SBS_LOGIN_URL, headers=headers, json=payload, timeout=20)

    # print(f"[DEBUG] login status: {response.status_code}")
    # print(f"[DEBUG] login content-type: {response.headers.get('content-type', '')}")
    # print(f"[DEBUG] login preview: {response.text[:500]}")

    if response.status_code != 200:
        raise RuntimeError(f"SBS login failed, status code: {response.status_code}")

    try:
        data = response.json()
    except Exception as e:
        raise RuntimeError(f"SBS login did not return valid JSON: {e}")

    access_token = data.get("accessToken")
    id_token = data.get("idToken")

    if not access_token:
        raise RuntimeError("SBS login response did not contain accessToken")

    expiry_dt = jwt_expiry_utc(access_token)

    return {
        "token": access_token,
        "id_token": id_token or "",
        "expiry": expiry_dt.isoformat() if expiry_dt else "",
    }

# Function to retrieve access toekn
def get_sbs_access_token(config, credentials):
    config = ensure_sbs_cache(config)
    cache = config["sbs"]["cache"]["login"]

    cached_token = cache.get("token", "")
    cached_expiry = cache.get("expiry", "")

    if token_is_valid(cached_token, cached_expiry):
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Using cached token{bcolors.ENDC}")
        return cached_token

    username, password = parse_sbs_credentials(credentials)
    print(f"{bcolors.OKCYAN}{icons.ICON_INFO} Cached token missing/expired, logging in...{bcolors.ENDC}")

    login_data = sbs_login(username, password)

    cache["token"] = login_data["token"]
    cache["id_token"] = login_data["id_token"]
    cache["expiry"] = login_data["expiry"]

    save_config(config)

    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Token cache updated{bcolors.ENDC}")
    return login_data["token"]

# Function to extract video ID from URL
def extract_video_id(video_url):
    match = re.search(r"/(\d+)", video_url)
    return match.group(1) if match else None

def extract_season_episode(text):
    if not isinstance(text, str):
        return 0, 0

    match = re.search(r"Season\s+(\d+)\s+Episode\s+(\d+)", text, re.IGNORECASE)
    if not match:
        return 0, 0

    return int(match.group(1)), int(match.group(2))

def parse_series_input_url(series_url):
    series_url = series_url.strip()
    parts = urlsplit(series_url)
    path_parts = [part for part in parts.path.split("/") if part]

    series_type = None
    slug = None

    if "catalogue.pr.sbsod.com" in parts.netloc:
        if len(path_parts) >= 2:
            series_type = path_parts[0]
            slug = path_parts[1]
    else:
        for index, segment in enumerate(path_parts):
            if segment.endswith("-series"):
                series_type = segment
                if index + 1 < len(path_parts):
                    slug = path_parts[index + 1]
                break

    if not series_type:
        series_type = "tv-series"
    if not slug and path_parts:
        slug = path_parts[-1]

    return series_type, slug

def pick_image_id(images):
    if not images:
        return None

    for image in images:
        category = (image.get("category") or "").upper()
        if "16:9" in category and "KEY_ART" in category:
            return image.get("id")

    for image in images:
        category = (image.get("category") or "").upper()
        if "16:9" in category and "BANNER" in category:
            return image.get("id")

    for image in images:
        if image.get("id"):
            return image.get("id")

    return None

def get_series_catalog(series_type, slug):
    url = SBS_CATALOGUE_URL.format(series_type=series_type, slug=slug)
    headers = {
        "user-agent": "Dalvik/2.1.0 (Linux; U; Android 14; SM-S901B Build/UP1A.231005.007)",
    }

    response = requests.get(url, headers=headers, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to retrieve SBS catalogue, status code: {response.status_code}")

    try:
        data = response.json()
    except Exception as e:
        raise RuntimeError(f"SBS catalogue endpoint did not return valid JSON: {e}")

    if not data.get("seasons"):
        raise RuntimeError(f"No seasons found in SBS catalogue for {series_type}/{slug}")

    return url, data

def build_thumbnail_url(catalog_data, episode):
    thumbnail_id = pick_image_id(episode.get("images") or [])
    if not thumbnail_id:
        thumbnail_id = pick_image_id(catalog_data.get("images") or [])

    if not thumbnail_id:
        return "Not Available"

    return f"https://image.pr.sbsod.com/{thumbnail_id}?width=320&height=180&type=webp&quality=100"

def collect_episode_details(series_slug, catalog_data):
    series_title = catalog_data.get("title") or series_slug.replace("-", " ").title()
    episode_details = []
    episode_summary = []
    seen_ids = set()

    for season in catalog_data.get("seasons", []) or []:
        season_number = season.get("seasonNumber", 0)
        for episode in season.get("episodes", []) or []:
            media_id = episode.get("mpxMediaID")
            if not media_id:
                continue

            video_id = str(media_id)
            if video_id in seen_ids:
                continue

            episode_season = episode.get("seasonNumber", season_number)
            episode_number = episode.get("episodeNumber", 0)
            episode_name = episode.get("title") or ""
            title = f"Season {episode_season} Episode {episode_number}"
            if episode_name:
                title = f"{title} - {episode_name}"

            video_url = f"https://www.sbs.com.au/ondemand/watch/{video_id}"
            episode_summary.append(f"{series_title} {title} ID: {video_id}")
            episode_details.append({
                "Video URL": video_url,
                "Video ID": video_id,
                "Show Title": series_title,
                "Title": title,
                "Season": episode_season,
                "Episode": episode_number,
                "Date Aired": (episode.get("availability") or {}).get("start", "Not Available"),
                "Description": episode.get("description") or "Not Available",
                "Thumbnail": build_thumbnail_url(catalog_data, episode),
            })
            seen_ids.add(video_id)

    episode_details.sort(key=lambda item: (item.get("Season") or 0, item.get("Episode") or 0), reverse=True)
    episode_summary.sort(key=extract_season_episode, reverse=True)

    return {
        "Episode Summary": episode_summary,
        "Episode Details": episode_details,
    }

def save_episode_list_json(series_slug, episode_data):
    output_dir = os.path.join(os.path.dirname(CONFIG_PATH), "temp")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"sbs_{safe_windows_filename(series_slug)}_episodes.json")

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(episode_data, file, ensure_ascii=False, indent=4)

    return output_path

def export_episode_list_text(series_slug, episodes):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(EXPORT_DIR, f"sbs_{safe_windows_filename(series_slug)}_export_{timestamp}.txt")

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
        print(f"{bcolors.WARNING}No playable SBS episodes found.{bcolors.ENDC}")
        return

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

    tree_style = "grey70"
    label_style = "bold grey70"
    header_style = "bright_blue"

    console.print(Rule(Text.assemble(("SBS Series: ", f"bold {header_style}"), (series_title, "bold white")), style=header_style))
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
            title = episode.get("Title") or ""
            title = re.sub(r"^Season\s+\d+\s+Episode\s+\d+\s+-\s*", "", title, flags=re.IGNORECASE)

            console.print(
                Text.assemble(
                    (season_child_prefix, tree_style),
                    (f"{branch} ", tree_style),
                    (f"{episode.get('Episode') or '-'}. ", label_style),
                    (title or "-", "white"),
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
    series_type, slug = parse_series_input_url(series_url)
    if not slug:
        raise ValueError("Could not determine SBS show slug from the URL.")

    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    _, catalog_data = get_series_catalog(series_type, slug)
    episode_data = collect_episode_details(slug, catalog_data)
    episodes = episode_data["Episode Details"]
    series_title = catalog_data.get("title") or slug.replace("-", " ").title()
    output_path = save_episode_list_json(slug, episode_data)

    try:
        console.print()
        print_episode_list(series_title, episodes)
        print(f"\n{bcolors.OKGREEN}{icons.ICON_SUCCESS} Found {len(episodes)} episode(s){bcolors.ENDC}")
        if export_list:
            export_path = export_episode_list_text(slug, episodes)
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
    series_type, slug = parse_series_input_url(series_url)
    if not slug:
        raise ValueError("Could not determine SBS show slug from the URL.")

    _, catalog_data = get_series_catalog(series_type, slug)
    episode_data = collect_episode_details(slug, catalog_data)
    return slug, episode_data["Episode Details"]

def clean_queue_title(episode):
    title = episode.get("Title") or "-"
    return re.sub(r"^Season\s+\d+\s+Episode\s+\d+\s+-\s*", "", title, flags=re.IGNORECASE) or title

def select_episodes(series_url, selector):
    parsed_selector = parse_download_selector(selector)
    slug, episodes = get_series_episodes(series_url)
    selected = []
    for item in episodes:
        season = int(item.get("Season") or 0)
        episode = int(item.get("Episode") or 0)
        if episode <= 0:
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
        normalized = format_download_selector(parsed_selector)
        series_title = episodes[0].get("Show Title") if episodes else slug.replace("-", " ").title()
        raise LookupError(f"No SBS episodes found for selector {normalized} in {series_title}.")

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
                (clean_queue_title(episode), "white"),
            )
        )

def download_selected_episodes(series_url, selector, downloads_path, credentials):
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
        print(f"\n{bcolors.LIGHTBLUE}{icons.ICON_INFO} Downloading {index}/{len(episodes)}: {clean_queue_title(episode)}{bcolors.ENDC}")
        main(episode["Video URL"], downloads_path, credentials, mode="auto", export_list=False, download_selector=None, auto_download=True)

# Function to get show information from playback catalogue
def get_playback_data(video_id, access_token):
    url = SBS_PLAYBACK_URL.format(video_id=video_id)

    headers = {
        "authorization": f"Bearer {access_token}",
        "user-agent": (
            "okhttp/4.10.0"
        ),
    }

    payload = {
        "deviceClass": "androidtv",
        "advertising": {
            "headerBidding": True,
            "telariaID": "",
            "ozTamSessionID": "",
            "subtitle": "",
            "resume": True,
        },
        "streamOptions": {
            "audio": "demuxed"
        },
        "streamProviders": ["GoogleDAI", "HLS"],
    }

    response = requests.post(url, headers=headers, json=payload, timeout=20)

    # print(f"[DEBUG] playback status: {response.status_code}")
    # print(f"[DEBUG] playback content-type: {response.headers.get('content-type', '')}")
    # print(f"[DEBUG] playback preview: {response.text[:1000]}")

    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch playback data, status code: {response.status_code}")

    try:
        return response.json()
    except Exception as e:
        raise RuntimeError(f"Playback endpoint did not return valid JSON: {e}")

# Function to get manifest URL
def find_hls_url(playback_data):
    for provider in playback_data.get("streamProviders", []):
        if provider.get("type") == "HLS" and provider.get("url"):
            return provider["url"]
    return None

def find_hls_provider(playback_data):
    for provider in playback_data.get("streamProviders", []):
        if provider.get("type") == "HLS" and provider.get("url"):
            return provider
    return None

def collect_subtitles(provider, english_only=True):
    subtitles = []
    seen_urls = set()

    for row in (provider or {}).get("textTracks", []) or []:
        url = row.get("url")
        language = (row.get("lang") or "und").strip().lower()
        if not url or url in seen_urls:
            continue
        if english_only and not language.startswith("en"):
            continue

        seen_urls.add(url)
        subtitles.append({
            "url": url,
            "language": language or "und",
            "name": (row.get("name") or language or "Subtitle").strip(),
            "kind": (row.get("type") or "subtitle").strip().lower(),
            "extension": "srt",
        })

    return subtitles

# Function to get max resolution from manifest
def get_max_height_m3u8(url_m3u8):
    try:
        response = requests.get(url_m3u8, timeout=20)
        response.raise_for_status()

        max_height = 0
        for line in response.text.splitlines():
            match = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if match:
                height = int(match.group(1))
                max_height = max(max_height, height)

        return max_height
    except Exception as e:
        print(f"Error fetching max height from m3u8: {e}")
        return 0

def get_m3u8_streams(url_m3u8):
    streams = []
    try:
        response = requests.get(url_m3u8, timeout=20)
        response.raise_for_status()
        pending = None
        for line in response.text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                resolution = re.search(r"RESOLUTION=(\d+x\d+)", line)
                bandwidth = re.search(r"BANDWIDTH=(\d+)", line)
                codecs = re.search(r'CODECS="([^"]+)"', line)
                pending = {
                    "resolution": resolution.group(1) if resolution else "",
                    "bandwidth": int(bandwidth.group(1)) if bandwidth else 0,
                    "codecs": codecs.group(1) if codecs else "",
                }
            elif pending and line and not line.startswith("#"):
                streams.append(pending)
                pending = None
    except Exception as e:
        print(f"Error fetching m3u8 streams: {e}")
    return sorted(streams, key=lambda item: item["bandwidth"], reverse=True)

def print_streams(streams):
    if not streams:
        print(f"\n{bcolors.WARNING}No stream variants found.{bcolors.ENDC}")
        return

    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    header = f"  {'#':>2}  {'Type':<4} {'Resolution':<10} {'Bitrate':<16} {'Codec':<18} {'Lang':<5}"
    divider = f"  {'-' * 2}  {'-' * 4} {'-' * 10} {'-' * 16} {'-' * 18} {'-' * 5}"
    print(header)
    print(divider)
    for idx, stream in enumerate(streams, start=1):
        kbps = round(stream.get("bandwidth", 0) / 1000)
        bitrate = f"{kbps} Kbps" if kbps else "unknown bitrate"
        codecs = stream.get("codecs") or "unknown codecs"
        print(f"  {idx:>2}  {'Vid':<4} {(stream.get('resolution') or '-'):<10} {bitrate:<16} {codecs:<18} {'-':<5}")

def print_external_subtitles(subtitles):
    if not subtitles:
        return

    print(f"\n{bcolors.YELLOW}External subtitles:{bcolors.ENDC}")
    header = f"  {'#':>2}  {'Lang':<5} {'Kind':<10} {'Format':<6} {'Name':<20}"
    divider = f"  {'-' * 2}  {'-' * 5} {'-' * 10} {'-' * 6} {'-' * 20}"
    print(header)
    print(divider)
    for idx, subtitle in enumerate(subtitles, start=1):
        print(
            f"  {idx:>2}  "
            f"{subtitle.get('language', '-'):<5} "
            f"{subtitle.get('kind', '-'):<10} "
            f"{subtitle.get('extension', '-'):<6} "
            f"{subtitle.get('name', '-'):<20}"
        )

def find_catalogue_description_url(playback_data):
    for provider in playback_data.get("streamProviders", []) or []:
        ad_params = provider.get("adTagParameters") or {}
        description_url = ad_params.get("description_url")
        if description_url:
            return description_url
    return ""

def find_catalogue_episode(playback_data):
    video_id = str((playback_data.get("externalIDs") or {}).get("mpxMediaID") or "")
    description_url = find_catalogue_description_url(playback_data)
    if not video_id or not description_url:
        return {}

    try:
        series_type, slug = parse_series_input_url(description_url)
        _, catalog_data = get_series_catalog(series_type, slug)
    except Exception:
        return {}

    for season in catalog_data.get("seasons", []) or []:
        for episode in season.get("episodes", []) or []:
            if str(episode.get("mpxMediaID") or "") == video_id:
                return {
                    "seriesTitle": catalog_data.get("title"),
                    "title": episode.get("title"),
                    "availability": episode.get("availability"),
                    "description": episode.get("description"),
                }
    return {}

def fetch_page_description(page_url):
    if not page_url:
        return ""

    try:
        response = requests.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        response.raise_for_status()
    except Exception:
        return ""

    for candidate in re.findall(r'<span[^>]*fontSize_md_xs[^>]*>(.*?)</span>', response.text, flags=re.DOTALL):
        candidate = html.unescape(re.sub(r"<[^>]+>", "", candidate))
        candidate = clean_info_value(candidate)
        if len(candidate) > 60 and "stream free" not in candidate.lower():
            return candidate

    match = re.search(r'"description","((?:\\.|[^"\\])*)"', response.text)
    if not match:
        return ""

    try:
        description = json.loads(f'"{match.group(1)}"')
    except Exception:
        description = match.group(1)

    return html.unescape(clean_info_value(description))

def clean_info_value(value):
    if value in (None, "", "Not Available"):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()

def format_info_date(value):
    if value in (None, "", "Not Available"):
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return f"{parsed.day} {parsed.strftime('%B %Y')}"
    except Exception:
        return str(value)

def print_info_metadata(playback_data):
    if not playback_data:
        return

    catalogue_episode = find_catalogue_episode(playback_data)
    availability = catalogue_episode.get("availability") or playback_data.get("availability") or {}
    show_title = clean_info_value(catalogue_episode.get("seriesTitle") or playback_data.get("seriesTitle"))
    episode_title = clean_info_value(catalogue_episode.get("title") or playback_data.get("title"))
    date_aired = format_info_date(availability.get("start"))
    description = clean_info_value(
        catalogue_episode.get("description")
        or playback_data.get("description")
        or playback_data.get("shortDescription")
        or playback_data.get("longDescription")
    )
    if not description:
        description = fetch_page_description(find_catalogue_description_url(playback_data))

    rows = [
        ("Show", show_title),
        ("Title", episode_title),
        ("Date Aired", date_aired),
        ("Description", description),
    ]
    rows = [(label, value) for label, value in rows if value]
    if not rows:
        return

    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

def vtt_timestamp_to_srt(timestamp):
    return timestamp.replace(".", ",")

def clean_srt_text(line):
    line = re.sub(r"</?c(?:\.[^>]*)?>", "", line)
    line = re.sub(r"</?v(?:\s+[^>]*)?>", "", line)
    return line

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

        text_lines = [clean_srt_text(line) for line in cue[timing_index + 1:]]
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
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Subtitle saved:{bcolors.ENDC} {path}")

# Function to build the video file name
def build_filename(playback_data, video_height):
    entity_type = playback_data.get("entityType", "")
    title = (playback_data.get("title") or "Unknown Title").replace(" ", ".")
    series_title = (playback_data.get("seriesTitle") or "").replace(" ", ".")
    season_number = str(playback_data.get("seasonNumber", 0)).zfill(2)
    episode_number = str(playback_data.get("episodeNumber", 0)).zfill(2)

    resolution_tag = f"{video_height or 720}p"

    if entity_type == "MOVIE" or not series_title:
        return f"{title}.{resolution_tag}.SBS.WEB-DL.AAC2.0.H.264"

    return f"{series_title}.S{season_number}E{episode_number}.{title}.{resolution_tag}.SBS.WEB-DL.AAC2.0.H.264"

# Function to extract and print m3u8 URL
def extract_info(video_url, access_token):
    video_id = extract_video_id(video_url)
    if not video_id:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} SBS series URLs need a flag.{bcolors.ENDC}")
        print(f"{bcolors.YELLOW}{icons.ICON_INFO} Use -l to list episodes or -d with a selector to download from a series.{bcolors.ENDC}")
        return None, None, [], {}

    playback_data = get_playback_data(video_id, access_token)

    hls_provider = find_hls_provider(playback_data)
    manifest_url = hls_provider.get("url") if hls_provider else None
    if not manifest_url:
        print("No HLS manifest URL found in playback data.")
        print(json.dumps(playback_data, indent=2)[:4000])
        return None, None, [], {}

    video_height = get_max_height_m3u8(manifest_url)
    formatted_file_name = build_filename(playback_data, video_height)
    subtitles = collect_subtitles(hls_provider)
    return manifest_url, formatted_file_name, subtitles, playback_data

# Function to format and display download command
def build_download_command(manifest_url, formatted_file_name, downloads_path, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    download_command = (
        f'N_m3u8DL-RE "{manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}"'
    )
    return append_downloader_proxy(download_command)

def display_info(manifest_url, formatted_file_name, subtitles=None, metadata=None):
    print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest_url}")
    print_streams(get_m3u8_streams(manifest_url))
    print_external_subtitles(subtitles or [])
    print_info_metadata(metadata or {})
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")

# Function to format and display download command
def display_download_command(manifest_url, formatted_file_name, downloads_path, mode="auto", subtitles=None, auto_download=False, metadata=None):
    if mode == "info":
        display_info(manifest_url, formatted_file_name, subtitles, metadata)
        return

    download_command = build_download_command(
        manifest_url,
        formatted_file_name,
        downloads_path,
        interactive=(mode == "interactive"),
    )

    print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest_url}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(mask_proxy_command(download_command))
    print_external_subtitles(subtitles or [])

    user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == "y":
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
        result = subprocess.run(download_command, shell=True)
        if result.returncode == 0:
            save_external_subtitles(subtitles or [], downloads_path, formatted_file_name)
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
    else:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")

# Main function
def main(video_url, downloads_path, credentials, mode="auto", export_list=False, download_selector=None, auto_download=False):
    if mode == "list":
        list_show_episodes(video_url, export_list)
        return

    if mode == "download":
        download_selected_episodes(video_url, download_selector, downloads_path, credentials)
        return

    config = load_config()
    access_token = get_sbs_access_token(config, credentials)

    manifest_url, formatted_file_name, subtitles, metadata = extract_info(video_url, access_token)
    if not manifest_url:
        return

    display_download_command(manifest_url, formatted_file_name, downloads_path, mode, subtitles, auto_download, metadata)
