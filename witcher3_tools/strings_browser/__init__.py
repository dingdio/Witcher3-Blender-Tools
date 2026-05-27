"""Witcher 2 / Witcher 3 Strings Browser subpackage.
"""

from . import strings_sources
from . import ui_strings_browser

GAME_W3 = strings_sources.GAME_W3
GAME_W2 = strings_sources.GAME_W2


def register():
    ui_strings_browser.register()


def unregister():
    ui_strings_browser.unregister()


def draw_launcher(layout):
    ui_strings_browser.draw_launcher(layout)
