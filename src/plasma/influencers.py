"""Influencers, their social media profiles, and the collages made about them.

Everything here is added in the web UI, so nothing in info/ can bring back what was removed.
"""

import ipaddress
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from plasma.db import fold

MAX_NAME = 100
MAX_TEXT = 5000
MAX_URL = 2000
LONG_URL = 300
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ITEM_KINDS = ("note", "link", "video", "image")

URL_SHORTENERS = {
    "bit.ly", "buff.ly", "cutt.ly", "goo.gl", "is.gd", "lnkd.in", "ow.ly", "rb.gy",
    "rebrand.ly", "shorturl.at", "t.co", "t.ly", "tiny.cc", "tinyurl.com", "linktr.ee",
}
DOWNLOAD_SUFFIXES = (".apk", ".bat", ".cmd", ".dmg", ".exe", ".jar", ".msi", ".pkg", ".scr")
YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
HOST_LABEL = re.compile(r"(?!-)[a-z0-9_-]{1,63}(?<!-)", re.IGNORECASE)


class DuplicateInfluencerError(Exception):
    pass


class DuplicateCollageError(Exception):
    pass


@dataclass(frozen=True)
class Network:
    key: str
    label: str
    hosts: tuple[str, ...]
    profile_url: str
    example: str


# The main social networks in the West, in the order they're shown.
NETWORKS = (
    Network("youtube", "YouTube", ("youtube.com",), "https://www.youtube.com/@{}", "@mkbhd"),
    Network("instagram", "Instagram", ("instagram.com",), "https://www.instagram.com/{}",
            "@natgeo"),
    Network("tiktok", "TikTok", ("tiktok.com",), "https://www.tiktok.com/@{}", "@khaby.lame"),
    Network("x", "X (Twitter)", ("x.com", "twitter.com"), "https://x.com/{}", "@nasa"),
    Network("facebook", "Facebook", ("facebook.com", "fb.com"), "https://www.facebook.com/{}",
            "natgeo"),
    Network("twitch", "Twitch", ("twitch.tv",), "https://www.twitch.tv/{}", "pokimane"),
    Network("threads", "Threads", ("threads.net", "threads.com"),
            "https://www.threads.com/@{}", "@zuck"),
    Network("bluesky", "Bluesky", ("bsky.app",), "https://bsky.app/profile/{}",
            "name.bsky.social"),
    Network("linkedin", "LinkedIn", ("linkedin.com",), "https://www.linkedin.com/in/{}",
            "williamhgates"),
    Network("snapchat", "Snapchat", ("snapchat.com",), "https://www.snapchat.com/add/{}",
            "kyliejenner"),
    Network("pinterest", "Pinterest", ("pinterest.com",), "https://www.pinterest.com/{}",
            "marthastewart"),
    Network("reddit", "Reddit", ("reddit.com",), "https://www.reddit.com/user/{}", "u/spez"),
    Network("patreon", "Patreon", ("patreon.com",), "https://www.patreon.com/{}", "kurzgesagt"),
)
NETWORKS_BY_KEY = {network.key: network for network in NETWORKS}
HANDLE = re.compile(r"[A-Za-z0-9._-]{1,100}")

IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


# --- Checking what was typed --------------------------------------------------------------


def _bare_host(host: str) -> str:
    host = host.lower().rstrip(".")
    for prefix in ("www.", "m.", "mobile."):
        host = host.removeprefix(prefix)
    return host


