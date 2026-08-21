import re
from dataclasses import dataclass
from pathlib import Path


URL_RE = re.compile(r"https?://[^\s\t]+", re.IGNORECASE)


@dataclass(frozen=True)
class BatchEntry:
    url: str
    source_path: Path
    line_number: int
    label: str = ""


def clean_export_url(value):
    value = str(value or "").strip().strip("<>").strip("\"'")
    return value.rstrip(".,;")


def extract_url_from_line(line):
    match = URL_RE.search(line)
    if not match:
        return ""
    return clean_export_url(match.group(0))


def extract_label_from_line(line, url):
    if "\t" not in line:
        return ""

    parts = [part.strip() for part in line.split("\t")]
    label_parts = [part for part in parts if part and part != url and not URL_RE.search(part)]
    return " ".join(label_parts[:2]).strip()


def iter_export_files(export_dir):
    export_path = Path(export_dir)
    if not export_path.exists():
        return []
    return sorted(path for path in export_path.glob("*.txt") if path.is_file())


def load_batch_entries(export_dir):
    entries = []
    seen_urls = set()

    for path in iter_export_files(export_dir):
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()

        for line_number, line in enumerate(lines, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            url = extract_url_from_line(line)
            if not url or url in seen_urls:
                continue

            seen_urls.add(url)
            entries.append(BatchEntry(
                url=url,
                source_path=path,
                line_number=line_number,
                label=extract_label_from_line(line, url),
            ))

    return entries


def source_counts(entries):
    counts = {}
    for entry in entries:
        counts[entry.source_path] = counts.get(entry.source_path, 0) + 1
    return counts
