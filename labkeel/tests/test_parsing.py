"""Pure helpers that read other programs' output or Docker's data."""
import main
import drivecare


def test_docker_time():
    assert main.parse_docker_time("0001-01-01T00:00:00Z") is None
    assert main.parse_docker_time("") is None
    t = main.parse_docker_time("2026-11-02T18:00:00.123456789Z")
    assert abs(t - 1793642400.123456) < 0.001
    assert main.parse_docker_time("garbage") is None


def test_private_folders_found_from_mounts():
    mounts = [
        {"Type": "bind", "Source": "/srv/dash/data", "Destination": "/data"},
        {"Type": "bind", "Source": "/srv/dash/config", "Destination": "/config"},
        {"Type": "bind", "Source": "/srv", "Destination": "/browse/srv"},
        {"Type": "bind", "Source": "/mnt/media", "Destination": "/browse/media"},
    ]
    labels = {"com.docker.compose.project.config_files": "/srv/dash/docker-compose.yml",
              "com.docker.compose.project.working_dir": "/srv/dash"}
    out = main.private_from_mounts(mounts, labels, alone_in_project=False)
    assert "/browse/srv/dash/data" in out
    assert "/browse/srv/dash/config" in out
    assert "/browse/srv/dash/docker-compose.yml" in out
    assert "/browse/srv/dash/.env" in out
    assert not any(p.startswith("/browse/media") for p in out)
    alone = main.private_from_mounts(mounts, labels, alone_in_project=True)
    assert "/browse/srv/dash" in alone


ZPOOL_STATUS = """  pool: tank
 state: ONLINE
  scan: scrub repaired 0B in 00:10:00 with 0 errors on Sun Oct 11 00:34:01 2026
config:

\tNAME         STATE     READ WRITE CKSUM
\ttank         ONLINE       0     0     0
\t  raidz1-0   ONLINE       0     0     0
\t    nvme0n1  ONLINE       0     0     0
\t    nvme1n1  ONLINE       0     0     2

errors: No known data errors
"""


def test_zpool_status_config_rows():
    rows = drivecare.parse_config(ZPOOL_STATUS)
    assert rows, "no rows parsed"
    flat = str(rows)
    for name in ("tank", "raidz1-0", "nvme0n1", "nvme1n1"):
        assert name in flat


def test_counts():
    assert drivecare._count("0") == 0
    assert drivecare._count("2") == 2
    assert drivecare._count("1.5K") == 1500
    assert drivecare._count("junk") == 0


def test_zfs_snapshot_folder_name_is_hidden():
    assert main._hidden_part(".zfs")
    assert main._hidden_part(".dashboard-trash")
    assert main._hidden_part(".photo.jpg.dash-upload-0123456789abcdef.part")
    assert not main._hidden_part("photo.jpg")
