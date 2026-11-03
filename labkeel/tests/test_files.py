"""The file browser: nothing outside the shared folders, nothing secret, and changes that are undoable."""
import os

import pytest

from conftest import PASSWORD, TMP


def test_lists_folders(client, browse):
    r = client.get("/api/files")
    assert [e["name"] for e in r.json()["entries"]] == ["media"]
    r = client.get("/api/files", params={"path": "media"})
    assert [e["name"] for e in r.json()["entries"]] == ["notes.txt"]


@pytest.mark.parametrize("path", ["../config", "media/../../config", "/../../etc", "media/../..", "a\x00b"])
def test_cannot_climb_out(client, browse, path):
    assert client.get("/api/files", params={"path": path}).status_code in (400, 403)


def test_cannot_read_outside_through_a_link(client, browse):
    (browse / "media" / "escape").symlink_to(TMP / "config")
    (browse / "media" / "passwd").symlink_to("/etc/passwd")
    names = [e["name"] for e in client.get("/api/files", params={"path": "media"}).json()["entries"]]
    assert "escape" not in names and "passwd" not in names
    assert client.get("/api/files", params={"path": "media/escape"}).status_code == 403
    assert client.get("/api/files/raw", params={"path": "media/passwd"}).status_code == 403
    assert client.get("/api/files/preview", params={"path": "media/passwd"}).status_code == 403


def test_link_inside_is_fine(client, browse):
    (browse / "media" / "shortcut.txt").symlink_to(browse / "media" / "notes.txt")
    r = client.get("/api/files/raw", params={"path": "media/shortcut.txt"})
    assert r.status_code == 200 and r.text == "hello\n"


@pytest.mark.parametrize("name", [".ssh", ".env", "server.key", "id_ed25519", "ID_RSA", "prod.env"])
def test_secrets_are_hidden(client, browse, name):
    p = browse / "media" / name
    p.mkdir() if name == ".ssh" else p.write_text("secret")
    names = [e["name"] for e in client.get("/api/files", params={"path": "media"}).json()["entries"]]
    assert name not in names
    assert client.get("/api/files/raw", params={"path": f"media/{name}"}).status_code == 403


def test_zfs_snapshot_folder_blocked(client, browse):
    (browse / "media" / ".zfs").mkdir()
    assert client.get("/api/files", params={"path": "media/.zfs"}).status_code == 403


def test_uploaded_web_page_is_never_run(client, browse):
    (browse / "media" / "page.html").write_text("<script>alert(1)</script>")
    r = client.get("/api/files/raw", params={"path": "media/page.html"})
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-security-policy"] == "sandbox"


