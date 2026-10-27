# Troubleshooting a new Pi

Things that went wrong while building Tubeless on fresh Raspberry Pi OS cards, and what fixed them. The installer
([README](../README.md#install-on-a-fresh-pi)) already guards against most of these; this is for when it stops and you want to know why.

## The SD card

* **A failing or fake card.** Signs: `Unable to fetch`, "compressed data is corrupt" or "unexpected end of file" during `apt install`, and
  `sdhost ... timeout waiting for hardware interrupt` in `dmesg`, while the power (`vcgencmd get_throttled` = `0x0`) and RAM are fine. A
  write-then-read-back test returned different data every time; the Imager's own verify step does not catch this. The installer runs that
  read-back test first and stops if the card fails. Re-seat the card, and if it persists use a genuine card from a reputable seller
  (`f3` can test a card for fake capacity).
* **Recovering after a flaky card.** Once the card reads back correctly, the damage already done still has to be undone:
  1. Make the next boot run a full filesystem check: add `fsck.mode=force` to the end of `/boot/firmware/cmdline.txt` (on this OS the check
     happens in the early-boot stage: the flickering activity light). Then **remove the flag**, or every boot repeats it.
  2. `sudo apt-get install debsums && sudo debsums -s` lists every installed file whose checksum does not match its package.
  3. Reinstall exactly those packages: `sudo apt-get install --reinstall --no-install-recommends <packages>`. If apt refuses one that is
     half-configured (`Internal Error, No file name for ...`), fetch it with `apt-get download <pkg>` and install it with `sudo dpkg -i`.
  4. Re-run `sudo debsums -s` and `sudo dpkg --audit` until both are quiet.

  If the original system is damaged too, re-flash instead. It is quicker than chasing it.

## Installing

* **`sudo` asks for a password.** Images made by recent Imager versions do. Tubeless can't type it for you; type it when the installer asks.
  The installer then adds a narrow rule (power off, reboot, restart Tubeless, and the Wi-Fi helper) so later updates need none.
* **The install seems stuck.** Packages take a long time, but if the output has not moved for 15 minutes, look from a second SSH session
  at whether `dpkg` is still using the CPU and writing (`ps aux | grep dpkg`). On one rebuild `dpkg` froze on the biggest package
  (`libllvm19`) for 47 minutes. Ctrl-C the installer, run `sudo dpkg --configure -a && sudo apt-get -f install -y`, then run the installer
  again: it is safe to repeat. (The installer also says so itself when a command goes quiet.)
* **An unreachable package mirror.** `raspbian.raspberrypi.com` redirects downloads to a mirror; if that one can't be reached you get
  `Unable to connect to mirror0.it.ox.ac.uk`. Point `URIs:` in `/etc/apt/sources.list.d/raspbian.sources` at another mirror (for example
  `http://archive.raspbian.org/raspbian/`); signatures are still verified.
* **Installing `mpv` pulls in a lot by default** (about 280 packages, including X11 tools and speech models) and ran a 512 MB Pi out of
  memory. The installer uses `--no-install-recommends` (about 180 packages, still large: mpv's video libraries).
* **The screen stays dark during the install.** Expected: the display driver isn't set up until the install has run. The terminal prints
  an address and PIN for a progress page you can open on a phone.

## A TV that won't boot or shows nothing

* **Do not use `dtoverlay=sdio,...`** to slow the Wi-Fi bus. It claims GPIO pins 22 to 27, which drive the display on this build, and the TV
  stops booting. The correct line is the plain `dtparam=sdio_overclock=25` (see the README's Wi-Fi chip section). If it has happened, put the
  card in a computer, remove the line from `config.txt` on the boot partition, and put it back.
* **The first-run touch wizard doesn't appear** on a card with no videos: fixed in 0.1.x; update.

## Wi-Fi

* **The picture freezes, then the Wi-Fi is gone until a restart**, with `brcmfmac` bus errors in `journalctl -k`: the Zero 2 W's Wi-Fi
  chip locking up. See the README's "Wi-Fi chip and the slower bus clock" for the clock setting and for what Tubeless does about it by itself.
* **A saved Wi-Fi takes up to half a minute to connect after boot.** Upload mode waits for it; a streaming channel waits up to a minute
  before it starts ("Waiting for Wi-Fi").

## Reading the log

```
ssh <tv> 'tail -f ~/.local/share/tubeless/tubeless.log'
```

It records every gesture, menu choice and episode change with times, a health note once a minute (power flags, temperature, load, free
memory, Wi-Fi signal), and why a YouTube or Jellyfin video was skipped. mpv's own log is `mpv.log` beside it. The system log survives
restarts (`journalctl -b -1` is the previous boot).
