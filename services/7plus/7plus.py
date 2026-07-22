import requests
import json
import re
import subprocess
from http.cookiejar import MozillaCookieJar
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from lxml import etree
import base64
import binascii
import os
import datetime as dt
import time
import yaml
from urllib.parse import urlsplit
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from rich.console import Console
from rich.rule import Rule
from rich.text import Text
from colors import bcolors
from filename_utils import safe_windows_filename
from services.proxy import append_downloader_proxy, mask_proxy_command
import icons


_PRINT = print

# URLs and Headers
BASE_URL = "https://7plus.com.au"
PLATFORM_VERSION = "1.0.106518"
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"))
TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "temp"))
EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "export"))
console = Console()

def _default_headers(referer_path="/", auth_token=None, conn_close=False):
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}{referer_path}",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "x-client-capabilities": "drm-auth",
        "Connection": "keep-alive",
    }
    if conn_close:
        h["Connection"] = "close"
    if auth_token:
        h["Authorization"] = f"Bearer {auth_token}"
    return h

def _session_with_retries(total=3, backoff=0.5, pool_maxsize=20):
    s = requests.Session()
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        status=total,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD", "OPTIONS"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)

def ensure_7plus_cache(config):
    config.setdefault("credentials", {})
    config.setdefault("7plus", {})
    config["7plus"].setdefault("cache", {})
    config["7plus"]["cache"].setdefault("auth", {})
    return config

def parse_iso_datetime(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        return None

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
        return dt.datetime.fromtimestamp(exp, tz=dt.timezone.utc)
    except Exception:
        return None

def token_is_valid(token, expiry, buffer_minutes=5):
    if not token or not expiry:
        return False

    expiry_dt = parse_iso_datetime(expiry)
    if not expiry_dt:
        return False

    return expiry_dt > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=buffer_minutes)

def cache_7plus_auth(config, auth_data):
    config = ensure_7plus_cache(config)
    cache = config["7plus"]["cache"]["auth"]
    token = auth_data.get("token") or ""
    refresh_token = auth_data.get("refreshToken") or cache.get("refresh_token", "")
    expiry_dt = None

    if auth_data.get("exp"):
        expiry_dt = dt.datetime.fromtimestamp(int(auth_data["exp"]), tz=dt.timezone.utc)
    if not expiry_dt:
        expiry_dt = jwt_expiry_utc(token)

    cache["token"] = token
    cache["refresh_token"] = refresh_token
    cache["expiry"] = expiry_dt.isoformat() if expiry_dt else ""
    save_config(config)
    return token

def refresh_7plus_auth_token(refresh_token):
    if not refresh_token:
        return None

    response = requests.post(
        "https://7plus.com.au/auth/refresh",
        headers={
            "User-Agent": _default_headers()["User-Agent"],
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": BASE_URL,
        },
        data=json.dumps({"refreshToken": refresh_token, "platformId": "web", "regSource": "7plus"}),
        timeout=(8, 25),
    )
    if response.status_code != 200:
        return None
    data = response.json()
    return data if data.get("token") else None

