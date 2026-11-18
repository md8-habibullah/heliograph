#!/usr/bin/env python3
"""Build Heliograph's installable packages (.deb, .rpm, Arch) from one source tree.

    python3 packaging/build_packages.py deb   [--out dist]    needs dpkg-deb   (Debian, Ubuntu, Mint, ...)
    python3 packaging/build_packages.py rpm   [--out dist]    needs rpmbuild   (Fedora, openSUSE, ...)
    python3 packaging/build_packages.py arch  [--out dist]    needs makepkg    (Arch, CachyOS, Manjaro, ...; not as root)
    python3 packaging/build_packages.py sums  [--out dist]    writes dist/SHA256SUMS
    python3 packaging/build_packages.py stage --root DIR [--prefix /usr] [--docs none|deb|arch]

Standard library only. All three package types are made from the same `stage` step, so they install exactly
the same files. Heliograph is plain Python with no compiled parts and no Python dependencies, so every
package is architecture independent ("all" / "noarch" / "any") and keeps the program in its own folder:

    <prefix>/bin/heliograph                  small launcher (finds ../share/heliograph next to itself)
    <prefix>/share/heliograph/heliograph/    the Python package, including the web interface
    <prefix>/share/applications/dev.habibullah.Heliograph.desktop
    <prefix>/share/icons/hicolor/scalable/apps/dev.habibullah.Heliograph.svg
    <prefix>/share/metainfo/dev.habibullah.Heliograph.metainfo.xml
    <prefix>/share/man/man1/heliograph.1.gz

A private folder (instead of the distribution's python site-packages) means one package works on every
release of a distribution, whatever its Python minor version is. The package needs `python3` 3.10 or newer;
it never installs or changes anything else on the system, and it has no install or remove scripts.

Set SOURCE_DATE_EPOCH (for example `git log -1 --format=%ct`) so the output does not depend on the build time
(identical bytes on the same machine and tools; other environments give the same installed files).
"""

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
NAME = "heliograph"
APP_ID = "dev.habibullah.Heliograph"
URL = "https://github.com/md8-habibullah/heliograph"
# A GitHub "no-reply" address: package formats want a contact, and this one reveals nothing private.
MAINTAINER = "MD. Habibullah Sharif <149287500+md8-habibullah@users.noreply.github.com>"
SUMMARY = "A fast, private media library and player for Linux"

# Debian-family package names (checked against the Debian and Ubuntu archives; see docs/packaging.md).
DEB_DEPENDS = "python3 (>= 3.10), hicolor-icon-theme"
DEB_RECOMMENDS = ("ffmpeg, chromium | chromium-browser | google-chrome-stable | brave-browser | "
                  "microsoft-edge-stable | vivaldi-stable, python3-pil")
DEB_SUGGESTS = "imagemagick, libheif-examples, libraw-bin, mpv"
DEB_DESCRIPTION = """\
 Heliograph browses and plays very large collections of pictures, videos and
 music without slowing the computer down. It loads only what is on the screen,
 keeps its index in a local SQLite database, plays formats that browsers cannot
 (MOV, MKV, HEVC and more) by repackaging or converting them on the fly with
 FFmpeg, and converts pictures, video and audio. Everything stays on this
 computer: no account and no telemetry.
 .
 The window is drawn by a Chromium-based browser in app mode. A built-in MCP
 server lets AI assistants search the library (read-only unless enabled)."""

LAUNCHER = '''#!/usr/bin/python3
"""Starts Heliograph from the files this package installed in ../share/heliograph (next to this bin folder)."""
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("Heliograph needs Python 3.10 or newer; this is Python %d.%d." % sys.version_info[:2])
# The install folder belongs to the package manager: never leave bytecode caches in it (not even when root runs
# this, or in the background service this starts), because the package manager would not know to remove them.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "share", "heliograph"))

from heliograph.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
'''


class BuildError(Exception):
    """A readable reason a package could not be built (printed without a traceback)."""


def version():
    text = (ROOT / "src" / "heliograph" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], **kw)
    if r.returncode:
        raise BuildError(f"{Path(str(cmd[0])).name} failed (exit {r.returncode})")


def need(tool, hint):
    path = shutil.which(tool)
    if not path:
        raise BuildError(f"{tool} was not found. {hint}")
    return path


def epoch():
    value = os.environ.get("SOURCE_DATE_EPOCH", "")
    return int(value) if value.isdigit() else None


# --------------------------------------------------------------------------- the installed files

