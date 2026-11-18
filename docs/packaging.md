# Packaging and releasing

How Heliograph's install files are made, how to cut a release, and what it takes to get Heliograph into apt, dnf, pacman and other package repositories. Users: see [install.md](install.md).

## What a package contains

Heliograph is plain Python with no compiled parts and no Python dependencies, so each package is one architecture-independent file (`all`, `noarch`, `any`):

| Installed path | What |
| --- | --- |
| `/usr/bin/heliograph` | a small launcher; it finds `../share/heliograph` next to itself |
| `/usr/share/heliograph/heliograph/` | the Python package, including the web interface (architecture-independent files belong in `/usr/share`) |
| `/usr/share/applications/dev.habibullah.Heliograph.desktop` | app-menu entry and "Open with" |
| `/usr/share/icons/hicolor/scalable/apps/dev.habibullah.Heliograph.svg` | icon |
| `/usr/share/metainfo/dev.habibullah.Heliograph.metainfo.xml` | AppStream data (software centres) |
| `/usr/share/man/man1/heliograph.1.gz` | the manual page (`man heliograph`) |
| `/usr/share/doc/heliograph/` or `/usr/share/licenses/heliograph/` | licence (and README for the `.deb`) |

Design choices, and why:

- **One private folder, not the distribution's `site-packages`.** The path of `site-packages` changes with every Python minor version, so a package that used it would be tied to one release of one distribution. With a private folder, one `.deb` works on Ubuntu 22.04, 24.04, Debian 12 and everything in between. The launcher puts that folder on the module path, and the background service the program starts inherits it (`client.ensure_server`).
- **No install or remove scripts.** Nothing runs as root at install time. The desktop database and icon cache are refreshed by each distribution's own triggers (dpkg triggers, RPM file triggers, pacman hooks).
- **No bytecode caches.** The launcher turns bytecode writing off, so running `heliograph` as root can never leave files in the package folder that the package manager does not know about. Measured cost: about 30 ms per start.
- **Only Python is a hard dependency.** FFmpeg and a Chromium-based browser are *recommended* (`.deb`, `.rpm`) or required/optional by the usual Arch rules, so a minimal install still starts and `heliograph doctor` says what is missing.
- **Every package installs the same files.** `packaging/build_packages.py: stage()` is the only place that decides what goes where; the `.deb`, the `.rpm` and the Arch package all call it, and `tests/test_packaging.py` checks the spec's `%files` list against it.

## Build the packages yourself

Standard library only; each command needs the native tool of that package type.

```bash
python3 packaging/build_packages.py deb  --out dist     # needs dpkg-deb  (Debian, Ubuntu, Mint)
python3 packaging/build_packages.py rpm  --out dist     # needs rpmbuild  (Fedora: sudo dnf install rpm-build)
python3 packaging/build_packages.py arch --out dist     # needs makepkg   (Arch; not as root)
python3 packaging/build_packages.py sums --out dist     # SHA256SUMS for everything in dist/
python3 packaging/build_packages.py stage --root /tmp/root   # just the installed files, like DESTDIR
```

On a different distribution, run the build in a container of the right one (nothing is installed on your machine):

```bash
docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:22.04 bash -c \
  'apt-get update -qq && apt-get install -y -qq python3 && python3 /src/packaging/build_packages.py deb --out /out'
```

The same file names appear in every build: `heliograph_<version>_all.deb`, `heliograph-<version>-1.noarch.rpm`, `heliograph-<version>-1-any.pkg.tar.zst`. Set `SOURCE_DATE_EPOCH` (for example `git log -1 --format=%ct`) and the output no longer depends on the time of the build. Rebuilding on the same machine with the same tools then gives identical bytes; a different build environment (another `xz`, `dpkg` or `makepkg` version, another file list) gives different bytes with the same installed files. The 0.1.1 files built by the release workflow and those built locally were compared file by file: the `.deb`, `.rpm`, wheel and source archive hold identical contents, and the Arch package differs only in its build metadata (build date, build path, packages present in the build container). Builds from a git checkout include new files that `.gitignore` does not exclude, so stage or ignore stray files first.

