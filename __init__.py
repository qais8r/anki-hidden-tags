from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import aqt
from aqt import gui_hooks
from aqt.browser import Browser, SidebarItem, SidebarItemType, SidebarStage, SidebarTreeView
from aqt.qt import (
    QAbstractItemView,
    QAction,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMenu,
    QModelIndex,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ADDON_NAME = __name__
DEFAULT_CONFIG: dict[str, Any] = {
    "hidden_tags": [],
    "show_hide_hint": True,
}

MENU_HIDE_TAG = "Hide Tag"
MENU_HIDDEN_TAGS = "Hidden Tags"
HIDE_HINT_TEXT = "Tag hidden. You can unhide tags from Tools > Hidden Tags."

_tools_menu_action: QAction | None = None
_installed = False


def _normalize_hidden_tags(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    hidden_tags: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        tag = value.strip()
        if not tag:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        hidden_tags.append(tag)
    hidden_tags.sort(key=str.casefold)
    return hidden_tags


def _normalize_config(raw_config: Any) -> dict[str, Any]:
    config = dict(raw_config) if isinstance(raw_config, dict) else {}
    hidden_tags = _normalize_hidden_tags(config.get("hidden_tags", []))
    show_hide_hint = bool(config.get("show_hide_hint", True))
    return {
        "hidden_tags": hidden_tags,
        "show_hide_hint": show_hide_hint,
    }


def _load_config() -> dict[str, Any]:
    if aqt.mw is None:
        return dict(DEFAULT_CONFIG)

    raw_config = aqt.mw.addonManager.getConfig(ADDON_NAME)
    config = _normalize_config(raw_config)

    # Keep on-disk config normalized so persistence is predictable.
    if raw_config != config:
        _save_config(config)

    return config


def _save_config(config: dict[str, Any]) -> None:
    if aqt.mw is None:
        return

    # Persist hidden tags and hint preference via add-on config.
    aqt.mw.addonManager.writeConfig(ADDON_NAME, config)


def _add_hidden_tag(full_tag_path: str) -> bool:
    config = _load_config()
    hidden_tags: list[str] = config["hidden_tags"]
    if full_tag_path in hidden_tags:
        return False

    hidden_tags.append(full_tag_path)
    config["hidden_tags"] = _normalize_hidden_tags(hidden_tags)
    _save_config(config)
    return True


def _remove_hidden_tags(tags_to_remove: Iterable[str]) -> bool:
    tags_to_remove_set = {tag for tag in tags_to_remove if isinstance(tag, str) and tag}
    if not tags_to_remove_set:
        return False

    config = _load_config()
    hidden_tags: list[str] = config["hidden_tags"]
    updated = [tag for tag in hidden_tags if tag not in tags_to_remove_set]
    if len(updated) == len(hidden_tags):
        return False

    config["hidden_tags"] = updated
    _save_config(config)
    return True


def _clear_hidden_tags() -> bool:
    config = _load_config()
    if not config["hidden_tags"]:
        return False

    config["hidden_tags"] = []
    _save_config(config)
    return True


def _hidden_tags_set() -> set[str]:
    return set(_load_config()["hidden_tags"])


def _iter_open_browsers() -> list[Browser]:
    app = QApplication.instance()
    if app is None:
        return []

    return [widget for widget in app.topLevelWidgets() if isinstance(widget, Browser)]


def _refresh_open_browser_sidebars() -> None:
    # Refresh Browser sidebars after hide/unhide so changes are visible immediately.
    for browser in _iter_open_browsers():
        sidebar = getattr(browser, "sidebar", None)
        if sidebar is not None:
            sidebar.refresh()


def _maybe_show_hide_hint_once(parent: QWidget | None = None) -> None:
    config = _load_config()
    if not config["show_hide_hint"]:
        return

    dialog = QDialog(parent or aqt.mw)
    dialog.setWindowTitle(MENU_HIDDEN_TAGS)

    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(HIDE_HINT_TEXT, dialog))
    dont_show_again = QCheckBox("Do not show again", dialog)
    layout.addWidget(dont_show_again)

    button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dialog)
    button_box.accepted.connect(dialog.accept)
    layout.addWidget(button_box)

    dialog.exec()

    # This acts as a first-use hint gate; once shown, we disable future hints.
    config["show_hide_hint"] = False
    _save_config(config)


def _hide_sidebar_tag(sidebar: SidebarTreeView, full_tag_path: str) -> None:
    if not full_tag_path:
        return

    if _add_hidden_tag(full_tag_path):
        _refresh_open_browser_sidebars()
        _maybe_show_hide_hint_once(parent=sidebar)


