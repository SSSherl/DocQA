"""Durable, process-isolated imports. Job files are private runtime data, never release assets."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 19:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.02)


def read_json(path):
    # Windows can briefly deny opens while another process atomically replaces a file.
    # Bound the wait so progress/health requests remain responsive.
    for attempt in range(20):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except (PermissionError, FileNotFoundError, json.JSONDecodeError):
            if attempt == 19:
                raise
            time.sleep(0.02)


def alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def paths(folder, state):
    # Both paths derive from a validated basename, never from browser-supplied absolute paths.
    name = state['name']
    if Path(name).name != name or any(c in name for c in '/\\:'):
        raise ValueError('无效文件名')
    return folder/'original'/name, folder.parent.parent/'uploads'/name


def committed(folder, state):
    from contextlib import closing
    dbpath = folder.parent.parent/'docqa.sqlite3'
    if not dbpath.exists():
        return None
    _, target = paths(folder, state)
    with closing(sqlite3.connect(dbpath, timeout=2)) as db:
        row = db.execute('SELECT id,hash,fingerprint,warnings FROM documents WHERE source=?', (str(target.resolve()),)).fetchone()
        if row and row[1] == state['digest'] and row[2] == state['fingerprint']:
            return {'id':row[0], 'status':'indexed', 'chunks':db.execute('SELECT count(*) FROM chunks WHERE doc_id=?', (row[0],)).fetchone()[0], 'warnings':json.loads(row[3])}
    return None


def restore_original(folder, state):
    _, target = paths(folder, state)
    backup = folder/'previous.bin'
    if state.get('had_previous') and backup.exists():
        shutil.copy2(backup, target)
    # Failed incoming files stay under original/name for retry; existing sources are unchanged.


class ImportJobs:
    def __init__(self, base):
        self.base = base
        self.root = base.root
        self.pointer = self.root/'import-job.json'
        self.process = None
        self.folder = None

    def status(self):
        if not self.pointer.exists():
            return {'status':'idle'}
        job_id = read_json(self.pointer)['id']
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('无效导入任务')
        folder = self.root/'import-jobs'/job_id
        state = read_json(folder/'state.json')
        if self.process is not None:
            self.process.poll()  # Reap completed children; avoid process-handle leaks.
        running = state['status'] in {'queued', 'running'}
        owned = self.folder == folder and self.process is not None and self.process.poll() is None
        if running and not owned and not alive(state.get('pid')):
            result = committed(folder, state)
            if result:
                state.update(status='complete', stage='入库完成', result=result)
            else:
                restore_original(folder, state)
                state.update(status='failed', error='导入进程已中断；原文件和旧索引已保留，可点击重试导入')
            state['finished_at'] = time.time()
            write_json(folder/'state.json', state)
        output = {k:v for k,v in state.items() if k in {'status','name','category','stage','completed','total','started_at','updated_at','finished_at','result','error','error_type'}}
        if output['status'] == 'queued':
            output['status'] = 'running'
        output['source_retained'] = paths(folder, state)[0].is_file()
        output['elapsed_seconds'] = round(state.get('finished_at',time.time())-state['started_at'],1)
        return output

    def start(self, name, raw, category):
        if self.status()['status'] == 'running':
            raise ValueError('已有文档正在入库，请等待当前任务完成')
        job_id = uuid.uuid4().hex
        folder = self.root/'import-jobs'/job_id
        (folder/'original').mkdir(parents=True)
        state = {'status':'queued','name':name,'category':category,'stage':'等待独立导入进程','completed':0,'total':0,'started_at':time.time(),
                 'digest':hashlib.sha256(raw).hexdigest(),'fingerprint':self.base.fingerprint()}
        original, target = paths(folder, state)
        target.parent.mkdir(exist_ok=True)
        original.write_bytes(raw)
        state['had_previous'] = target.exists()
        if target.exists():
            shutil.copy2(target, folder/'previous.bin')
        cfg = dict(self.base.cfg, data_dir=str(self.root))
        # Runtime snapshots reference existing credential stores/env names; no resolved key is copied.
        write_json(folder/'config.json',cfg)
        write_json(folder/'state.json',state)
        write_json(self.pointer, {'id':job_id})
        try:
            self.process = subprocess.Popen([sys.executable,'-m','docqa.import_jobs',str(folder)],
                                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.folder = folder
        except Exception:
            state.update(status='failed',error='无法启动导入进程；原文件已保留，可重试',finished_at=time.time())
            write_json(folder/'state.json',state)
            raise ValueError(state['error']) from None
        return {'status':'running'}

    def retry(self):
        state = self.status()
        if state['status'] != 'failed' or not state.get('source_retained'):
            raise ValueError('当前没有可重试的失败导入')
        job_id = read_json(self.pointer)['id']
        original = self.root/'import-jobs'/job_id/'original'/state['name']
        return self.start(state['name'],original.read_bytes(),state.get('category') or '默认')


def worker(folder):
    from .docbase import DocBase
    from .models import ModelError
    state = read_json(folder/'state.json')
    state.update(status='running',pid=os.getpid(),updated_at=time.time())
    write_json(folder/'state.json',state)
    def progress(stage, done, total):
        state.update(stage=stage,completed=done,total=total,updated_at=time.time())
        write_json(folder/'state.json',state)
    try:
        cfg = read_json(folder/'config.json')
        base = DocBase(cfg)
        original, target = paths(folder,state)
        result = base.build(target,state['category'],progress=progress,input_path=original)
        state.update(status='complete',stage='入库完成',result=result)
    except Exception as exc:
        restore_original(folder,state)
        message = str(exc) if isinstance(exc,(ModelError,ValueError)) else '入库失败，请检查原文件和模型服务'
        state.update(status='failed',error=message+'；原文件和旧索引已保留，重试会复用已完成的模型处理结果',error_type=type(exc).__name__)
    finally:
        state.update(finished_at=time.time(),updated_at=time.time())
        write_json(folder/'state.json',state)
        (folder/'config.json').unlink(missing_ok=True)
        if state['status'] == 'complete':
            original, _ = paths(folder, state)
            original.unlink(missing_ok=True)
            (folder/'previous.bin').unlink(missing_ok=True)


if __name__ == '__main__':
    worker(Path(sys.argv[1]).resolve())
