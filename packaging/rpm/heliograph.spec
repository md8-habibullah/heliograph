Name:           heliograph
Version:        0.1.1
Release:        1%{?dist}
Summary:        A fast, private media library and player for Linux

License:        MIT
URL:            https://github.com/md8-habibullah/heliograph
Source0:        %{url}/archive/refs/tags/v%{version}/%{name}-%{version}.tar.gz

# Plain Python with no compiled parts: one package for every architecture and every Python 3.10+.
BuildArch:      noarch
BuildRequires:  python3 >= 3.10
Requires:       python3 >= 3.10
Requires:       hicolor-icon-theme
# The window is a Chromium-based browser in app mode; FFmpeg plays and converts what browsers cannot.
# Fedora's own "ffmpeg-free" has patent-encumbered codecs (H.264, HEVC, AAC) removed: for MOV, MKV and
# most phone videos enable RPM Fusion and install "ffmpeg" instead (see docs/install.md).
Recommends:     (ffmpeg or ffmpeg-free)
Recommends:     (chromium or google-chrome-stable or brave-browser or microsoft-edge-stable or vivaldi-stable)
Recommends:     python3-pillow
Suggests:       ImageMagick
Suggests:       libheif-tools
Suggests:       mpv

%description
Heliograph browses and plays very large collections of pictures, videos and
music without slowing the computer down. It loads only what is on the screen,
keeps its index in a local SQLite database, plays formats that browsers cannot
(MOV, MKV, HEVC and more) by repackaging or converting them on the fly with
FFmpeg, and converts pictures, video and audio. Everything stays on this
computer: no account and no telemetry.

The window is drawn by a Chromium-based browser in app mode. A built-in MCP
server lets AI assistants search the library (read-only unless enabled).

%prep
%autosetup -n %{name}-%{version}

%build
# Nothing to compile.

%install
# One staging step shared with the .deb and the Arch package, so every package installs the same files.
python3 packaging/build_packages.py stage --root %{buildroot} --prefix %{_prefix} --docs none

%check
# The installed launcher starts and reports its version (the full test suite runs in CI).
HELIOGRAPH_HOME=%{_builddir}/home %{buildroot}%{_bindir}/heliograph --version

%files
%license LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/heliograph
%{_datadir}/heliograph/
%{_datadir}/applications/dev.habibullah.Heliograph.desktop
%{_datadir}/icons/hicolor/scalable/apps/dev.habibullah.Heliograph.svg
%{_datadir}/metainfo/dev.habibullah.Heliograph.metainfo.xml
%{_mandir}/man1/heliograph.1*

%changelog
* Wed Nov 18 2026 MD. Habibullah Sharif <149287500+md8-habibullah@users.noreply.github.com> - 0.1.1-1
- First release with an .rpm package
