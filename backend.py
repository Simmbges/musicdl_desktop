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

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
DEFAULT_REPO = Path(sys._MEIPASS) / 'musicdl-master' if getattr(sys, 'frozen', False) else ROOT.parent.parent / 'musicdl-master'
SOURCES = {'网易云': 'NeteaseMusicClient', 'QQ音乐': 'QQMusicClient',
           '酷狗': 'KugouMusicClient', '酷我': 'KuwoMusicClient',
           '咪咕': 'MiguMusicClient', '千千': 'QianqianMusicClient',
           '街声': 'StreetVoiceMusicClient', 'ccMixter': 'CCMixterMusicClient'}
LOSSLESS = {'flac', 'wav', 'alac', 'ape', 'wv', 'tta', 'dsf', 'dff', 'aiff'}
SOURCE_PRIORITY = {'酷我': 0, '网易云': 1, '酷狗': 2, 'QQ音乐': 3,
                   '咪咕': 4, '千千': 5, '街声': 6, 'ccMixter': 7}


def lossless(song):
    return str(song.get('ext') or '').lower().lstrip('.') in LOSSLESS or str(song.get('codec') or '').lower() == 'alac'


def clean_text(value):
    return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).strip()


def numeric_size(value):
    if isinstance(value, (list, tuple)):
        return max((numeric_size(item) for item in value), default=0)
    try:
        text = str(value or 0).strip()
        if text.upper().endswith('MB'): return int(float(text[:-2].strip()) * 1048576)
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def names(items):
    return ', '.join(clean_text(item.get('name')) for item in (items or []) if isinstance(item, dict) and item.get('name'))


