# -*- coding: utf-8 -*-
# EditXT
# Copyright 2007-2012 Daniel Miller <millerdev@gmail.com>
#
# This file is part of EditXT, a programmer's text editor for Mac OS X,
# which can be found at http://editxt.org/.
#
# EditXT is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# EditXT is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EditXT.  If not, see <http://www.gnu.org/licenses/>.
import logging
import objc
import os
from collections import defaultdict
from itertools import chain, repeat, izip

import objc
from AppKit import *
from Foundation import *
from PyObjCTools import AppHelper

import editxt
import editxt.constants as const
from editxt.controls.cells import BUTTON_STATE_HOVER, BUTTON_STATE_NORMAL, BUTTON_STATE_PRESSED
from editxt.document import TextDocumentView
from editxt.project import Project
from editxt.util import KVOList, RecentItemStack, load_image, perform_selector
from editxt.util import untested, message, representedObject, user_path

log = logging.getLogger(__name__)

class Error(Exception): pass

BUTTON_STATE_SELECTED = object()


class Editor(object):

    supported_drag_types = [const.DOC_ID_LIST_PBOARD_TYPE, NSFilenamesPboardType]

    def __init__(self, window_controller, serial_data=None):
        self._current_view = None
        self.wc = window_controller
        self.serial_data = serial_data
        self.projects = KVOList.alloc().init()
        self.recent = self._suspended_recent = RecentItemStack(20)
        self.window_settings_loaded = False

    def window_did_load(self):
        wc = self.wc
        wc.setShouldCloseDocument_(False)
        wc.docsView.setRefusesFirstResponder_(True)
        wc.plusButton.setRefusesFirstResponder_(True)
        wc.plusButton.setImage_(load_image(const.PLUS_BUTTON_IMAGE))
        wc.propsViewButton.setRefusesFirstResponder_(True)
        wc.propsViewButton.setImage_(load_image(const.PROPS_DOWN_BUTTON_IMAGE))
        wc.propsViewButton.setAlternateImage_(load_image(const.PROPS_UP_BUTTON_IMAGE))

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            wc, "windowDidBecomeKey:", NSWindowDidBecomeKeyNotification, wc.window())
        assert hasattr(EditorWindowController, "windowDidBecomeKey_")

        wc.cleanImages = {
            BUTTON_STATE_HOVER: load_image(const.CLOSE_CLEAN_HOVER),
            BUTTON_STATE_NORMAL: load_image(const.CLOSE_CLEAN_NORMAL),
            BUTTON_STATE_PRESSED: load_image(const.CLOSE_CLEAN_PRESSED),
            BUTTON_STATE_SELECTED: load_image(const.CLOSE_CLEAN_SELECTED),
        }
        wc.dirtyImages = {
            BUTTON_STATE_HOVER: load_image(const.CLOSE_DIRTY_HOVER),
            BUTTON_STATE_NORMAL: load_image(const.CLOSE_DIRTY_NORMAL),
            BUTTON_STATE_PRESSED: load_image(const.CLOSE_DIRTY_PRESSED),
            BUTTON_STATE_SELECTED: load_image(const.CLOSE_DIRTY_SELECTED),
        }

        wc.docsView.registerForDraggedTypes_(self.supported_drag_types)

        self.deserialize(self.serial_data)
        self.serial_data = None

        if not self.projects:
            self.new_project()

        self.window_settings = editxt.app.load_window_settings(self)

    def deserialize(self, data):
        if data:
            for serial in data.get("project_serials", []):
                proj = Project.create_with_serial(serial)
                self.projects.append(proj)
            for proj_index, doc_index in data.get("recent_items", []):
                if proj_index < len(self.projects):
                    proj = self.projects[proj_index]
                    if doc_index == "<project>":
                        self.recent.push(proj.id)
                    elif doc_index < len(proj.documents()):
                        doc = proj.documents()[doc_index]
                        self.recent.push(doc.id)
            self.discard_and_focus_recent(None)

    def serialize(self):
        def iter_settings():
            indexes = {}
            serials = []
            for i, project in enumerate(self.projects):
                serial = project.serialize()
                if serial:
                    serials.append(serial)
                indexes[project.id] = (i, "<project>")
                offset = 0
                for j, doc in enumerate(project.documents()):
                    if doc.file_path and os.path.exists(doc.file_path):
                        indexes[doc.id] = (i, j - offset)
                    else:
                        offset += 1
            yield "project_serials", serials
            rits = []
            for ident in self.recent:
                pair = indexes.get(ident)
                if pair is not None:
                    rits.append(pair)
            yield "recent_items", rits
        return dict((key, val) for key, val in iter_settings() if val)

    def discard_and_focus_recent(self, item):
        ident = None if item is None else item.id
        lookup = {}
        recent = self.recent
        self.suspend_recent_updates()
        try:
            for project in list(self.projects):
                pid = project.id
                for docview in list(project.documents()):
                    did = docview.id
                    if ident in (pid, did):
                        recent.discard(did)
                        project.remove_document_view(docview)
                        docview.close()
                    else:
                        lookup[did] = docview
                if ident == pid:
                    recent.discard(pid)
                    self.projects.remove(project)
                    project.close()
                else:
                    lookup[pid] = project
        finally:
            self.resume_recent_updates()
        while True:
            ident = recent.pop()
            if ident is None:
                break
            item = lookup.get(ident)
            if item is not None:
                self.current_view = item
                break
        if not recent and self.current_view is not None:
            recent.push(self.current_view.id)

    def suspend_recent_updates(self):
        self.recent = RecentItemStack(20)

    def resume_recent_updates(self):
        self.recent = self._suspended_recent

    def _get_current_view(self):
        return self._current_view

    def _set_current_view(self, view):
        if view is self._current_view:
            return
        self._current_view = view
        main_view = self.wc.mainView
        if view is not None:
            sel = self.wc.docsController.selectedObjects()
            if not sel or sel[0] is not view:
                self.wc.docsController.setSelectedObject_(view)
            self.recent.push(view.id)
            if isinstance(view, TextDocumentView):
                if view.scroll_view not in main_view.subviews():
                    for subview in main_view.subviews():
                        subview.removeFromSuperview()
                    view.document.addWindowController_(self.wc)
                    view.set_main_view_of_window(main_view, self.wc.window())
                    #self.wc.setDocument_(view.document)
                    if self.find_project_with_document_view(view) is None:
                        self.add_document_view(view)
                return
            #else:
            #    self.wc.window().setTitle_(view.displayName())
            #    log.debug("self.wc.window().setTitle_(%r)", view.displayName())
        for subview in main_view.subviews():
            subview.removeFromSuperview()
        self.wc.setDocument_(None)

    current_view = property(_get_current_view, _set_current_view)

    def selected_view_changed(self):
        selected = self.wc.docsController.selectedObjects()
        if selected and selected[0] is not self.current_view:
            self.current_view = selected[0]

    def add_document_view(self, doc_view):
        """Add document view to current project

        This does nothing if the current project already contains a view of the
        document encapsulated by doc_view.

        :returns: The document view from the current project.
        """
        proj = self.get_current_project(create=True)
        view = proj.document_view_for_document(doc_view.document)
        if view is None:
            view = doc_view
            proj.append_document_view(doc_view)
        return view

    def iter_views_of_document(self, doc):
        for project in self.projects:
            view = project.find_view_with_document(doc)
            if view is not None:
                yield view

    def count_views_of_document(self, doc):
        return len(list(self.iter_views_of_document(doc)))

    def should_select_item(self, outlineview, item):
        return True
        obj = outlineview.realItemForOpaqueItem_(item)
        if isinstance(obj, TextDocumentView):
            return True
        return False

    def new_project(self):
        project = Project.create()
        view = project.create_document_view()
        self.projects.append(project)
        self.current_view = view
        return project

    def toggle_properties_pane(self):
        tree_rect = self.wc.docsScrollview.frame()
        prop_rect = self.wc.propsView.frame()
        if self.wc.propsViewButton.state() == NSOnState:
            # hide properties view
            tree_rect.size.height += prop_rect.size.height - 1.0
            tree_rect.origin.y = prop_rect.origin.y
            prop_rect.size.height = 0.0
        else:
            # show properties view
            tree_rect.size.height -= 115.0
            if prop_rect.size.height > 0:
                tree_rect.size.height += (prop_rect.size.height - 1.0)
            tree_rect.origin.y = prop_rect.origin.y + 115.0
            prop_rect.size.height = 116.0
            self.wc.propsView.setHidden_(False)
        resize_tree = NSDictionary.dictionaryWithObjectsAndKeys_(
            self.wc.docsScrollview, NSViewAnimationTargetKey,
            NSValue.valueWithRect_(tree_rect), NSViewAnimationEndFrameKey,
            None,
        )
        resize_props = NSDictionary.dictionaryWithObjectsAndKeys_(
            self.wc.propsView, NSViewAnimationTargetKey,
            NSValue.valueWithRect_(prop_rect), NSViewAnimationEndFrameKey,
            None,
        )
        anims = NSArray.arrayWithObjects_(resize_tree, resize_props, None)
        animation = NSViewAnimation.alloc().initWithViewAnimations_(anims)
        #animation.setAnimationBlockingMode_(NSAnimationBlocking)
        animation.setDuration_(0.25)
        animation.startAnimation()

    def find_project_with_document_view(self, doc):
        for proj in self.projects:
            for d in proj.documents():
                if doc is d:
                    return proj
        return None

    def find_project_with_path(self, path):
        for proj in self.projects:
            p = proj.file_path
            if p and os.path.exists(p) and os.path.samefile(p, path):
                return proj
        return None

    def get_current_project(self, create=False):
        docs_controller = self.wc.docsController
        if docs_controller is not None:
            path = docs_controller.selectionIndexPath()
            if path is not None:
                index = path.indexAtPosition_(0)
                path2 = NSIndexPath.indexPathWithIndex_(index)
                return docs_controller.objectAtArrangedIndexPath_(path2)
        if create:
            proj = Project.create()
            self.projects.append(proj)
            return proj
        return None

    def item_changed(self, item, change_type):
        view = self.wc.docsView
        if item is not None and view is not None:
            for row, obj in view.iterVisibleObjects():
                if obj is item or getattr(obj, "document", None) is item:
                    view.setNeedsDisplayInRect_(view.rectOfRow_(row))
                    break

    def tooltip_for_item(self, view, item):
        it = view.realItemForOpaqueItem_(item)
        null = it is None or it.file_path is None
        return None if null else user_path(it.file_path)

    def should_edit_item(self, col, item):
        if col.isEditable():
            obj = representedObject(item)
            return isinstance(obj, Project) and obj.can_rename()
        return False

    def close_button_clicked(self, row):
        docs_view = self.wc.docsView
        if row < docs_view.numberOfRows():
            item = docs_view.itemAtRow_(row)
            item = docs_view.realItemForOpaqueItem_(item)
            item.perform_close(self)

    def window_did_become_key(self, window):
        view = self.current_view
        if isinstance(view, TextDocumentView):
            # TODO refactor TextDocumentView to support check_for_external_changes()
            view.document.check_for_external_changes(window)

    def window_should_close(self, window):
        from editxt import app
        from editxt.application import DocumentSavingDelegate
        # this method is called after the window controller has prompted the
        # user to save the current document (if it is dirty). This causes some
        # wierdness with the window and subsequent sheets. Technically we
        # do not need to prompt to save the current document a second time.
        # However, we will because it is easier... THIS IS UGLY! but there
        # doesn't seem to be a way to prevent the window controller from
        # prompting to save the current document when the window's close button
        # is clicked. UPDATE: the window controller seems to only prompt to save
        # the current document if the document is new (untitled).
        def iter_dirty_docs():
            for proj in self.projects:
                eds = app.find_editors_with_project(proj)
                if eds == [self]:
                    for dv in proj.dirty_documents():
                        doc = dv.document
                        editors = app.iter_editors_with_view_of_document(doc)
                        if list(editors) == [self]:
                            yield dv
                    yield proj
        def callback(should_close):
            if should_close:
                window.close()
        saver = DocumentSavingDelegate.alloc(). \
            init_callback_(iter_dirty_docs(), callback)
        saver.save_next_document()
        return False

    def window_will_close(self):
        if self.window_settings_loaded:
            editxt.app.save_window_settings(self)
        editxt.app.discard_editor(self)

    def _get_window_settings(self):
        return dict(
            frame_string=self.wc.window().stringWithSavedFrame(),
            splitter_pos=self.wc.splitView.fixedSideThickness(),
            properties_hidden=(self.wc.propsViewButton.state() == NSOnState),
        )
    def _set_window_settings(self, settings):
        fs = settings.get("frame_string")
        if fs is not None:
            self.wc.window().setFrameFromString_(fs)
            self.wc.setShouldCascadeWindows_(False)
        sp = settings.get("splitter_pos")
        if sp is not None:
            self.wc.splitView.setFixedSideThickness_(sp)
        if settings.get("properties_hidden", False):
            # REFACTOR eliminate boilerplate here (similar to toggle_properties_pane)
            self.wc.propsViewButton.setState_(NSOnState)
            tree_view = self.wc.docsScrollview
            prop_view = self.wc.propsView
            tree_rect = tree_view.frame()
            prop_rect = prop_view.frame()
            tree_rect.size.height += prop_rect.size.height - 1.0
            tree_rect.origin.y = prop_rect.origin.y
            tree_view.setFrame_(tree_rect)
            prop_rect.size.height = 0.0
            prop_view.setFrame_(prop_rect)
        self.window_settings_loaded = True
    window_settings = property(_get_window_settings, _set_window_settings)

    def close(self):
        wc = self.wc
        if wc is not None:
            self.window_settings_loaded = False
            for proj in self.projects:
                proj.close()
            #wc.docsController.setContent_(None)
            #wc.setDocument_(None)
            #self.wc = None

    # drag/drop logic ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def is_project_drag(self, info):
        """Return True if only projects are being dropped else False"""
        pb = info.draggingPasteboard()
        t = pb.availableTypeFromArray_(self.supported_drag_types)
        if t == const.DOC_ID_LIST_PBOARD_TYPE:
            items = self.iter_dropped_id_list(pb)
            return all(isinstance(item, Project) for item in items)
        elif t == NSFilenamesPboardType:
            paths = pb.propertyListForType_(NSFilenamesPboardType)
            return all(Project.is_project_path(path) for path in paths)
        return False

    def write_items_to_pasteboard(self, outline_view, items, pboard):
        """Write dragged items to pasteboard

        :param outline_view: The OutlineView containing the items.
        :param items: A list of opaque outline view item objects.
        :param pboard: NSPasteboard object.
        :returns: True if items were written else False.
        """
        data = defaultdict(list)
        for item in items:
            item = outline_view.realItemForOpaqueItem_(item)
            data[const.DOC_ID_LIST_PBOARD_TYPE].append(item.id)
            path = item.file_path
            if path is not None and os.path.exists(path):
                data[NSFilenamesPboardType].append(path)
        if data:
            types = [t for t in self.supported_drag_types if t in data]
            pboard.declareTypes_owner_(types, None)
            for t in types:
                pboard.setPropertyList_forType_(data[t], t)
        return bool(data)

    def validate_drop(self, outline_view, info, item, index):
        if self.is_project_drag(info):
            if item is not None:
                obj = representedObject(item)
                path = self.wc.docsController.indexPathForObject_(obj)
                if path is not None:
                    index = path.indexAtPosition_(0)
                    outline_view.setDropItem_dropChildIndex_(None, index)
                else:
                    return NSDragOperationNone
            elif index < 0:
                nprojs = len(self.projects)
                outline_view.setDropItem_dropChildIndex_(None, nprojs)
        else:
            # text document drag
            if item is not None:
                obj = representedObject(item)
                if isinstance(obj, Project):
                    if index < 0:
                        #outline_view.setDropItem_dropChildIndex_(item, 0)
                        # the following might be more correct, but is too confusing
                        outline_view.setDropItem_dropChildIndex_(item, len(obj.documents()))
                else:
                    return NSDragOperationNone # document view cannot have children
            else:
                if index < 0:
                    # drop on listview background
                    last_proj_index = len(self.projects) - 1
                    if last_proj_index > -1:
                        # we have at least one project
                        path = NSIndexPath.indexPathWithIndex_(last_proj_index)
                        node = self.wc.docsController.nodeAtArrangedIndexPath_(path)
                        proj = representedObject(node)
                        outline_view.setDropItem_dropChildIndex_(node, len(proj.documents()))
                    else:
                        outline_view.setDropItem_dropChildIndex_(None, -1)
                elif index == 0:
                    return NSDragOperationNone # prevent drop above top project
        # src = info.draggingSource()
        # if src is not None:
        #   # internal drag
        #   if src is not outline_view:
        #       delegate = getattr(src, "delegate", lambda:None)()
        #       if isinstance(delegate, EditorWindowController) and \
        #           delegate is not self.wc:
        #           # drag from some other window controller
        #           # allow copy (may need to override outline_view.ignoreModifierKeysWhileDragging)
        return NSDragOperationGeneric

    def accept_drop(self, outline_view, info, item, index):
        """Accept drop operation

        :param outline_view: The OutlineView on which the drop occurred.
        :param info: NSDraggingInfo object.
        :param item: The parent item in the outline view.
        :param index: The index in the outline view or parent item at which the
            drop occurred.
        :returns: True if the drop was accepted, otherwise False.
        """
        pb = info.draggingPasteboard()
        t = pb.availableTypeFromArray_(self.supported_drag_types)
        action = None
        if t == const.DOC_ID_LIST_PBOARD_TYPE:
            items = self.iter_dropped_id_list(pb)
            action = const.MOVE
        elif t == NSFilenamesPboardType:
            items = self.iter_dropped_paths(pb)
        else:
            assert t is None, t
            return False
        parent = None if item is None else representedObject(item)
        return self.accept_dropped_items(items, parent, index, action)

    def iter_dropped_id_list(self, pasteboard):
        """Iterate TextDocument objects referenced by pasteboard (if any)"""
        IDLT = const.DOC_ID_LIST_PBOARD_TYPE
        if not pasteboard.types().containsObject_(IDLT):
            raise StopIteration()
        for ident in pasteboard.propertyListForType_(IDLT):
            item = editxt.app.find_item_with_id(ident)
            if item is not None:
                yield item

    def iter_dropped_paths(self, pasteboard):
        from editxt.document import TextDocument
        if not pasteboard.types().containsObject_(NSFilenamesPboardType):
            raise StopIteration()
        for path in pasteboard.propertyListForType_(NSFilenamesPboardType):
            if Project.is_project_path(path):
                proj = editxt.app.find_project_with_path(path)
                if proj is None:
                    proj = Project.create_with_path(path)
                yield proj
            else:
                yield TextDocument.get_with_path(path)

    @untested("untested with non-null project and index < 0")
    def accept_dropped_items(self, items, project, index, action):
        """Insert dropped items into the document tree

        :param items: A sequence of dropped projects and/or documents.
        :param project: The parent project into which items are being dropped.
        :param index: The index in the outline view or parent project at which
            the drop occurred.
        :param action: The type of drop: None (unspecified), MOVE, or COPY.
        :returns: True if the items were accepted, otherwise False.
        """
        if project is None:
            # a new project will be created if/when needed
            if index < 0:
                proj_index = 0
            else:
                proj_index = index
            index = 0
        else:
            proj_index = len(self.projects) # insert projects at end of list
            assert isinstance(project, Project), project
            if index < 0:
                index = len(project.documents())
        accepted = False
        focus = None
        is_move = action is not const.COPY
        self.suspend_recent_updates()
        try:
            for item in items:
                accepted = True
                if isinstance(item, Project):
                    if not is_move:
                        raise NotImplementedError('cannot copy project yet')
                    editors = editxt.app.find_editors_with_project(item)
                    assert len(editors) < 2, editors
                    if item in self.projects:
                        editor = self
                        pindex = self.projects.index(item)
                        if pindex == proj_index:
                            continue
                        if pindex - proj_index <= 0:
                            proj_index -= 1
                    else:
                        editor = editors[0]

                    # BEGIN HACK crash on remove project with documents
                    pdocs = item.documents()
                    docs, pdocs[:] = list(pdocs), []
                    editor.projects.remove(item) # this line should be all that's necessary
                    pdocs.extend(docs)
                    # END HACK

                    self.projects.insert(proj_index, item)
                    proj_index += 1
                    focus = item
                    continue

                if project is None:
                    if isinstance(item, TextDocumentView) and is_move:
                        view = item
                        item.project.remove_document_view(view)
                    else:
                        view = TextDocumentView.create_with_document(item)
                    project = Project.create()
                    self.projects.insert(proj_index, project)
                    proj_index += 1
                    index = 0
                else:
                    if isinstance(item, TextDocumentView):
                        view, item = item, item.document
                    else:
                        view = project.document_view_for_document(item)
                    if is_move and view is not None:
                        if view.project == project:
                            vindex = project.documents().index(view)
                            if vindex in [index - 1, index]:
                                continue
                            if vindex - index <= 0:
                                index -= 1
                        view.project.remove_document_view(view)
                    else:
                        view = TextDocumentView.create_with_document(item)
                project.insert_document_view(index, view)
                focus = view
                index += 1
        finally:
            self.resume_recent_updates()
        if focus is not None:
            self.current_view = focus
        return accepted

    def undo_manager(self):
        doc = self.wc.document()
        if doc is None:
            return NSUndoManager.alloc().init()
        return doc.undoManager()


