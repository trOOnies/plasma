import base64

import pytest

from plasma import influencers
from plasma.influencers import NETWORKS_BY_KEY, check_social, check_url, check_video, link_warnings

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
COLLAGE = "/influencers/1/collages/1"


def picture(name: str = "a.png", content: bytes = PNG) -> dict:
    return {"picture": (name, content, "image/png")}


def make_collage(client, name: str = "Creator") -> None:
    client.post("/influencers", data={"name": name})
    client.post("/influencers/1/collages", data={"name": "Best of"})


# --- Checks ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("example.com", "https://example.com"),
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://münchen.de", "https://münchen.de"),
    ],
)
def test_check_url_accepts_and_adds_https(value, expected):
    assert check_url(value) == expected


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "enter a web address"),
        ("https://exa mple.com", "spaces"),
        ("javascript:alert(1)", "https://"),
        ("mailto:me@example.com", "https://"),
        ("ftp://example.com", "https://"),
        ("example", "isn't a full site name"),
        ("https://a..b.com", "isn't a valid site name"),
        ("https://-bad-.com", "isn't a valid site name"),
        ("https://example.com:99999", "invalid port"),
        ("https://" + "a" * 2000 + ".com", "up to"),
    ],
)
def test_check_url_rejects(value, reason):
    with pytest.raises(ValueError, match=reason):
        check_url(value)


@pytest.mark.parametrize(
    ("url", "warning"),
    [
        ("http://example.com", "Not secure"),
        ("https://user:pw@example.com", "before the @"),
        ("https://192.168.0.1/admin", "IP address"),
        ("https://xn--pple-43d.com", "other alphabets"),
        ("https://example.com:8443", "unusual port"),
        ("https://bit.ly/abc", "shortened"),
        ("https://example.com/setup.exe", "downloads a program"),
        ("https://example.com/?" + "x" * 400, "very long"),
        ("https://youtu.be/dQw4w9WgXcQ", "add it as a Video"),
    ],
)
def test_link_warnings(url, warning):
    assert any(warning in text for text in link_warnings(url))


def test_safe_link_has_no_warnings():
    assert link_warnings("https://www.example.com/article") == []


def test_duplicate_link_ignores_www_and_trailing_slash():
    others = ["https://example.com/post/"]
    assert "already in this collage" in link_warnings("https://www.example.com/post", others)[0]
    assert link_warnings("https://example.com/other", others) == []


@pytest.mark.parametrize(
    ("url", "provider", "video_id"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42", "youtube", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "youtube", "dQw4w9WgXcQ"),
        ("youtube.com/shorts/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/live/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
        ("https://www.tiktok.com/@scout/video/6718335390845095173", "tiktok",
         "6718335390845095173"),
        ("https://www.tiktok.com/embed/v2/6718335390845095173", "tiktok", "6718335390845095173"),
        ("https://m.tiktok.com/v/6718335390845095173.html", "tiktok", "6718335390845095173"),
    ],
)
def test_check_video_reads_youtube_and_tiktok(url, provider, video_id):
    assert check_video(url)[1:] == (provider, video_id)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://vimeo.com/76979871", "only YouTube and TikTok"),
        ("https://www.youtube.com/@mkbhd", "not a single video"),
        ("https://www.youtube.com/watch?v=short", "not a single video"),
        ("https://vm.tiktok.com/ZMabc123/", "short TikTok links"),
        ("https://www.tiktok.com/t/ZTabc/", "short TikTok links"),
        ("https://www.tiktok.com/@scout", "not a single video"),
        ("https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ", "only YouTube and TikTok"),
    ],
)
def test_check_video_rejects(url, reason):
    with pytest.raises(ValueError, match=reason):
        check_video(url)