def _on(host: str, domains: set[str] | tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def check_url(value: str) -> str:
    """A web address that's safe to link to, with https:// added if it was left out.

    Raises ValueError with a readable reason for anything that can't work as a link.
    """
    url = value.strip()
    if not url:
        raise ValueError("enter a web address")
    if len(url) > MAX_URL:
        raise ValueError(f"use an address of up to {MAX_URL} characters")
    if re.search(r"\s", url):
        raise ValueError("a web address can't contain spaces")
    if not re.match(r"[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        if re.match(r"[a-z][a-z0-9+.-]*:(?!\d)", url, re.IGNORECASE):
            # Such as javascript: or mailto:, which mustn't become a link.
            raise ValueError("use an address starting with https://")
        url = f"https://{url.removeprefix('//')}"
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("use an address starting with https://")
    try:
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises for a port that isn't a number
    except ValueError as error:
        raise ValueError("that address has an invalid port") from error
    if not host:
        raise ValueError("that address is missing the site's name, such as example.com")
    if not _is_ip(host):
        labels = host.rstrip(".").split(".")
        if len(labels) < 2 and host != "localhost":
            raise ValueError(f"{host} isn't a full site name, such as {host}.com")
        if labels[-1].isdecimal() or not all(map(_valid_label, labels)):
            raise ValueError(f"{host} isn't a valid site name")
    return url


def _valid_label(label: str) -> bool:
    """One dot-separated part of a site name, which may be in any alphabet."""
    try:
        encoded = label.encode("idna").decode() if not label.isascii() else label
    except UnicodeError:
        return False
    return bool(HOST_LABEL.fullmatch(encoded))


def link_warnings(url: str, others: list[str] = ()) -> list[str]:
    """Reasons to look twice at a link that is valid. They don't stop it being saved."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    bare = _bare_host(host)
    warnings = []
    if parts.scheme.lower() == "http":
        warnings.append("Not secure: it starts with http://, not https://.")
    if parts.username or parts.password:
        warnings.append("It has a name or password before the @, which can hide the real site.")
    if _is_ip(host):
        warnings.append("It goes to a bare IP address instead of a site name.")
    if "xn--" in host or not host.isascii():
        warnings.append("Its site name has letters from other alphabets, which can imitate "
                        "a well-known site.")
    if parts.port and parts.port not in (80, 443):
        warnings.append(f"It uses an unusual port ({parts.port}).")
    if bare in URL_SHORTENERS:
        warnings.append("It's a shortened link, so you can't see where it leads.")
    if parts.path.lower().endswith(DOWNLOAD_SUFFIXES):
        warnings.append("It downloads a program, not a page.")
    if len(url) > LONG_URL:
        warnings.append("It's very long, which is often tracking information.")
    if video_for(url):
        warnings.append("It's a video, so add it as a Video to watch it right here.")
    if any(_same_link(url, other) for other in others):
        warnings.append("The same link is already in this collage.")
    return warnings


def _same_link(first: str, second: str) -> bool:
    def key(url: str) -> tuple[str, str, str]:
        parts = urlsplit(url)
        return _bare_host(parts.hostname or ""), parts.path.rstrip("/"), parts.query

    return key(first) == key(second)


def video_for(url: str) -> tuple[str, str] | None:
    """The provider and video id of a YouTube or TikTok address, or None for anything else."""
    try:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = _bare_host(parts.hostname or "")
    except ValueError:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if host == "youtu.be" and segments and YOUTUBE_ID.fullmatch(segments[0]):
        return "youtube", segments[0]
    if host in YOUTUBE_HOSTS:
        if segments == ["watch"]:
            video_id = parse_qs(parts.query).get("v", [""])[0]
            return ("youtube", video_id) if YOUTUBE_ID.fullmatch(video_id) else None
        if len(segments) >= 2 and segments[0] in ("shorts", "embed", "live", "v"):
            return ("youtube", segments[1]) if YOUTUBE_ID.fullmatch(segments[1]) else None
    if host == "tiktok.com":
        # Such as /@user/video/123, /embed/v2/123, /player/v1/123 or /v/123.html.
        for index, segment in enumerate(segments[:-1]):
            if segment in ("video", "v2", "v1", "v"):
                video_id = segments[index + 1].removesuffix(".html")
                if video_id.isdecimal():
                    return "tiktok", video_id
    return None


def check_video(value: str) -> tuple[str, str, str]:
    """The address, provider and id of a YouTube or TikTok video. Raises ValueError otherwise."""
    url = check_url(value)
    found = video_for(url)
    if found:
        return url, *found
    parts = urlsplit(url)
    host = _bare_host(parts.hostname or "")
    if host in TIKTOK_SHORT_HOSTS or (host == "tiktok.com" and parts.path.startswith("/t/")):
        raise ValueError("short TikTok links can't play here. Open it, then copy the full "
                         "address from your browser, the one with /video/ in it")
    if host in YOUTUBE_HOSTS or host == "youtu.be" or host == "tiktok.com":
        raise ValueError("that's not a single video. Use the address of the video itself")
    raise ValueError("only YouTube and TikTok videos can be added")


def is_vertical(item: sqlite3.Row) -> bool:
    """TikToks and YouTube Shorts are filmed upright, so they get a taller player."""
    return item["video_provider"] == "tiktok" or "/shorts/" in item["url"]


def embed_url(provider: str, video_id: str) -> str:
    if provider == "youtube":
        return f"https://www.youtube-nocookie.com/embed/{video_id}"
    return f"https://www.tiktok.com/player/v1/{video_id}"


def check_social(network: Network, value: str) -> str:
    """A profile address on that network, from an address or a bare handle such as @name."""
    text = value.strip()
    if not text:
        return ""
    handle = text
    if network.key == "reddit":
        handle = re.sub(r"^/?u/", "", handle)
    handle = handle.removeprefix("@")
    # Handles can have dots, as in khaby.lame or name.bsky.social, so only a slash or the
    # network's own site name makes it an address.
    if "/" not in handle and not _on(_bare_host(handle), network.hosts):
        if not HANDLE.fullmatch(handle):
            raise ValueError("use a profile address or a handle such as "
                             f"{network.example}, without spaces")
        return network.profile_url.format(handle)
    url = check_url(text)
    parts = urlsplit(url)
    if not _on(_bare_host(parts.hostname or ""), network.hosts):
        raise ValueError(f"that's not an address on {network.label}. Use one on "
                         f"{' or '.join(network.hosts)}, or just the handle")
    if not parts.path.strip("/"):
        raise ValueError(f"that's {network.label}'s home page, not a profile")
    return url


def check_name(value: str, what: str = "name") -> str:
    name = value.strip()
    if not name:
        raise ValueError(f"enter a {what}")
    if len(name) > MAX_NAME:
        raise ValueError(f"use a {what} of up to {MAX_NAME} characters")
    return name


def check_text(value: str, what: str, required: bool = False) -> str:
    text = value.strip()
    if required and not text:
        raise ValueError(f"write something in the {what}")
    if len(text) > MAX_TEXT:
        raise ValueError(f"keep the {what} under {MAX_TEXT} characters")
    return text


def image_type(content: bytes) -> str:
    """The media type of a picture, judged by its content. Raises ValueError for anything else."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"use a picture of up to {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    for signature, media_type in IMAGE_SIGNATURES:
        if content.startswith(signature):
            return media_type
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    raise ValueError("use a JPEG, PNG, GIF, WebP or AVIF picture")


@dataclass
class InfluencerData:
    name: str
    website: str
    socials: dict[str, str]


def parse_influencer(form: dict[str, str]) -> tuple[InfluencerData, list[str]]:
    """Read the influencer form. Returns what could be read and every problem found."""
    errors = []
    name = website = ""
    try:
        name = check_name(form.get("name", ""))
    except ValueError as error:
        errors.append(f"Name: {error}")
    if form.get("website", "").strip():
        try:
            website = check_url(form["website"])
        except ValueError as error:
            errors.append(f"Website: {error}")
    socials = {}
    for network in NETWORKS:
        try:
            url = check_social(network, form.get(f"social_{network.key}", ""))
        except ValueError as error:
            errors.append(f"{network.label}: {error}")
            continue
        if url:
            socials[network.key] = url
    return InfluencerData(name, website, socials), errors


# --- Storage -------------------------------------------------------------------------------


def add_upload(conn: sqlite3.Connection, content: bytes) -> int:
    return conn.execute(
        "INSERT INTO uploads (media_type, content) VALUES (?, ?)", (image_type(content), content)
    ).lastrowid


def get_upload(conn: sqlite3.Connection, upload_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()


def _drop_unused_uploads(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM uploads WHERE id NOT IN"
        " (SELECT picture_id FROM influencers WHERE picture_id IS NOT NULL"
        "  UNION SELECT upload_id FROM collage_items WHERE upload_id IS NOT NULL)"
    )


def list_influencers(conn: sqlite3.Connection, query: str = "") -> list[sqlite3.Row]:
    where, params = "", []
    if query:
        folded = fold(query) or ""
        escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where, params = " WHERE fold(i.name) LIKE ? ESCAPE '\\'", [f"%{escaped}%"]
    return conn.execute(
        "SELECT i.*, (SELECT COUNT(*) FROM collages c WHERE c.influencer_id = i.id)"
        f" AS collage_count FROM influencers i{where} ORDER BY i.name COLLATE NOCASE",
        params,
    ).fetchall()


def get_influencer(conn: sqlite3.Connection, influencer_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM influencers WHERE id = ?", (influencer_id,)).fetchone()


def influencer_socials(conn: sqlite3.Connection, influencer_id: int) -> list[dict]:
    """The profiles in the order of NETWORKS, each with its network's label."""
    rows = conn.execute(
        "SELECT network, url FROM influencer_socials WHERE influencer_id = ?", (influencer_id,)
    )
    urls = {row["network"]: row["url"] for row in rows}
    return [
        {"network": network.key, "label": network.label, "url": urls[network.key]}
        for network in NETWORKS
        if network.key in urls
    ]


def _set_socials(conn: sqlite3.Connection, influencer_id: int, socials: dict[str, str]) -> None:
    conn.execute("DELETE FROM influencer_socials WHERE influencer_id = ?", (influencer_id,))
    conn.executemany(
        "INSERT INTO influencer_socials (influencer_id, network, url) VALUES (?, ?, ?)",
        [(influencer_id, network, url) for network, url in socials.items()],
    )


def create_influencer(
    conn: sqlite3.Connection, data: InfluencerData, picture: bytes | None = None
) -> int:
    picture_id = add_upload(conn, picture) if picture else None
    try:
        influencer_id = conn.execute(
            "INSERT INTO influencers (name, website, picture_id) VALUES (?, ?, ?)",
            (data.name, data.website, picture_id),
        ).lastrowid
    except sqlite3.IntegrityError as error:
        raise DuplicateInfluencerError(data.name) from error
    _set_socials(conn, influencer_id, data.socials)
    return influencer_id


def update_influencer(
    conn: sqlite3.Connection,
    influencer_id: int,
    data: InfluencerData,
    picture: bytes | None = None,
    remove_picture: bool = False,
) -> None:
    try:
        conn.execute(
            "UPDATE influencers SET name = ?, website = ? WHERE id = ?",
            (data.name, data.website, influencer_id),
        )
    except sqlite3.IntegrityError as error:
        raise DuplicateInfluencerError(data.name) from error
    if picture:
        conn.execute(
            "UPDATE influencers SET picture_id = ? WHERE id = ?",
            (add_upload(conn, picture), influencer_id),
        )
    elif remove_picture:
        conn.execute("UPDATE influencers SET picture_id = NULL WHERE id = ?", (influencer_id,))
    _set_socials(conn, influencer_id, data.socials)
    _drop_unused_uploads(conn)


def delete_influencer(conn: sqlite3.Connection, influencer_id: int) -> None:
    conn.execute("DELETE FROM influencers WHERE id = ?", (influencer_id,))
    _drop_unused_uploads(conn)


def list_collages(conn: sqlite3.Connection, influencer_id: int) -> list[sqlite3.Row]:
    """Each collage, with how many items it holds and the first picture in it, if any."""
    return conn.execute(
        "SELECT c.*, COUNT(ci.id) AS item_count,"
        " (SELECT upload_id FROM collage_items WHERE collage_id = c.id AND kind = 'image'"
        "  ORDER BY position LIMIT 1) AS cover_id"
        " FROM collages c LEFT JOIN collage_items ci ON ci.collage_id = c.id"
        " WHERE c.influencer_id = ? GROUP BY c.id ORDER BY c.name COLLATE NOCASE",
        (influencer_id,),
    ).fetchall()


def get_collage(
    conn: sqlite3.Connection, influencer_id: int, collage_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM collages WHERE id = ? AND influencer_id = ?", (collage_id, influencer_id)
    ).fetchone()


def create_collage(
    conn: sqlite3.Connection, influencer_id: int, name: str, description: str = ""
) -> int:
    try:
        return conn.execute(
            "INSERT INTO collages (influencer_id, name, description) VALUES (?, ?, ?)",
            (influencer_id, name, description),
        ).lastrowid
    except sqlite3.IntegrityError as error:
        raise DuplicateCollageError(name) from error


def update_collage(conn: sqlite3.Connection, collage_id: int, name: str, description: str) -> None:
    try:
        conn.execute(
            "UPDATE collages SET name = ?, description = ? WHERE id = ?",
            (name, description, collage_id),
        )
    except sqlite3.IntegrityError as error:
        raise DuplicateCollageError(name) from error


def delete_collage(conn: sqlite3.Connection, collage_id: int) -> None:
    conn.execute("DELETE FROM collages WHERE id = ?", (collage_id,))
    _drop_unused_uploads(conn)


def collage_items(conn: sqlite3.Connection, collage_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM collage_items WHERE collage_id = ? ORDER BY position, id", (collage_id,)
    ).fetchall()


def get_item(conn: sqlite3.Connection, collage_id: int, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM collage_items WHERE id = ? AND collage_id = ?", (item_id, collage_id)
    ).fetchone()


def add_item(
    conn: sqlite3.Connection,
    collage_id: int,
    kind: str,
    text: str = "",
    url: str = "",
    video: tuple[str, str] | None = None,
    upload_id: int | None = None,
) -> int:
    position = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM collage_items WHERE collage_id = ?",
        (collage_id,),
    ).fetchone()[0]
    provider, video_id = video or (None, None)
    return conn.execute(
        "INSERT INTO collage_items"
        " (collage_id, position, kind, text, url, video_provider, video_id, upload_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (collage_id, position, kind, text, url, provider, video_id, upload_id),
    ).lastrowid


def update_item_text(conn: sqlite3.Connection, item_id: int, text: str) -> None:
    conn.execute("UPDATE collage_items SET text = ? WHERE id = ?", (text, item_id))


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM collage_items WHERE id = ?", (item_id,))
    _drop_unused_uploads(conn)


def move_item(conn: sqlite3.Connection, collage_id: int, item_id: int, step: int) -> None:
    """Swap an item with the one before it (step -1) or after it (step 1)."""
    ids = [row["id"] for row in collage_items(conn, collage_id)]
    index = ids.index(item_id)
    other = index + step
    if not 0 <= other < len(ids):
        return
    ids[index], ids[other] = ids[other], ids[index]
    conn.executemany(
        "UPDATE collage_items SET position = ? WHERE id = ?",
        [(position, row_id) for position, row_id in enumerate(ids, start=1)],
    )
