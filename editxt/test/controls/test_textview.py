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
from __future__ import with_statement

import logging
import os

from AppKit import *
from Foundation import *
from mocker import Mocker, MockerTestCase, expect, ANY
from nose.tools import *
from editxt.test.util import TestConfig, untested

import editxt.constants as const
from editxt.controls.textview import TextView

log = logging.getLogger(__name__)

# log.debug("""TODO
#     implement TextDocumentView.pasteboard_data()
# """)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

def test_TextView_performFindPanelAction_():
    from editxt.findpanel import FindController
    m = Mocker()
    tv = TextView.alloc().init()
    fc = m.replace("editxt.findpanel.FindController")
    sender = m.mock()
    (fc.shared_controller() >> m.mock(FindController)).perform_action(sender)
    with m:
        tv.performFindPanelAction_(sender)

def test_TextView_performTextCommand_():
    from editxt.textcommand import TextCommandController
    m = Mocker()
    tv = TextView.alloc().init()
    tc = m.replace("editxt.app").text_commander >> m.mock(TextCommandController)
    sender = m.mock()
    tc.do_textview_command(tv, sender)
    with m:
        tv.performTextCommand_(sender)

def test_TextView_doCommandBySelector_():
    from editxt.textcommand import TextCommandController
    m = Mocker()
    tv = TextView.alloc().init()
    tc = m.replace("editxt.app").text_commander >> m.mock(TextCommandController)
    selector = m.mock()
    tc.do_textview_command_by_selector(tv, selector) >> True # omit super call
    with m:
        tv.doCommandBySelector_(selector)

def test_TextView_validateUserInterfaceItem_():
    from editxt.findpanel import FindController
    from editxt.textcommand import TextCommandController
    def test(c):
        m = Mocker()
        fc = m.replace("editxt.findpanel.FindController", passthrough=False)
        tv = TextView.alloc().init()
        item = m.mock(NSMenuItem)
        expectation = (item.action() << c.action)
        if c.action == "performFindPanelAction:":
            tag = item.tag() >> 42
            (fc.shared_controller() >> m.mock(FindController)). \
                validate_action(tag) >> True
        elif c.action == "performTextCommand:":
            expectation.count(2)
            tc = m.replace("editxt.app").text_commander >> m.mock(TextCommandController)
            tc.is_textview_command_enabled(tv, item) >> True
        else:
            raise NotImplementedError # left untested because I don't know how to mock a super call
        with m:
            assert tv.validateUserInterfaceItem_(item)
    c = TestConfig()
    yield test, c(action="performFindPanelAction:")
    yield test, c(action="performTextCommand:")

def test_TextView_setFrameSize():
    def test(c):
        m = Mocker()
        tv = TextView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 100)) # x, y, w, h
        tc = m.method(tv.textContainer)() >> (m.mock(NSTextContainer) if c.setup else None)
        lm = m.method(tv.layoutManager)() >> (m.mock(NSLayoutManager) if c.setup else None)
        sv = m.method(tv.enclosingScrollView)() >> (m.mock(NSScrollView) if c.setup else None)
        height = 100
        if c.setup:
            lm.usedRectForTextContainer_(tc) >> NSMakeRect(0, 0, 100, c.content_height)
            sv.contentSize() >> NSMakeSize(100, 100) # w, h
            if c.content_height + 75 > 100:
                height = c.content_height + 75
        with m:
            tv.setFrameSize_(NSMakeSize(100, height))
            eq_(tv.frameSize(), NSMakeSize(100, c.final_height))
    c = TestConfig(setup=True, content_height=100, final_height=175)
    yield test, c
    yield test, c(content_height=10, final_height=100)
    yield test, c(setup=False, final_height=100)

