from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import aqt
from aqt import gui_hooks
from aqt.browser import Browser, SidebarItem, SidebarItemType, SidebarTreeView
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
    Qt,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

ADDON_NAME = __name__.split(".")[0]
DEFAULT_CONFIG: dict[str, Any] = {
    "hidden_tags": [],
    "show_hide_hint": True,
}

MENU_HIDE_TAG = "Hide Tag"
MENU_HIDDEN_TAGS = "Hidden Tags"
HIDE_HINT_TEXT = "Tag hidden. You can manage hidden tags from Tools > Hidden Tags."

TAG_SEPARATOR = "::"
QT_USER_ROLE = getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")

_tools_menu_action: QAction | None = None
_installed = False
_original_tag_tree_builder: Any | None = None


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


def _add_hidden_tags(full_tag_paths: Iterable[str]) -> bool:
    new_hidden_tags = _normalize_hidden_tags(full_tag_paths)
    if not new_hidden_tags:
        return False

    config = _load_config()
    hidden_tags: list[str] = config["hidden_tags"]
    hidden_tags_set = set(hidden_tags)
    tags_to_add = [tag for tag in new_hidden_tags if tag not in hidden_tags_set]
    if not tags_to_add:
        return False

    config["hidden_tags"] = _normalize_hidden_tags([*hidden_tags, *tags_to_add])
    _save_config(config)
    return True


def _add_hidden_tag(full_tag_path: str) -> bool:
    return _add_hidden_tags([full_tag_path])


def _tag_path_prefixes(full_tag_path: str) -> list[str]:
    parts = [part for part in full_tag_path.split(TAG_SEPARATOR) if part]
    prefixes: list[str] = []
    for index in range(1, len(parts) + 1):
        prefixes.append(TAG_SEPARATOR.join(parts[:index]))
    return prefixes


def _has_tag_ancestor(full_tag_path: str, ancestor_tags: set[str]) -> bool:
    prefixes = _tag_path_prefixes(full_tag_path)
    return any(prefix in ancestor_tags for prefix in prefixes[:-1])


def _collapse_descendant_tags(full_tag_paths: Iterable[str]) -> list[str]:
    collapsed: list[str] = []
    collapsed_set: set[str] = set()
    for tag in sorted(
        set(full_tag_paths),
        key=lambda tag: (tag.count(TAG_SEPARATOR), tag.casefold()),
    ):
        if _has_tag_ancestor(tag, collapsed_set):
            continue
        collapsed.append(tag)
        collapsed_set.add(tag)
    return collapsed


def _collection_tag_paths() -> list[str]:
    if aqt.mw is None:
        return []

    col = getattr(aqt.mw, "col", None)
    tag_manager = getattr(col, "tags", None)
    if tag_manager is None:
        return []

    raw_tags: Iterable[Any] | None = None
    for method_name in ("all", "all_names"):
        method = getattr(tag_manager, method_name, None)
        if not callable(method):
            continue
        try:
            raw_tags = method()
        except Exception:
            continue
        break

    if raw_tags is None:
        method = getattr(tag_manager, "all_names_and_counts", None)
        if callable(method):
            try:
                raw_tags = [name for name, _count in method()]
            except Exception:
                raw_tags = None

    if raw_tags is None:
        return []

    tag_paths: set[str] = set()
    for value in raw_tags:
        if not isinstance(value, str):
            continue
        tag = value.strip()
        if not tag:
            continue
        tag_paths.update(_tag_path_prefixes(tag))

    return sorted(
        tag_paths,
        key=lambda tag: tuple(part.casefold() for part in tag.split(TAG_SEPARATOR)),
    )


def _visible_collection_tag_paths() -> list[str]:
    hidden_tags = _hidden_tags_set()
    return [
        tag
        for tag in _collection_tag_paths()
        if tag not in hidden_tags and not _has_tag_ancestor(tag, hidden_tags)
    ]


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

    if dont_show_again.isChecked():
        config["show_hide_hint"] = False
        _save_config(config)


def _hide_sidebar_tag(sidebar: SidebarTreeView, full_tag_path: str) -> None:
    if not full_tag_path:
        return

    if _add_hidden_tag(full_tag_path):
        sidebar.refresh()
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


