# Adobe ZXPSignCmd (Windows build) under Wine, for signing .zxp on Linux.
FROM scottyhardy/docker-wine:latest
ARG ZXP_URL=https://raw.githubusercontent.com/Adobe-CEP/CEP-Resources/master/ZXPSignCMD/4.1.103/win32/ZXPSignCmd.exe
ADD --chmod=755 ${ZXP_URL} /opt/ZXPSignCmd.exe
ENV WINEDEBUG=-all WINEPREFIX=/tmp/wine
ENTRYPOINT ["wine", "/opt/ZXPSignCmd.exe"]
