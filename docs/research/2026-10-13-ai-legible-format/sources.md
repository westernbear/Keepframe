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

## SaaS promo search (2026-10-13)

Web search "saas promo video" (motion graphics template / after effects) → Uppbeat (429 bot checkpoint), VideoHive (403), Dribbble (no media for a headless browser), **Envato Elements** `https://elements.envato.com/video-templates/saas+promo` (200): 48 template previews, `preview_540p_crf26_lower_quality.mp4` on `public-assets.content-platform.envatousercontent.com`, 960x540, 30 fps, 14–75 s. Paid templates; previews read for research only, not committed. Duplicates (same template, other host app): e06=e20, e10=e45, e11=e47, e19=e44, e24=e48 → 43 unique.

| id | title | author | page |
| --- | --- | --- | --- |
| e01 | SaaS Product Promo | M_Yunusoglu | https://elements.envato.com/saas-product-promo-5EN4HC8 |
| e02 | SaaS Presentation / Startup Tech Promo | Rendixx | https://elements.envato.com/saas-presentation-startup-tech-promo-HMGUQ7P |
| e03 | Ai Saas Promo | peafowldesign | https://elements.envato.com/ai-saas-promo-UAFA234 |
| e04 | SaaS Website Promo | gerak_visual | https://elements.envato.com/saas-website-promo-CX45TJ5 |
| e05 | SaaS Product Promo | Elfko | https://elements.envato.com/saas-product-promo-9FLG3LJ |
| e06 | SaaS Product Promo | MotionRam | https://elements.envato.com/saas-product-promo-27STTSW |
| e07 | SaaS Product Promo | New-Motion | https://elements.envato.com/saas-product-promo-G4AP6AB |
| e08 | AI SaaS Product Promo | RusseRocket | https://elements.envato.com/ai-saas-product-promo-T7WDTR8 |
| e09 | Saas Promo Pack | 142videos | https://elements.envato.com/saas-promo-pack-EXMT22V |
| e10 | SaaS Promo | EasyFrox | https://elements.envato.com/saas-promo-LTP7JQ9 |
| e11 | SaaS Product Promo | MotionAxx | https://elements.envato.com/saas-product-promo-4MET2RP |
| e12 | SaaS Tech Product Promo | Buriak_Motion | https://elements.envato.com/saas-tech-product-promo-VSJZJBU |
| e13 | SaaS Product Promo - Ai Website Promo Teaser | lolkiy | https://elements.envato.com/saas-product-promo-ai-website-promo-teaser-77D5BKA |
| e14 | AI SaaS Promo | PrivalFX | https://elements.envato.com/ai-saas-promo-YE5UC6A |
| e15 | Saas Product Promo | Dots_design | https://elements.envato.com/saas-product-promo-98HLNCL |
| e16 | SaaS AI Promo | EasyFrox | https://elements.envato.com/saas-ai-promo-EC2SSSG |
| e17 | SaaS Product Launch Promo | Buriak_Motion | https://elements.envato.com/saas-product-launch-promo-4GSN54D |
| e18 | Modern SaaS Product Promo | Rendixx | https://elements.envato.com/modern-saas-product-promo-KVM58G3 |
| e19 | Explore Saas Promo for DaVinci Resolve | Promak | https://elements.envato.com/explore-saas-promo-for-davinci-resolve-EBNWJPW |
| e20 | Saas Product Promo - Premiere Pro | MotionRam | https://elements.envato.com/saas-product-promo-premiere-pro-89KDSHB |
| e21 | SaaS Product Promo | RusseRocket | https://elements.envato.com/saas-product-promo-NHM8BC3 |
| e22 | Ai SaaS Product Promo | lolkiy | https://elements.envato.com/ai-saas-product-promo-HJS5KXP |
| e23 | SaaS Website Promo | RusseRocket | https://elements.envato.com/saas-website-promo-HVCGY7F |
| e24 | SaaS Promo / Software & AI Product Showcase | FXDNZ | https://elements.envato.com/saas-promo-software-ai-product-showcase-YMH7MJS |
| e25 | Abstract SaaS Promo for Premiere Pro | Yura_Kobitovich | https://elements.envato.com/abstract-saas-promo-for-premiere-pro-QEFYJ2Y |
| e26 | SaaS Product Promo | Ritchard | https://elements.envato.com/saas-product-promo-DAXG4PU |
| e27 | SaaS Promo | NeoMograph | https://elements.envato.com/saas-promo-3V2ZDCB |
| e28 | SaaS Product Promo | YETYYY | https://elements.envato.com/saas-product-promo-VNQ947M |
| e29 | SaaS Website Promo | SnnDesign | https://elements.envato.com/saas-website-promo-GJQYB53 |
| e30 | SaaS Promo | YouGn | https://elements.envato.com/saas-promo-6ZFLR3B |
| e31 | SaaS Promo Intro / Tech Promo | theTrendy | https://elements.envato.com/saas-promo-intro-tech-promo-28T8E8K |
| e32 | Creative SaaS Promo | Motion-Mania | https://elements.envato.com/creative-saas-promo-4MGFAS3 |
| e33 | SaaS App Promo | ProMotionSquad-1 | https://elements.envato.com/saas-app-promo-VKV7JL9 |
| e34 | AI SaaS Promo for Premiere Pro | refinality | https://elements.envato.com/ai-saas-promo-for-premiere-pro-A7M8WX3 |
| e35 | AI Saas Promo | Makhmudovich | https://elements.envato.com/ai-saas-promo-3Y2A4C4 |
| e36 | SaaS Product Promo | hintstudioo | https://elements.envato.com/saas-product-promo-QQB7AT3 |
| e37 | SaaS Product Promo | Nick_Chvalun | https://elements.envato.com/saas-product-promo-XUNBSH6 |
| e38 | SaaS Product Promo | Motion-Bear | https://elements.envato.com/saas-product-promo-U3TYTC6 |
| e39 | SaaS App Promo X | RojinDesign | https://elements.envato.com/saas-app-promo-x-W6LF86B |
| e40 | AI SaaS Product Promo | MotionSquence | https://elements.envato.com/ai-saas-product-promo-YNWL77W |
| e41 | AI SaaS Product Promo | pitstud_motion | https://elements.envato.com/ai-saas-product-promo-ARPUETH |
| e42 | AI SaaS Product Promo | Loinstd | https://elements.envato.com/ai-saas-product-promo-4GBN9XU |
| e43 | AI Saas Promo | Yura_Kobitovich | https://elements.envato.com/ai-saas-promo-4MPK9BC |
| e44 | Explore Saas Promo | Promak | https://elements.envato.com/explore-saas-promo-GLX9LUK |
| e45 | SaaS Promo | EasyFrox | https://elements.envato.com/saas-promo-VCVXAQV |
| e46 | Ai SaaS Promo | lolkiy | https://elements.envato.com/ai-saas-promo-YUZLHGB |
| e47 | SaaS Product Promo for Premiere Pro | MotionAxx | https://elements.envato.com/saas-product-promo-for-premiere-pro-SXFT5G7 |
| e48 | SaaS Promo / Software & AI Product Showcase | FXDNZ | https://elements.envato.com/saas-promo-software-ai-product-showcase-93DZPZX |
