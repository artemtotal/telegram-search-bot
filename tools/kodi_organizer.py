import argparse
import json
import logging
import re
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv", ".webm"}


def _clean_separators(value: str) -> str:
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -._")


def parse_tv_show(value: str):
    name = Path(value).name
    name = re.sub(r"\.(mkv|mp4|avi|m4v|mov|ts|m2ts|wmv|webm)$", "", name, flags=re.I)
    match = re.search(r"(?i)(?:^|[ ._\-])S(\d{1,2})(?:E\d{1,3})?(?=$|[ ._\-])", name)
    if not match:
        return None
    title = _clean_separators(name[: match.start()])
    title = re.sub(r"\s+(?:19|20)\d{2}$", "", title)
    aliases = {
        "la casa de papel": "Money Heist",
    }
    title = aliases.get(title.lower(), title)
    if not title:
        return None
    return title, int(match.group(1))


def clean_movie_title(value: str) -> str:
    name = Path(value).name
    name = re.sub(r"\.(mkv|mp4|avi|m4v|mov|ts|m2ts|wmv|webm)$", "", name, flags=re.I)
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", name)
    if match:
        title = _clean_separators(name[: match.start()])
        return f"{title} ({match.group(1)})"
    stop = re.search(
        r"(?i)(?:^|[ ._\-])(?:2160p|1080p|720p|480p|uhd|bluray|blu-ray|bdremux|remux|web-dl|webrip|hdtv|hdr|dv)(?:$|[ ._\-])",
        name,
    )
    return _clean_separators(name[: stop.start()] if stop else name)