def package_files():
    """Every file of the Python package (sources and web interface), without caches."""
    base = ROOT / "src" / "heliograph"
    return [p for p in sorted(base.rglob("*")) if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")]


def debian_copyright():
    """Debian's machine-readable copyright file (DEP-5), with the licence text from LICENSE."""
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8").strip("\n")
    holder = next((ln for ln in lic.splitlines() if ln.startswith("Copyright")), "Copyright (c) MD. Habibullah Sharif")
    holder = re.sub(r"^Copyright( \(c\))? ", "", holder)
    text = "\n".join(" " + ln if ln.strip() else " ." for ln in lic.splitlines())
    return (f"Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
            f"Upstream-Name: {NAME}\nUpstream-Contact: {URL}/issues\nSource: {URL}\n\n"
            f"Files: *\nCopyright: {holder}\nLicense: MIT\n{text}\n")


def stage(root, prefix="/usr", docs="none"):
    """Write the files a package installs under `root` (like make's DESTDIR); returns their paths relative to it.

    docs: "none" (the package format adds its own licence and docs), "deb" (Debian copyright file and README),
    "arch" (licence in /usr/share/licenses)."""
    root = Path(root)
    pre = prefix.strip("/")
    made = []

    def put(rel, data=None, src=None, mode=0o644):
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src is not None:
            shutil.copyfile(src, dest)
        elif isinstance(data, bytes):
            dest.write_bytes(data)
        else:
            dest.write_text(data, encoding="utf-8")
        dest.chmod(mode)
        made.append(str(Path(rel)))

    for f in package_files():
        put(f"{pre}/share/{NAME}/heliograph/{f.relative_to(ROOT / 'src' / 'heliograph')}", src=f)
    put(f"{pre}/bin/{NAME}", data=LAUNCHER, mode=0o755)
    put(f"{pre}/share/applications/{APP_ID}.desktop", src=PKG / f"{APP_ID}.desktop")
    put(f"{pre}/share/icons/hicolor/scalable/apps/{APP_ID}.svg", src=PKG / f"{APP_ID}.svg")
    put(f"{pre}/share/metainfo/{APP_ID}.metainfo.xml", src=PKG / f"{APP_ID}.metainfo.xml")
    put(f"{pre}/share/man/man1/{NAME}.1.gz", data=gzip.compress((PKG / f"{NAME}.1").read_bytes(), 9, mtime=0))
    if docs == "deb":
        put(f"{pre}/share/doc/{NAME}/copyright", data=debian_copyright())
        put(f"{pre}/share/doc/{NAME}/README.md", src=ROOT / "README.md")
    elif docs == "arch":
        put(f"{pre}/share/licenses/{NAME}/LICENSE", src=ROOT / "LICENSE")

    # Same permissions whatever the builder's umask is; optional fixed times for repeatable output.
    stamp = epoch()
    paths = [root] + sorted(p for p in root.rglob("*"))
    for p in paths:
        if p.is_dir() and not p.is_symlink():
            p.chmod(0o755)
        if stamp is not None:
            os.utime(p, (stamp, stamp), follow_symlinks=False)
    return sorted(made)


# --------------------------------------------------------------------------- the source archive (for rpm and makepkg)

def source_files():
    """Files of the source tree: tracked files plus new ones that .gitignore does not exclude, else a plain walk."""
    git = shutil.which("git")
    if git:
        r = subprocess.run([git, "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True)
        if r.returncode == 0 and r.stdout:
            names = [n for n in r.stdout.decode("utf-8").split("\0") if n]
            return sorted(n for n in names if (ROOT / n).is_file())
    skip = {".git", "__pycache__", "dist", "build", ".venv", "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
    names = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.endswith(".egg-info"))
        for f in sorted(filenames):
            if not f.endswith((".pyc", ".db", ".log")):
                names.append(str((Path(dirpath) / f).relative_to(ROOT)))
    return names


def source_tarball(dest, ver):
    """`heliograph-<ver>.tar.gz` with a `heliograph-<ver>/` folder inside, like GitHub's archive of a tag."""
    stamp = epoch()
    with gzip.GzipFile(dest, "wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name in source_files():
            path = ROOT / name
            info = tar.gettarinfo(str(path), arcname=f"{NAME}-{ver}/{name}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if stamp is not None:
                info.mtime = stamp
            if info.isreg():
                with open(path, "rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.addfile(info)
    return Path(dest)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- .deb

def deb_control(ver, installed_kib):
    fields = [("Package", NAME), ("Version", ver), ("Architecture", "all"), ("Maintainer", MAINTAINER),
              ("Installed-Size", str(installed_kib)), ("Depends", DEB_DEPENDS), ("Recommends", DEB_RECOMMENDS),
              ("Suggests", DEB_SUGGESTS), ("Section", "graphics"), ("Priority", "optional"), ("Homepage", URL)]
    return "".join(f"{k}: {v}\n" for k, v in fields) + f"Description: {SUMMARY}\n{DEB_DESCRIPTION}\n"


def build_deb(out):
    dpkg_deb = need("dpkg-deb", "It is part of dpkg: run this on Debian, Ubuntu or Mint (or in a container of one).")
    ver = version()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="heliograph-deb-") as tmp:
        tree = Path(tmp) / f"{NAME}_{ver}_all"
        files = stage(tree, "/usr", docs="deb")
        kib = sum(-(-(tree / f).stat().st_size // 1024) for f in files)
        meta = tree / "DEBIAN"
        meta.mkdir(mode=0o755)
        meta.chmod(0o755)
        (meta / "control").write_text(deb_control(ver, kib), encoding="utf-8")
        (meta / "md5sums").write_text("".join(f"{hashlib.md5((tree / f).read_bytes()).hexdigest()}  {f}\n" for f in files), encoding="utf-8")
        for f in ("control", "md5sums"):
            (meta / f).chmod(0o644)
        target = out / f"{NAME}_{ver}_all.deb"
        # xz, not zstd: older Debian and Ubuntu releases cannot read zstd-compressed packages.
        run([dpkg_deb, "--build", "--root-owner-group", "-Zxz", tree, target])
    return [target]


# --------------------------------------------------------------------------- .rpm

def build_rpm(out):
    rpmbuild = need("rpmbuild", "Install rpm-build (Fedora: sudo dnf install rpm-build) or run this in a Fedora container.")
    ver = version()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="heliograph-rpm-") as tmp:
        top = Path(tmp)
        for d in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS"):
            (top / d).mkdir()
        source_tarball(top / "SOURCES" / f"{NAME}-{ver}.tar.gz", ver)
        spec = top / "SPECS" / f"{NAME}.spec"
        shutil.copyfile(PKG / "rpm" / f"{NAME}.spec", spec)
        # No ".fc43"-style tag in the name: the same file installs on every RPM-based distribution.
        run([rpmbuild, "-bb", "--define", f"_topdir {top}", "--define", "dist %{nil}", spec])
        made = sorted((top / "RPMS").rglob("*.rpm"))
        if not made:
            raise BuildError("rpmbuild produced no .rpm file")
        targets = []
        for f in made:
            shutil.copyfile(f, out / f.name)
            targets.append(out / f.name)
    return targets


# --------------------------------------------------------------------------- Arch

def build_arch(out):
    makepkg = need("makepkg", "Run this on Arch Linux, CachyOS or Manjaro (or in an Arch container).")
    if os.geteuid() == 0:
        raise BuildError("makepkg refuses to run as root. Run this as a normal user.")
    ver = version()
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="heliograph-arch-") as tmp:
        work = Path(tmp)
        tarball = source_tarball(work / f"{NAME}-{ver}.tar.gz", ver)
        text = (PKG / "arch" / "PKGBUILD").read_text(encoding="utf-8")
        # The published PKGBUILD downloads the tag from GitHub; this build uses the local tree instead.
        text, n_src = re.subn(r"^source=.*$", 'source=("$pkgname-$pkgver.tar.gz")', text, flags=re.M)
        text, n_sum = re.subn(r"^sha256sums=.*$", f"sha256sums=('{sha256(tarball)}')", text, flags=re.M)
        if (n_src, n_sum) != (1, 1):
            raise BuildError("packaging/arch/PKGBUILD must have exactly one source= line and one sha256sums= line")
        (work / "PKGBUILD").write_text(text, encoding="utf-8")
        env = dict(os.environ, PKGDEST=str(out), SRCDEST=str(work), BUILDDIR=str(work / "build"), PACKAGER=MAINTAINER)
        before = set(out.glob("*.pkg.tar.*"))
        # Tests run in CI before a release; the build host may lack the tools some tests use.
        run([makepkg, "--nodeps", "--nocheck", "--noconfirm", "--force"], cwd=work, env=env)
        made = sorted(set(out.glob("*.pkg.tar.*")) - before) or sorted(out.glob(f"{NAME}-{ver}-*.pkg.tar.*"))
        if not made:
            raise BuildError("makepkg produced no package")
    return made


# --------------------------------------------------------------------------- checksums

def write_sums(out):
    files = sorted(p for p in out.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    if not files:
        raise BuildError(f"nothing to checksum in {out}")
    (out / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files), encoding="utf-8")
    return [out / "SHA256SUMS"]


# --------------------------------------------------------------------------- command line

def main(argv=None):
    ap = argparse.ArgumentParser(description="Build Heliograph's installable packages.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stage", help="write the installed files under a folder (used by the package recipes)")
    st.add_argument("--root", required=True, help="destination folder (like DESTDIR)")
    st.add_argument("--prefix", default="/usr", help="install prefix (default /usr)")
    st.add_argument("--docs", choices=("none", "deb", "arch"), default="none", help="which licence/docs layout to add")
    for name, helptext in (("deb", "build the .deb"), ("rpm", "build the .rpm"), ("arch", "build the Arch package"),
                           ("sums", "write SHA256SUMS for the files in --out")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--out", default="dist", help="folder for the result (default: dist)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "stage":
            made = stage(Path(args.root), args.prefix, args.docs)
            print(f"staged {len(made)} files under {args.root}")
            return 0
        builders = {"deb": build_deb, "rpm": build_rpm, "arch": build_arch, "sums": write_sums}
        for f in builders[args.cmd](Path(args.out)):
            print(f"built {f}")
        return 0
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
