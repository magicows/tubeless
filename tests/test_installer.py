from pathlib import Path

from tubeless import installer

TRIXIE_CONFIG = """# For more options and information see
# http://rptl.io/configtxt
dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
disable_overscan=1
arm_boost=1
[cm4]
otg_mode=1
[cm5]
dtoverlay=dwc2,dr_mode=host
[pi5]
dtoverlay=nospi10
[all]
"""

# the config.txt of the TV that works today (already has everything)
WORKING_TV_CONFIG = """dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
max_framebuffers=2
disable_overscan=1
[cm4]
otg_mode=1
[pi4]
arm_boost=1
[all]
dtoverlay=vc4-kms-v3d
dtoverlay=waveshare-28dpi-3b-4b
dtoverlay=waveshare-28dpi-3b
dtoverlay=waveshare-28dpi-4b
dtoverlay=waveshare-touch-28dpi
dtoverlay=vc4-kms-dpi-2inch8
dtparam=audio=on
dtoverlay=audremap,enable_jack,pins_18_19
"""

TRIXIE_CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=ff65591c-02 rootfstype=ext4 fsck.repair=yes rootwait "
                  "ds=nocloud;i=rpi-imager-1791738139168 cfg80211.ieee80211_regdom=GB\n")
WORKING_TV_CMDLINE = ("console=serial0,115200 video=DPI-1:480x640@60,rotate=90 console=tty3 root=PARTUUID=194375f9-02 "
                      "rootfstype=ext4 rootwait cfg80211.ieee80211_regdom=GB consoleblank=0 logo.nologo quiet splash "
                      "vt.global_cursor_default=0\n")


def test_config_adds_what_is_missing_in_the_all_section_and_keeps_everything_else():
    out = installer.apply_config_txt(TRIXIE_CONFIG)
    assert out.startswith(TRIXIE_CONFIG.rstrip("[all]\n")[:40])
    for line in TRIXIE_CONFIG.splitlines():
        assert line in out.splitlines()                          # nothing of the original was lost
    block = out.split(installer.BEGIN)[1].split(installer.END)[0].split()
    assert "dtoverlay=vc4-kms-dpi-2inch8" in block and "dtoverlay=audremap,enable_jack,pins_18_19" in block
    assert not any("sdio" in line for line in block)                                      # that overlay takes the display's GPIOs
    assert "dtoverlay=vc4-kms-v3d" not in block and "dtparam=audio=on" not in block      # already there: not repeated
    assert out.index(installer.BEGIN) > out.rindex("[all]")


def test_config_is_idempotent():
    once = installer.apply_config_txt(TRIXIE_CONFIG)
    assert installer.apply_config_txt(once) == once


def test_config_opens_all_if_the_file_ends_in_another_section():
    text = TRIXIE_CONFIG.replace("[all]\n", "[pi5]\ndtoverlay=nospi10\n")
    out = installer.apply_config_txt(text).splitlines()
    assert out[out.index(installer.BEGIN) - 1] == "[all]"


def test_a_config_that_already_has_everything_is_left_alone():
    assert installer.apply_config_txt(WORKING_TV_CONFIG) == WORKING_TV_CONFIG
    assert installer.apply_cmdline(WORKING_TV_CMDLINE).split() == WORKING_TV_CMDLINE.split()


def test_cmdline_moves_the_console_and_adds_tokens_as_one_line():
    out = installer.apply_cmdline(TRIXIE_CMDLINE)
    assert out.count("\n") == 1 and out.endswith("\n")
    tokens = out.split()
    assert "console=tty3" in tokens and "console=tty1" not in tokens
    assert "console=serial0,115200" in tokens and "ds=nocloud;i=rpi-imager-1791738139168" in tokens   # untouched
    for t in installer.CMDLINE_TOKENS:
        assert tokens.count(t) == 1
    assert installer.apply_cmdline(out) == out


def test_cmdline_replaces_a_different_video_setting():
    out = installer.apply_cmdline("console=tty1 video=DPI-1:800x600@60 root=/dev/sda2\n").split()
    assert out.count(installer.VIDEO_TOKEN) == 1 and not any("800x600" in t for t in out)


def test_cli_dry_run_changes_nothing_and_apply_backs_up_once(tmp_path, capsys):
    boot = tmp_path / "firmware"
    (boot / "overlays").mkdir(parents=True)
    (boot / "config.txt").write_text(TRIXIE_CONFIG)
    (boot / "cmdline.txt").write_text(TRIXIE_CMDLINE)
    src = tmp_path / "dtbo"
    src.mkdir()
    (src / "vc4-kms-dpi-2inch8.dtbo").write_bytes(b"overlay")
    (src / "._vc4-kms-dpi-2inch8.dtbo").write_bytes(b"junk")

    assert installer.main(["boot", "--boot-dir", str(boot), "--overlays-from", str(src)]) == 0
    assert (boot / "config.txt").read_text() == TRIXIE_CONFIG and not (boot / "overlays" / "vc4-kms-dpi-2inch8.dtbo").exists()
    assert "dry run" in capsys.readouterr().out

    assert installer.main(["boot", "--boot-dir", str(boot), "--overlays-from", str(src), "--apply"]) == 0
    assert (boot / "overlays" / "vc4-kms-dpi-2inch8.dtbo").read_bytes() == b"overlay"
    assert not (boot / "overlays" / "._vc4-kms-dpi-2inch8.dtbo").exists()
    assert (boot / "config.txt.before-tubeless").read_text() == TRIXIE_CONFIG
    assert (boot / "cmdline.txt.before-tubeless").read_text() == TRIXIE_CMDLINE
    changed = (boot / "config.txt").read_text()

    assert installer.main(["boot", "--boot-dir", str(boot), "--overlays-from", str(src), "--apply"]) == 0   # again: no-op
    assert (boot / "config.txt").read_text() == changed
    assert (boot / "config.txt.before-tubeless").read_text() == TRIXIE_CONFIG                    # the original backup survives
    assert "already have everything" in capsys.readouterr().out


def test_the_sdio_overlay_is_never_written_and_is_removed_from_a_block_that_has_it():
    """`dtoverlay=sdio` claims GPIOs 22-27, which carry this display's picture: a TV with it in config.txt does not start."""
    assert not any("sdio" in line for line in installer.CONFIG_LINES)
    once = installer.apply_config_txt(TRIXIE_CONFIG)
    broken = once.replace(installer.END, "dtoverlay=sdio,sdio_overclock=25\n" + installer.END)           # what an earlier version wrote
    assert "dtoverlay=sdio" in broken
    fixed = installer.apply_config_txt(broken)
    assert "sdio" not in fixed and fixed == once                                          # re-running the installer repairs it
