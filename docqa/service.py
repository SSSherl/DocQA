import threading
import time
from contextlib import nullcontext
from pathlib import Path
from .doc2md import SUPPORTED


class Watcher:
    def __init__(self, base):
        self.base = base
        self.stop = threading.Event()
        self.events = []
        self.build_lock = None

    def run(self):
        directory = Path(self.base.cfg.get("watch_dir", ""))
        if not self.base.cfg.get("watch_dir") or not directory.is_dir():
            raise ValueError("请配置存在的 watch_dir 目录")
        data = self.base.root
        if directory.resolve().is_relative_to(data) or data.is_relative_to(directory.resolve()):
            raise ValueError("监控目录与 data_dir 不能互相包含")
        while not self.stop.is_set():
            for file in sorted(directory.rglob("*")):
                if self.stop.is_set():
                    break
                if file.is_file() and file.suffix.lower() in SUPPORTED:
                    try:
                        with self.build_lock or nullcontext():
                            result = self.base.build(file, file.parent.name)
                        if result["status"] != "unchanged":
                            self.events.append({"file":file.name,"status":"indexed","time":time.time()})
                    except Exception as exc:
                        self.events.append({"file":file.name,"status":"error","error":str(exc),"time":time.time()})
                    self.events = self.events[-100:]
            self.stop.wait(max(2, self.base.cfg.get("watch_interval", 30)))
