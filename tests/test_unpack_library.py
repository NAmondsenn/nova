"""
Unpack Library Test
Checks the script install.ps1 takes the echo canceller's library out of its conda package
with writes only that file, into the folder it was given, and refuses a path which leaves
the package, a link and a file the package hasn't got. The part which reads the package's
zstd needs the zstandard package, so runs only where that is installed, as it is in the
PC's venv.
"""

import importlib.util
import io
import os
import tarfile
import zipfile

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "windows",
                      "unpack_library.py")


def load_unpacker():
    """Imports unpack_library.py, which isn't in a package."""
    spec = importlib.util.spec_from_file_location("unpack_library", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


unpack_library = load_unpacker()


def _tar(files, links=None):
    """A tar, opened to be read as a stream, holding the named files and any links."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buffer.getvalue()


def _stream(data):
    return tarfile.open(fileobj=io.BytesIO(data), mode="r|")


def test_only_the_named_file_is_written_under_its_own_name(tmp_path):
    data = _tar({"Library/include/speex.h": b"header", "Library/bin/speexdsp.dll": b"library",
                 "Library/lib/speexdsp.lib": b"import library"})
    written = unpack_library.extract_member(_stream(data), "Library/bin/speexdsp.dll", str(tmp_path))
    assert written == str(tmp_path / "speexdsp.dll")
    assert [path.name for path in tmp_path.iterdir()] == ["speexdsp.dll"]
    assert (tmp_path / "speexdsp.dll").read_bytes() == b"library"


@pytest.mark.parametrize("member", ["../speexdsp.dll", "Library/../../speexdsp.dll", "/Library/bin/speexdsp.dll",
                                    "C:/speexdsp.dll", "Library\\bin\\speexdsp.dll", ""])
def test_a_path_which_leaves_the_package_is_refused(tmp_path, member):
    # Even though the package holds a file of that name.
    data = _tar({"../speexdsp.dll": b"escaped", "Library/../../speexdsp.dll": b"escaped",
                 "/Library/bin/speexdsp.dll": b"escaped", "Library\\bin\\speexdsp.dll": b"escaped"})
    folder = tmp_path / "lib"
    folder.mkdir()
    with pytest.raises(ValueError):
        unpack_library.extract_member(_stream(data), member, str(folder))
    assert not list(tmp_path.rglob("*.dll"))


def test_a_link_or_a_missing_file_is_refused(tmp_path):
    data = _tar({"Library/bin/other.dll": b"other"}, links={"Library/bin/speexdsp.dll": "../../../outside"})
    with pytest.raises(ValueError, match="plain file"):
        unpack_library.extract_member(_stream(data), "Library/bin/speexdsp.dll", str(tmp_path))
    with pytest.raises(ValueError, match="no Library/bin/gone.dll"):
        unpack_library.extract_member(_stream(data), "Library/bin/gone.dll", str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_a_file_is_taken_out_of_a_conda_package(tmp_path):
    zstandard = pytest.importorskip("zstandard")
    inner = zstandard.ZstdCompressor().compress(_tar({"Library/bin/speexdsp.dll": b"library", "Library/x": b"x"}))
    package = tmp_path / "speexdsp.conda"
    with zipfile.ZipFile(package, "w") as outer:
        outer.writestr("metadata.json", "{}")
        outer.writestr("pkg-speexdsp-1.2.1-h5112557_1.tar.zst", inner)
        outer.writestr("info-speexdsp-1.2.1-h5112557_1.tar.zst", inner)
    folder = tmp_path / "lib"
    folder.mkdir()
    assert unpack_library.main(["unpack_library.py", str(package), "Library/bin/speexdsp.dll", str(folder)]) == 0
    assert [path.name for path in folder.iterdir()] == ["speexdsp.dll"]
    assert (folder / "speexdsp.dll").read_bytes() == b"library"
    # Anything which goes wrong is a non-zero exit, which install.ps1 turns into a warning.
    assert unpack_library.main(["unpack_library.py", str(package), "Library/bin/gone.dll", str(folder)]) == 1
    assert unpack_library.main(["unpack_library.py", str(tmp_path / "missing.conda"), "x", str(folder)]) == 1
