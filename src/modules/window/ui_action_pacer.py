"""One clock for all desktop actions in the embedded foreground workers."""
from __future__ import annotations

from collections import deque
from threading import RLock
import math
import time
from typing import Callable, Any


class UiActionCancelled(RuntimeError):
    pass


class UiActionPacer:
    def __init__(self, interval: float = .5, *, clock=time.monotonic, sleep=time.sleep):
        if not math.isfinite(interval) or interval < .5:
            raise ValueError('界面操作间隔不能小于 0.5 秒')
        self.interval = interval
        self.clock, self.sleep = clock, sleep
        self.lock = RLock()
        self.last_finished: float | None = None
        self.cancelled: Callable[[], bool] = lambda: False
        self.events: deque[dict[str, Any]] = deque(maxlen=2000)

    def run(self, label: str, action: Callable[[], Any], *, cleanup=False):
        with self.lock:
            while True:
                if not cleanup and self.cancelled():
                    raise UiActionCancelled('界面操作已取消')
                delay = 0 if self.last_finished is None else self.last_finished + self.interval - self.clock()
                if delay <= 0:
                    break
                self.sleep(min(delay, .05))
            started = self.clock()
            try:
                return action()
            finally:
                self.last_finished = self.clock()
                self.events.append({'action': label, 'started': started, 'finished': self.last_finished})


class PacedComponent:
    """Wrap only entry operations; internal double clicks/wheel ticks stay atomic."""
    def __init__(self, component, pacer, methods, cleanup_methods=()):
        self._component, self._pacer = component, pacer
        self._methods, self._cleanup_methods = set(methods), set(cleanup_methods)

    def __getattr__(self, name):
        value = getattr(self._component, name)
        if name not in self._methods:
            return value
        def call(*args, **kwargs):
            return self._pacer.run(name, lambda: value(*args, **kwargs), cleanup=name in self._cleanup_methods)
        return call


class ObservedTabService(PacedComponent):
    def __init__(self, component, pacer, factory):
        super().__init__(component, pacer, ('close_article_tab',), ('close_article_tab',))
        self._factory = factory

    def __getattr__(self, name):
        value = super().__getattr__(name)
        if name not in ('wait_for_article_tab', 'wait_for_opened_article_tab'):
            return value
        def call(*args, **kwargs):
            tab = value(*args, **kwargs)
            callback = self._factory.article_opened
            if callback is not None:
                callback(tab)
            return tab
        return call
