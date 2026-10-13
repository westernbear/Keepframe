# Direct reads by the agent (contact sheets, 4-frame steps around events) — kept unedited; corrections from measurement are at the end

## mk544 "Bold Text Titles" 1920x1080 59.94fps 219f
A f0–31  dark radial bg. label "The best for" (bold sans, white, top-left) + dash.
         stacked heavy-uppercase lines "THE BEST FOR / CREATIVES / THE BEST / FOR CREATIVES …" scroll UP continuously (≈1 line / 5f);
         letters pop in per-glyph at the bottom line.
  f32–43 inverse flash: bg white/grey radial, text black, glyphs switch OFF in random order.
B f44–87 dark bg. "CREATIVES" centred heavy white; glyphs flicker ON in random order (f44 "CR T S", f52 full, f56 gap again, f60 full).
         small tracked label "THE BEST FOR" above-left, word by word (THE f48, BEST f52, FOR f60). Hold f60–84.
T f84–127 blue (#2468f4) rectangles enter in staggered columns, full blue f96–104, white rectangles enter f108–124, then blocks clear.
C f120–151 black bg, giant blue "Mixkit" (only "X"/"ixki" visible), vertical small white "THE BEST" text at sides, "BE/ST" centre;
         one scale-down from ≈20× toward 1×.
D f152–218 "Mixkit" blue heavy centred, undershoot then settle (f152 ≈0.6×, f160 1×). two small white labels "THE BEST / FOR CREATIVES"
         bottom-left and bottom-right. Hold.

## mk571 "Swipe Corporate Logo" 1920x1080 59.94fps 482f
f0–43 black. f44–56 teal (#58c8a0) wipes in left→right (black bars between = staggered vertical bars).
f56–72 dot at centre grows into filled dark-teal (#12708f) square; f76–88 becomes outline square (stroke thins while size grows).
f92–104 logo icon (white hex "b" mark) scales up from 0 inside the square, ease-out.
f112–128 small url "www.yourwebsite.com" fades in below.
f140–172 box widens to the right (slow then fast, ≈3.5× width), icon stays left-aligned.
f172–184 "Your" then "Business" slide up through a mask; f188–200 "logo" slides up (word stagger ≈8f). Hold f200–481.

## mk543 "Message Interface" 1080x1920 59.94fps 419f
Static chat UI: header (back, "CS" avatar, "Customer Support / Active", call/video/info icons), footer (apps, mic, "Aa" field, blue dot).
9 messages alternate agent (left, grey bubble + CS avatar) / user (right, blue bubble), each with a centred timestamp "JUN 27 AT 2:5x PM".
Cadence ≈42f (0.7 s). Each bubble: empty pill grows, then text fades in. List scrolls up to keep the newest at the bottom.
Texts: "Hello Jim! Thank you for reaching out to customer support. Please describe the problems you have encountered." /
"I am trying to find video files to complete my design project." / "Would you like to receive new videos weekly?" /
"Weekly? Yes! Thank you." / "Can I have your email address, please?" / "your_email@gmail.com" /
"You are successful unlocked your first 35 videos" / "Thank you. Excellent!" / "Welcome to the Mixkit Crew!"

## mk800 "Kinetic Opener" 1920x1080 59.94fps 239f
f0–95 black; "WELCOME" white condensed caps; glyphs grow in left→right (each scales from small, staggered ≈6f), settle f56; slight drift.
f88–100 zoom-blur push into the text ("ELCOI" huge, blurred) = transition. f100+ LIVE-ACTION footage (nightclub) → out of scope.

## mk713 "Shape Motion Intro" 1920x1080 59.94fps 901f — ~8 shots
hex dots on black → spinning hexagon outlines on white dashed bg → purple "mixkit presents" (giant faint letters bg) →
split white/pink panels: purple circle + hexagon badge "FALL"→"2020", pink "hexagon" + purple X →
orange "OPENER template" with hexagon outlines bg (slide transitions) → "FREE download" over purple/orange/pink + hexagon →
orange "exclusively on mixkit.co" over giant letters bg.

# Corrections after CV measurement (what my eye got wrong)

mk571 (measured every 2–4 frames: teal extent per row, dark-teal frame bbox and stroke, white ink per word box)
- WRONG direction: teal does not wipe left→right; it opens from the centre outward (slit f14–f44, snap f44–f90).
- WRONG direction: the frame does not widen to the right; it widens symmetrically about x=959.5 (241→1076 px, f118–f214, expo.inOut),
  height 241→312; the mark slides left with it (−372 px, same ease). Then the frame settles to 1012×298 (f250–f290) — I missed this.
- WRONG motion type: the url does not fade; it drifts DOWN 24 px into place (f112–f190, expo.out).
- Right but imprecise: "stroke thins while size grows" — measured 39→21 px (f76–f100). Words: "Your Business" f175–f183, "logo" f183–f240 (power4.out).

mk544 (measured per 2 frames: bg luminance, blue/white coverage and boxes)
- Inverse flash is f30–f43 (not f32).
- "CREATIVES" (phase B) slides UP ≈170 px while its glyphs flicker on (f44–f56) — it continues the scroller; I read it as static.
- Transition: blue blocks f82–f98 → white blocks f102–f120 → blue again f122–f140 → black + zoomed blue letters f142–f148.
- The "Mixkit" zoom is a STEP sequence, not a tween: ≥2× (f142–148) → 0.56× hold (f150–154) → 1× (f155+). Labels "THE BEST" f150, "FOR CREATIVES" f159.

mk543 (from the pipeline's measured scroll, every line moves up ≈166 px at 0.77 / 1.57 / 2.34 / 3.10 / 3.90 s)
- Cadence is ≈47 f (0.78 s), not 42 f.
