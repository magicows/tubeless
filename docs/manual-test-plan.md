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

## 2. The menu

1. **Hold** (about a second, finger still): the menu appears *while you are still holding*. The title is the TV's name.
2. Tap **Brightness** repeatedly: 25 -> 50 -> 75 -> 100%, the screen really changing.
3. Tap **Settings**: the settings page. Tap **Back**: the main page.
4. **Swipe up/down** on the menu: the list scrolls. A tap never scrolls it.
5. Tap a **channel** row: the long static, then that channel.
6. Tap **outside** the menu panel: it closes. Leave it alone: it hides itself after about 5 seconds.

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
2. **Settings -> Sleep timer 15 min** and walk away: after 15 minutes the screen turns off and the video pauses. A touch wakes it
   and it carries on, again without that touch doing anything else.

## 7. Safe shutdown

1. **Settings -> scroll down -> Shut down**: the row changes to "Tap again to confirm". Wait five seconds: it reverts.
2. Tap Shut down twice: a "SYSTEM HALT" screen, then the Pi powers down. (If a battery keeps it powered, cut power by hand once the
   screen is dark.)
3. Power it back on: boots normally.

## 8. Boot

1. Power-cycle. The screen stays dark for about nine seconds (the display driver is not up yet), then the green rain and status
   panel appear: the typed greeting, lines turning to OK, a progress bar, then static and your episode.
2. Roughly how long from power to picture? (It was 19 seconds to the service being ready when last measured.)

## 9. Renaming

```
ssh <tv> 'cd ~/tubeless && python3 -m tubeless config set name "Den" --restart'
```

The menu title and the boot screen change. A name longer than 16 characters is refused. Set it back the same way.

## What to tell us

* What you did, and roughly what time (the log has times).
* What you expected, and what happened.
* Anything that felt slow, laggy or hit the wrong place.
