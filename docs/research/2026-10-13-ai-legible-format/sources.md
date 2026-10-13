# Reference clips (not committed; Mixkit free licence)

Listed with headless Chromium from https://mixkit.co/free-after-effects-templates/{titles,openers,logo}/ on 2026-10-13.
Previews downloaded at 1080p: `https://assets.mixkit.co/video-templates/<id>/mixkit-<id>-1080.mp4`.

| id | name | size | fps | frames |
| --- | --- | --- | --- | --- |
| 543 | Message Interface | 1080x1920 | 59.94 | 419 |
| 544 | Bold Text Titles | 1920x1080 | 59.94 | 219 |
| 571 | Swipe Corporate Logo | 1920x1080 | 59.94 | 482 |
| 713 | Shape Motion Intro | 1920x1080 | 59.94 | 901 |
| 800 | Kinetic Opener Video | 1920x1080 | 59.94 | 239 |

Contact sheets: `ffmpeg -i mkN.mp4 -vf "drawtext=text='%{n}':…,select='not(mod(n\,STEP))',scale=W:-1,tile=CxR"` (frame number burned in before `select`, so it is the source frame).
Gold check: `python tools/gold_compare.py $PWD/gold/mk571.html mk571.mp4 out 0 480 4` (Playwright 1.56 matches the preinstalled Chromium 1194 here).
