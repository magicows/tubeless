# Manual test plan

What to try on a real TV, per feature. Each flow says what you do and what you should see. If something differs, note
roughly *when* it happened: the app logs every gesture, menu choice and episode change with timestamps, so we can find it.

Watch the log live while you test:

```
ssh <tv> 'tail -f ~/.local/share/tubeless/tubeless.log'
```

## 1. Everyday playback

1. **Tap** the picture: it pauses. Tap again: it resumes.
2. **Swipe right-to-left**: a short burst of static, then the next episode.
3. **Swipe left-to-right**: static, then the *previous* episode. Do it five times in a row, including right after changing
   channel: it must change the episode every time, never replay the same one.
4. **Swipe up / down**: the longer channel-change static, then the next / previous channel. A channel with no videos in it
   is skipped.
5. Every video, however you got to it, should have the static in front of it.
6. Set **Between videos** to Off on the page's TV static card (and leave upload mode): one episode now cuts straight into the next with
   nothing between, swiping to skip still goes to the next / previous episode, and changing channel still shows its static. With ads on, the ad
   plays alone between two episodes (no static) and cuts straight into the next episode; with static on, you get episode, static, ad, episode.
   Set **When you change channel** to Off instead: a swipe up/down starts the new channel at once. Try other lengths for each (they work with
   the built-in static too, and the default 0.6 and 3 seconds play the original clips).

## 2. The menu

1. **Hold** (about a second, finger still): the menu appears *while you are still holding*. The title is the TV's name.
2. Tap **Brightness** repeatedly: 25 -> 50 -> 75 -> 100%, the screen really changing.
3. Tap **Settings**: the settings page. Tap **Back**: the main page.
4. **Swipe up/down** on the menu: the list scrolls. A tap never scrolls it.
5. Tap a **channel** row: the long static, then that channel.
6. Tap **outside** the menu panel: it closes. Leave it alone: it hides itself after about 5 seconds.
7. The header: the TV's name, a small gap, then the channel name (long names shrink before they are cut), and three icon buttons at the right (guide, upload, power).
   Tap the **upload** icon once: it fills in and the footer says to tap again; tap again: upload mode starts. Tap the **power** icon the same way
   (the second tap shuts down: do this one last). Tapping one then the other leaves only the second armed. Wait 4 seconds: the first tap is forgotten.
8. The guide: tap the **guide** icon (it fills in). Every channel is listed with its number, its name, and a line under it: what would play next
   and how long is left of it (or, on a live channel, what is on air now); a channel not watched yet says how many videos it has. The channel you
   are on is marked and in view. Swipe to scroll, tap a row to tune to it. Open the menu again: it opens on the guide. Tap the icon again: the usual menu.

## 3. Shuffle that doesn't repeat