def catalog_row(source, item):
    """Convert one source search item to visible metadata without a download URL."""
    title = singers = album = identifier = catalog_format = ''
    duration = lossless_bytes = 0
    catalog_lossless = requires_rights = False
    if source == '网易云':
        title, singers = item.get('name'), names(item.get('ar') or item.get('artists'))
        album_info = item.get('al') or item.get('album') or {}
        album = album_info.get('name') if isinstance(album_info, dict) else album_info
        identifier, duration = item.get('id'), float(item.get('dt') or item.get('duration') or 0) / 1000
        qualities = [item.get('hr') or {}, item.get('sq') or {}]
        lossless_bytes = max((numeric_size(meta.get('size')) for meta in qualities if isinstance(meta, dict)), default=0)
        catalog_lossless = bool(lossless_bytes or item.get('hr') or item.get('sq'))
        if any(item.get(key) for key in ('h', 'm', 'l', 'hMusic', 'mMusic', 'lMusic')): catalog_format = 'MP3'
    elif source == 'QQ音乐':
        file_info, album_info = item.get('file') or {}, item.get('album') or {}
        title = item.get('title') or item.get('songname')
        singers, album = names(item.get('singer')), album_info.get('title') if isinstance(album_info, dict) else item.get('albumname')
        album = album or item.get('albumname')
        identifier, duration = item.get('mid') or item.get('songmid'), item.get('interval') or 0
        lossless_bytes = max((numeric_size(file_info.get(key)) for key in ('size_flac', 'size_hires', 'size_new')), default=0)
        catalog_lossless = bool(lossless_bytes)
        requires_rights = bool((item.get('pay') or {}).get('pay_play'))
        if any(numeric_size(file_info.get(key)) for key in ('size_320mp3', 'size_128mp3')): catalog_format = 'MP3'
    elif source == '酷狗':
        title = item.get('songname') or item.get('SongName') or item.get('songname_original') or item.get('OriSongName') or item.get('filename') or item.get('FileName') or item.get('name')
        singers = item.get('singername') or item.get('SingerName') or names(item.get('singerinfo') or item.get('Singers'))
        album = item.get('album_name') or item.get('AlbumName') or ((item.get('albuminfo') or {}).get('name'))
        identifier = item.get('hash') or item.get('FileHash')
        duration = item.get('duration') or item.get('Duration') or (float(item.get('timelen') or 0) / 1000)
        lossless_bytes = max(numeric_size(item.get(key)) for key in ('SQFileSize', 'sqfilesize', 'ResFileSize', 'resfilesize'))
        catalog_lossless = bool(lossless_bytes or item.get('SQFileHash') or item.get('sqhash'))
        if item.get('FileHash') or item.get('hash'): catalog_format = 'MP3'
    elif source == '酷我':
        title = item.get('SONGNAME') or item.get('name') or item.get('songName')
        singers, album = item.get('ARTIST') or item.get('artist'), item.get('ALBUM') or item.get('album')
        identifier, duration = item.get('MUSICRID') or item.get('musicrid'), item.get('DURATION') or item.get('duration') or 0
        formats = str(item.get('FORMATS') or item.get('formats') or '').lower()
        catalog_lossless = 'flac' in formats
        catalog_format = next((ext.upper() for ext in ('flac', 'ape', 'mp3', 'aac', 'wma') if ext in formats), '')
    elif source == '咪咕':
        title = item.get('name') or item.get('songName')
        singers = names(item.get('singers') or item.get('singerList'))
        album = item.get('album') or names(item.get('albums'))
        identifier = item.get('contentId')
        duration = item.get('duration') or item.get('length') or 0
        formats = (item.get('rateFormats') or []) + (item.get('newRateFormats') or []) + (item.get('audioFormats') or [])
        lossless_formats = [meta for meta in formats if isinstance(meta, dict) and str(meta.get('formatType') or '').upper() in {'SQ', 'ZQ', 'FLAC'}]
        lossless_bytes = max((numeric_size(meta.get('size') or meta.get('iosSize') or meta.get('androidSize')) for meta in lossless_formats), default=0)
        catalog_lossless = bool(lossless_formats)
        if any(isinstance(meta, dict) and str(meta.get('formatType') or '').upper() in {'PQ', 'HQ', 'MP3'} for meta in formats): catalog_format = 'MP3'
    elif source == '千千':
        title, singers, album = item.get('title'), names(item.get('artist')), item.get('albumTitle')
        identifier, duration = item.get('TSID'), item.get('duration') or 0
        catalog_lossless = bool(item.get('hasLossless') or item.get('isLossless'))
    elif source == '街声':
        title, singers = item.get('title'), item.get('artist')
        identifier = item.get('song_id')
    elif source == 'ccMixter':
        title, singers, album = item.get('title'), item.get('creator'), item.get('album')
        identifier, duration = item.get('identifier'), item.get('duration') or 0
        ext = Path(urllib.parse.urlsplit(item.get('download_url') or '').path).suffix.lstrip('.').lower()
        if ext in LOSSLESS | {'mp3', 'aac', 'ogg', 'm4a', 'opus', 'wma'}: catalog_format = ext.upper()
    if catalog_lossless and source in {'网易云', 'QQ音乐', '酷狗', '酷我', '咪咕'}:
        catalog_format = 'FLAC'
    try: duration = int(float(duration or 0))
    except (TypeError, ValueError): duration = 0
    return {
        'song_name': clean_text(title) or '未知歌曲', 'singers': clean_text(singers), 'album': clean_text(album),
        'ext': '', 'codec': '', 'duration': time.strftime('%M:%S', time.gmtime(duration)),
        'file_size': f'{lossless_bytes / 1048576:.2f} MB' if lossless_bytes else '',
        'download_url': '', 'protocol': 'HTTP', 'identifier': str(identifier or ''),
        'downloaded_contents': None, 'source': source, 'downloadable': False,
        'catalog_lossless': catalog_lossless, 'requires_rights': requires_rights,
        'catalog_format': catalog_format,
        'catalog_item': item,
    }


def qq_catalog_row(item):
    return catalog_row('QQ音乐', item)


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
        'download_url', 'protocol', 'identifier', 'downloaded_contents', 'lyric', 'cover_url')}
    if row['protocol'] != 'HTTP' or not isinstance(row['download_url'], str):
        return None
    row['headers'] = song.default_download_headers or client.default_download_headers
    row['cookies'] = song.default_download_cookies or client.default_download_cookies
    row['source'] = source
    row['downloadable'] = True
    catalog = catalog_row(source, (song.raw_data or {}).get('search') or {})
    row['catalog_lossless'] = catalog['catalog_lossless']
    row['requires_rights'] = catalog['requires_rights']
    return row


