"""Path handling for the FTPS transport - the bit that could clobber the SD card."""

from __future__ import annotations

import pytest

from mhs.drivers.bambu.files import _parse_list_line, sanitize_remote_name
from mhs.errors import FileTransferError


@pytest.mark.parametrize(
    "name",
    ["benchy.3mf", "part v2 (final).gcode", "a-b_c[1].3mf"],
)
def test_accepts_reasonable_names(name):
    assert sanitize_remote_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["bad\x00name.3mf", "", "..", "shell;rm -rf.3mf", "x" * 200, "../.."],
)
def test_rejects_unsafe_names(name):
    with pytest.raises(FileTransferError):
        sanitize_remote_name(name)


def test_strips_directories_so_traversal_cannot_escape_the_upload_dir():
    assert sanitize_remote_name("/cache/nested/benchy.3mf") == "benchy.3mf"
    assert sanitize_remote_name("../../etc/passwd") == "passwd"


def test_parse_unix_list_line():
    entry = _parse_list_line("-rw-r--r-- 1 root root 1048576 Sep 18 07:12 benchy.3mf", "cache")
    assert entry.name == "benchy.3mf"
    assert entry.path == "cache/benchy.3mf"
    assert entry.size_bytes == 1_048_576
    assert entry.is_dir is False

    directory = _parse_list_line("drwxr-xr-x 2 root root 4096 Sep 18 07:12 timelapse", "")
    assert directory.is_dir is True and directory.path == "timelapse"
    assert _parse_list_line("total 8", "") is None
