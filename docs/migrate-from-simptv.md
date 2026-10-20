# Moving a SimpTV to Tubeless on Trixie

For a TV that already runs the original Simpsons TV scripts (or an earlier Tubeless) on Raspberry Pi OS Bullseye. You flash a
fresh card, install Tubeless on it, and copy your videos back. The old card stays untouched until the new one works, so you
can swap it back at any point.

Nothing is converted. Your folders of videos work as they are.

## What to save first

| What | Where it is on the old card | What to do |
|---|---|---|
| Videos | `~/videos/<channel>/` | Copy them off (see below). This is the slow part. |
| TV name and boot greeting | `~/.config/tubeless/config.json` (`name`, `boot_greeting`) | Note the two values. |
| Your own static clips | wherever `static_short` and `static_long` point (`python3 -m tubeless config show`) | Copy the two files off if you want to keep them. Tubeless ships a generated static if you don't. |
| Touch calibration | `~/.local/share/tubeless/settings.json` | Leave it. The new card asks you to calibrate again, which takes a minute. |
| Watch progress and shuffle order | `~/.local/share/tubeless/` | Leave it. Each channel starts fresh. |

## Time

Copying is the long part. Rough numbers for 42 GB (about 760 files):

* Over Wi-Fi with `rsync`: the Zero 2 W manages about 1.5 MB/s, so around 8 hours. Start it in the evening.
* With the card in a reader: 20 to 30 minutes each way, but a Mac can't read the card's ext4 partition without extra software.
  A Linux machine can.

The install itself (step 3) takes 30 to 45 minutes on a Zero 2 W, nearly all of it downloading and unpacking packages, and the screen stays
dark while it runs.

`rsync` can stop and start again without losing what it already copied. Use it unless you have a Linux machine and a reader.

## 1. Copy the videos off

On your computer, with the old TV running and on the network:

```
rsync -a --info=progress2 simpsons@<old-tv-address>:videos/ ~/simptv-videos/
```

The trailing slashes matter. Run it again afterwards; the second run should finish in seconds and copy nothing, which tells you the
first one completed. Keep the old card in a drawer from here on.

## 2. Flash the new card

Follow step 1 of [Install on a fresh Pi](../README.md#install-on-a-fresh-pi): Raspberry Pi OS Lite (32-bit, Trixie), your Wi-Fi and
Wi-Fi country, SSH on with your public key. Use the same user name as before if you like (the old TV used `simpsons`); any name works.

Two things to expect when you connect:

* **A host key warning.** A new card has new SSH host keys, so if it has the same address as the old TV your computer says
  `REMOTE HOST IDENTIFICATION HAS CHANGED`. That is right after a reflash. Remove the old entry and connect again:
  `ssh-keygen -R <address>`, then `ssh <user>@<address>`.
* **`sudo` asks for a password.** Recent Imager images do. Tubeless can't type it for you, so type it yourself when the installer asks.
  The installer then adds a narrow rule (power off, reboot, restart Tubeless) so later updates need no password.

## 3. Install Tubeless

Put the new card in, power the TV on and SSH in. The screen stays dark until the installer has set up the display.

```
curl -fsSL https://raw.githubusercontent.com/magicows/tubeless/main/get.sh | sudo sh -s -- --name "SimpTV" --with-youtube
```

(If you'd rather try it first, add `--dry-run` at the end: it prints what it would change and changes nothing.)

`get.sh` downloads the latest release, checks its SHA-256 and runs the installer. It tests the SD card, installs the packages (mpv,
pigpio, mediainfo, ffmpeg for YouTube), sets up the display and touch, installs the services and, with `--with-youtube`, yt-dlp. This is
the slow part: 30 to 45 minutes, mostly `apt`. It looks idle while packages unpack; that is normal.

Then reboot:

```
sudo reboot
```

After the reboot the screen shows the boot sequence, then touch calibration (tap the five targets; it only runs on a card with no videos
yet, and a long press skips it), then the "No videos yet" screen. Calibration can be done again any time from Settings.

Set the greeting back (run these from `~/tubeless`, where the installer put the code):

```
cd ~/tubeless
python3 -m tubeless config set boot_greeting "Wake up, Homer..." --restart
```

If you saved your own static clips, copy them to the TV and point Tubeless at them:

```
python3 -m tubeless config set static_short ~/smallStatic.mp4
python3 -m tubeless config set static_long ~/longerStatic.mp4 --restart
```

## 4. Test with a few videos

Before the long copy, check that playback works. Copy one short video into a channel folder from your computer:

```
rsync -a ~/simptv-videos/<one-channel>/<one-file> simpsons@<new-tv-address>:videos/<one-channel>/
```

Tap **Check again** on the TV. It should start playing. Swipe to try next episode, channel change, hold for the menu.
[manual-test-plan.md](manual-test-plan.md) has a longer checklist.

## 5. Copy everything back

```
rsync -a --info=progress2 ~/simptv-videos/ simpsons@<new-tv-address>:videos/
```

The TV can keep playing while this runs, but the Wi-Fi is busy, so expect it to be slow. Run the command a second time at the end.
New files appear in the right channel after the next restart, or when you switch channel.

You can also use the TV's upload page for a few files at a time (Settings > Upload videos).

## 6. Check it

* The channel list in the menu matches your folders.
* A video from each channel plays, with static in front of it.
* Brightness, shuffle and the sleep timer work from Settings.
* Pull the plug while a video plays, power it back on, and check it returns to the same episode.
* If you added YouTube channels, add one video from a playlist on the phone page and let it download.

## If something goes wrong

* **The install seems stuck.** Packages can take a long time, but if the output has not moved for 15 minutes, check from a second SSH
  session whether `dpkg` is still using the CPU and writing (`ps aux | grep dpkg`; `ls -l` the newest `*.dpkg-new` file under `/usr/lib` twice a
  minute apart). On one rebuild `dpkg` froze on the biggest package (`libllvm19`) at 100% CPU with no disk activity for 47 minutes. Press Ctrl-C in the
  installer session, then run:
  `sudo dpkg --configure -a && sudo apt-get -f install -y` and then the `get.sh` line again. The installer is safe to run again.
* **A tap on "Upload videos" just after power-on shows Wi-Fi setup, or nothing.** The saved Wi-Fi takes up to half a minute to connect after
  boot. Upload now waits for it for 20 seconds; if you still land on the setup screen, wait a moment and try again.
* **YouTube channels:** `--with-youtube` installs the current yt-dlp (it needs Python 3.10 or newer, which Trixie has; the old Bullseye card
  does not). Keep it current from the upload page or with `python3 -m tubeless youtube update`.

## Going back

Power off, swap the old card back in and power on. It boots as before. The new card keeps its videos and settings, so you can try
again later.

When the new card has run for a week without trouble, the old one is free to reuse.

## Updating afterwards

Open the upload page (Settings > Upload videos), go to the TV tab and use **Check for updates**. It installs a new release and restarts the TV;
**Go back** undoes it. Updating from your computer with `tools/deploy.sh <ssh-host>` also works.
