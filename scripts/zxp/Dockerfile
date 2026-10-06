# Adobe ZXPSignCmd (Windows build) under Wine, for signing .zxp on Linux.
# Base image pinned by digest (scottyhardy/docker-wine:latest as of 2026-10-06).
FROM scottyhardy/docker-wine@sha256:0952273fae7eada20bd4b8393fcc133082d0ad57cef45d88d524dc6b2d985f18
# Adobe-CEP/CEP-Resources pinned to a commit; exe verified by sha256 (win32 build: the win64 one fails to sign under Wine).
ADD --chmod=755 --checksum=sha256:6e9d0ce79dd1f766da65acc2130a4388c0b7619a6bcdda71c2eafcccbe118a72 \
  https://raw.githubusercontent.com/Adobe-CEP/CEP-Resources/ab5e4e3e53a42fad08e1225a22a991bb1ffe73f6/ZXPSignCMD/4.1.103/win32/ZXPSignCmd.exe /opt/ZXPSignCmd.exe
ENV WINEDEBUG=-all WINEPREFIX=/tmp/wine
ENTRYPOINT ["wine", "/opt/ZXPSignCmd.exe"]