def _filter_hidden_tags_recursive(parent: SidebarItem, hidden_tags: set[str]) -> None:
    filtered_children: list[SidebarItem] = []

    for child in parent.children:
        if child.item_type == SidebarItemType.TAG and child.full_name in hidden_tags:
            continue
        _filter_hidden_tags_recursive(child, hidden_tags)
        filtered_children.append(child)

    parent.children = filtered_children


def _filter_hidden_tags_in_tree(tree: SidebarItem, hidden_tags: set[str]) -> None:
    for section in tree.children:
        if section.item_type != SidebarItemType.TAG_ROOT:
            continue

        # Filter the tag branch while the sidebar tree is being built.
        _filter_hidden_tags_recursive(section, hidden_tags)
        return


def _on_browser_will_build_tree(
    handled: bool,
    tree: SidebarItem,
    stage: SidebarStage,
    browser: Browser,
) -> bool:
    if handled or stage != SidebarStage.TAGS:
        return handled

    hidden_tags = _hidden_tags_set()
    if not hidden_tags:
        return handled

    sidebar = getattr(browser, "sidebar", None)
    if sidebar is None:
        return handled

    default_tag_tree_builder = getattr(sidebar, "_tag_tree", None)
    if not callable(default_tag_tree_builder):
        return handled

    default_tag_tree_builder(tree)
    _filter_hidden_tags_in_tree(tree, hidden_tags)
    return True


def _on_sidebar_context_menu(
    sidebar: SidebarTreeView,
    menu: QMenu,
    item: SidebarItem,
    _index: QModelIndex,
) -> None:
    if item.item_type != SidebarItemType.TAG:
        return

    # Extend the Browser sidebar context menu with Hide Tag for tag nodes.
    if menu.actions():
        menu.addSeparator()

    hide_action = menu.addAction(MENU_HIDE_TAG)
    hide_action.triggered.connect(
        lambda _checked=False, s=sidebar, tag=item.full_name: _hide_sidebar_tag(s, tag)
    )


class HiddenTagsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(MENU_HIDDEN_TAGS)
        self.resize(440, 340)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Hidden tags:", self))

        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.list_widget.itemSelectionChanged.connect(self._update_button_state)
        layout.addWidget(self.list_widget)

        actions_layout = QHBoxLayout()
        self.unhide_selected_button = QPushButton("Unhide Selected", self)
        self.unhide_selected_button.clicked.connect(self._unhide_selected)
        actions_layout.addWidget(self.unhide_selected_button)

        self.unhide_all_button = QPushButton("Unhide All", self)
        self.unhide_all_button.clicked.connect(self._unhide_all)
        actions_layout.addWidget(self.unhide_all_button)
        layout.addLayout(actions_layout)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self._refresh_list()

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for tag in _load_config()["hidden_tags"]:
            self.list_widget.addItem(tag)

        self._update_button_state()

    def _update_button_state(self) -> None:
        has_items = self.list_widget.count() > 0
        has_selection = len(self.list_widget.selectedItems()) > 0
        self.unhide_selected_button.setEnabled(has_selection)
        self.unhide_all_button.setEnabled(has_items)

    def _unhide_selected(self) -> None:
        selected_tags = [item.text() for item in self.list_widget.selectedItems()]
        if _remove_hidden_tags(selected_tags):
            _refresh_open_browser_sidebars()
        self._refresh_list()

    def _unhide_all(self) -> None:
        if _clear_hidden_tags():
            _refresh_open_browser_sidebars()
        self._refresh_list()


def _open_hidden_tags_dialog() -> None:
    dialog = HiddenTagsDialog(parent=aqt.mw)
    dialog.exec()


def _add_tools_menu_entry() -> None:
    global _tools_menu_action

    if aqt.mw is None or _tools_menu_action is not None:
        return

    menu = aqt.mw.form.menuTools
    action = QAction(MENU_HIDDEN_TAGS, aqt.mw)
    action.triggered.connect(_open_hidden_tags_dialog)
    menu.addAction(action)

    _tools_menu_action = action


def _install_hooks() -> None:
    global _installed

    if _installed:
        return

    if hasattr(gui_hooks, "main_window_did_init"):
        gui_hooks.main_window_did_init.append(_add_tools_menu_entry)
    else:
        _add_tools_menu_entry()

    if hasattr(gui_hooks, "browser_sidebar_will_show_context_menu"):
        gui_hooks.browser_sidebar_will_show_context_menu.append(_on_sidebar_context_menu)

    if hasattr(gui_hooks, "browser_will_build_tree"):
        gui_hooks.browser_will_build_tree.append(_on_browser_will_build_tree)

    _installed = True


_install_hooks()
