"""Task-owned WeChat navigation. UIA is imported only in the desktop process."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import time
from typing import Any

from .ui_action_pacer import UiActionCancelled
from .wechat_home_window_finder import enumerate_uia_windows
from .wechat_home_reader import WechatHomeReader
from .wechat_document_reader import find_wechat_document_control
from .wechat_browser_tabs import _post_ctrl_w


class AccountNavigationError(RuntimeError):
    pass


def exact_account_candidates(items, name):
    """Do not confuse highlighted name fragments with a complete account title."""
    return [item for item in items if ' '.join(item['name'].split()) == ' '.join(name.split())]


def choose_account(items, name):
    matches = exact_account_candidates(items, name)
    if len(matches) > 1:
        raise AccountNavigationError('公众号同名，无法唯一确定：' + '；'.join(item.get('description', item['name']) for item in matches))
    return matches[0] if matches else None


def search_result_title(control):
    # WeChat exposes highlighted fragments and suffixes as sibling nodes.
    # Gather the entire first text line, excluding entity/description below.
    leaves = [c for c in nodes(control, depth=6, limit=150)
              if c.ControlTypeName == 'TextControl' and not c.GetChildren() and c.Name]
    if not leaves:
        return ''
    first = min(leaves, key=lambda c: (c.BoundingRectangle.top, c.BoundingRectangle.left))
    top = first.BoundingRectangle.top
    line = [c for c in leaves if abs(c.BoundingRectangle.top - top) <= 4]
    return ''.join(c.Name for c in sorted(line, key=lambda c: c.BoundingRectangle.left)).strip()


def local_account_candidates(popover):
    # SearchContentCellView is also used for contacts. Only the 公众号 section
    # can supply an account candidate; other categories trigger web search.
    result = []
    def visit(root):
        section = ''
        for c in root.GetChildren():
            if c.ClassName == 'mmui::XTableCell' and c.Name:
                section = c.Name
            elif c.ClassName == 'mmui::SearchContentCellView':
                if section == '公众号': result.append({'name': c.Name, 'control': c})
            else:
                visit(c)
    visit(popover)
    return result


def nodes(root, depth=24, limit=3000, *, chrome_only=False):
    queue = deque([(root, 0)])
    count = 0
    while queue and count < limit:
        control, level = queue.popleft()
        count += 1
        yield control
        if level < depth and not (chrome_only and control.ControlTypeName == 'DocumentControl'):
            try:
                queue.extend((c, level + 1) for c in control.GetChildren())
            except Exception:
                pass


def runtime_id(control):
    try:
        return list(control.GetRuntimeId())
    except Exception:
        return []


def valid_rect(control):
    try:
        r = control.BoundingRectangle
        return r.right > r.left and r.bottom > r.top and not control.IsOffscreen
    except Exception:
        return False


def active_tab_row(root, title):
    """Qt/Chromium tabs may be Groups rather than TabItems. Require an exact title and close control; WeChat can report zero-sized tab rectangles."""
    matches = []
    for c in nodes(root, depth=17, limit=1800, chrome_only=True):
        if c.ControlTypeName == 'DocumentControl':
            continue
        try: children = c.GetChildren()
        except Exception: continue
        if any(ch.Name == title and ch.ControlTypeName == 'TextControl' for ch in children) and any(
            ch.Name in ('关闭', '关闭标签页') and ch.ControlTypeName == 'ButtonControl' for ch in children
        ):
            matches.append(c)
    if not matches: return None
    if len(matches) != 1:
        raise AccountNavigationError('无法唯一识别当前主页标签，保留页面以免误关')
    return matches[0]


@dataclass
class OwnedPage:
    handle: int
    pid: int
    created: float
    title: str
    role: str
    tab_runtime_id: list[int]


class DesktopLease:
    """OS-held lock releases on crash; the journal survives for verified recovery."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0)
        if self.path.stat().st_size == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close(); self.file = None
            raise AccountNavigationError('已有任务正在操作或清理微信桌面') from exc

    def release(self):
        if self.file:
            self.file.close(); self.file = None