def exchange_7plus_id_token(session, id_token, headers):
    auth_url = "https://7plus.com.au/auth/token"
    response = session.post(
        auth_url,
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps({"idToken": id_token, "platformId": "web", "regSource": "7plus"}),
        timeout=(8, 25),
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("token"):
        raise RuntimeError("No auth token returned by /auth/token.")
    return data

def parse_7plus_url(video_url):
    match = re.search(
        r'https?://(?:www\.)?7plus\.com\.au/(?P<path>[^?]+\?.*?\bepisode-id=(?P<id>[^&#]+))',
        video_url,
    )
    if not match:
        resolved_url = resolve_short_7plus_video_url(video_url)
        if resolved_url and resolved_url != video_url:
            return parse_7plus_url(resolved_url)
        raise ValueError("Could not parse 7Plus show path and episode-id from URL")
    path = match.group("path")
    episode_id = match.group("id")
    return path.split("?")[0], episode_id

def parse_show_slug(series_url):
    parts = urlsplit(series_url.strip())
    path_parts = [part for part in parts.path.split("/") if part]
    return path_parts[0] if path_parts else ""

def is_7plus_episode_url(value):
    return bool(re.search(r"[?&]episode-id=", value or "", re.IGNORECASE))

def print_show_url_required(series_url, selector=None):
    show_slug = parse_show_slug(series_url)
    suggested_url = f"{BASE_URL}/{show_slug}" if show_slug else "the show URL without ?episode-id=..."
    print(f"{bcolors.WARNING}{icons.ICON_WARNING} 7Plus selector modes need a show URL, not an episode URL.{bcolors.ENDC}")
    if selector:
        print(f"{bcolors.YELLOW}{icons.ICON_INFO} Try: {suggested_url} -d {selector}{bcolors.ENDC}")
    else:
        print(f"{bcolors.YELLOW}{icons.ICON_INFO} Try: {suggested_url} -l{bcolors.ENDC}")

def find_episode_id_in_component(value):
    if isinstance(value, dict):
        for key in ("playerId", "catalogueNumber"):
            if value.get(key):
                return str(value[key])

        player_data = value.get("playerData")
        if isinstance(player_data, dict) and player_data.get("episodePlayerId"):
            return str(player_data["episodePlayerId"])

        for key in ("videoUrl", "url"):
            match = re.search(r"referenceId=ref:([^&]+)", str(value.get(key) or ""))
            if match:
                return match.group(1)

        for child in value.values():
            found = find_episode_id_in_component(child)
            if found:
                return found

    if isinstance(value, list):
        for child in value:
            found = find_episode_id_in_component(child)
            if found:
                return found

    return ""

def collect_episode_ids_in_component(value, episode_ids=None):
    episode_ids = episode_ids if episode_ids is not None else set()

    if isinstance(value, dict):
        for key in ("playerId", "catalogueNumber"):
            if value.get(key):
                episode_ids.add(str(value[key]))

        player_data = value.get("playerData")
        if isinstance(player_data, dict) and player_data.get("episodePlayerId"):
            episode_ids.add(str(player_data["episodePlayerId"]))

        for key in ("videoUrl", "url"):
            match = re.search(r"referenceId=ref:([^&]+)", str(value.get(key) or ""))
            if match:
                episode_ids.add(match.group(1))

        for child in value.values():
            collect_episode_ids_in_component(child, episode_ids)

    if isinstance(value, list):
        for child in value:
            collect_episode_ids_in_component(child, episode_ids)

    return episode_ids

def is_single_asset_7plus_page(series_data):
    featured = find_featured_metadata(series_data)
    subtitle = str(featured.get("subtitle") or "")
    if re.search(r"\bS\d+\s+E\d+\b|Season\s+\d+\s+Episode\s+\d+", subtitle, re.IGNORECASE):
        return False

    return len(collect_episode_ids_in_component(series_data)) == 1

def resolve_short_7plus_video_url(video_url, session=None, auth_token=None):
    if "episode-id=" in video_url:
        return video_url

    show_slug = parse_show_slug(video_url)
    if not show_slug:
        return video_url

    try:
        series_data = get_series_data(show_slug, session=session, auth_token=auth_token)
    except Exception:
        return video_url

    if not is_single_asset_7plus_page(series_data):
        return video_url

    episode_id = find_episode_id_in_component(series_data)
    if not episode_id:
        return video_url

    return f"{BASE_URL}/{show_slug}?episode-id={episode_id}"

def get_json(session, url, referer_path, auth_token=None):
    response = session.get(
        url,
        headers=_default_headers(referer_path, auth_token),
        timeout=(8, 25),
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.json()

def get_series_data(show_slug, session=None, auth_token=None):
    session = session or _session_with_retries()
    signed_up = "true" if auth_token else "false"
    url = (
        f"https://component-cdn.swm.digital/content/{show_slug}"
        f"?platform-id=web&market-id=29&platform-version={PLATFORM_VERSION}"
        f"&api-version=4.9&signedup={signed_up}"
    )
    return get_json(session, url, f"/{show_slug}", auth_token)

def get_season_data(source_url, show_slug, session=None, auth_token=None):
    session = session or _session_with_retries()
    return get_json(session, source_url, f"/{show_slug}", auth_token)

def extract_season_sources(series_data):
    season_sources = []

    for item in series_data.get("items", []) or []:
        for tab in item.get("items", []) or []:
            if tab.get("title") != "Episodes":
                continue

            for container in tab.get("items", []) or []:
                for season in container.get("items", []) or []:
                    source_url = ((season.get("items") or [{}])[0].get("source") or {}).get("url")
                    if not source_url:
                        continue

                    title = str(season.get("title") or season.get("id") or "Episodes").strip()
                    season_sources.append({
                        "label": normalize_season_label(title, container.get("title")),
                        "sort_key": season_sort_key(title),
                        "source_url": source_url,
                    })

    return season_sources

def normalize_season_label(title, container_title=None):
    title = str(title or "").strip()
    if not title:
        return "Episodes"

    if re.fullmatch(r"\d{4}", title):
        return title

    if re.fullmatch(r"\d+", title):
        label = "Season" if str(container_title or "").lower() != "year" else "Year"
        return f"{label} {int(title)}"

    return title

def season_sort_key(title):
    text = str(title or "")
    numbers = [int(value) for value in re.findall(r"\d+", text)]
    primary = numbers[0] if numbers else 0
    after_show = 1 if "after show" in text.lower() else 0
    return (primary, after_show, text.lower())

def extract_episode_numbers(episode, fallback_label):
    candidates = [
        episode.get("playerData", {}).get("image", {}).get("altTag"),
        episode.get("cardData", {}).get("image", {}).get("altTag"),
        episode.get("infoPanelData", {}).get("subtitle"),
        episode.get("playerData", {}).get("title"),
        episode.get("cardData", {}).get("title"),
        episode.get("catalogueNumber"),
    ]

    season = None
    episode_number = None
    for value in candidates:
        text = str(value or "")
        match = re.search(r"Season\s+(\d+)\s+Episode\s+(\d+)", text, re.IGNORECASE)
        if match:
            season = int(match.group(1))
            episode_number = int(match.group(2))
            break

        match = re.search(r"S(\d+)\s*E(\d+)", text, re.IGNORECASE)
        if match:
            season = int(match.group(1))
            episode_number = int(match.group(2))
            break

        match = re.search(r"-S(\d+)T(\d+)", text, re.IGNORECASE)
        if match:
            season = int(match.group(1))
            episode_number = int(match.group(2))
            break

    if season is None:
        label_match = re.search(r"\d+", str(fallback_label or ""))
        season = int(label_match.group(0)) if label_match else 0

    if episode_number is None:
        video_id = str(episode.get("catalogueNumber") or episode.get("playerData", {}).get("episodePlayerId") or "")
        match = re.search(r"-(\d+)$", video_id)
        if match:
            episode_number = int(match.group(1))

    return season, episode_number or 0

def clean_episode_title(episode):
    title = (
        episode.get("cardData", {}).get("title")
        or episode.get("playerData", {}).get("title")
        or episode.get("infoPanelData", {}).get("subtitle")
        or "Unknown Title"
    )
    title = re.sub(r"^S\d+\s+E\d+\s*[-:]\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^\d+\.\s*", "", title)
    title = re.sub(r"^Episode\s+\d+\s*[-:]\s*", "", title, flags=re.IGNORECASE)
    return title.strip() or "Unknown Title"

def collect_episode_details(show_slug, series_data, session=None, auth_token=None):
    session = session or _session_with_retries()
    show_title = series_data.get("title") or show_slug.replace("-", " ").title()
    episode_details = []
    episode_summary = []
    seen_ids = set()

    for season_source in extract_season_sources(series_data):
        season_data = get_season_data(season_source["source_url"], show_slug, session, auth_token)
        label = season_source["label"]
        for episode in season_data.get("mediaItems", []) or []:
            video_id = episode.get("catalogueNumber") or episode.get("playerData", {}).get("episodePlayerId")
            if not video_id or video_id in seen_ids:
                continue

            content_link = episode.get("cardData", {}).get("contentLink", {}).get("url") or f"/{show_slug}?episode-id={video_id}"
            video_url = f"{BASE_URL}{content_link}".replace("&autoplay=true", "").replace("?autoplay=true", "")
            season_number, episode_number = extract_episode_numbers(episode, label)
            title = clean_episode_title(episode)
            thumbnail = episode.get("cardData", {}).get("image", {}).get("url") or episode.get("playerData", {}).get("image", {}).get("url")
            if thumbnail and thumbnail.startswith("https://imagemap.swm.digital/image/"):
                thumbnail = f"https://images.swm.digital/image?u={thumbnail}&q=95&w=320"

            episode_details.append({
                "Video URL": video_url,
                "Video ID": video_id,
                "Show Title": show_title,
                "Title": title,
                "Season": season_number,
                "Season Label": label,
                "Season Sort": season_source["sort_key"],
                "Episode": episode_number,
                "Date Aired": episode.get("infoPanelData", {}).get("airDate", "Not Available"),
                "Description": episode.get("infoPanelData", {}).get("shortSynopsis") or "Not Available",
                "Thumbnail": thumbnail or "Not Available",
            })
            episode_summary.append(f"{show_title} {label} Episode {episode_number} - {title} ID: {video_id}")
            seen_ids.add(video_id)

    episode_details.sort(key=lambda item: (item.get("Season Sort") or (0, 0, ""), item.get("Episode") or 0))
    episode_summary.sort()

    return {
        "Episode Summary": episode_summary,
        "Episode Details": episode_details,
    }

def save_episode_list_json(show_slug, episode_data):
    os.makedirs(TEMP_DIR, exist_ok=True)
    output_path = os.path.join(TEMP_DIR, f"7plus_{safe_windows_filename(show_slug)}_episodes.json")

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(episode_data, file, ensure_ascii=False, indent=4)

    return output_path

def export_episode_list_text(show_slug, episodes):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(EXPORT_DIR, f"7plus_{safe_windows_filename(show_slug)}_export_{timestamp}.txt")

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
        print(f"{bcolors.WARNING}No playable 7Plus episodes found.{bcolors.ENDC}")
        return

    tree_style = "grey70"
    label_style = "bold grey70"
    header_style = "bright_blue"

    groups = {}
    group_sort = {}
    for episode in episodes:
        label = episode.get("Season Label") or f"Season {episode.get('Season') or 0}"
        groups.setdefault(label, []).append(episode)
        group_sort[label] = episode.get("Season Sort") or (episode.get("Season") or 0, 0, label.lower())

    group_labels = sorted(groups, key=lambda label: group_sort[label])
    for group_episodes in groups.values():
        group_episodes.sort(key=lambda item: item.get("Episode") or 0)

    season_summary = ",  ".join(
        f"{label}({len(groups[label])})"
        for label in group_labels
    )

    console.print(Rule(Text.assemble(("7Plus Series: ", f"bold {header_style}"), (series_title, "bold white")), style=header_style))
    console.print()
    console.print(
        Text.assemble(
            (f"{len(group_labels)} Seasons", label_style),
            (f",  {season_summary}" if season_summary else "", "white"),
        )
    )

    for group_index, label in enumerate(group_labels):
        if group_index > 0:
            console.print(Text("│", style=tree_style))

        group_is_last = group_index == len(group_labels) - 1
        group_branch = "└─" if group_is_last else "├─"
        group_child_prefix = "   " if group_is_last else "│  "
        group_episodes = groups[label]
        console.print(
            Text.assemble(
                (f"{group_branch} ", tree_style),
                (f"{label}: ", label_style),
                (f"{len(group_episodes)} episodes", "white"),
            )
        )

        for index, episode in enumerate(group_episodes):
            is_last = index == len(group_episodes) - 1
            branch = "└─" if is_last else "├─"
            url_branch = "  " if is_last else "│ "
            episode_number = episode.get("Episode") or "-"

            console.print(
                Text.assemble(
                    (group_child_prefix, tree_style),
                    (f"{branch} ", tree_style),
                    (f"{episode_number}. ", label_style),
                    (episode.get("Title") or "-", "white"),
                )
            )
            console.print(
                Text.assemble(
                    (group_child_prefix, tree_style),
                    (f"{url_branch} ", tree_style),
                    (episode.get("Video URL") or "-", "bright_blue"),
                )
            )

def list_show_episodes(series_url, cookies_path=None, export_list=False):
    if is_7plus_episode_url(series_url):
        print_show_url_required(series_url)
        return

    show_slug = parse_show_slug(series_url)
    if not show_slug:
        raise ValueError("Could not determine 7Plus show slug from the URL.")

    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    session = _session_with_retries()
    auth_token = None
    try:
        series_data = get_series_data(show_slug, session=session)
        episode_data = collect_episode_details(show_slug, series_data, session=session)
    except Exception:
        if not cookies_path:
            raise
        session, auth_token = get_authenticated_session(series_url, cookies_path)
        series_data = get_series_data(show_slug, session=session, auth_token=auth_token)
        episode_data = collect_episode_details(show_slug, series_data, session=session, auth_token=auth_token)

    episodes = episode_data["Episode Details"]
    series_title = series_data.get("title") or show_slug.replace("-", " ").title()
    output_path = save_episode_list_json(show_slug, episode_data)

    try:
        console.print()
        print_episode_list(series_title, episodes)
        print(f"\n{bcolors.OKGREEN}{icons.ICON_SUCCESS} Found {len(episodes)} episode(s){bcolors.ENDC}")
        if export_list:
            export_path = export_episode_list_text(show_slug, episodes)
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

def get_series_episodes(series_url, cookies_path=None):
    show_slug = parse_show_slug(series_url)
    if not show_slug:
        raise ValueError("Could not determine 7Plus show slug from the URL.")

    session = _session_with_retries()
    try:
        series_data = get_series_data(show_slug, session=session)
        episode_data = collect_episode_details(show_slug, series_data, session=session)
    except Exception:
        if not cookies_path:
            raise
        session, auth_token = get_authenticated_session(series_url, cookies_path)
        series_data = get_series_data(show_slug, session=session, auth_token=auth_token)
        episode_data = collect_episode_details(show_slug, series_data, session=session, auth_token=auth_token)

    return show_slug, episode_data["Episode Details"]

def is_extra_season_label(episode):
    label = str(episode.get("Season Label") or "").lower()
    return "after show" in label

def select_episodes(series_url, selector, cookies_path=None):
    parsed_selector = parse_download_selector(selector)
    show_slug, episodes = get_series_episodes(series_url, cookies_path)
    selected = []
    for item in episodes:
        if is_extra_season_label(item):
            continue

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
        series_title = episodes[0].get("Show Title") if episodes else show_slug.replace("-", " ").title()
        raise LookupError(f"No 7Plus episodes found for selector {normalized} in {series_title}.")

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

def download_selected_episodes(series_url, selector, downloads_path, wvd_device_path, cookies_path):
    if is_7plus_episode_url(series_url):
        print_show_url_required(series_url, selector)
        return

    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    try:
        episodes = select_episodes(series_url, selector, cookies_path)
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
        main(episode["Video URL"], downloads_path, wvd_device_path, cookies_path, mode="auto", export_list=False, download_selector=None, auto_download=True)

def get_authenticated_session(video_url, cookies_path):
    """
    Reuses cached auth, refreshes it, or loads cookies for Gigya -> id_token -> 7plus auth flow,
    and returns (session, auth_token).
    """
    config = ensure_7plus_cache(load_config())
    cache = config["7plus"]["cache"]["auth"]

    # Reuse your retry-capable session factory + browser-y headers
    session = _session_with_retries()
    cached_token = cache.get("token", "")
    cached_expiry = cache.get("expiry", "")

    if token_is_valid(cached_token, cached_expiry):
        _PRINT(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Using cached 7Plus auth token{bcolors.ENDC}")
        return session, cached_token

    refreshed = refresh_7plus_auth_token(cache.get("refresh_token", ""))
    if refreshed:
        _PRINT(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Refreshed 7Plus auth token{bcolors.ENDC}")
        return session, cache_7plus_auth(config, refreshed)

    # Load your exported cookies
    cookies = MozillaCookieJar(cookies_path)
    cookies.load(ignore_discard=True, ignore_expires=True)
    session.cookies = cookies

    # Touch the page once to refresh cookie flags
    headers = _default_headers("/", auth_token=None)
    try:
        session.get(video_url, headers=headers, timeout=(8, 25))
    except Exception:
        pass

    # Find Gigya APIKey + login_token from glt_<APIKEY> cookie
    api_key, login_token = None, None
    for c in cookies:
        if c.name.startswith('glt_'):
            api_key = c.name[4:]
            login_token = c.value
            break
    if not api_key or not login_token:
        raise RuntimeError("Failed to find Gigya cookies (glt_*). Export cookies while logged in.")

    # Gigya -> id_token
    login_url = "https://login.7plus.com.au/accounts.getJWT"
    login_params = {
        "APIKey": api_key,
        "sdk": "js_latest",
        "login_token": login_token,
        "authMode": "cookie",
        "pageURL": "https://7plus.com.au/",
        "sdkBuild": "12471",
        "format": "json",
    }
    r = session.get(login_url, params=login_params, headers=headers, timeout=(8, 25))
    r.raise_for_status()
    id_token = r.json().get("id_token")
    if not id_token:
        raise RuntimeError("No id_token returned by Gigya.")

    # id_token -> 7plus auth token (Bearer)
    auth_data = exchange_7plus_id_token(session, id_token, headers)
    auth_token = cache_7plus_auth(config, auth_data)
    _PRINT(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} 7Plus auth token cache updated{bcolors.ENDC}")

    return session, auth_token

def get_pssh(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to load MPD, status code: {response.status_code}{bcolors.ENDC}")
        return None
    mpd_xml = etree.fromstring(response.content)
    pssh_elements = mpd_xml.xpath('.//cenc:pssh', namespaces={'cenc': 'urn:mpeg:cenc:2013'})
    if not pssh_elements:
        print(f"{bcolors.FAIL}Failed to find PSSH in MPD{bcolors.ENDC}")
        return None
    return pssh_elements[0].text

# Function to extract video details
def extract_info(video_url, cookies_path=None, session=None, auth_token=None):
    # Try anonymous playback first. Keep the old cookie/Gigya flow as fallback.
    if session is None:
        session = _session_with_retries()

    headers = _default_headers("/", auth_token)

    # Playback endpoint + params 
    media_url = 'https://videoservice.swm.digital/playback'
    _, episode_id = parse_7plus_url(video_url)
    media_params = {
        'appId': '7plus',
        'deviceType': 'web',
        'platformType': 'web',
        'accountId': 5303576322001,
        'referenceId': 'ref:' + episode_id,
        'deliveryId': 'csai',
        'videoType': 'vod',
    }

    # First attempt (keep-alive)
    try:
        r = session.get(media_url, params=media_params, headers=headers, timeout=(8, 25))
        r.raise_for_status()
    except Exception:
        try:
            # Fallback: force Connection: close (helps when server drops keep-alive)
            r = session.get(
                media_url, params=media_params,
                headers=_default_headers("/", auth_token, conn_close=True),
                timeout=(8, 25)
            )
            r.raise_for_status()
        except Exception:
            if auth_token or not cookies_path:
                raise
            _PRINT(f"{bcolors.WARNING}{icons.ICON_WARNING} Anonymous 7Plus playback failed; falling back to cookies...{bcolors.ENDC}")
            session, auth_token = get_authenticated_session(video_url, cookies_path)
            headers = _default_headers("/", auth_token, conn_close=True)
            r = session.get(media_url, params=media_params, headers=headers, timeout=(8, 25))
            r.raise_for_status()

    media_resp = r.json()

    media = media_resp.get('media', {})
    sources = media.get('sources', [])
    mpd_url = None
    license_url = None
    m3u8_url = None

    for source in sources:
        if source.get('type') == 'application/dash+xml' and "playready" not in source.get('src'):
            mpd_url = source.get('src')
            key_systems = source.get('key_systems', {})
            widevine = key_systems.get('com.widevine.alpha', {})
            license_url = widevine.get('license_url')
            break
        elif source.get('type') == 'application/x-mpegURL' and 'master.m3u8' in source.get('src') and "fairplay" not in source.get('src'):
            m3u8_url = source.get('src')
            break

    if mpd_url and license_url:
        max_height = get_max_height_from_mpd(mpd_url)
        if max_height and max_height < 720 and not auth_token and cookies_path:
            session, auth_token = get_authenticated_session(video_url, cookies_path)
            return extract_info(video_url, cookies_path, session=session, auth_token=auth_token)

        pssh = get_pssh(mpd_url)
        if not pssh:
            print("Failed to extract PSSH from MPD")
            return None
        return {
            'formats': [{'url': mpd_url, 'ext': 'mpd', 'pssh': pssh}],
            'license_url': license_url,
        }
    elif m3u8_url:
        max_height = get_max_height_from_m3u8(m3u8_url)
        if max_height and max_height < 720 and not auth_token and cookies_path:
            session, auth_token = get_authenticated_session(video_url, cookies_path)
            return extract_info(video_url, cookies_path, session=session, auth_token=auth_token)

        return {
            'formats': [{'url': m3u8_url, 'ext': 'm3u8'}]
        }
    else:
        print("No suitable source found for video")
        return None

# Function to get decryption keys
def get_keys(pssh, lic_url, wvd_device_path):
    try:
        pssh = PSSH(pssh)
    except binascii.Error as e:
        print(f"Could not decode PSSH data as Base64: {e}")
        return []

    device = Device.load(wvd_device_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    challenge = cdm.get_license_challenge(session_id, pssh)
    
    headers = {
        'Content-Type': 'application/dash+xml',
        'Origin': 'https://7plus.com.au',
        'Referer': 'https://7plus.com.au',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    }

    licence = requests.post(lic_url, headers=headers, data=challenge)
    
    try:
        licence.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"HTTPError: {e}")
        print(f"Response Headers: {licence.headers}")
        print(f"Response Text: {licence.text}")
        raise

    cdm.parse_license(session_id, licence.content)
    keys = [f"{str(key.kid).replace('-', '')}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
    cdm.close(session_id)

    return keys

# Function to get the maximum video resolution from the MPD manifest
def get_resolution_from_mpd(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to load MPD, status code: {response.status_code}{bcolors.ENDC}")
        return None

    mpd_xml = etree.fromstring(response.content)
    representations = mpd_xml.xpath('//default:Representation', namespaces={'default': 'urn:mpeg:dash:schema:mpd:2011'})
    if not representations:
        print(f"{bcolors.FAIL}Failed to find Representations in MPD{bcolors.ENDC}")
        return None

    best_representation = representations[-1]
    height = best_representation.attrib.get('height')
    return f"{height}p" if height else None

def get_max_height_from_mpd(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code != 200:
        return 0

    mpd_xml = etree.fromstring(response.content)
    representations = mpd_xml.xpath('//default:Representation', namespaces={'default': 'urn:mpeg:dash:schema:mpd:2011'})
    heights = []
    for rep in representations:
        height = rep.attrib.get('height')
        if str(height or "").isdigit():
            heights.append(int(height))
    return max(heights) if heights else 0

# Function to get the maximum video resolution from the M3U8 manifest
def get_resolution_from_m3u8(m3u8_url):
    response = requests.get(m3u8_url)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to load M3U8, status code: {response.status_code}{bcolors.ENDC}")
        return None
    lines = response.text.split('\n')
    resolutions = [re.search(r'RESOLUTION=(\d+x\d+)', line) for line in lines if line.startswith('#EXT-X-STREAM-INF')]
    resolutions = [res.group(1) for res in resolutions if res]
    if not resolutions:
        print(f"{bcolors.FAIL}Failed to find RESOLUTION in M3U8{bcolors.ENDC}")
        return None
    best_resolution = max(resolutions, key=lambda r: int(r.split('x')[1]))
    return f"{best_resolution.split('x')[1]}p"

def get_max_height_from_m3u8(m3u8_url):
    response = requests.get(m3u8_url)
    if response.status_code != 200:
        return 0

    heights = [
        int(match.group(1))
        for match in re.finditer(r"RESOLUTION=\d+x(\d+)", response.text)
    ]
    return max(heights) if heights else 0

def get_mpd_streams(mpd_url):
    streams = []
    response = requests.get(mpd_url)
    if response.status_code != 200:
        _PRINT(f"{bcolors.FAIL}Failed to load MPD, status code: {response.status_code}{bcolors.ENDC}")
        return streams

    mpd_xml = etree.fromstring(response.content)
    adaptation_sets = mpd_xml.xpath('//default:AdaptationSet', namespaces={'default': 'urn:mpeg:dash:schema:mpd:2011'})
    for adaptation in adaptation_sets:
        mime_type = adaptation.attrib.get("mimeType", "")
        content_type = adaptation.attrib.get("contentType", "")
        language = adaptation.attrib.get("lang", "")
        if "video" in mime_type or content_type == "video":
            stream_type = "video"
        elif "audio" in mime_type or content_type == "audio":
            stream_type = "audio"
        elif "text" in mime_type or "ttml" in mime_type or content_type == "text":
            stream_type = "subtitle"
        else:
            stream_type = "stream"

        representations = adaptation.xpath('./default:Representation', namespaces={'default': 'urn:mpeg:dash:schema:mpd:2011'})
        for rep in representations:
            width = rep.attrib.get("width")
            height = rep.attrib.get("height")
            bandwidth = rep.attrib.get("bandwidth")
            streams.append({
                "type": stream_type,
                "resolution": f"{width}x{height}" if width and height else "",
                "bandwidth": int(bandwidth) if str(bandwidth or "").isdigit() else 0,
                "codecs": rep.attrib.get("codecs") or adaptation.attrib.get("codecs", ""),
                "language": language,
            })
    return sorted(streams, key=lambda item: (item["type"] != "video", -item["bandwidth"]))

def get_m3u8_streams(m3u8_url):
    streams = []
    response = requests.get(m3u8_url)
    if response.status_code != 200:
        _PRINT(f"{bcolors.FAIL}Failed to load M3U8, status code: {response.status_code}{bcolors.ENDC}")
        return streams

    pending = None
    for line in response.text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            resolution = re.search(r"RESOLUTION=(\d+x\d+)", line)
            bandwidth = re.search(r"BANDWIDTH=(\d+)", line)
            codecs = re.search(r'CODECS="([^"]+)"', line)
            pending = {
                "type": "video",
                "resolution": resolution.group(1) if resolution else "",
                "bandwidth": int(bandwidth.group(1)) if bandwidth else 0,
                "codecs": codecs.group(1) if codecs else "",
                "language": "",
            }
        elif pending and line and not line.startswith("#"):
            streams.append(pending)
            pending = None
    return sorted(streams, key=lambda item: item["bandwidth"], reverse=True)

def print_streams(streams):
    if not streams:
        _PRINT(f"\n{bcolors.WARNING}No stream variants found.{bcolors.ENDC}")
        return

    _PRINT(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    header = f"  {'#':>2}  {'Type':<4} {'Resolution':<10} {'Bitrate':<16} {'Codec':<18} {'Lang':<5}"
    divider = f"  {'-' * 2}  {'-' * 4} {'-' * 10} {'-' * 16} {'-' * 18} {'-' * 5}"
    _PRINT(header)
    _PRINT(divider)
    for idx, stream in enumerate(streams, start=1):
        kbps = round(stream.get("bandwidth", 0) / 1000)
        bitrate = f"{kbps} Kbps" if kbps else "unknown bitrate"
        codecs = stream.get("codecs") or "unknown codecs"
        stream_type = stream.get("type", "stream")
        if stream_type == "video":
            label = "Vid"
            resolution = stream.get("resolution") or "-"
        elif stream_type == "audio":
            label = "Aud"
            resolution = "-"
        elif stream_type == "subtitle":
            label = "Sub"
            resolution = "-"
        else:
            label = "Stream"
            resolution = stream.get("resolution") or "-"
        language = stream.get("language") or "-"
        _PRINT(f"  {idx:>2}  {label:<4} {resolution:<10} {bitrate:<16} {codecs:<18} {language:<5}")

def build_7plus_command(url, downloads_path, formatted_file_name, keys=None, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    download_command = (
        f'N_m3u8DL-RE "{url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}"'
    )
    download_command = append_downloader_proxy(download_command)
    if keys:
        download_command += " --key " + " --key ".join(keys)
    return download_command

def find_featured_metadata(show_response):
    for item in show_response.get("items", []) or []:
        if item.get("type") == "featuredShowHeader":
            return item
    return {}

def clean_info_episode_title(value):
    title = str(value or "").strip()
    title = re.sub(r"^S\d+\s+E\d+\s*[-:]\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^Season\s+\d+\s+Episode\s+\d+\s*[-:]\s*", "", title, flags=re.IGNORECASE)
    return title.strip() or None

def format_info_date(value, production_year=None):
    text = str(value or "").strip()
    match = re.fullmatch(r"Added\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})", text, re.IGNORECASE)
    if match and production_year:
        day, month = match.groups()
        try:
            parsed = dt.datetime.strptime(f"{int(day)} {month[:3]} {production_year}", "%d %b %Y")
            return parsed.strftime("%d %B %Y").lstrip("0")
        except ValueError:
            pass
    return text

def print_info_metadata(show_response):
    if not show_response:
        return

    featured = find_featured_metadata(show_response)
    page_metadata = show_response.get("pageMetaData") or {}
    fields = [
        ("Show", show_response.get("title") or featured.get("title")),
        ("Title", clean_info_episode_title(featured.get("subtitle") or page_metadata.get("pageTitle"))),
        ("Date Aired", format_info_date(featured.get("airDate"), featured.get("productionYear"))),
        ("Description", featured.get("shortSynopsis") or page_metadata.get("description")),
    ]
    visible_fields = [(label, str(value).strip()) for label, value in fields if value and str(value).strip() and str(value).strip() != "Not Available"]
    if not visible_fields:
        return

    _PRINT(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in visible_fields:
        _PRINT(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

def print_7plus_info(source_url, source_type, formatted_file_name, lic_url=None, pssh=None, keys=None, metadata=None):
    if source_type == "mpd":
        _PRINT(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{source_url}")
        if lic_url:
            _PRINT(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
        if pssh:
            _PRINT(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
        for key in keys or []:
            _PRINT(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
        print_streams(get_mpd_streams(source_url))
    else:
        _PRINT(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{source_url}")
        print_streams(get_m3u8_streams(source_url))
    print_info_metadata(metadata or {})
    _PRINT(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")

def season_episode_from_episode_id(episode_id):
    match = re.search(r'^[A-Z]+(?P<season>\d{2,4})-(?P<episode>\d+)$', episode_id or "", re.IGNORECASE)
    if not match:
        return ""

    season = match.group("season")
    episode = match.group("episode")

    if len(season) == 2 and int(season) >= 20:
        season = f"20{season}"
    elif len(season) == 2:
        season = season.zfill(2)

    return f"S{season}E{int(episode):02d}"

def season_episode_from_metadata(alt_tag, episode_id):
    match = re.search(r'Season\s+(\d+)\s+Episode\s+(\d+)', alt_tag or "", re.IGNORECASE)
    if not match:
        match = re.search(r'\bS\s*(\d+)\s*E\s*(\d+)\b', alt_tag or "", re.IGNORECASE)
    if match:
        season, episode = match.groups()
        return f"S{season.zfill(2)}E{episode.zfill(2)}"
    return season_episode_from_episode_id(episode_id)

def is_movie_metadata(show_response, episode_id):
    featured = find_featured_metadata(show_response)
    if not featured:
        return False

    if featured.get("playerId") and featured.get("playerId") != episode_id:
        return False

    page_metadata = show_response.get("pageMetaData") or {}
    alt_tag = (page_metadata.get("objectGraphImage") or {}).get("altTag") or ""
    episode_text = " ".join(
        str(value or "")
        for value in (
            alt_tag,
            featured.get("subtitle"),
            featured.get("title"),
        )
    )
    if re.search(r"Season\s+\d+\s+Episode\s+\d+|\bS\s*\d+\s*E\s*\d+\b", episode_text, re.IGNORECASE):
        return False

    return bool(featured.get("playerId") or featured.get("duration") or featured.get("productionYear"))

def fetch_show_metadata(show_name, episode_id, session=None, auth_token=None):
    session = session or _session_with_retries()
    signed_up = "true" if auth_token else "false"
    show_api_url = (
        f"https://component-cdn.swm.digital/content/{show_name}"
        f"?episode-id={episode_id}"
        f"&platform-id=web&market-id=29&platform-version={PLATFORM_VERSION}&api-version=4.9&signedup={signed_up}"
    )
    headers = _default_headers(f"/{show_name}", auth_token)
    response = session.get(show_api_url, headers=headers, timeout=(8, 25))
    response.raise_for_status()
    return response.json()

# Function to format and display download command
def get_download_command(info, show_title, season_episode_tag, downloads_path, wvd_device_path, mode="auto", auto_download=False, metadata=None):
    formats = info.get('formats')
    if not formats:
        print(f"{bcolors.FAIL}No formats found in info{bcolors.ENDC}")
        return

    format_info = formats[0]
    url = format_info.get('url')
    ext = format_info.get('ext')
    resolution = None

    if ext == 'mpd':
        pssh = format_info.get('pssh')
        lic_url = info.get('license_url')
        if url and lic_url and pssh:
            keys = get_keys(pssh, lic_url, wvd_device_path)
            resolution = get_resolution_from_mpd(url)
            if not resolution:
                resolution = "best"

            formatted_file_name = f"{show_title}"
            if season_episode_tag:
                formatted_file_name += f".{season_episode_tag}"
            formatted_file_name += f".{resolution}.7PLUS.WEB-DL.AAC2.0.H.264"
            if mode == "info":
                print_7plus_info(url, "mpd", formatted_file_name, lic_url, pssh, keys, metadata)
                return
            download_command = build_7plus_command(url, downloads_path, formatted_file_name, keys, interactive=(mode == "interactive"))
        
        # -- VISIBLE OUTPUT (encrypted MPD) -----------------------------------
        _PRINT(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{url}")
        _PRINT(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
        _PRINT(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
        for key in keys:
            _PRINT(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
        _PRINT(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
        _PRINT(mask_proxy_command(download_command))
        # ---------------------------------------------------------------------
        
        user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == 'y':
            _PRINT(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
            result = subprocess.run(download_command, shell=True)
            if result.returncode == 0:
                _PRINT(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
        else:
            _PRINT(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
    elif ext == 'm3u8':
        if url:
            resolution = get_resolution_from_m3u8(url)
            if not resolution:
                resolution = "best"
            formatted_file_name = f"{show_title}"
            if season_episode_tag:
                formatted_file_name += f".{season_episode_tag}"
            formatted_file_name += f".{resolution}.7PLUS.WEB-DL.AAC2.0.H.264"
            if mode == "info":
                print_7plus_info(url, "m3u8", formatted_file_name, metadata=metadata)
                return
            download_command = build_7plus_command(url, downloads_path, formatted_file_name, interactive=(mode == "interactive"))
            
            # -- VISIBLE OUTPUT (unencrypted m3u8) -----------------------------
            _PRINT(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{url}")
            _PRINT(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
            _PRINT(mask_proxy_command(download_command))
            # ------------------------------------------------------------------
            
            user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
            if user_input == 'y':
                _PRINT(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
                result = subprocess.run(download_command, shell=True)
                if result.returncode == 0:
                    _PRINT(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
            else:
                _PRINT(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
        else:
            print(f"{bcolors.FAIL}Failed to retrieve necessary information for download{bcolors.ENDC}")       

# Main logic
def main(video_url, downloads_path, wvd_device_path, cookies_path, mode="auto", export_list=False, download_selector=None, auto_download=False): 
    if mode == "list":
        list_show_episodes(video_url, cookies_path, export_list)
        return

    if mode == "download":
        download_selected_episodes(video_url, download_selector, downloads_path, wvd_device_path, cookies_path)
        return

    try:
        show_name, episode_id = parse_7plus_url(video_url)
    except Exception as e:
        if "episode-id=" not in video_url and parse_show_slug(video_url):
            _PRINT(f"{bcolors.WARNING}{icons.ICON_WARNING} 7Plus series URLs need a flag.{bcolors.ENDC}")
            _PRINT(f"{bcolors.YELLOW}{icons.ICON_INFO} Use -l to list episodes or -d with a selector to download from a series.{bcolors.ENDC}")
            _PRINT(f"{bcolors.YELLOW}{icons.ICON_INFO} For a single episode, use the expanded 7Plus URL with ?episode-id=...{bcolors.ENDC}")
            return
        _PRINT(f"{bcolors.FAIL}Could not parse 7Plus URL: {e}{bcolors.ENDC}")
        return

    resolved_video_url = f"{BASE_URL}/{show_name}?episode-id={episode_id}"
    info = extract_info(resolved_video_url, cookies_path)
    if not info:
        _PRINT(f"{bcolors.FAIL}Failed to extract 7Plus playback information{bcolors.ENDC}")
        return

    try:
        show_response = fetch_show_metadata(show_name, episode_id)
    except Exception:
        session, auth_token = get_authenticated_session(video_url, cookies_path)
        show_response = fetch_show_metadata(show_name, episode_id, session=session, auth_token=auth_token)

    try:
        show_title = show_response["title"].replace(" ", ".")
        alt_tag = show_response["pageMetaData"]["objectGraphImage"]["altTag"]
    except Exception as e:
        _PRINT(f"{bcolors.FAIL}Failed to parse 7Plus show metadata: {e}{bcolors.ENDC}")
        return

    season_episode_tag = "" if is_movie_metadata(show_response, episode_id) else season_episode_from_metadata(alt_tag, episode_id)
    get_download_command(info, show_title, season_episode_tag, downloads_path, wvd_device_path, mode, auto_download, show_response)
    return
