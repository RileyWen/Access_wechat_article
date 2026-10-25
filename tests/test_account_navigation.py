import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from threading import Event
from unittest.mock import Mock, patch
from src.modules.window.ui_action_pacer import UiActionPacer, UiActionCancelled
from src.modules.window.account_navigation import (choose_account, search_result_title,
    local_account_candidates, AccountNavigationError, AccountNavigationSession, DesktopLease, OwnedPage)
from src.services.main_flow.main_flow_models import MainFlowCommand


class Clock:
    now = 0.0
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


class PacingTests(unittest.TestCase):
    def test_end_to_start_spacing_and_long_load_without_extra_wait(self):
        c = Clock(); p = UiActionPacer(clock=c.time, sleep=c.sleep)
        p.run('search', lambda: c.sleep(.3))
        p.run('open', lambda: c.sleep(.2))
        self.assertAlmostEqual(p.events[1]['started'], .8)
        c.sleep(2)
        before = c.now
        p.run('close', lambda: None)
        self.assertEqual(p.events[-1]['started'], before)

    def test_double_click_is_atomic_and_cancel_interrupts_wait(self):
        c = Clock(); p = UiActionPacer(clock=c.time, sleep=c.sleep)
        clicks = []
        def double():
            clicks.append(c.now); c.sleep(.08); clicks.append(c.now)
        p.run('double', double)
        p.cancelled = lambda: c.now >= .18
        with self.assertRaises(UiActionCancelled): p.run('business', lambda: self.fail())
        self.assertLess(c.now, .3)
        p.run('cleanup', lambda: None, cleanup=True)
        self.assertAlmostEqual(clicks[1] - clicks[0], .08)
        self.assertAlmostEqual(p.events[-1]['started'] - p.events[0]['finished'], .5)

    def test_minimum_and_non_finite_rejected(self):
        for interval in (.49, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError): UiActionPacer(interval)


def node(name='', kind='GroupControl', children=(), top=10, left=0, klass=''):
    return NS(Name=name, ControlTypeName=kind, ClassName=klass,
              BoundingRectangle=NS(top=top, left=left), GetChildren=lambda:list(children))


class IdentityTests(unittest.TestCase):
    def test_split_highlight_is_not_a_shorter_account_name(self):
        highlighted = node(children=[node('金渐成', 'TextControl')])
        suffix = node('学习笔记', 'TextControl', left=80)
        subject = node('个人', 'TextControl', top=55)
        result = node(children=[highlighted, suffix, subject])
        self.assertEqual(search_result_title(result), '金渐成学习笔记')
        self.assertIsNone(choose_account([{'name': search_result_title(result)}], '金渐成'))

    def test_ambiguity_requires_user_choice(self):
        with self.assertRaisesRegex(AccountNavigationError, '甲.*乙'):
            choose_account([{'name':'金渐成','description':'甲'}, {'name':'金渐成','description':'乙'}], '金渐成')

    def test_local_contacts_are_excluded(self):
        root = node(children=[node('联系人', klass='mmui::XTableCell'),
            node('金渐成', klass='mmui::SearchContentCellView'),
            node('公众号', klass='mmui::XTableCell'),
            node('金渐成Pro', klass='mmui::SearchContentCellView')])
        self.assertEqual([x['name'] for x in local_account_candidates(root)], ['金渐成Pro'])

    def test_api_defaults_and_automatic_validation(self):
        old = MainFlowCommand.from_payload({})
        self.assertEqual(old.account_mode, 'manual')
        self.assertTrue(old.close_home_after_task)
        with self.assertRaises(ValueError): MainFlowCommand.from_payload({'accountMode':'automatic'})
        new = MainFlowCommand.from_payload({'accountMode':'automatic','targetAccountName':'金渐成','closeHomeAfterTask':False})
        self.assertEqual(new.target_account_name,'金渐成')
        self.assertFalse(new.close_home_after_task)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.factory = NS(pacer=UiActionPacer(), article_opened=None)
        self.nav = AccountNavigationSession(self.factory,self.tmp.name,'test',Event(),Mock())

    def test_os_lease_rejects_concurrent_owner(self):
        first = DesktopLease(Path(self.tmp.name)/'lease')
        second = DesktopLease(Path(self.tmp.name)/'lease')
        first.acquire()
        try:
            with self.assertRaises(AccountNavigationError): second.acquire()
        finally: first.release()
        second.acquire(); second.release()

    def test_missing_windows_and_repeated_cleanup_are_safe(self):
        self.nav.pages = [OwnedPage(123,1,1,'金渐成','home',[1])]
        with patch.object(self.nav,'_matching_window',return_value=None), patch.object(self.nav,'_close_window') as close:
            self.assertEqual(self.nav._close_pages(), [])
            self.assertEqual(self.nav._close_pages(), [])
            close.assert_not_called()

    def test_tab_identity_never_falls_back_to_closing_shared_window(self):
        page=OwnedPage(123,1,1,'金渐成','home',[1])
        self.nav.pages=[page]
        with patch.object(self.nav,'_matching_window',return_value=NS()), patch.object(self.nav,'_close_home',side_effect=AccountNavigationError('身份不明')), patch.object(self.nav,'_close_window') as close:
            self.assertIn('身份不明', self.nav._close_pages()[0]); close.assert_not_called()

    def test_unfinished_foreground_is_journaled_and_not_closed(self):
        self.nav.started=self.nav.recovered=True
        with patch.object(self.nav,'_close_pages') as close:
            result=self.nav.finish(foreground_safe=False)
            self.assertEqual(result['status'],'blocked'); close.assert_not_called()
        self.assertEqual(json.loads(self.nav.journal.read_text())['phase'],'foreground_unverified')
        with self.assertRaises(AccountNavigationError): self.nav._recover()

    def test_failed_recovery_preserves_existing_journal(self):
        self.nav.journal.write_text('{"phase":"foreground_unverified","pages":[]}',encoding='utf-8')
        self.nav.started=True
        before=self.nav.journal.read_bytes()
        self.assertEqual(self.nav.finish()['status'],'blocked')
        self.assertEqual(self.nav.journal.read_bytes(),before)

