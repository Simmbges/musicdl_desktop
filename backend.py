"""musicdl adapter and cancellable, verified HTTP downloads."""
import contextlib
import io
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = ROOT.parent.parent / 'musicdl-master'
SOURCES = {'网易云': 'NeteaseMusicClient', 'QQ音乐': 'QQMusicClient',
           '酷狗': 'KugouMusicClient', '酷我': 'KuwoMusicClient',
           '咪咕': 'MiguMusicClient', '千千': 'QianqianMusicClient',
           '街声': 'StreetVoiceMusicClient', 'ccMixter': 'CCMixterMusicClient'}
LOSSLESS = {'flac', 'wav', 'alac', 'ape', 'wv', 'tta', 'dsf', 'dff', 'aiff'}


def lossless(song):
    return str(song.get('ext') or '').lower().lstrip('.') in LOSSLESS or str(song.get('codec') or '').lower() == 'alac'


class SearchLog:
    def __init__(self):
        self.errors = 0
    def info(self, *args, **kwargs): pass
    def debug(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): self.errors += 1


def source_worker(repo, source, keyword, limit, channel):
    try:
        sys.path.insert(0, repo)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from musicdl.modules import BuildMusicClient
            logger = SearchLog()
            with tempfile.TemporaryDirectory(prefix='shiyin-search-') as cache:
                client = BuildMusicClient(module_cfg=dict(type=SOURCES[source], work_dir=cache,
                    search_size_per_source=limit, search_size_per_page=limit,
                    max_retries=1, disable_print=True, logger_handle=logger))
                songs = client.search(keyword, num_threadings=3, request_overrides={'timeout': (8, 15)})
                results = []
                for song in songs:
                    if not song.with_valid_download_url:
                        continue
                    row = {key: getattr(song, key, None) for key in (
                        'song_name', 'singers', 'album', 'ext', 'codec', 'duration', 'file_size',
                        'download_url', 'protocol', 'identifier', 'downloaded_contents')}
                    if row['protocol'] != 'HTTP' or not isinstance(row['download_url'], str):
                        continue
                    row['headers'] = song.default_download_headers or client.default_download_headers
                    row['cookies'] = song.default_download_cookies or client.default_download_cookies
                    row['source'] = source
                    results.append(row)
                channel.put((results, f'{len(results)} 首' + (f' · {logger.errors} 次接口错误' if logger.errors else '')))
    except Exception as exc:
        channel.put(([], f'不可用：{type(exc).__name__}: {exc}'[:220]))


def search(repo, sources, keyword, limit, cancel, emit, timeout=90):
    ctx = mp.get_context('spawn')
    def run(source):
        channel = ctx.Queue()
        process = ctx.Process(target=source_worker, args=(str(repo), source, keyword, limit, channel), daemon=True)
        process.start()
        deadline = time.monotonic() + timeout
        try:
            while not cancel.is_set() and time.monotonic() < deadline:
                try:
                    rows, status = channel.get(timeout=0.15)
                    emit('source', (source, rows, status))
                    return
                except queue.Empty:
                    if not process.is_alive():
                        emit('source', (source, [], '搜索进程退出，检查依赖'))
                        return
            emit('source', (source, [], '已取消' if cancel.is_set() else '超时，请稍后重试'))
        finally:
            if process.is_alive(): process.terminate()
            process.join(3)
            channel.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, source) for source in sources]
        for future in futures: future.result()


class Cancelled(Exception): pass


def filename(song):
    name = f"{song.get('song_name') or '未命名'} - {song.get('singers') or '未知歌手'} [{song['source']}]"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:140]
    ext = re.sub('[^a-z0-9]', '', str(song.get('ext') or '').lower())
    if not ext: raise ValueError('来源未提供音频格式')
    return name + '.' + ext


def download(song, directory, cancel, progress):
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename(song)
    fd, temporary = tempfile.mkstemp(prefix='.shiyin-', suffix='.' + target.suffix.lstrip('.'), dir=directory)
    try:
        with os.fdopen(fd, 'wb') as output:
            if cancel.is_set(): raise Cancelled()
            if song.get('downloaded_contents'):
                content = song['downloaded_contents']
                output.write(content)
                progress(len(content), len(content))
            else:
                url = str(song.get('download_url') or '')
                if not url.startswith(('http://', 'https://')): raise ValueError('无可用下载链接，请重新搜索')
                headers = dict(song.get('headers') or {})
                cookies = song.get('cookies') or {}
                if cookies: headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
                headers['Accept-Encoding'] = 'identity'
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
                    total = int(response.headers.get('Content-Length') or 0)
                    size = 0
                    while True:
                        if cancel.is_set(): raise Cancelled()
                        chunk = response.read(64 * 1024)
                        if not chunk: break
                        output.write(chunk)
                        size += len(chunk)
                        progress(size, total)
                    if not size or (total and size != total): raise ValueError('下载不完整，请重试')
        if cancel.is_set(): raise Cancelled()
        from mutagen import File
        audio = File(temporary)
        if audio is None or audio.info.length <= 0: raise ValueError('返回的文件不是有效音频')
        # Check the actual parsed container, not merely the advertised suffix.
        actual_lossless = type(audio).__name__ in {'FLAC', 'WAVE', 'AIFF', 'MonkeysAudio', 'WavPack', 'TrueAudio', 'DSF', 'DSDIFF'} or getattr(audio.info, 'codec', '') == 'alac'
        if lossless(song) and not actual_lossless: raise ValueError('实际音频与来源标注的无损格式不一致')
        for number in range(10000):
            destination = target if number == 0 else target.with_stem(target.stem + f' ({number})')
            try:
                # On Windows rename never overwrites an existing file.
                os.rename(temporary, destination)
                return destination
            except FileExistsError:
                continue
        raise ValueError('同名文件过多')
    finally:
        Path(temporary).unlink(missing_ok=True)
