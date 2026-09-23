"""musicdl adapter and cancellable, verified HTTP downloads."""
import contextlib
import html
import io
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import ssl
import sys
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = ROOT.parent.parent / 'musicdl-master'
SOURCES = {'网易云': 'NeteaseMusicClient', 'QQ音乐': 'QQMusicClient',
           '酷狗': 'KugouMusicClient', '酷我': 'KuwoMusicClient',
           '咪咕': 'MiguMusicClient', '千千': 'QianqianMusicClient',
           '街声': 'StreetVoiceMusicClient', 'ccMixter': 'CCMixterMusicClient'}
LOSSLESS = {'flac', 'wav', 'alac', 'ape', 'wv', 'tta', 'dsf', 'dff', 'aiff'}
SOURCE_PRIORITY = {'酷我': 0, '网易云': 1, '酷狗': 2, 'QQ音乐': 3,
                   '咪咕': 4, '千千': 5, '街声': 6, 'ccMixter': 7}


def lossless(song):
    return str(song.get('ext') or '').lower().lstrip('.') in LOSSLESS or str(song.get('codec') or '').lower() == 'alac'


def qq_catalog_row(item):
    """Convert a QQ search item to a visible, non-downloadable catalog row."""
    def clean_text(value):
        return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).strip()

    file_info = item.get('file') or {}
    def numeric_size(value):
        try: return int(value or 0)
        except (TypeError, ValueError): return 0
    lossless_bytes = max((numeric_size(file_info.get(key)) for key in ('size_flac', 'size_hires', 'size_new')), default=0)
    singers = ', '.join(clean_text(singer.get('name')) for singer in (item.get('singer') or []) if singer.get('name'))
    album = item.get('album') or {}
    duration = int(float(item.get('interval') or 0))
    return {
        'song_name': clean_text(item.get('title') or item.get('songname') or '未知歌曲'),
        'singers': singers, 'album': clean_text(album.get('title') or item.get('albumname')),
        'ext': '', 'codec': '', 'duration': time.strftime('%M:%S', time.gmtime(duration)),
        'file_size': f'{lossless_bytes / 1048576:.2f} MB' if lossless_bytes else '',
        'download_url': '', 'protocol': 'HTTP', 'identifier': str(item.get('mid') or item.get('songmid') or ''),
        'downloaded_contents': None, 'source': 'QQ音乐', 'downloadable': False,
        'catalog_lossless': bool(lossless_bytes),
        'requires_rights': bool((item.get('pay') or {}).get('pay_play')),
        'catalog_item': item,
    }


def qq_catalog_search(client, keyword, limit):
    """Read official QQ metadata so unresolved songs do not disappear from search."""
    rows = []
    original_source_limit, original_page_limit = client.search_size_per_source, client.search_size_per_page
    client.search_size_per_source = limit
    client.search_size_per_page = limit
    try:
        for search_meta in client._constructsearchurls(keyword, request_overrides={}):
            search_meta = dict(search_meta)
            url = search_meta.pop('url')
            search_meta.pop('page_no', None)
            response = client.post(url, **search_meta)
            items = response.json()['music.search.SearchCgiService.DoSearchForQQMusicMobile']['data']['body']['item_song']
            rows.extend(qq_catalog_row(item) for item in items)
            if len(rows) >= limit:
                break
    finally:
        client.search_size_per_source = original_source_limit
        client.search_size_per_page = original_page_limit
    return rows[:limit]


class SearchLog:
    def __init__(self):
        self.errors = 0
    def info(self, *args, **kwargs): pass
    def debug(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): self.errors += 1


def song_row(song, client, source):
    row = {key: getattr(song, key, None) for key in (
        'song_name', 'singers', 'album', 'ext', 'codec', 'duration', 'file_size',
        'download_url', 'protocol', 'identifier', 'downloaded_contents')}
    if row['protocol'] != 'HTTP' or not isinstance(row['download_url'], str):
        return None
    row['headers'] = song.default_download_headers or client.default_download_headers
    row['cookies'] = song.default_download_cookies or client.default_download_cookies
    row['source'] = source
    row['downloadable'] = True
    if source == 'QQ音乐':
        catalog = qq_catalog_row((song.raw_data or {}).get('search') or {})
        row['catalog_lossless'] = catalog['catalog_lossless']
        row['requires_rights'] = catalog['requires_rights']
    return row