class EditorWindowController(NSWindowController):

    docsController = objc.IBOutlet()
    docsScrollview = objc.IBOutlet()
    docsView = objc.IBOutlet()
    mainView = objc.IBOutlet()
    splitView = objc.IBOutlet()
    plusButton = objc.IBOutlet()
    propsView = objc.IBOutlet()
    propsViewButton = objc.IBOutlet()
    #propCharacterEncoding = objc.IBOutlet()
    #propLanguageSelector = objc.IBOutlet()
    #propLineEndingType = objc.IBOutlet()
    #propTabSpacesInput = objc.IBOutlet()
    #propTabSpacesSelector = objc.IBOutlet()
    #propWrapLines = objc.IBOutlet()

    def windowDidLoad(self):
        self.editor.window_did_load()

    def characterEncodings(self):
        return NSValueTransformer.valueTransformerForName_("CharacterEncodingTransformer").names
        #return const.CHARACTER_ENCODINGS

    def setCharacterEncodings_(self, value):
        pass

    def syntaxDefNames(self):
        return [d.name for d in editxt.app.syntaxdefs]

    def setSyntaxDefNames_(self, value):
        pass

    def projects(self):
        return self.editor.projects

    def newProject_(self, sender):
        self.editor.new_project()

    def togglePropertiesPane_(self, sender):
        self.editor.toggle_properties_pane()

    def outlineViewSelectionDidChange_(self, notification):
        self.editor.selected_view_changed()

    def outlineViewItemDidCollapse_(self, notification):
        representedObject(notification.userInfo()["NSObject"]).expanded = False

    def outlineViewItemDidExpand_(self, notification):
        representedObject(notification.userInfo()["NSObject"]).expanded = True

    def outlineView_shouldSelectItem_(self, outlineview, item):
        return self.editor.should_select_item(outlineview, item)

    def outlineView_willDisplayCell_forTableColumn_item_(self, view, cell, col, item):
        if col.identifier() == "name":
            cell.setImage_(representedObject(item).icon())

    def outlineView_shouldEditTableColumn_item_(self, view, col, item):
        return self.editor.should_edit_item(col, item)

    def outlineView_toolTipForCell_rect_tableColumn_item_mouseLocation_(
        self, view, cell, rect, col, item, mouseloc):
        return self.editor.tooltip_for_item(view, item), rect

    def hoverButton_rowClicked_(self, cell, row):
        self.editor.close_button_clicked(row)

    @untested
    def hoverButtonCell_imageForState_row_(self, cell, state, row):
        if state is BUTTON_STATE_NORMAL and self.docsView.isRowSelected_(row):
            state = BUTTON_STATE_SELECTED
        if row >= 0 and row < self.docsView.numberOfRows():
            item = self.docsView.itemAtRow_(row)
            doc = self.docsView.realItemForOpaqueItem_(item)
            if doc is not None and doc.is_dirty:
                return self.dirtyImages[state]
        return self.cleanImages[state]

    def undo_manager(self):
        return self.editor.undo_manager()

    def windowTitleForDocumentDisplayName_(self, name):
        view = self.editor.current_view
        if view is not None and view.file_path is not None:
            return user_path(view.file_path)
        return name

    def windowDidBecomeKey_(self, notification):
        self.editor.window_did_become_key(notification.object())

    def windowShouldClose_(self, window):
        return self.editor.window_should_close(window)

    def windowWillClose_(self, notification):
        self.editor.window_will_close()

    # outlineview datasource methods ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def outlineView_writeItems_toPasteboard_(self, view, items, pboard):
        return self.editor.write_items_to_pasteboard(view, items, pboard)

    def outlineView_acceptDrop_item_childIndex_(self, view, info, item, index):
        return self.editor.accept_drop(view, info, item, index)

    def outlineView_validateDrop_proposedItem_proposedChildIndex_(self, view, info, item, index):
        return self.editor.validate_drop(view, info, item, index)

    # def outlineView_namesOfPromisedFilesDroppedAtDestination_forDraggedItems_(
    #   self, view, names, items):
    #   item = representedObject(item)
    #   raise NotImplementedError

    # the following are dummy implementations since we are using bindings (they
    # are required since we are using NSOutlineView's drag/drop datasource methods)
    # see: http://theocacao.com/document.page/130

    def outlineView_child_ofItem_(self, view, index, item):
        return None

    def outlineView_isItemExpandable_(self, view, item):
        return False

    def outlineView_numberOfChildrenOfItem_(self, view, item):
        return 0

    def outlineView_objectValueForTableColumn_byItem_(self, view, col, item):
        return None