def source_worker(repo, source, keyword, resolve_limit, catalog_limit, channel):
    try:
        sys.path.insert(0, repo)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from musicdl.modules import BuildMusicClient, SongInfo
            logger = SearchLog()
            with tempfile.TemporaryDirectory(prefix='shiyin-search-') as cache:
                client = BuildMusicClient(module_cfg=dict(type=SOURCES[source], work_dir=cache,
                    search_size_per_source=catalog_limit, search_size_per_page=catalog_limit,
                    max_retries=1, disable_print=True, logger_handle=logger))
                def metadata_only_parser(search_result, *args, **kwargs):
                    row = catalog_row(source, search_result)
                    identifier = row['identifier'] or f"{row['song_name']}|{row['singers']}"
                    return SongInfo(source=client.source, raw_data={'search': search_result, 'download': {}, 'lyric': {}},
                        song_name=row['song_name'], singers=row['singers'], album=row['album'], ext='mp3',
                        file_size_bytes=0, file_size='', duration_s=0, duration=row['duration'],
                        identifier=identifier, download_url=f"https://catalog.invalid/{urllib.parse.quote(identifier)}",
                        download_url_status={'ok': True})
                client._parsewithofficialapiv1 = metadata_only_parser
                if hasattr(client, '_parsewiththirdpartapis'):
                    client._parsewiththirdpartapis = metadata_only_parser
                songs = client.search(keyword, num_threadings=3, request_overrides={})
                results = [catalog_row(source, (song.raw_data or {}).get('search') or {}) for song in songs]
                status = f'{len(results)} 首目录结果 · 等待按需解析'
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
                source = catalog_song.get('source') or ''
                if source not in SOURCES: raise ValueError('目录结果缺少有效音源，请重新搜索')
                client = BuildMusicClient(module_cfg=dict(type=SOURCES[source], work_dir=cache,
                    search_size_per_source=1, search_size_per_page=1, max_retries=1,
                    disable_print=True, logger_handle=logger))
                item = catalog_song.get('catalog_item') or {}
                if not item: raise ValueError('目录结果缺少解析信息，请重新搜索')
                third_party = SongInfo(source=client.source)
                if hasattr(client, '_parsewiththirdpartapis'):
                    with contextlib.suppress(Exception):
                        third_party = client._parsewiththirdpartapis(search_result=item, request_overrides={})
                resolved = SongInfo(source=client.source)
                with contextlib.suppress(Exception):
                    resolved = client._parsewithofficialapiv1(search_result=item, song_info_flac=third_party,
                        lossless_quality_is_sufficient=True, request_overrides={})
                resolved = resolved if resolved.with_valid_download_url else third_party
                if not resolved.with_valid_download_url: raise ValueError(f'{source}当前未返回可下载地址，请改选其他音源')
                if resolved is not third_party:
                    if str(resolved.lyric or '').strip().lower() in {'', 'null', 'none'}:
                        resolved.lyric = third_party.lyric
                    resolved.cover_url = resolved.cover_url or third_party.cover_url
                if str(resolved.protocol or '').upper() == 'HLS':
                    resolved.work_dir = cache
                    downloaded = client.download([resolved], num_threadings=1, request_overrides={}, auto_supplement_song=False)
                    if not downloaded or not Path(downloaded[0].save_path).is_file():
                        raise ValueError(f'{source}分段音频下载失败')
                    resolved.downloaded_contents = Path(downloaded[0].save_path).read_bytes()
                    resolved.protocol = 'HTTP'
                row = song_row(resolved, client, source)
                if not row: raise ValueError(f'{source}返回的音频协议暂不支持')
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
                if not process.is_alive(): raise ValueError(f"{song.get('source') or '音源'}解析进程异常退出")
        if cancel.is_set(): raise Cancelled()
        raise TimeoutError(f"{song.get('source') or '音源'}按需解析超过 {timeout} 秒")
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


def fill_flac_metadata(audio, song, cancel):
    """Fill missing FLAC tags and artwork from the selected source's resolved song."""
    from mutagen.flac import Picture
    changed = False
    for tag, field in (('TITLE', 'song_name'), ('ARTIST', 'singers'), ('ALBUM', 'album')):
        value = clean_text(song.get(field))
        if value and not audio.get(tag):
            audio[tag] = value
            changed = True
    lyric = str(song.get('lyric') or '').replace('\r\n', '\n').strip()
    if lyric.lower() not in {'', 'null', 'none'} and not audio.get('LYRICS'):
        audio['LYRICS'] = lyric
        changed = True
    cover_url = str(song.get('cover_url') or '')
    if cover_url.startswith(('http://', 'https://')) and not audio.pictures:
        try:
            request = urllib.request.Request(cover_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read(5 * 1024 * 1024 + 1)
            mime = 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else ''
            if mime and len(data) <= 5 * 1024 * 1024:
                picture = Picture()
                picture.type, picture.mime, picture.data = 3, mime, data
                audio.add_picture(picture)
                changed = True
            else:
                song['_download_note'] = (song.get('_download_note', '') + ' · 封面图片格式不支持或过大').strip(' ·')
        except (OSError, ValueError):
            song['_download_note'] = (song.get('_download_note', '') + ' · 封面暂不可用').strip(' ·')
    if cancel.is_set(): raise Cancelled()
    if changed: audio.save()


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
        if type(audio).__name__ == 'FLAC': fill_flac_metadata(audio, song, cancel)
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