def test_preview_text_and_binary(client, browse):
    (browse / "media" / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    assert client.get("/api/files/preview", params={"path": "media/notes.txt"}).json()["text"] == "hello\n"
    assert client.get("/api/files/preview", params={"path": "media/blob.bin"}).json()["kind"] == "binary"


def test_special_files_refused(client, browse):
    os.mkfifo(browse / "media" / "pipe")
    assert client.get("/api/files/raw", params={"path": "media/pipe"}).status_code == 400


# --------------------------------------------------------------------------- changing files
def test_changes_need_the_password_again(client, browse):
    r = client.post("/api/files/mkdir", json={"path": "media", "name": "new"})
    assert r.status_code == 428
    r = client.post("/api/files/mkdir", json={"path": "media", "name": "new", "password": "wrong"})
    assert r.status_code == 403
    assert not (browse / "media" / "new").exists()


def test_make_rename_and_save(client, browse):
    assert client.post("/api/files/mkdir", json={"path": "media", "name": "photos", "password": PASSWORD}).status_code == 200
    assert (browse / "media" / "photos").is_dir()
    # confirmed once: no password needed again for a while
    assert client.post("/api/files/rename", json={"path": "media/photos", "name": "pictures"}).status_code == 200
    assert (browse / "media" / "pictures").is_dir()
    assert client.post("/api/files/newfile", json={"path": "media", "name": "todo.md"}).status_code == 200
    mtime = client.get("/api/files/preview", params={"path": "media/todo.md"}).json()["mtime_ns"]
    r = client.put("/api/files/text", json={"path": "media/todo.md", "text": "- milk\n", "mtime_ns": mtime})
    assert r.status_code == 200, r.text
    assert (browse / "media" / "todo.md").read_text() == "- milk\n"


@pytest.mark.parametrize("name", ["..", ".", "a/b", "", "bad\x07name", "evil‮txt.exe", ".env", "id_rsa"])
def test_bad_names_refused(client, browse, name):
    r = client.post("/api/files/mkdir", json={"path": "media", "name": name, "password": PASSWORD})
    assert r.status_code == 400, name


def test_cannot_create_outside(client, browse):
    r = client.post("/api/files/mkdir", json={"path": "../config", "name": "x", "password": PASSWORD})
    assert r.status_code in (400, 403)
    assert not (TMP / "config" / "x").exists()


def test_delete_goes_to_trash_and_comes_back(client, browse):
    r = client.post("/api/files/delete", json={"paths": ["media/notes.txt"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert not (browse / "media" / "notes.txt").exists()
    items = client.get("/api/files/trash").json()["items"]
    assert [i["name"] for i in items] == ["notes.txt"]
    r = client.post("/api/files/trash/restore", json={"ids": [items[0]["id"]]})
    assert r.status_code == 200, r.text
    assert (browse / "media" / "notes.txt").read_text() == "hello\n"


def test_trash_folder_is_hidden(client, browse):
    client.post("/api/files/delete", json={"paths": ["media/notes.txt"], "password": PASSWORD})
    names = [e["name"] for e in client.get("/api/files", params={"path": "media"}).json()["entries"]]
    assert ".dashboard-trash" not in names


def test_write_switch_off(client, browse, monkeypatch):
    import fileops
    monkeypatch.setattr(fileops, "WRITE", False)
    r = client.post("/api/files/mkdir", json={"path": "media", "name": "x", "password": PASSWORD})
    assert r.status_code == 403
    assert client.get("/api/files", params={"path": "media"}).json()["writable"] is False


# --------------------------------------------------------------------------- uploads (in pieces, resumable)
def _start(client, name, data, **kw):
    r = client.post("/api/files/upload/start", json={"path": "media", "name": name, "size": len(data),
                                                     "lastModified": 1, "password": PASSWORD, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _piece(client, uid, offset, data):
    return client.put("/api/files/upload/chunk", params={"id": uid, "offset": offset}, content=data,
                      headers={"content-type": "application/octet-stream"})


def test_upload_in_pieces(client, browse):
    data = os.urandom(5 * 1024 * 1024 + 123)  # crosses the 4 MB write step
    up = _start(client, "video.bin", data)
    half = len(data) // 2
    assert _piece(client, up["id"], 0, data[:half]).status_code == 200
    assert _piece(client, up["id"], half, data[half:]).status_code == 200
    r = client.post("/api/files/upload/finish", json={"id": up["id"]})
    assert r.status_code == 200, r.text
    assert (browse / "media" / "video.bin").read_bytes() == data
    leftovers = [p.name for p in (browse / "media").iterdir() if ".dash-upload-" in p.name]
    assert leftovers == []


def test_upload_resumes_where_it_stopped(client, browse):
    data = b"abcdefghij" * 1000
    up = _start(client, "doc.txt", data)
    assert _piece(client, up["id"], 0, data[:4000]).status_code == 200
    again = _start(client, "doc.txt", data)  # the same file chosen again, as after a dropped connection
    assert again["id"] == up["id"] and again["received"] == 4000
    assert _piece(client, up["id"], 0, data[:10]).status_code == 409  # the wrong place is refused
    assert _piece(client, up["id"], 4000, data[4000:]).status_code == 200
    assert client.post("/api/files/upload/finish", json={"id": up["id"]}).status_code == 200
    assert (browse / "media" / "doc.txt").read_bytes() == data


def test_upload_refuses_too_much(client, browse):
    up = _start(client, "small.txt", b"12345")
    assert _piece(client, up["id"], 0, b"123456789").status_code == 413


def test_upload_wont_overwrite_unless_asked(client, browse):
    r = client.post("/api/files/upload/start", json={"path": "media", "name": "notes.txt", "size": 3,
                                                     "lastModified": 1, "password": PASSWORD})
    assert r.status_code == 409
    up = _start(client, "notes.txt", b"new", overwrite=True)
    _piece(client, up["id"], 0, b"new")
    assert client.post("/api/files/upload/finish", json={"id": up["id"]}).status_code == 200
    assert (browse / "media" / "notes.txt").read_bytes() == b"new"
    assert "notes.txt" in [i["name"] for i in client.get("/api/files/trash").json()["items"]]  # old one kept


def test_upload_size_limit(client, browse, monkeypatch):
    import fileops
    monkeypatch.setattr(fileops, "UPLOAD_MAX", 10)
    r = client.post("/api/files/upload/start", json={"path": "media", "name": "big.bin", "size": 11,
                                                     "lastModified": 1, "password": PASSWORD})
    assert r.status_code == 413


def test_trash_item_whose_copy_has_gone(client, browse):
    """The Trash folder was deleted by hand (or its drive isn't mounted): no crash, and the stale entry goes."""
    import shutil
    client.post("/api/files/delete", json={"paths": ["media/notes.txt"], "password": PASSWORD})
    item = next(i for i in client.get("/api/files/trash").json()["items"] if i["from"] == "media/notes.txt")
    shutil.rmtree(browse / "media" / ".dashboard-trash")
    r = client.post("/api/files/trash/restore", json={"ids": [item["id"]]})
    assert r.status_code == 409 and "isn't there any more" in r.text
    assert item["id"] not in [i["id"] for i in client.get("/api/files/trash").json()["items"]]


def test_delete_forever_clears_a_missing_item(client, browse):
    import shutil
    client.post("/api/files/delete", json={"paths": ["media/notes.txt"], "password": PASSWORD})
    item = next(i for i in client.get("/api/files/trash").json()["items"] if i["from"] == "media/notes.txt")
    shutil.rmtree(browse / "media" / ".dashboard-trash")
    r = client.post("/api/files/trash/delete", json={"ids": [item["id"]]})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert item["id"] not in [i["id"] for i in client.get("/api/files/trash").json()["items"]]
