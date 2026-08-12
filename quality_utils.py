import re


def normalize_quality(value):
    if value is None:
        return None

    quality = str(value).strip().lower()
    if quality.endswith("p"):
        quality = quality[:-1]

    if not quality.isdigit() or int(quality) <= 0:
        raise ValueError("Quality must be a positive height such as 720 or 1080.")

    return str(int(quality))


def video_selector(quality=None, default="best"):
    quality = normalize_quality(quality)
    selector = f"res={quality}" if quality else default
    return f"--select-video {selector}"


def quality_tag(quality):
    quality = normalize_quality(quality)
    return f"{quality}p" if quality else None


def apply_quality_to_filename(filename, quality=None):
    tag = quality_tag(quality)
    if not tag:
        return filename

    if re.search(r"\.(\d{3,4})p\.", filename) or ".best." in filename.lower():
        return re.sub(r"\.(?:\d{3,4}p|best)\.", f".{tag}.", filename, count=1, flags=re.IGNORECASE)

    service_tags = ("ABCiView", "TVNZ", "ThreeNow", "SBS", "7PLUS", "10Play", "BROLLIE", "MPLUS")
    for service_tag in service_tags:
        marker = f".{service_tag}."
        if marker in filename:
            return filename.replace(marker, f".{tag}{marker}", 1)

    return filename
