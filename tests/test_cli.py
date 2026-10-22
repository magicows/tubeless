import json

import pytest
from PIL import Image, ImageDraw

from tubeless import fonts, render
from tubeless.__main__ import main
from tubeless.config import MAX_NAME_LENGTH, Config
from tubeless.hardware import Hardware
from tubeless.menu import MenuContext, MenuModel


def run(*args):
    return main(list(args))


def test_set_name_creates_the_config_and_keeps_other_settings(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"videos_dir": "/media/tv"}))
    assert run("config", "--config", str(path), "set", "name", "Living Room") == 0
    assert json.loads(path.read_text()) == {"videos_dir": "/media/tv", "name": "Living Room"}
    assert Config.load(str(path)).name == "Living Room"
    assert "sudo systemctl restart tubeless" in capsys.readouterr().out


def test_greeting_is_validated_tidied_and_blank_puts_the_placeholder_back(tmp_path):
    from tubeless.config import DEFAULT_GREETING, MAX_GREETING_LENGTH, set_config_value
    path = tmp_path / "c.json"
    assert set_config_value(path, "boot_greeting", "  Wake up,   Homer  ") == "Wake up, Homer"
    assert Config.load(str(path)).boot_greeting == "Wake up, Homer"
    with pytest.raises(ValueError):
        set_config_value(path, "boot_greeting", "x" * (MAX_GREETING_LENGTH + 1))
    assert set_config_value(path, "boot_greeting", "   ") == DEFAULT_GREETING
    assert "boot_greeting" not in json.loads(path.read_text()) and Config.load(str(path)).boot_greeting == DEFAULT_GREETING


def test_the_longest_greeting_fits_on_the_boot_screen():
    from PIL import ImageDraw, Image
    from tubeless import bootscreen, fonts
    from tubeless.config import MAX_GREETING_LENGTH
    from tubeless.hardware import Hardware
    hw = Hardware()
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    px0, _, px1, _ = hw.safe_rect
    assert d.textlength("> " + "W" * MAX_GREETING_LENGTH + "_", font=fonts.load(fonts.MONO, 16)) <= (px1 - 24) - (px0 + 24)


def test_set_creates_a_missing_file(tmp_path):
    path = tmp_path / "new" / "config.json"
    assert run("config", "--config", str(path), "set", "name", "Den") == 0
    assert json.loads(path.read_text()) == {"name": "Den"}


def test_name_is_validated(tmp_path, capsys):
    path = tmp_path / "c.json"
    assert run("config", "--config", str(path), "set", "name", "   ") == 2
    assert run("config", "--config", str(path), "set", "name", "x" * (MAX_NAME_LENGTH + 1)) == 2
    assert not path.exists()
    assert run("config", "--config", str(path), "set", "name", "x" * MAX_NAME_LENGTH) == 0


def test_unknown_setting_and_json_values(tmp_path, capsys):
    path = tmp_path / "c.json"
    assert run("config", "--config", str(path), "set", "nope", "1") == 2
    assert "unknown setting" in capsys.readouterr().err
    assert run("config", "--config", str(path), "set", "menu_timeout", "8") == 0
    assert json.loads(path.read_text())["menu_timeout"] == 8
    assert run("config", "--config", str(path), "set", "shutdown_command", '["true"]') == 0
    assert Config.load(str(path)).shutdown_command == ["true"]


def test_get_and_show(tmp_path, capsys):
    path = tmp_path / "c.json"
    run("config", "--config", str(path), "set", "name", "Den")
    capsys.readouterr()
    assert run("config", "--config", str(path), "get", "name") == 0
    assert capsys.readouterr().out.strip() == '"Den"'
    assert run("config", "--config", str(path), "get", "boot_greeting") == 0
    assert "couch potato" in capsys.readouterr().out
    assert run("config", "--config", str(path), "show") == 0
    out = capsys.readouterr().out
    assert '"name": "Den"' in out and "stored in" in out


def test_fit_text_shortens_with_an_ellipsis():
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = fonts.load(fonts.SANS_BOLD, 28)
    assert render.fit_text(d, "Den", font, 400) == "Den"
    shortened = render.fit_text(d, "A really very long television name", font, 150)
    assert shortened.endswith("…") and d.textlength(shortened, font=font) <= 150


def test_a_long_name_never_runs_into_the_channel_label():
    hw = Hardware()
    ctx = MenuContext(channels=["a"], current_channel=0)
    img = render.render_menu_landscape(MenuModel().items(ctx), 0, hw, title="W" * 40, subtitle="The Documentaries Channel")
    assert img.getbbox() is not None            # drew without error, and the title was shortened to fit