1. Flip through about ten episodes (swipe right-to-left). Note any you see twice; there should be none until a channel has been
   played right through. (Afterwards the log's `now playing` lines show the exact order.)
2. **Settings -> Shuffle OFF**: the current episode keeps playing; skipping now goes in order. Turn it **ON** again.
3. **Settings -> Reshuffle channel**: a new order starts.

## 4. Resume

1. Pause partway into an episode (the position is saved the moment you pause).
2. **Settings -> Shut down** (see flow 7), then power it back on: the same episode, a few seconds before where you paused.
3. Play for a minute without pausing, then pull the power. It should come back to within about 30 seconds of where you were.
4. Change channel and back: each channel remembers its own episode and position.

## 5. Touch calibration

1. **Settings -> Calibrate touch**. Tap each of the five crosshairs. It ends with "TOUCH CALIBRATED" and an error in pixels (a few is good).
2. Afterwards, taps on menu rows should still land on the row you meant.
3. Start it again and **hold** instead of tapping: it cancels and nothing changes.
4. Start it again and tap in the *wrong* places on purpose: "Taps were too far off - try again", and it starts over.

## 6. Dim, sleep and wake

1. **Settings -> Auto-dim 2 min**, leave it alone: after about two minutes the screen dims. **Touch it**: it brightens and
   *nothing else happens* (no pause, no skip). Set it back to Off if you like.
2. **Pause** the video and leave it alone: after a minute the screen dims. **Touch it**: it brightens and stays paused.
3. **Settings -> Sleep timer 15 min** and walk away: after 15 minutes the screen turns off and the video pauses. A touch wakes it
   and it carries on, again without that touch doing anything else.

## 7. Safe shutdown

1. **Settings -> scroll down -> Shut down**: the row changes to "Tap again to confirm". Wait five seconds: it reverts. (The power icon in the
   menu header works the same way.)
2. Tap Shut down twice: a "SYSTEM HALT" screen, then the Pi powers down. (If a battery keeps it powered, cut power by hand once the
   screen is dark.)
3. Power it back on: boots normally.

## 8. Boot

1. Power-cycle. The screen stays dark for about nine seconds (the display driver is not up yet), then the green rain and status
   panel appear: the typed greeting, lines turning to OK, a progress bar, then static and your episode.
2. Roughly how long from power to picture? (It was 19 seconds to the service being ready when last measured.)

## 9. Renaming and the wake-up message

On the upload page's TV tab: rename the TV and change the wake-up message; the menu title changes at once and the boot screen at the next
start. A name over 16 characters or a message over 40 is refused; a blank message puts the standard one back. (From a shell:
`python3 -m tubeless config set name "Den" --restart`.)

## 10. The web page

Open it from Settings > Upload videos (or the menu's upload icon) and enter the PIN.

1. **Channels:** drag the rows into a new order with the up/down arrows icon: the numbers update, and swiping on the TV follows the new order,
   staying on the channel it was on. The shuffle icon on a row opens its play order menu.
2. **Themes** (TV tab): choose each one; the page and the TV's menu change. Add a palette from hex codes or a coolors.co link. The boot screen keeps its rain.
3. **Dim at night** (TV tab): tick it, press *Use my time zone's city*, check today's sunrise and sunset. To see it work, choose *At set times* with a
   night that began a few minutes ago: the screen fades to the night level within about half a minute, and back at the end time.
4. **Live channels** (Channels tab): in a channel's order menu choose *Make this channel live*. The TV tab lists it as live once its video lengths
   are known; tune to it, switch away for a few minutes, and come back: it has moved on. A channel you left alone is not live and still resumes
   where you left off. *Reshuffle* (on a live channel's order menu) gives it a new running order. Turn it off again from the same menu.
5. **yt-dlp** (TV tab): the automatic update checkbox is ticked by default.
6. **Ads between videos** (TV tab): off by default. Tick it with an empty `~/ads` folder: the page warns that nothing will play. Add a clip
   (Upload tab, put it in *Ads*; or paste a YouTube link in the ads box): the warning goes. Set *one ad every* 1 video and let a video end: the
   static runs, then the ad, then the next video, with no stall or glitch (try clips of a different size and codec from the videos). A change
   of channel and a swipe to skip play no ad. Untick it: no more ads. On a live channel, the ad sits in the gap and the channel runs a
   little behind until you tune in again.

## 11. Jellyfin

See [jellyfin.md](jellyfin.md). Sign in on the Channels tab, press *Test stream* on a movie (about 15 seconds; it says whether the stream keeps up with playing, and if not, whether the server or the connection is slow), add two movies to a new channel, play it on the TV. Open the channel on the Channels tab and press *Remove channel*: after you confirm, it is gone from the list and the TV (and the TV stays on the channel it was on).
Switch the Wi-Fi off while it plays: the TV says why it can't play and moves on. Within about 20 seconds the Jellyfin channel (and any YouTube
channel with nothing downloaded) is left out: swipe past it, and the menu marks it "No Wi-Fi" (the guide says "Needs Wi-Fi"). A channel with some
downloaded videos plays only those. Switch the Wi-Fi back on: they return.

## 12. Starting with no network

Power-cycle with the router off, on a channel that streams: the TV shows "Waiting for Wi-Fi", then starts anyway after about a minute with the
usual "couldn't play" card, unless another channel has downloaded videos: it then starts on that one instead. With the router on, it
waits only until the Wi-Fi is up.

## What to tell us

* What you did, and roughly what time (the log has times).
* What you expected, and what happened.
* Anything that felt slow, laggy or hit the wrong place.