class DocumentCleanupTests(unittest.TestCase):
    def run_cleanup(self, documents, owned_id):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        clock = Clock()
        factory = NS(pacer=UiActionPacer(clock=clock.time,sleep=clock.sleep), create_home_guard=lambda:Mock())
        nav = AccountNavigationSession(factory,temp.name,'tabs',Event(),Mock())
        current = [0]; closed=[]
        def document(_):
            if not documents: return None
            key,title=documents[current[0]]
            return NS(Name=title,GetRuntimeId=lambda:[key])
        def switch(_): current[0]=(current[0]+1)%len(documents)
        def close(_):
            closed.append(documents.pop(current[0]))
            current[0]=0
        window=NS(handle=123,control=object())
        nav.auto=NS(SendKeys=switch)
        page=OwnedPage(123,1,1,'金渐成','home',[99],[owned_id])
        with patch('src.modules.window.account_navigation.find_wechat_document_control',side_effect=document), \
             patch('src.modules.window.account_navigation._post_ctrl_w',side_effect=close), \
             patch('src.modules.window.account_navigation.time.sleep',side_effect=clock.sleep), \
             patch.object(nav,'_matching_window',side_effect=lambda p:window if documents else None):
            nav._close_home(window,page)
        return documents,closed,page

    def test_reused_tab_row_does_not_close_neighbour(self):
        remaining,closed,page=self.run_cleanup([(2,'金渐成'),(1,'金渐成Pro')],2)
        self.assertEqual(remaining,[(1,'金渐成Pro')]);self.assertEqual(len(closed),1)
        self.assertTrue(page.closed)

    def test_same_title_uses_document_identity(self):
        remaining,closed,_=self.run_cleanup([(1,'金渐成'),(2,'金渐成')],2)
        self.assertEqual(remaining,[(1,'金渐成')]);self.assertEqual(closed,[(2,'金渐成')])

    def test_previously_closed_document_is_idempotent(self):
        remaining,closed,page=self.run_cleanup([(1,'金渐成Pro')],2)
        self.assertEqual(remaining,[(1,'金渐成Pro')]);self.assertEqual(closed,[])
        self.assertTrue(page.closed)

    def test_unowned_identical_name_is_preserved(self):
        with self.assertRaisesRegex(AccountNavigationError,'同名文档'):
            self.run_cleanup([(1,'金渐成')],2)
