# Install Heliograph

Every release has one install file per Linux family on the [Releases page](https://github.com/md8-habibullah/heliograph/releases/latest). Each file installs the program, the `heliograph` command, a manual page, an app-menu entry and the icon, and changes nothing else on your system. Packages contain no install or remove scripts. How they are built: [packaging.md](packaging.md).

## Pick your system

| Your system | File | Install it with |
| --- | --- | --- |
| Ubuntu, Debian, Linux Mint, Pop!_OS, Zorin | `heliograph_<version>_all.deb` | `sudo apt install ./heliograph_*_all.deb` |
| Fedora, Nobara | `heliograph-<version>-1.noarch.rpm` | `sudo dnf install ./heliograph-*.noarch.rpm` |
| openSUSE | `heliograph-<version>-1.noarch.rpm` | `sudo zypper install ./heliograph-*.noarch.rpm` |
| Arch, CachyOS, Manjaro, EndeavourOS | `heliograph-<version>-1-any.pkg.tar.zst` | `sudo pacman -U heliograph-*-any.pkg.tar.zst` |
| Anything else with Python 3.10 or newer | | `pipx install "heliograph[image] @ git+https://github.com/md8-habibullah/heliograph.git"` |

Run the command in the folder where you saved the file. The `./` in the apt and dnf commands matters: it tells them to install a file, not a package from a repository.

## Download and check

In a browser, take the file from the Releases page. From a terminal with the GitHub CLI:

```bash
gh release download --repo md8-habibullah/heliograph --pattern '*.deb' --pattern SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS      # prints "OK" for each file it finds
```

(Use `'*.rpm'` or `'*.pkg.tar.zst'` for the other families.)

## What gets installed with it

Only Python 3.10 or newer is required. The rest is *recommended*, because Heliograph is much more useful with it:

| Wanted | Why | Debian, Ubuntu | Fedora | Arch |
| --- | --- | --- | --- | --- |
| FFmpeg | video and audio details, thumbnails, playing MOV/MKV/HEVC, conversion | pulled in by apt | pulled in by dnf (see Fedora codecs below) | required |
| A Chromium-based browser | draws the window (Brave, Chrome, Edge, Vivaldi and Chromium all work) | pulled in unless one is installed | pulled in unless one is installed | optional dependency |
| Pillow | faster picture thumbnails | pulled in by apt | pulled in by dnf | optional dependency |

Without a Chromium-based browser Heliograph opens in your default browser instead. `heliograph doctor` lists what it found and says what to install for anything missing. To skip the recommended packages: `sudo apt install --no-install-recommends ./heliograph_*_all.deb` or `sudo dnf install --setopt=install_weak_deps=False ./heliograph-*.rpm`.

**Ubuntu note.** On Ubuntu the `chromium-browser` package installs Chromium as a snap. Only the window runs inside it (Heliograph reads your files itself, and starts the browser with no special profile folder), so it should work, but that combination has not been tried. If you already use Brave, Chrome, Edge or Vivaldi, Heliograph uses that one.

**Fedora codecs.** Fedora's own `ffmpeg-free` has the patent-encumbered codecs (H.264, HEVC, AAC and others) removed, and most phone and camera videos use them. For full playback and conversion, enable [RPM Fusion](https://rpmfusion.org/Configuration) and swap in the full FFmpeg, as Fedora's own [multimedia guide](https://docs.fedoraproject.org/en-US/quick-docs/rpmfusion-setup/) describes: `sudo dnf swap ffmpeg-free ffmpeg --allowerasing`. Heliograph then needs no change.

## After installing

- Open **Heliograph** from the app menu, or run `heliograph ~/Pictures`.
- **Wayland** (GNOME, KDE, Hyprland and others): run `heliograph install` once. A Chromium-based browser names its window itself on Wayland, and this one command makes the launcher match, so the dock or taskbar shows the right icon. (Details in the [FAQ](faq.md).)
- Make it the default for pictures, videos and music, if you like (undo any time): `heliograph default --images --videos --audio` and `heliograph default --restore`.

## Update

Install the newer file the same way. Then run `heliograph stop` once: a background service that is still running keeps the old code until it stops (it stops by itself a few minutes after you close the window).

## Remove

```bash
sudo apt remove heliograph          # Debian, Ubuntu, Mint
sudo dnf remove heliograph          # Fedora
sudo zypper remove heliograph       # openSUSE
sudo pacman -R heliograph           # Arch
```

Your library database, settings and thumbnails live in your home folder and are kept, so reinstalling picks up where you left off. To delete them too, run `heliograph stop` first, then remove `~/.local/share/heliograph`, `~/.cache/heliograph` and `~/.local/state/heliograph`.

## Too old?

Heliograph needs Python 3.10 or newer. That rules out Ubuntu 20.04, Debian 11 and RHEL, AlmaLinux or Rocky Linux 9 (they ship Python 3.9; the package refuses to install there instead of failing later). `pipx` with a newer Python is the way on those.

On a distribution with an older FFmpeg, such as Ubuntu 22.04 (FFmpeg 4.4), a few *conversion outputs* are missing because that FFmpeg cannot write them: AVIF, JPEG XL and AV1. Everything else works.

## Tested on

The packages were built and then installed, run and removed in clean containers of each distribution, using only what the package's own dependencies pulled in. "Run" means: the command starts, the background service starts as a normal user and answers, `heliograph doctor` reports, and the service stops. This does **not** include opening the window, which needs a desktop and a browser.

| Distribution | Python | File | Result |
| --- | --- | --- | --- |
| Ubuntu 22.04 | 3.10.12 | `.deb` | installed, every file verified against the package, service ran as a normal user, removed with nothing left behind |
| Ubuntu 24.04 | 3.12.3 | `.deb` | the same |
| Debian 12 | 3.11.2 | `.deb` | the same, and `man heliograph` opened |
| Fedora 45 | 3.15.2 | `.rpm` | installed with dnf's real dependency resolution, `rpm -V` clean, service ran, removed. `rpmlint`: 0 errors, 0 warnings |
| openSUSE Tumbleweed | 3.13.16 | `.rpm` | installed (it fetched Python 3.13 itself), `rpm -V` clean, service ran, removed |
| AlmaLinux 9 | 3.9 | `.rpm` | **refused**, as intended: `nothing provides python3 >= 3.10` |
| Arch Linux | 3.14.10 | `.pkg.tar.zst` | installed with pacman's real dependency resolution (python, ffmpeg, hicolor-icon-theme), service ran, removed. `namcap`: no errors |
| Plain Python 3.12 | 3.12.x | wheel | `pip install` works, the command starts, the web interface is inside |

The complete test suite (160 tests) was also run, as a normal user, on the oldest and newest stacks: Ubuntu 22.04 with its own Python 3.10, FFmpeg 4.4.2, Pillow 9.0.1 and ImageMagick (9 skipped: the three encoders FFmpeg 4.4 lacks, the Node.js syntax check, and tests for tools not installed there); Fedora 45 on Python 3.15 and Arch on Python 3.14, both with no FFmpeg and no Pillow (24 and 25 skipped). All passed. Testing on Ubuntu 22.04 found one real bug, fixed in 0.1.1: Pillow thumbnails silently fell back to a slower path on Pillow older than 9.1.

**Not tested**, so do not take it as promised:

- Opening the actual window on any desktop except Hyprland (GNOME, KDE and others are expected to work, since the window is a plain Chromium app window, but nobody has looked yet).
- Ubuntu's snap Chromium, Linux Mint, Pop!_OS, Zorin, Nobara, Manjaro, EndeavourOS and CachyOS themselves. They are built on the families above and should behave the same, but the packages were not installed on them.
- The RPM Fusion codec swap on Fedora (the command is the one in Fedora's own guide).
- Hardware video acceleration inside a package install.
- The files attached to the GitHub release were built by the release workflow, not by hand. Their installed contents were compared file by file with the files tested above and are identical (the Arch package differs only in build metadata), but the workflow's own files were not installed again on every distribution in the table.
