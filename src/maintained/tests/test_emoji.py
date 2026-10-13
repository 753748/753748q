import pytest

from rich.emoji import Emoji, NoEmoji

from .render import render


def test_no_emoji():
    with pytest.raises(NoEmoji):
        Emoji("ambivalent_bunny")


def test_str_repr():
    assert str(Emoji("pile_of_poo")) == "💩"
    assert repr(Emoji("pile_of_poo")) == "<emoji 'pile_of_poo'>"


def test_replace():
    assert Emoji.replace("my code is :pile_of_poo:") == "my code is 💩"


def test_render():
    render_result = render(Emoji("pile_of_poo"))
    assert render_result == "💩"


def test_variant():
    print(repr(Emoji.replace(":warning:")))
    assert Emoji.replace(":warning:") == "⚠"
    assert Emoji.replace(":warning-text:") == "⚠" + "\uFE0E"
    assert Emoji.replace(":warning-emoji:") == "⚠" + "\uFE0F"
    assert Emoji.replace(":warning-foo:") == ":warning-foo:"


def test_variant_non_default():
    render_result = render(Emoji("warning", variant="emoji"))
    assert render_result == "⚠" + "\uFE0F"