@pytest.mark.parametrize(
    ("network", "value", "expected"),
    [
        ("instagram", "@khaby.lame", "https://www.instagram.com/khaby.lame"),
        ("instagram", "instagram.com/natgeo", "https://instagram.com/natgeo"),
        ("tiktok", "khaby.lame", "https://www.tiktok.com/@khaby.lame"),
        ("youtube", "@mkbhd", "https://www.youtube.com/@mkbhd"),
        ("x", "https://twitter.com/nasa", "https://twitter.com/nasa"),
        ("x", "nasa", "https://x.com/nasa"),
        ("bluesky", "name.bsky.social", "https://bsky.app/profile/name.bsky.social"),
        ("reddit", "u/spez", "https://www.reddit.com/user/spez"),
        ("linkedin", "https://www.linkedin.com/in/someone", "https://www.linkedin.com/in/someone"),
        ("twitch", "", ""),
    ],
)
def test_check_social_takes_handles_and_addresses(network, value, expected):
    assert check_social(NETWORKS_BY_KEY[network], value) == expected


@pytest.mark.parametrize(
    ("network", "value", "reason"),
    [
        ("instagram", "https://x.com/foo", "not an address on Instagram"),
        ("instagram", "https://notinstagram.com/foo", "not an address on Instagram"),
        ("youtube", "youtube.com", "home page"),
        ("tiktok", "@two words", "without spaces"),
        ("facebook", "javascript:alert(1)", "profile address or a handle"),
    ],
)
def test_check_social_rejects(network, value, reason):
    with pytest.raises(ValueError, match=reason):
        check_social(NETWORKS_BY_KEY[network], value)


@pytest.mark.parametrize(
    ("content", "media_type"),
    [
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"\x00\x00\x00\x1cftypavif", "image/avif"),
    ],
)
def test_image_type_reads_the_content(content, media_type):
    assert influencers.image_type(content) == media_type


def test_image_type_rejects_other_files_and_big_ones():
    with pytest.raises(ValueError, match="JPEG, PNG"):
        influencers.image_type(b"<svg onload=alert(1)>")
    with pytest.raises(ValueError, match="10 MB"):
        influencers.image_type(PNG + b"\x00" * influencers.MAX_UPLOAD_BYTES)


# --- Pages ----------------------------------------------------------------------------------


def test_influencers_tab_starts_empty(client):
    page = client.get("/influencers").text
    assert "None yet" in page and 'href="/influencers/new"' in page
    assert 'href="/influencers">Influencers</a>' in client.get("/").text


def test_add_influencer_with_picture_and_profiles(client):
    form = {
        "name": "Marques",
        "website": "mkbhd.com",
        "social_youtube": "@mkbhd",
        "social_x": "twitter.com/mkbhd",
    }
    response = client.post("/influencers", data=form, files=picture())
    assert response.status_code == 303 and response.headers["location"] == "/influencers/1"
    page = client.get("/influencers/1").text
    assert 'href="https://mkbhd.com"' in page
    assert 'href="https://www.youtube.com/@mkbhd"' in page and ">YouTube</a>" in page
    assert 'href="https://twitter.com/mkbhd"' in page
    assert page.index(">YouTube</a>") < page.index(">X (Twitter)</a>")
    assert 'src="/uploads/1"' in page and 'src="/uploads/1"' in client.get("/influencers").text
    image = client.get("/uploads/1")
    assert image.content == PNG and image.headers["content-type"] == "image/png"


def test_influencer_form_lists_every_problem_and_keeps_input(client, conn):
    form = {"name": " ", "website": "javascript:alert(1)", "social_instagram": "x.com/foo"}
    response = client.post("/influencers", data=form, files=picture("a.png", b"not an image"))
    assert response.status_code == 422
    assert "Name: enter a name" in response.text
    assert "Website: use an address starting with https://" in response.text
    assert "Instagram: that&#39;s not an address on Instagram" in response.text
    assert "Picture: use a JPEG" in response.text
    assert 'value="x.com/foo"' in response.text
    assert conn.execute("SELECT COUNT(*) FROM influencers").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0


def test_influencer_names_are_unique_ignoring_case(client):
    client.post("/influencers", data={"name": "Pokimane"})
    response = client.post("/influencers", data={"name": "POKIMANE"})
    assert response.status_code == 409 and "already here" in response.text


def test_influencer_search_ignores_accents(client):
    client.post("/influencers", data={"name": "Chloé Ting"})
    client.post("/influencers", data={"name": "Mark Rober"})
    page = client.get("/influencers?q=chloe").text
    assert "Chloé Ting" in page and "Mark Rober" not in page


