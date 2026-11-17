"""Run at most `limit` jobs at a time, the rest wait in click order. No bpy; single-threaded use."""


class Scheduler:
    def __init__(self, limit=2):
        self.limit = limit
        self.active = set()
        self.waiting = {}  # key -> run, oldest first (dicts keep insertion order)

    def add(self, key, run):
        """Queue `run(finished)`; it is called when a slot is free and must call `finished()` when the job
        is over. False if `key` is already queued or running."""
        if key in self.active or self.queued(key):
            return False
        self.waiting[key] = run
        self._pump()
        return True

    def queued(self, key):
        return key in self.waiting

    def cancel(self, key):
        """Drop a job that has not started; False if it is not waiting."""
        return self.waiting.pop(key, None) is not None

    def _pump(self):
        while self.waiting and len(self.active) < self.limit:
            key = next(iter(self.waiting))
            run = self.waiting.pop(key)
            self.active.add(key)
            try:
                run(lambda key=key: self._finished(key))
            except BaseException:
                self._finished(key)
                raise

    def _finished(self, key):
        if key in self.active:
            self.active.discard(key)
            self._pump()