def _find_video_files(source: Path):
    if source.is_file():
        return [source] if source.suffix.lower() in VIDEO_EXTENSIONS else []
    return [path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def organize(category: str, content_path: str, library_root: Optional[str] = None, dry_run: bool = False):
    category_key = category.strip().lower()
    if category_key not in {"movies", "series"}:
        return {"status": "ignored", "reason": "category", "category": category}

    source = Path(content_path)
    if not source.exists() and not dry_run:
        raise FileNotFoundError(content_path)

    if category_key == "series":
        parsed = parse_tv_show(source.name)
        if not parsed:
            for video in _find_video_files(source):
                parsed = parse_tv_show(video.name)
                if parsed:
                    break
        if not parsed:
            raise ValueError(f"Cannot identify TV show and season: {source.name}")
        show, season = parsed
        root = Path(library_root) if library_root else Path(r"C:\Media\Series")
        if re.fullmatch(r"(?i)Season \d{2}", source.name) and source.parent.parent == root:
            destination_dir = source
        else:
            destination_dir = root / show / f"Season {season:02d}"
    else:
        movie = clean_movie_title(source.name)
        root = Path(library_root) if library_root else Path(r"C:\Media\Movies")
        if source.is_file() and source.parent.parent == root and source.parent.name == movie:
            destination_dir = source.parent
        else:
            destination_dir = root / movie

    if source == destination_dir or (source.is_file() and source.parent == destination_dir):
        return {
            "status": "organized",
            "category": category,
            "source": str(source),
            "destination": str(destination_dir),
            "files": [],
        }

    result = {
        "status": "dry-run" if dry_run else "organized",
        "category": category,
        "source": str(source),
        "destination": str(destination_dir),
        "files": [],
    }
    if dry_run:
        return result

    videos = _find_video_files(source)
    if not videos:
        raise ValueError(f"No video files found: {source}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    for video in videos:
        destination = _unique_destination(destination_dir / video.name)
        shutil.move(str(video), str(destination))
        result["files"].append({"source": str(video), "destination": str(destination)})

    if source.is_dir():
        for directory in sorted(source.rglob("*"), reverse=True):
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        try:
            source.rmdir()
        except OSError:
            pass
    return result


def organize_series_library(
    library_root: str = r"C:\Media\Series",
    dry_run: bool = False,
    minimum_size_bytes: int = 100 * 1024 * 1024,
):
    root = Path(library_root)
    if not root.is_dir():
        raise FileNotFoundError(library_root)

    result = {
        "status": "dry-run" if dry_run else "organized",
        "root": str(root),
        "planned": 0,
        "moved": 0,
        "skipped_incomplete": 0,
        "skipped_existing": 0,
        "unrecognized": 0,
        "files": [],
    }
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    for video in candidates:
        parsed = parse_tv_show(video.name)
        if not parsed:
            result["unrecognized"] += 1
            continue
        if video.stat().st_size < minimum_size_bytes:
            result["skipped_incomplete"] += 1
            continue

        show, season = parsed
        destination = root / show / f"Season {season:02d}" / video.name
        if video == destination:
            continue
        if destination.exists():
            result["skipped_existing"] += 1
            continue

        result["planned"] += 1
        result["files"].append(
            {"source": str(video), "destination": str(destination)}
        )
        if dry_run:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(video), str(destination))
        result["moved"] += 1

    if not dry_run:
        directories = [path for path in root.rglob("*") if path.is_dir()]
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
    return result


def plan_added_torrent(category: str, torrent_name: str, files: list[dict]):
    category_key = category.strip().lower()
    if category_key not in {"movies", "series"} or not files:
        return {"status": "ignored"}

    first_name = files[0]["name"].replace("\\", "/")
    root = first_name.split("/", 1)[0]
    if category_key == "series":
        parsed = parse_tv_show(torrent_name) or parse_tv_show(first_name)
        if not parsed or "/" not in first_name:
            return {"status": "ignored"}
        show, season = parsed
        return {"rename_folder": [root, f"{show}/Season {season:02d}"]}

    if "/" in first_name:
        return {"status": "ignored"}
    movie = clean_movie_title(torrent_name)
    return {"rename_file": [first_name, f"{movie}/{first_name}"]}


def _api_post(api_url: str, endpoint: str, values: dict):
    data = urllib.parse.urlencode(values).encode()
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/v2/{endpoint.lstrip('/')}", data=data, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"qBittorrent returned HTTP {response.status}")


def enable_streaming(torrent_hash: str, post):
    if not torrent_hash:
        return
    post("torrents/toggleFirstLastPiecePrio", {"hashes": torrent_hash})
    post("torrents/toggleSequentialDownload", {"hashes": torrent_hash})


def arrange_added_torrent(api_url: str, torrent_hash: str, category: str, torrent_name: str):
    files_url = (
        f"{api_url.rstrip('/')}/api/v2/torrents/files?"
        + urllib.parse.urlencode({"hash": torrent_hash})
    )
    with urllib.request.urlopen(files_url, timeout=10) as response:
        files = json.load(response)
    enable_streaming(
        torrent_hash,
        lambda endpoint, values: _api_post(api_url, endpoint, values),
    )
    plan = plan_added_torrent(category, torrent_name, files)
    if "rename_folder" in plan:
        old_path, new_path = plan["rename_folder"]
        _api_post(
            api_url,
            "torrents/renameFolder",
            {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
        )
    elif "rename_file" in plan:
        old_path, new_path = plan["rename_file"]
        _api_post(
            api_url,
            "torrents/renameFile",
            {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
        )
    return plan


def remove_torrent(api_url: str, torrent_hash: str):
    if not torrent_hash:
        return
    _api_post(
        api_url,
        "torrents/delete",
        {"hashes": torrent_hash, "deleteFiles": "false"},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", default="")
    parser.add_argument("--content-path", default="")
    parser.add_argument("--hash", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--on-added", action="store_true")
    parser.add_argument("--scan-series", action="store_true")
    parser.add_argument("--library-root", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-torrent", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    parser.add_argument("--log", default=r"C:\Users\Admin\kodi-organizer\organizer.log")
    args = parser.parse_args()

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=args.log,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    try:
        if args.scan_series:
            result = organize_series_library(
                library_root=args.library_root or r"C:\Media\Series",
                dry_run=args.dry_run,
            )
        elif args.on_added:
            result = arrange_added_torrent(
                args.api_url, args.hash, args.category, args.name
            )
        else:
            result = organize(args.category, args.content_path, dry_run=args.dry_run)
        logging.info(json.dumps(result, ensure_ascii=False))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if (
            not args.on_added
            and result["status"] == "organized"
            and not args.keep_torrent
        ):
            remove_torrent(args.api_url, args.hash)
            logging.info("Removed torrent task: %s", args.hash)
    except Exception:
        logging.exception("Organization failed: category=%s path=%s", args.category, args.content_path)
        raise


if __name__ == "__main__":
    main()