def _patch_sidebar_tag_tree_builder() -> None:
    global _original_tag_tree_builder

    if _original_tag_tree_builder is not None:
        return

    original = getattr(SidebarTreeView, "_tag_tree", None)
    if not callable(original):
        return

    _original_tag_tree_builder = original

    def wrapped_tag_tree(sidebar: SidebarTreeView, root: SidebarItem) -> None:
        _original_tag_tree_builder(sidebar, root)
        hidden_tags = _hidden_tags_set()
        if hidden_tags:
            # Filter hidden tags every time Anki rebuilds the sidebar tag tree.
            _filter_hidden_tags_in_tree(root, hidden_tags)

    SidebarTreeView._tag_tree = wrapped_tag_tree


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


class HideTagsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hide Tags")
        self.resize(620, 540)
        self._tree_expanded = True

        layout = QVBoxLayout(self)

        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel("Visible tags:", self))
        header_layout.addStretch()

        self.expand_toggle_button = QPushButton("Collapse All", self)
        self.expand_toggle_button.clicked.connect(self._toggle_expanded)
        header_layout.addWidget(self.expand_toggle_button)
        layout.addLayout(header_layout)

        self.tree_widget = QTreeWidget(self)
        self.tree_widget.setHeaderHidden(True)
        self.tree_widget.setAlternatingRowColors(True)
        self.tree_widget.setAnimated(True)
        self.tree_widget.setIndentation(18)
        self.tree_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.tree_widget.itemSelectionChanged.connect(self._update_button_state)
        layout.addWidget(self.tree_widget)

        actions_layout = QHBoxLayout()
        actions_layout.addStretch()

        self.hide_selected_button = QPushButton("Hide Selected", self)
        self.hide_selected_button.setDefault(True)
        self.hide_selected_button.clicked.connect(self._hide_selected)
        actions_layout.addWidget(self.hide_selected_button)

        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.reject)
        actions_layout.addWidget(close_button)
        layout.addLayout(actions_layout)

        self._refresh_tree()

    def _refresh_tree(self) -> None:
        signals_were_blocked = self.tree_widget.blockSignals(True)
        try:
            self.tree_widget.clear()

            items_by_path: dict[str, QTreeWidgetItem] = {}
            for full_tag_path in _visible_collection_tag_paths():
                parent_item: QTreeWidgetItem | None = None
                for path in _tag_path_prefixes(full_tag_path):
                    item = items_by_path.get(path)
                    if item is None:
                        item = QTreeWidgetItem([path.split(TAG_SEPARATOR)[-1]])
                        item.setData(0, QT_USER_ROLE, path)
                        if parent_item is None:
                            self.tree_widget.addTopLevelItem(item)
                        else:
                            parent_item.addChild(item)
                        items_by_path[path] = item
                    parent_item = item
        finally:
            self.tree_widget.blockSignals(signals_were_blocked)

        self.tree_widget.expandAll()
        self._tree_expanded = True
        self.expand_toggle_button.setText("Collapse All")
        self._update_button_state()

    def _toggle_expanded(self) -> None:
        if self._tree_expanded:
            self.tree_widget.collapseAll()
            self.expand_toggle_button.setText("Expand All")
        else:
            self.tree_widget.expandAll()
            self.expand_toggle_button.setText("Collapse All")
        self._tree_expanded = not self._tree_expanded

    def _selected_tags(self) -> list[str]:
        selected_tags: list[str] = []
        for item in self.tree_widget.selectedItems():
            tag = item.data(0, QT_USER_ROLE)
            if isinstance(tag, str) and tag:
                selected_tags.append(tag)
        return _collapse_descendant_tags(selected_tags)

    def _update_button_state(self) -> None:
        self.hide_selected_button.setEnabled(len(self._selected_tags()) > 0)

    def _hide_selected(self) -> None:
        selected_tags = self._selected_tags()
        if _add_hidden_tags(selected_tags):
            _refresh_open_browser_sidebars()
            self.accept()
        else:
            self._update_button_state()


class HiddenTagsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(MENU_HIDDEN_TAGS)
        self.resize(440, 340)

        layout = QVBoxLayout(self)

        self.hide_tags_button = QPushButton("Hide Tags...", self)
        self.hide_tags_button.clicked.connect(self._open_hide_tags_dialog)
        layout.addWidget(self.hide_tags_button)

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

    def _open_hide_tags_dialog(self) -> None:
        dialog = HideTagsDialog(self)
        if dialog.exec():
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

    _patch_sidebar_tag_tree_builder()

    if hasattr(gui_hooks, "main_window_did_init"):
        gui_hooks.main_window_did_init.append(_add_tools_menu_entry)
    else:
        _add_tools_menu_entry()

    if hasattr(gui_hooks, "browser_sidebar_will_show_context_menu"):
        gui_hooks.browser_sidebar_will_show_context_menu.append(_on_sidebar_context_menu)

    _installed = True


_install_hooks()