def test_website_warnings_show_on_the_profile(client):
    client.post("/influencers", data={"name": "Old site", "website": "http://example.com"})
    assert "Not secure" in client.get("/influencers/1").text


def test_edit_influencer_replaces_and_removes_picture(client, conn):
    client.post("/influencers", data={"name": "A", "social_twitch": "a"}, files=picture())
    edit = client.get("/influencers/1/edit").text
    assert 'value="https://www.twitch.tv/a"' in edit and "Remove this picture" in edit

    client.post("/influencers/1", data={"name": "B"}, files=picture("b.png"))
    influencer = influencers.get_influencer(conn, 1)
    assert influencer["name"] == "B" and influencer["picture_id"] == 2
    assert influencers.influencer_socials(conn, 1) == []
    assert client.get("/uploads/1").status_code == 404

    client.post("/influencers/1", data={"name": "B", "remove_picture": "true"})
    assert influencers.get_influencer(conn, 1)["picture_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0


def test_saving_without_a_new_picture_keeps_the_old_one(client, conn):
    client.post("/influencers", data={"name": "A"}, files=picture())
    client.post("/influencers/1", data={"name": "A"}, files=picture("", b""))
    assert influencers.get_influencer(conn, 1)["picture_id"] == 1


def test_delete_influencer_removes_collages_and_pictures(client, conn):
    make_collage(client)
    client.post(f"{COLLAGE}/items", data={"kind": "image"}, files={"image": ("a.png", PNG)})
    response = client.post("/influencers/1/delete")
    assert response.headers["location"] == "/influencers"
    for table in ("influencers", "collages", "collage_items", "uploads"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_missing_influencer_collage_and_upload_are_404(client):
    make_collage(client)
    assert client.get("/influencers/9").status_code == 404
    assert client.get("/influencers/1/collages/9").status_code == 404
    assert client.get("/uploads/9").status_code == 404
    client.post("/influencers", data={"name": "Other"})
    # A collage only exists under its own influencer.
    assert client.get("/influencers/2/collages/1").status_code == 404


def test_collages_are_listed_and_unique_per_influencer(client):
    make_collage(client)
    page = client.get("/influencers/1").text
    assert 'href="/influencers/1/collages/1"' in page and "Best of" in page
    response = client.post("/influencers/1/collages", data={"name": "best OF"})
    assert response.status_code == 409 and "already has a collage" in response.text
    client.post("/influencers", data={"name": "Other"})
    assert client.post("/influencers/2/collages", data={"name": "Best of"}).status_code == 303


def test_add_every_kind_of_item(client):
    make_collage(client)
    items = [
        {"kind": "note", "text": "Loves cameras"},
        {"kind": "link", "url": "example.com/interview", "text": "Interview"},
        {"kind": "video", "url": "https://youtu.be/dQw4w9WgXcQ"},
        {"kind": "video", "url": "https://www.tiktok.com/@a/video/123"},
    ]
    for item in items:
        assert client.post(f"{COLLAGE}/items", data=item).status_code == 303
    response = client.post(
        f"{COLLAGE}/items", data={"kind": "image", "text": "Studio"},
        files={"image": ("a.png", PNG)},
    )
    assert response.headers["location"] == f"{COLLAGE}#item-5"
    page = client.get(COLLAGE).text
    assert page.count('class="collage-item') == 5
    assert "Loves cameras" in page
    assert 'href="https://example.com/interview"' in page and ">Interview</a>" in page
    assert 'src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in page
    assert 'src="https://www.tiktok.com/player/v1/123"' in page and "video-frame vertical" in page
    assert 'src="/uploads/1"' in page and "Studio" in page
    assert 'src="/uploads/1"' in client.get("/influencers/1").text
    assert "5 items" in client.get("/influencers/1").text


def test_risky_links_are_saved_with_warnings(client):
    make_collage(client)
    client.post(f"{COLLAGE}/items", data={"kind": "link", "url": "http://bit.ly/x"})
    client.post(f"{COLLAGE}/items", data={"kind": "link", "url": "https://bit.ly/x/"})
    page = client.get(COLLAGE).text
    assert page.count("Not secure") == 1
    assert page.count("shortened link") == 2
    assert page.count("already in this collage") == 2


@pytest.mark.parametrize(
    ("item", "error"),
    [
        ({"kind": "note", "text": "  "}, "Note: write something in the note"),
        ({"kind": "link", "url": "javascript:alert(1)"}, "Link: use an address starting"),
        ({"kind": "link", "url": ""}, "Link: enter a web address"),
        ({"kind": "video", "url": "https://vimeo.com/1"}, "Video: only YouTube and TikTok"),
        ({"kind": "image"}, "Image: pick a picture"),
    ],
)
def test_invalid_items_are_refused(client, conn, item, error):
    make_collage(client)
    response = client.post(f"{COLLAGE}/items", data=item)
    assert response.status_code == 422
    assert error in response.text
    assert conn.execute("SELECT COUNT(*) FROM collage_items").fetchone()[0] == 0


def test_refused_link_keeps_what_was_typed(client):
    make_collage(client)
    data = {"kind": "link", "url": "not a link", "text": "My title"}
    page = client.post(f"{COLLAGE}/items", data=data).text
    assert 'value="not a link"' in page and 'value="My title"' in page


def test_image_item_must_be_a_picture(client):
    make_collage(client)
    files = {"image": ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")}
    response = client.post(f"{COLLAGE}/items", data={"kind": "image"}, files=files)
    assert response.status_code == 422 and "Image: use a JPEG" in response.text


def test_unknown_item_kind_is_refused(client):
    make_collage(client)
    assert client.post(f"{COLLAGE}/items", data={"kind": "audio"}).status_code == 422


def test_move_edit_and_remove_items(client, conn):
    make_collage(client)
    for text in ("one", "two", "three"):
        client.post(f"{COLLAGE}/items", data={"kind": "note", "text": text})

    def order() -> list[str]:
        return [item["text"] for item in influencers.collage_items(conn, 1)]

    client.post(f"{COLLAGE}/items/3/move", data={"step": "-1"})
    assert order() == ["one", "three", "two"]
    client.post(f"{COLLAGE}/items/1/move", data={"step": "-1"})
    assert order() == ["one", "three", "two"]
    assert client.post(f"{COLLAGE}/items/1/move", data={"step": "5"}).status_code == 422

    client.post(f"{COLLAGE}/items/2/text", data={"text": "TWO"})
    assert order() == ["one", "three", "TWO"]
    assert client.post(f"{COLLAGE}/items/2/text", data={"text": ""}).status_code == 422

    client.post(f"{COLLAGE}/items/1/delete")
    assert order() == ["three", "TWO"]
    assert client.post(f"{COLLAGE}/items/1/delete").status_code == 404


def test_removing_an_image_item_drops_its_picture(client, conn):
    make_collage(client)
    client.post(f"{COLLAGE}/items", data={"kind": "image"}, files={"image": ("a.png", PNG)})
    client.post(f"{COLLAGE}/items/1/delete")
    assert client.get("/uploads/1").status_code == 404


def test_rename_and_delete_collage(client, conn):
    make_collage(client)
    client.post("/influencers/1/collages", data={"name": "Taken"})
    response = client.post(COLLAGE, data={"name": "Renamed", "description": "Why I watch"})
    assert response.status_code == 303
    page = client.get(COLLAGE).text
    assert "<h1>Renamed</h1>" in page and "Why I watch" in page
    assert client.post(COLLAGE, data={"name": "taken"}).status_code == 409

    client.post(f"{COLLAGE}/items", data={"kind": "note", "text": "x"})
    assert client.post(f"{COLLAGE}/delete").headers["location"] == "/influencers/1"
    assert conn.execute("SELECT COUNT(*) FROM collage_items").fetchone()[0] == 0


def test_influencers_survive_reconnecting(client, db_path):
    make_collage(client)
    client.post(f"{COLLAGE}/items", data={"kind": "note", "text": "kept"})
    from plasma.db import connect

    with connect(db_path) as again:
        assert influencers.collage_items(again, 1)[0]["text"] == "kept"
