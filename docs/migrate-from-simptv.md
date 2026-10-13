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
Wi-Fi country, SSH on. Use the same user name as before if you like (the old TV used `simpsons`); any name works.

## 3. Install Tubeless

Put the new card in, power the TV on and SSH in. The screen may stay dark until the installer has set up the display.

```
sudo apt install -y git
git clone https://github.com/magicows/tubeless.git
cd tubeless
sudo sh install.sh --name "SimpTV" --with-youtube
sudo reboot
```

Leave out `--with-youtube` if you don't want YouTube channels. After the reboot the screen shows the boot sequence, then the
touch calibration. With no videos yet it shows the "No videos yet" screen.

Set the greeting back:

```
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

## Going back

Power off, swap the old card back in and power on. It boots as before. The new card keeps its videos and settings, so you can try
again later.

When the new card has run for a week without trouble, the old one is free to reuse.