class AccountNavigationSession:
    def __init__(self, factory, directory, task_id, cancel_token, state):
        self.factory, self.pacer = factory, factory.pacer
        self.directory = Path(directory)
        self.task_id, self.cancel_token, self.state = task_id, cancel_token, state
        self.pages: list[OwnedPage] = []
        self.home = None
        self.empty_home = False
        self.baseline_handles = set()
        self.lease = DesktopLease(self.directory / 'desktop.lock')
        self.journal = self.directory / 'session.json'
        self.started = False
        self.recovered = False
        self.route = ''
        self.ownership_errors = []

    def _save(self, phase, **extra):
        import psutil
        payload = {'task_id': self.task_id, 'pid': os.getpid(), 'created': psutil.Process().create_time(),
                   'phase': phase, 'pages': [asdict(p) for p in self.pages], **extra}
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.journal.with_suffix('.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.journal)

    def _action(self, text):
        self.state.set_action(text)

    def _wait(self, probe, description, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancel_token.is_set():
                raise UiActionCancelled('导航已取消')
            try:
                result = probe()
            except Exception as exc:
                # Chromium replaces its UIA tree while navigating. Retrying
                # readonly probes is safe; never automatically replay a click.
                if type(exc).__name__ != 'COMError': raise
                result = None
            if result:
                return result
            self.cancel_token.wait(.2)
        raise AccountNavigationError(description)

    def _click(self, control, label, *, double=False):
        if not valid_rect(control):
            raise AccountNavigationError('控件不可见：' + label)
        return self.pacer.run(label, lambda: (control.DoubleClick(simulateMove=False) if double else control.Click(simulateMove=False)))

    def _control(self, root, kind, name='', class_name='', timeout=8):
        def probe():
            kwargs = {'searchDepth': 25}
            if name: kwargs['Name'] = name
            if class_name: kwargs['ClassName'] = class_name
            c = getattr(root, kind)(**kwargs)
            return c if c.Exists(.05) and valid_rect(c) else None
        return self._wait(probe, '未找到可操作控件：' + (name or class_name or kind), timeout)

    def _register(self, control, title, role, *, tab=False):
        import psutil
        owner = control
        if tab:
            row = self._wait(lambda: active_tab_row(control, title), "未加载可核验的标签栏", 5)
            tab_id = runtime_id(row)
            if not tab_id:
                raise AccountNavigationError('公众号标签没有可核验的身份')
        else:
            tab_id = []
        page = OwnedPage(owner.NativeWindowHandle, owner.ProcessId, psutil.Process(owner.ProcessId).create_time(), title, role, tab_id)
        if page not in self.pages:
            self.pages.append(page)
            self._save('navigating')
        return page

    def _home_candidates(self, name):
        result = []
        for w in enumerate_uia_windows():
            if w.process_name.lower() != 'wechatappex.exe' or not w.visible or w.is_minimized:
                continue
            try:
                if WechatHomeReader().read(w).account_name == name:
                    doc = find_wechat_document_control(w.control)
                    # Search documents can contain the name; a home must expose its tabs.
                    if (doc.HyperlinkControl(searchDepth=20, Name='全部').Exists(.05)
                            or (doc.ButtonControl(searchDepth=20, Name='私信').Exists(.05)
                                and doc.TextControl(searchDepth=20, Name=name).Exists(.05))):
                        result.append(w)
            except Exception:
                continue
        if len(result) > 1:
            raise AccountNavigationError('存在多个同名公众号主页，请保留一个后重试')
        return result[0] if result else None

    def open(self, name, mode):
        import uiautomation as auto
        import ctypes
        self.auto = auto
        # UIA coordinates are physical pixels; ensure the worker thread agrees.
        try: ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception: pass
        self.lease.acquire()
        self.started = True
        self._recover()
        self.recovered = True
        self.factory.article_opened = self.register_article
        self.pacer.cancelled = self.cancel_token.is_set
        self.pacer.events.clear()
        self.baseline_handles = {w.handle for w in enumerate_uia_windows()}
        self._save('navigating')
        if mode == 'manual':
            self.home = self.factory.find_home_window(reader=self.factory.create_reader())
            if self.home is None:
                raise AccountNavigationError('未找到公众号主页，请先打开主页')
            self.factory.create_home_guard().activate(self.home)
            actual = WechatHomeReader().read(self.home).account_name
            if name and actual != name:
                raise AccountNavigationError('当前主页与目标公众号不一致：' + actual)
            name = actual
            self.route = 'manual'
        else:
            self._action('核验目标公众号主页')
            self.home = self._home_candidates(name)
            if self.home is None:
                self._navigate(name)
                self.home = self._wait(lambda: self._home_candidates(name), '公众号主页加载超时或身份不符', 20)
            else:
                self.route = 'existing'
        self.factory.create_home_guard().activate(self.home)
        self._register(self.home.control, name, 'home', tab=True)
        self._action('准备公众号全部列表')
        doc = find_wechat_document_control(self.home.control)
        all_tab = doc.HyperlinkControl(searchDepth=20, Name='全部')
        if not all_tab.Exists(.1):
            text = WechatHomeReader().read(self.home).document_text
            if '已无更多消息' not in text:
                raise AccountNavigationError('公众号主页没有可采集的全部列表')
            self.empty_home = True
        else:
            self._click(all_tab, '切换全部')
            self._to_top(name)
        self._save('ready', account=name, route=self.route)
        return self.home

    def _navigate(self, name):
        main = self.auto.WindowControl(searchDepth=1, ClassName='mmui::MainWindow')
        if not main.Exists(.3):
            raise AccountNavigationError('微信未登录或主窗口不可用，请登录微信后重试')
        import ctypes
        ctypes.windll.user32.ShowWindow(main.NativeWindowHandle, 9)
        main.SetActive()
        self._action('搜索公众号：' + name)
        self._click(self._control(main, 'ButtonControl', '微信', 'mmui::XTabBarItem'), '切换微信会话')
        edit = self._control(main, 'EditControl', '搜索', 'mmui::XValidatorTextEdit')
        self._click(edit, '聚焦公众号搜索')
        self.pacer.run('输入公众号名称', lambda: edit.GetValuePattern().SetValue(name))
        # Wait for results to settle before declaring a local miss.
        result = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.cancel_token.wait(.2): raise UiActionCancelled('导航已取消')
            pop = main.WindowControl(searchDepth=3, ClassName='mmui::SearchContentPopover')
            if not pop.Exists(.05): continue
            candidates = local_account_candidates(pop)
            result = choose_account(candidates, name)
            if result: break
        if result:
            before = {c.NativeWindowHandle for c in self.auto.GetRootControl().GetChildren()}
            self._click(result['control'], '选择本地公众号')
            chat = self._wait(lambda: self._chat(name), '未打开目标公众号会话')
            if chat.NativeWindowHandle not in before: self._register(chat, name, 'chat')
            chat.SetActive()
            self._click(self._control(chat, 'ButtonControl', '公众号主页'), '打开公众号主页')
            self.route = 'local'
            return
        self._global_search(main, name)
        self.route = 'search'

    def _chat(self, name):
        chat = self.auto.WindowControl(searchDepth=1, ClassName='mmui::ChatSingleWindow', Name=name)
        return chat if chat.Exists(.05) else None

    def _global_search(self, main, name):
        self._action('通过搜一搜查找公众号')
        self.pacer.run('清空本地搜索', lambda: main.EditControl(searchDepth=25, Name='搜索').GetValuePattern().SetValue(''))
        self._click(self._control(main, 'ButtonControl', '发现', 'mmui::XTabBarItem'), '打开发现')
        baseline = {c.NativeWindowHandle for c in self.auto.GetRootControl().GetChildren()}
        self._click(self._control(main, 'ButtonControl', '搜一搜', 'mmui::ExtensionDiscoverContentCell'), '打开搜一搜', double=True)
        def search_document():
            for w in enumerate_uia_windows():
                if w.process_name.lower() != 'wechatappex.exe' or not w.visible: continue
                doc = w.control.DocumentControl(searchDepth=15, Name='搜一搜')
                if doc.Exists(.05): return w, doc
            return None
        w, doc = self._wait(search_document, '搜一搜加载失败，请检查网络或验证提示', 20)
        self.factory.create_home_guard().activate(w)
        search_page = self._register(w.control, '搜一搜', 'search', tab=True)
        self._click(self._control(doc, 'ButtonControl', '公众号'), '选择公众号分类')
        edit = self._control(doc, 'EditControl')
        self._click(edit, '聚焦网络搜索')
        self.pacer.run('输入网络搜索词', lambda: edit.GetValuePattern().SetValue(name))
        self.pacer.run('提交公众号搜索', lambda: edit.SendKeys('{Enter}'))
        def result_probe():
            root = self.auto.ControlFromHandle(w.handle)
            candidates = []
            for c in nodes(root, depth=28, limit=3500):
                if c.ControlTypeName != 'ButtonControl' or c.ClassName != 'header-detail': continue
                # The title container contains highlight fragments; join its text
                # leaves, never match the first highlighted TextControl alone.
                title = search_result_title(c)
                candidates.append({'name': title, 'description': c.Name[:300], 'control': c})
            selected = choose_account(candidates, name)
            return selected
        selected = self._wait(result_probe, '未找到完整名称匹配的公众号；请检查账号名称或微信验证提示', 20)
        search_page.title = find_wechat_document_control(self.auto.ControlFromHandle(w.handle)).Name
        self._save('navigating')
        self._click(selected['control'], '选择网络公众号')

    def _to_top(self, name):
        reader = self.factory.create_window_test_reader()
        scroller = self.factory.create_scroller()
        previous = None
        stable = 0
        for _ in range(30):
            if self.cancel_token.is_set(): raise UiActionCancelled('导航已取消')
            doc = find_wechat_document_control(self.home.control)
            if doc is None: raise AccountNavigationError('公众号文档不可读')
            header = doc.TextControl(searchDepth=20, Name=name)
            header_visible = header.Exists(.05) and valid_rect(header)
            try:
                snapshot = reader.read(self.home)
                signature = tuple((c.date_text, c.title, c.card_rect) for c in snapshot.all_cards[:2])
            except RuntimeError:
                signature = None
            if header_visible and signature and signature == previous:
                stable += 1
                if stable >= 2: return
            else: stable = 0
            if header_visible and not signature:
                text = WechatHomeReader().read(self.home).document_text
                if any(marker in text for marker in ('暂无内容', '暂无文章', '该账号暂无文章')): return
            previous = signature
            scroller.scroll_up(self.home, visible_targets=[])
            self.cancel_token.wait(.2)
        raise AccountNavigationError('无法确认公众号已回到顶部并加载文章列表')

    def _recover(self):
        if not self.journal.exists(): return
        import psutil
        old = json.loads(self.journal.read_text(encoding='utf-8'))
        if old.get('phase') in ('closed', 'kept'): return
        if old.get('phase') == 'foreground_unverified':
            try:
                if psutil.Process(old['pid']).create_time() == old['created']:
                    raise AccountNavigationError('上轮前台仍未确认停止，请等待或重启采集器后重试')
            except psutil.NoSuchProcess: pass
        self.pages = [OwnedPage(**p) for p in old.get('pages', [])]
        errors = self._close_pages()
        if errors: raise AccountNavigationError('上轮页面待清理：' + '；'.join(errors))
        self.pages.clear()

    def register_article(self, tab):
        owner = self.auto.ControlFromHandle(tab.owner_handle)
        try:
            self._register(owner, tab.title, 'article', tab=True)
        except Exception as exc:
            self.ownership_errors.append('文章标签登记失败：' + str(exc))

    def _matching_window(self, page):
        import psutil
        for w in enumerate_uia_windows():
            if w.handle != page.handle: continue
            try:
                if w.control.ProcessId != page.pid or psutil.Process(page.pid).create_time() != page.created:
                    return None  # The original process/window is gone; never act on a recycled handle.
            except psutil.NoSuchProcess: return None
            return w
        return None

    def _close_pages(self):
        errors = []
        for page in reversed(self.pages):
            try:
                w = self._matching_window(page)
                if w is None: continue
                if page.tab_runtime_id:
                    self._close_home(w, page)
                else:
                    if page.role == 'unverified':
                        raise AccountNavigationError('新增页面归属未核验，保留页面')
                    # A search host may have gained an unrelated tab. Close only
                    # when it still contains a search document; otherwise preserve.
                    if page.role == 'search':
                        doc = find_wechat_document_control(w.control)
                        if doc is None or '搜一搜' not in doc.Name:
                            raise AccountNavigationError('搜索窗口内容已变化，保留窗口')
                    elif w.title != page.title or w.class_name != 'mmui::ChatSingleWindow':
                        raise AccountNavigationError('公众号会话窗口身份已变化')
                    self.pacer.run('关闭' + page.role, lambda: self._close_window(w.handle), cleanup=True)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and self._matching_window(page) is not None: time.sleep(.1)
                    if self._matching_window(page) is not None: raise AccountNavigationError('窗口尚未关闭')
            except Exception as exc:
                errors.append(f'{page.role}: {exc}')
        return errors

    def _close_home(self, window, page):
        self.factory.create_home_guard().activate(window)
        matches = [c for c in nodes(window.control, depth=18, limit=2000, chrome_only=True) if runtime_id(c) == page.tab_runtime_id]
        if not matches: return  # Exact registered tab has already gone.
        row = matches[0]
        if page.role == 'search':
            labels = [c.Name for c in row.GetChildren() if c.ControlTypeName == 'TextControl']
            if len(labels) != 1 or '搜一搜' not in labels[0]:
                raise AccountNavigationError('搜索标签身份发生变化')
            page.title = labels[0]
        doc = find_wechat_document_control(window.control)
        if doc is None or doc.Name != page.title:
            if valid_rect(row):
                self.pacer.run('激活待关闭标签', lambda: row.Click(simulateMove=False), cleanup=True)
            else:
                # WeChat 4.1 may expose zero rectangles for every tab. Cycle
                # keyboard focus, checking document identity after each action.
                for _ in range(20):
                    self.pacer.run('切换待关闭标签', lambda: self.auto.SendKeys('{Ctrl}{Tab}'), cleanup=True)
                    time.sleep(.15)
                    doc = find_wechat_document_control(window.control)
                    if doc is not None and doc.Name == page.title: break
            time.sleep(.2)
            doc = find_wechat_document_control(window.control)
        if doc is None or doc.Name != page.title or runtime_id(active_tab_row(window.control, page.title)) != page.tab_runtime_id:
            raise AccountNavigationError('当前活动标签不是本轮主页，保留页面')
        self.pacer.run('关闭公众号主页', lambda: _post_ctrl_w(page.handle), cleanup=True)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            current = self._matching_window(page)
            if current is None or not any(runtime_id(c) == page.tab_runtime_id for c in nodes(current.control, depth=18, limit=2000, chrome_only=True)): return
            time.sleep(.1)
        raise AccountNavigationError('主页标签关闭未确认')

    @staticmethod
    def _close_window(handle):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        if not user32.PostMessageW(handle, 0x0010, 0, 0):
            raise AccountNavigationError('关闭窗口消息发送失败')

    def finish(self, *, close=True, foreground_safe=True):
        if not self.started: return {'status': 'not_needed', 'errors': []}
        try:
            if not self.recovered:
                return {'status': 'blocked', 'errors': ['上轮会话尚未核验，保留原记录和页面']}
            if not foreground_safe:
                self._save('foreground_unverified')
                return {'status': 'blocked', 'errors': ['前台工作线程尚未退出，未关闭主页']}
            # A timeout can occur after opening a window but before its document
            # becomes identifiable. Keep an explicit unresolved record, not a
            # misleading successful cleanup result.
            owned_handles = {page.handle for page in self.pages}
            if self.baseline_handles:
                for window in enumerate_uia_windows():
                    if window.process_name.lower() == 'wechatappex.exe' and window.handle not in self.baseline_handles | owned_handles:
                        self._register(window.control, window.title, 'unverified')
            errors = (self._close_pages() + self.ownership_errors) if close else []
            self._save('cleanup_failed' if errors else ('closed' if close else 'kept'))
            return {'status': 'failed' if errors else ('complete' if close else 'kept'), 'errors': errors}
        finally:
            try:
                (self.directory / f'{self.task_id}-actions.json').write_text(json.dumps(list(self.pacer.events), ensure_ascii=False, indent=2), encoding='utf-8')
            finally:
                if foreground_safe and self.recovered:
                    self.pacer.cancelled = lambda: False
                    self.factory.article_opened = None
                self.lease.release()
