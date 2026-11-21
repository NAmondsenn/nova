"""
Unpack Library.

Takes one file out of a conda package (a .conda file, which is a zip holding a
zstd-compressed tar), for install.ps1, which is run by Windows PowerShell 5.1, which can
read neither zstd nor tar. It is run with the venv's Python, which has the zstandard
package from requirements-pc.txt: Python 3.13's own library has no zstd.

Only the one file asked for is written, into the folder given and under its own name
whatever path it has in the package, so nothing in the package can put a file anywhere
else. It is called by install.ps1 once the package's SHA-256 has been checked, but it
doesn't rely on that:

    python unpack_library.py <package.conda> <path of the file in the package> <folder>
"""

import os
import posixpath
import shutil
import sys
import tarfile
import zipfile


def extract_member(archive: tarfile.TarFile, member: str, folder: str) -> str:
    """
    Writes one file of a tar into a folder, and nothing else.

    Args:
        archive: The tar, which may be read as a stream.
        member: The file's path in the tar, with forward slashes, which must be a
            relative one which stays inside it.
        folder: The folder to write it to, under the file's own name.

    Returns:
        The path written.

    Raises:
        ValueError: If the path leaves the tar, or the tar has no regular file of that name.
    """
    parts = member.split("/")
    if not member or member.startswith("/") or ".." in parts or "\\" in member or ":" in member:
        raise ValueError(f"{member} isn't a path inside the package")
    for entry in archive:
        if posixpath.normpath(entry.name) != posixpath.normpath(member):
            continue
        # A link could be made to point anywhere, so only the file itself is taken.
        if not entry.isfile():
            raise ValueError(f"{member} in the package isn't a plain file")
        target = os.path.join(folder, posixpath.basename(member))
        with archive.extractfile(entry) as source, open(target, "wb") as out:
            shutil.copyfileobj(source, out)
        return target
    raise ValueError(f"The package has no {member}")


def unpack(package: str, member: str, folder: str) -> str:
    """
    Writes one file of a conda package into a folder.

    Args:
        package: The .conda file.
        member: The file's path in the package's pkg-*.tar.zst, such as Library/bin/x.dll.
        folder: The folder to write it to.

    Returns:
        The path written.

    Raises:
        ValueError: If the package isn't laid out as a .conda is, or has no such file.
    """
    # Imported here so the rest can be used, and tested, where zstandard isn't installed.
    import zstandard

    with zipfile.ZipFile(package) as outer:
        names = [name for name in outer.namelist() if name.startswith("pkg-") and name.endswith(".tar.zst")]
        if len(names) != 1:
            raise ValueError(f"{package} holds {len(names)} pkg-*.tar.zst files, not one")
        with outer.open(names[0]) as compressed:
            reader = zstandard.ZstdDecompressor().stream_reader(compressed)
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                return extract_member(archive, member, folder)


def main(argv):
    if len(argv) != 4:
        print("Usage: unpack_library.py <package.conda> <path of the file in the package> <folder>", file=sys.stderr)
        return 2
    try:
        print(unpack(*argv[1:]))
    except Exception as e:
        print(f"Couldn't unpack {argv[2]} from {argv[1]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