## Make a release

1. Change the version in all of these (`tests/test_packaging.py` fails if any disagree): `src/heliograph/__init__.py`, `pyproject.toml`, `packaging/arch/PKGBUILD` (`pkgver`), `packaging/rpm/heliograph.spec` (`Version` and a new top `%changelog` entry ending `- X.Y.Z-1`), the first `<release>` in `packaging/dev.habibullah.Heliograph.metainfo.xml`, and a `## [X.Y.Z]` section in `CHANGELOG.md`.
2. Run the tests, commit, push.
3. Tag and push the tag. The tag must be `v` plus the version:

   ```bash
   git tag -s vX.Y.Z -m "Heliograph X.Y.Z"
   git push origin vX.Y.Z
   ```

4. `.github/workflows/release.yml` checks that the tag matches the code and the changelog, runs the tests, builds the wheel and source archive, the `.deb` (Ubuntu runner), the `.rpm` (Fedora container) and the Arch package (Arch container), writes `SHA256SUMS`, creates the GitHub release if it does not exist (notes from `CHANGELOG.md`) and attaches everything. Watch it with `gh run watch`.
5. Check the release page: five files plus `SHA256SUMS`. Install one on a spare machine or container.

Other ways to start it: `gh workflow run release.yml -f tag=vX.Y.Z` rebuilds and re-attaches the files for an existing tag; `gh workflow run release.yml` with no tag only tests the builds and publishes nothing.

## Getting into apt, dnf, pacman and other repositories

Yes, all of them are possible. They differ in who decides, how long it takes and who keeps the package up to date.

| Route | What users type | Who approves | Effort and time | Status |
| --- | --- | --- | --- | --- |
| GitHub release files | `sudo apt install ./heliograph_*.deb` | nobody | done by the release workflow | ready |
| PyPI | `pipx install heliograph` | nobody | one afternoon, free | not published; name was free on 2026-11-18 |
| AUR (Arch) | `yay -S heliograph` | nobody (community) | one afternoon | recipe ready; not submitted; name was free on 2026-11-18 |
| Fedora COPR | `sudo dnf copr enable <you>/heliograph` then `sudo dnf install heliograph` | nobody | one afternoon | spec ready; not tried on COPR |
| Your own apt / dnf / pacman repository | `sudo apt install heliograph` after adding the repo once | you | a few days, then automatic on each release | not built |
| Ubuntu PPA | `sudo add-apt-repository ppa:<you>/heliograph` | nobody | needs a Debian source package | not built |
| Debian archive (and so Ubuntu, Mint, ...) | `sudo apt install heliograph` | Debian | weeks to months | not started |
| Fedora archive | `sudo dnf install heliograph` | Fedora reviewers | weeks | not started |
| Arch `[extra]` repository | `sudo pacman -S heliograph` | an Arch Package Maintainer | needs a maintainer to adopt it | not started |
| Flathub, Snap, AppImage | store or `flatpak install` | store reviewers | significant work, see below | not started |

### PyPI (works on every distribution)

`pipx install heliograph` (or `pip install --user`) then works anywhere with Python 3.10+. The release workflow has an optional job that publishes with PyPI's trusted publishing, so no password or token is stored:

1. Create a PyPI account, then on pypi.org add a *pending publisher*: owner `md8-habibullah`, repository `heliograph`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repository: Settings, Environments, create `pypi`; Settings, Variables, add `PUBLISH_TO_PYPI` = `true`.
3. The next tag publishes to PyPI after the release files are attached. A version number can be published only once, so test the release on TestPyPI first if unsure.

Not tried here: this needs your PyPI account.