def source_worker(repo, source, keyword, resolve_limit, catalog_limit, channel):
    try:
        sys.path.insert(0, repo)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from musicdl.modules import BuildMusicClient
            logger = SearchLog()
            with tempfile.TemporaryDirectory(prefix='shiyin-search-') as cache:
                client = BuildMusicClient(module_cfg=dict(type=SOURCES[source], work_dir=cache,
                    search_size_per_source=resolve_limit, search_size_per_page=resolve_limit,
                    max_retries=1, disable_print=True, logger_handle=logger))
                catalog_rows = []
                if source == 'QQ音乐':
                    try:
                        catalog_rows = qq_catalog_search(client, keyword, catalog_limit)
                        channel.put(('partial', catalog_rows, f'{len(catalog_rows)} 首目录结果 · 正在解析前 {resolve_limit} 首'))
                    except Exception:
                        logger.errors += 1
                # Many musicdl parsers set their own timeout. Passing another timeout
                # here breaks them with "multiple values for keyword argument".
                songs = client.search(keyword, num_threadings=3, request_overrides={})
                results = []
                for song in songs:
                    if not song.with_valid_download_url:
                        continue
                    if row := song_row(song, client, source): results.append(row)
                if source == 'QQ音乐':
                    resolved = {str(row.get('identifier') or ''): row for row in results}
                    merged = [resolved.pop(row['identifier'], row) for row in catalog_rows]
                    results = merged + list(resolved.values())
                downloadable = sum(row.get('downloadable', True) for row in results)
                actual_lossless = sum(lossless(row) and row.get('downloadable', True) for row in results)
                catalog_only = len(results) - downloadable
                status = f'{downloadable} 首可下载 · {actual_lossless} 首无损'
                if catalog_only: status += f' · {catalog_only} 首仅目录'
                if logger.errors: status += f' · {logger.errors} 次接口错误'
                channel.put(('done', results, status))
    except Exception as exc:
        channel.put(('done', [], f'不可用：{type(exc).__name__}: {exc}'[:220]))


def search(repo, sources, keyword, resolve_limit, cancel, emit, timeout=90, catalog_limit=None):
    ctx = mp.get_context('spawn')
    catalog_limit = catalog_limit or resolve_limit
    def run(source):
        channel = ctx.Queue()
        process = ctx.Process(target=source_worker, args=(str(repo), source, keyword, resolve_limit, catalog_limit, channel), daemon=True)
        process.start()
        deadline = time.monotonic() + timeout
        try:
            while not cancel.is_set() and time.monotonic() < deadline:
                try:
                    message, rows, status = channel.get(timeout=0.15)
                    if message == 'partial': emit('source_partial', (source, rows, status))
                    else:
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
    ordered_sources = sorted(sources, key=lambda source: SOURCE_PRIORITY.get(source, 99))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, source) for source in ordered_sources]
        for future in futures: future.result()


def resolve_worker(repo, catalog_song, channel):
    try:
        sys.path.insert(0, repo)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from musicdl.modules import BuildMusicClient, SongInfo
            logger = SearchLog()
            with tempfile.TemporaryDirectory(prefix='shiyin-resolve-') as cache:
                client = BuildMusicClient(module_cfg=dict(type='QQMusicClient', work_dir=cache,
                    search_size_per_source=1, search_size_per_page=1, max_retries=1,
                    disable_print=True, logger_handle=logger))
                item = catalog_song.get('catalog_item') or {}
                if not item: raise ValueError('目录结果缺少解析信息，请重新搜索')
                third_party = client._parsewiththirdpartapis(search_result=item, request_overrides={})
                resolved = SongInfo(source=client.source)
                with contextlib.suppress(Exception):
                    resolved = client._parsewithofficialapiv1(search_result=item, song_info_flac=third_party,
                        lossless_quality_is_sufficient=True, request_overrides={})
                resolved = resolved if resolved.with_valid_download_url else third_party
                if not resolved.with_valid_download_url: raise ValueError('QQ 当前未返回可下载直链，请改选其他音源')
                row = song_row(resolved, client, 'QQ音乐')
                if not row: raise ValueError('QQ 返回的音频协议暂不支持')
                channel.put((row, ''))
    except Exception as exc:
        channel.put((None, f'{type(exc).__name__}: {exc}'))


def resolve_catalog_song(repo, song, cancel, timeout=45):
    ctx, deadline = mp.get_context('spawn'), time.monotonic() + timeout
    channel = ctx.Queue()
    process = ctx.Process(target=resolve_worker, args=(str(repo), song, channel), daemon=True)
    process.start()
    try:
        while not cancel.is_set() and time.monotonic() < deadline:
            try:
                resolved, error = channel.get(timeout=0.15)
                if error: raise ValueError(error)
                return resolved
            except queue.Empty:
                if not process.is_alive(): raise ValueError('QQ 解析进程异常退出')
        if cancel.is_set(): raise Cancelled()
        raise TimeoutError('QQ 按需解析超过 45 秒')
    finally:
        if process.is_alive(): process.terminate()
        process.join(3)
        channel.close()


class Cancelled(Exception): pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_audio(song, request, timeout=20):
    """Open strictly, with one tightly scoped fallback for a broken Kugou CDN cert."""
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        hostname = (urllib.parse.urlsplit(request.full_url).hostname or '').lower()
        certificate_error = isinstance(exc.reason, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(exc)
        if song.get('source') != '酷狗' or hostname != 'fs.youthandroid2.kugou.com' or not certificate_error:
            raise
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        # Do not follow redirects while verification is disabled: headers and cookies
        # must only reach the exact host that triggered the known compatibility case.
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
        response = opener.open(request, timeout=timeout)
        song['_download_note'] = '已使用酷狗证书兼容模式（仅限已知 CDN，禁止跨域跳转）'
        return response


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
                with open_audio(song, urllib.request.Request(url, headers=headers), timeout=20) as response:
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
