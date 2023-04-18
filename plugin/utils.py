from time import sleep
from typing import Optional

import sublime


def view_file_name(view: Optional[sublime.View]) -> str:
    if view is None:
        return ""
    name = view.file_name()
    return name if name else ""


# TODO: use sublime_plugin.EventListener.on_load()
# TODO: check if the view is closed with view.is_valid()
def view_is_loaded(view: Optional[sublime.View]) -> bool:
    if view is None or not view.is_valid():
        return False
    i = 0
    loading = view.is_loading()
    while loading and i < 5:
        i += 1
        sleep(0.05)
        if not view.is_valid():
            return False
        loading = view.is_loading()
    return loading is False