### AUR (Arch, CachyOS, Manjaro users type `yay -S heliograph`)

`packaging/arch/PKGBUILD` is the recipe. After the tag exists: set the checksum (`updpkgsums` in that folder), create an account on aur.archlinux.org and add an SSH key, then

```bash
git clone ssh://aur@aur.archlinux.org/heliograph.git aur-heliograph
cp packaging/arch/PKGBUILD aur-heliograph/ && cd aur-heliograph
makepkg --printsrcinfo > .SRCINFO
```

and commit and push both files there (the AUR is its own git repository). Every release needs a new `pkgver` and checksum pushed the same way; a `heliograph-bin` package that installs the release file is a common second package.

### Fedora COPR (Fedora, RHEL-like and openSUSE users)

COPR builds RPMs from a spec on Fedora's servers and hosts a repository for you. Create a project at copr.fedorainfracloud.org (needs a Fedora account), add a package from this repository with the spec at `packaging/rpm/heliograph.spec` (its `Source0` is the tag archive) and choose the Fedora releases. Users then run `sudo dnf copr enable <you>/heliograph`. Not tried; COPR's source options change, so follow its current documentation.

### Your own repository (the real `apt install heliograph`)

Anyone can host an apt, dnf or pacman repository on static hosting (GitHub Pages or a bucket). The release workflow would add one step that regenerates the repository from the new files:

- **apt**: `apt-ftparchive` or `dpkg-scanpackages` for the index, a signed `InRelease`, and users add one key file and one line under `/etc/apt/sources.list.d/`.
- **dnf**: `createrepo_c` for the metadata and a `.repo` file; packages and metadata signed with the same key.
- **pacman**: `repo-add` for the database, signed packages, and a `[heliograph]` section in `/etc/pacman.conf`.

After that, updates arrive with the system's normal updates. What it needs from you: a dedicated signing key (kept as a GitHub secret, never your personal key) and a decision where to host it. Not built yet.

### Ubuntu PPA

A PPA builds from a Debian *source* package (a `debian/` folder with `control`, `rules` and `changelog`) signed with your key and uploaded with `dput`; Launchpad then builds per Ubuntu release. The `.deb` made here is binary-only and cannot be uploaded to a PPA. A proper source package is also the first step toward Debian.

### The distributions' own archives

- **Debian** (which Ubuntu, Mint and others then inherit): file an "intent to package" bug, build a source package that follows Debian Policy (`dh-python`, no network access at build time, tests), and find a sponsor through mentors.debian.net, because only Debian Developers can upload. Weeks to months. Worth doing after the project is stable.
- **Fedora**: submit a package review request in Bugzilla; a first-time packager also needs a sponsor. The spec must follow Fedora's Python guidelines (`%pyproject_*` macros and the standard `site-packages` location), so `packaging/rpm/heliograph.spec` would be rewritten for it. Fedora's FFmpeg is the codec-limited `ffmpeg-free`, which matters for a media player (see [install.md](install.md)).
- **Arch `[extra]`**: there is no application process. An Arch Package Maintainer has to want to maintain it. The usual path is a popular AUR package that a maintainer then adopts.

### Flathub, Snap, AppImage

Not a good fit yet. Heliograph needs the whole file tree (a media library), FFmpeg with the codecs, and a Chromium-based browser for its window; a sandboxed bundle would have to carry or reach all three, and request broad file access. Possible, but a project of its own.

## What has and has not been tested

See [install.md, "Tested on"](install.md#tested-on): each package type was built in a clean container of its own distribution, installed, run as a normal user and removed on Ubuntu, Debian, Fedora, openSUSE and Arch, and the whole test suite was run on the oldest and newest stacks. The release workflow ran on GitHub for the first time on the `v0.1.1` tag (2026-11-18) and passed; it built and attached all five files and `SHA256SUMS`, and their installed contents match the files tested above. Not yet done: every route in the table above beyond the release files.
