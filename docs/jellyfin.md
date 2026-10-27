# Jellyfin channels

A channel whose videos come from your own Jellyfin server instead of a folder or YouTube. Everything after the source (static between
videos, shuffle or in order, resume, sleep timer, Live TV mode) works as for any channel. Channels are made on the web page, never on the TV.

## Using it

1. On the upload page's Channels tab, find the **Jellyfin** card. Enter the server address (`jelly.example.com`, or a home address such as
   `http://192.168.1.20:8096`) and sign in with a user name and password, or press *Use Quick Connect instead* and approve the code it shows in
   Jellyfin (Settings, then Quick Connect, on a device that is already signed in).
2. Search or browse. Tick shows, movies, collections or playlists. A show adds all its episodes; press *Episodes* to tick single ones. *Test
   stream* asks the server for the start of a video and says what came back.
3. Choose *A new channel...* (and name it, for example `jellymovies`) or one of your existing Jellyfin channels, and press **Add**. Nothing
   becomes a channel by itself.
4. A Jellyfin channel shows in *Your channels* with a Jellyfin badge; tap it to see its videos and remove any. A channel holds only Jellyfin
   videos (no mixing with YouTube or files, for now).

Only a token is kept on the TV (`jellyfin.json`, readable by the TV's user only), never your password. Revoke it any time from Jellyfin's dashboard
(it appears as a device called *Tubeless TV*), or press *Disconnect* on the page.

## How it works

* **Signing in.** `POST /Users/AuthenticateByName`, or Quick Connect (`/QuickConnect/Initiate`, poll `/QuickConnect/Connect`, then
  `/Users/AuthenticateWithQuickConnect`). Every request carries `Authorization: MediaBrowser Client="Tubeless", Device="Tubeless TV",
  DeviceId="...", Version="...", Token="..."`. Jellyfin 12 only takes that header (older servers also took `X-Emby-Authorization`).
* **Browsing.** `/UserViews`, `/Items` (search, parent, types) and `/Shows/{id}/Episodes`. A channel stores the chosen items (id, title, length,
  kind, series) in `jellyfin-channel.json` inside its folder, so nothing needs the server until a video plays.
* **Playing.** Each video is an entry `jf-<item id>` in the playlist, turned into an address when the playlist is written:
  `/Videos/{id}/stream.mp4?videoCodec=h264&audioCodec=aac&maxHeight=...&videoBitRate=...&api_key=<token>`. The server transcodes to the quality
  set for YouTube (360p by default), so the Pi only decodes a small picture. mpv plays it as it is: the yt-dlp hook is told to leave
  `/Videos/.../stream` alone, and mpv names itself `Tubeless/<version>`.
* **If it fails.** The TV says why on screen ("Jellyfin isn't connected: sign in on the upload page", "Your Jellyfin server wouldn't play it")
  and moves on. A streaming channel also waits for the network after a start.

## Limits and things to know

* **No mid-video resume.** Jellyfin 12 has no HLS playlist endpoint, so a video is one progressive transcode, which can't be sought into: a Jellyfin
  video always starts from the beginning, and in Live TV mode a Jellyfin channel joins the right video but not the right minute. (`startTimeTicks`
  in the address is the way to add it.)
* **The token is a password.** It is in `jellyfin.json` and in the playlist file while a Jellyfin channel plays (mode 0600), and in stream addresses
  (masked when logged). A plain `http://` server address sends it unencrypted across the home network.
* **Proxies.** A proxy such as Cloudflare refuses Python's default user agent (a 403), so the TV names itself `Tubeless/<version>`. A long stream
  through a free proxy may still be limited; using the server's home-network address avoids that.
* **Transcoding load is on the server.** A small NAS may struggle; a file that is already small H.264 can be sent as it is.
* **Buffering?** Press *Test stream* on the video that buffers. It listens to the stream for about 15 seconds and says whether it arrives faster
  than it plays, and if not, whether the server's transcoding or the connection is the limit (it compares the stream with Jellyfin's own plain
  speed test). A slow transcode usually means a large, HEVC or 10-bit video on a small server; converting that video once to a smaller 8-bit
  H.264 MP4 fixes it. Jellyfin's dashboard (Activity, and the FFmpeg log under Logs, where `speed=` below 1.0x means it cannot keep up) shows the same.
* The code is tested against a pretend server (`tests/jf_fake.py`) and was checked by hand against a Jellyfin 12.4 server.
