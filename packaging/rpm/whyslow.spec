Name:           whyslow
Version:        0.1.1
Release:        1%{?dist}
Summary:        Find out why your Linux system just stuttered, froze or crashed
License:        GPL-3.0-or-later
URL:            https://github.com/metalmike17/whyslow
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
Requires:       python3 >= 3.11
Requires:       python3-pyside6
Recommends:     mangohud
Suggests:       nvidia-utils

%description
WhySlow records a few seconds of system health every second and, when something
feels slow, explains in plain English which program caused it and what to do:
a black box for freezes and crashes, a known-offender database, game stutter
analysis from MangoHud logs, a hardware-gotcha check, laptop battery culprits,
a slow-boot explainer and an anonymized shareable report.

Recording is opt-in: run `whyslow enable` after installing.

%prep
%autosetup -n %{name}-%{version}

%build

%install
install -d %{buildroot}%{python3_sitelib} %{buildroot}%{_bindir}
cp -r whyslow %{buildroot}%{python3_sitelib}/
find %{buildroot} -name __pycache__ -prune -exec rm -rf {} +
install -Dm755 packaging/whyslow-launcher %{buildroot}%{_bindir}/whyslow
install -Dm644 packaging/whyslow-user.service %{buildroot}%{_userunitdir}/whyslow.service
install -Dm644 data/io.github.metalmike17.WhySlow.desktop -t %{buildroot}%{_datadir}/applications/
install -Dm644 data/io.github.metalmike17.WhySlow.metainfo.xml -t %{buildroot}%{_metainfodir}/
install -Dm644 data/io.github.metalmike17.WhySlow.svg -t %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.metalmike17.WhySlow.desktop
PYTHONPATH=%{buildroot}%{python3_sitelib} %{python3} -m unittest discover -s tests

%post
echo "WhySlow installed. Start recording with:  whyslow enable" >&2 || :

%files
%license LICENSE
%doc README.md
%{_bindir}/whyslow
%{python3_sitelib}/whyslow/
%{_userunitdir}/whyslow.service
%{_datadir}/applications/io.github.metalmike17.WhySlow.desktop
%{_metainfodir}/io.github.metalmike17.WhySlow.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.metalmike17.WhySlow.svg

%changelog
* Wed Nov 04 2026 metalmike17 <michaeljg0717@gmail.com> - 0.1.1-1
- GPU support for AMD (amdgpu) and Intel (i915, xe) alongside NVIDIA

* Wed Nov 04 2026 metalmike17 <michaeljg0717@gmail.com> - 0.1.0-1
- Initial package
