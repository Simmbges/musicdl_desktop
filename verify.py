"""Run local regression checks; --live also probes real musicdl providers."""
import io
import json
from pathlib import Path
import queue
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from unittest.mock import Mock, patch
import ssl
import urllib.error
import wave
import tkinter as tk
from backend import catalog_row, download, Cancelled, lossless, qq_catalog_row, quality_worker, resolve_worker, resolve_catalog_song, search, source_worker, DEFAULT_REPO, SOURCES


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 8000)
    return buffer.getvalue()


class Checks(unittest.TestCase):
    def song(self):
        return dict(song_name='测试/歌曲', singers='测试歌手', source='测试', ext='wav', downloaded_contents=wav_bytes())

    def test_valid_audio_and_duplicate_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            first = download(self.song(), folder, threading.Event(), lambda *_: None)
            second = download(self.song(), folder, threading.Event(), lambda *_: None)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), wav_bytes())

    def test_flac_tags_cover_and_lyrics(self):
        from mutagen.flac import FLAC
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        cover = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))
        with tempfile.TemporaryDirectory() as folder:
            source = next((Path(__file__).resolve().parent / 'verification-downloads').glob('Piano*.flac'))
            blank = Path(folder) / 'blank.flac'
            blank.write_bytes(source.read_bytes())
            audio = FLAC(blank)
            original_md5 = audio.info.md5_signature
            for key in ('TITLE', 'ARTIST', 'ALBUM'):
                audio.pop(key, None)
            audio.save()
            song = dict(song_name='测试歌', singers='测试歌手', album='测试专辑', source='测试', ext='flac',
                lyric='[00:01.00]测试歌词', cover_url='https://example.test/cover.png', downloaded_contents=blank.read_bytes())
            with patch('urllib.request.urlopen', return_value=io.BytesIO(cover)):
                result = download(song, folder, threading.Event(), lambda *_: None)
            tagged = FLAC(result)
            self.assertEqual(tagged.info.md5_signature, original_md5)
            self.assertEqual(tagged['TITLE'], ['测试歌'])
            self.assertEqual(tagged['ARTIST'], ['测试歌手'])
            self.assertEqual(tagged['ALBUM'], ['测试专辑'])
            self.assertEqual(tagged['LYRICS'], ['[00:01.00]测试歌词'])
            self.assertEqual(tagged.pictures[0].data, cover)
            self.assertEqual(tagged.pictures[0].type, 3)
            self.assertEqual(tagged.pictures[0].mime, 'image/png')
            self.assertFalse(result.with_suffix('.lrc').exists())
            self.assertIn('kHz', song['_audio_spec'])

    def test_quality_choices_only_include_resolved_lossless_tiers(self):
        import backend
        catalog = {'name': '测试歌', 'contentId': 'song', 'singers': [{'name': '歌手'}],
            'rateFormats': [{'formatType': 'ZQ24', 'size': '90000000'},
                {'formatType': 'SQ', 'size': '30000000'}, {'formatType': 'HQ', 'size': '10000000'}]}
        self.assertTrue(catalog_row('咪咕', dict(catalog, rateFormats=[catalog['rateFormats'][0]]))['catalog_lossless'])
        class FakeSong:
            def __init__(self, source, **kwargs):
                self.__dict__.update(source=source, raw_data={'search': {}},
                    with_valid_download_url=False, default_download_headers={}, default_download_cookies={})
                self.__dict__.update(kwargs)
        class FakeClient:
            source = 'MiguMusicClient'
            default_download_headers = {}
            default_download_cookies = {}
            def _parsewithofficialapiv1(self, search_result, **kwargs):
                tier = search_result['rateFormats'][0]['formatType']
                return FakeSong(self.source, raw_data={'search': search_result}, with_valid_download_url=True,
                    song_name='测试歌', singers='歌手', album='', ext='mp3' if tier == 'ZQ24' else 'flac',
                    codec='', duration='03:00', file_size='30 MB', download_url=f'https://example.test/{tier}',
                    protocol='HTTP', identifier='song', downloaded_contents=None, lyric='', cover_url='')
        fake_modules = types.ModuleType('musicdl.modules')
        fake_modules.BuildMusicClient = lambda **kwargs: FakeClient()
        fake_modules.SongInfo = FakeSong
        channel = queue.SimpleQueue()
        with patch.dict(sys.modules, {'musicdl': types.ModuleType('musicdl'), 'musicdl.modules': fake_modules}), \
             patch.object(backend, 'resolve_worker', side_effect=lambda repo, song, q: q.put((None, '无可用自动档位'))):
            quality_worker('', dict(source='咪咕', catalog_item=catalog), channel)
        options, error = channel.get_nowait()
        self.assertEqual(error, '')
        self.assertEqual([row['quality_label'] for row in options], ['标准无损'])
        self.assertEqual(options[0]['download_url'], 'https://example.test/SQ')

    def test_highest_download_keeps_lossless_when_official_is_lossy(self):
        class FakeSong:
            def __init__(self, source, ext='', **kwargs):
                self.__dict__.update(source=source, ext=ext, codec='', with_valid_download_url=bool(ext),
                    raw_data={'search': {'title': '测试歌'}}, default_download_headers={}, default_download_cookies={},
                    protocol='HTTP', download_url='https://example.test/audio', lyric='', cover_url='')
                self.__dict__.update(kwargs)
        class FakeClient:
            source = 'QQMusicClient'
            default_download_headers = {}
            default_download_cookies = {}
            def _parsewiththirdpartapis(self, **kwargs):
                return FakeSong(self.source, ext='flac', song_name='测试歌', singers='歌手')
            def _parsewithofficialapiv1(self, **kwargs):
                return FakeSong(self.source, ext='mp3', song_name='测试歌', singers='歌手')
        fake_modules = types.ModuleType('musicdl.modules')
        fake_modules.BuildMusicClient = lambda **kwargs: FakeClient()
        fake_modules.SongInfo = FakeSong
        channel = queue.SimpleQueue()
        with patch.dict(sys.modules, {'musicdl': types.ModuleType('musicdl'), 'musicdl.modules': fake_modules}):
            resolve_worker('', {'source': 'QQ音乐', 'catalog_item': {'title': '测试歌', 'mid': 'song'}}, channel)
        row, error = channel.get_nowait()
        self.assertEqual(error, '')
        self.assertEqual(row['ext'], 'flac')

    def test_invalid_audio_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            song = self.song()
            song['downloaded_contents'] = b'<html>expired link</html>'
            with self.assertRaises(Exception): download(song, folder, threading.Event(), lambda *_: None)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cancel_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            stop = threading.Event()
            stop.set()
            with self.assertRaises(Cancelled): download(self.song(), folder, stop, lambda *_: None)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_stream_download_and_truncation(self):
        class Response(io.BytesIO):
            def __init__(self, payload, size):
                super().__init__(payload)
                self.headers = {'Content-Length': str(size)}
        song = self.song()
        song.pop('downloaded_contents')
        song['download_url'] = 'https://example.test/music.wav'
        with tempfile.TemporaryDirectory() as folder:
            with patch('urllib.request.urlopen', return_value=Response(wav_bytes(), len(wav_bytes()))):
                result = download(song, folder, threading.Event(), lambda *_: None)
                self.assertEqual(result.read_bytes(), wav_bytes())
            with patch('urllib.request.urlopen', return_value=Response(wav_bytes(), len(wav_bytes()) + 100)):
                with self.assertRaises(ValueError): download(song, folder, threading.Event(), lambda *_: None)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_kugou_certificate_fallback_is_scoped(self):
        class Response(io.BytesIO):
            def __init__(self, payload):
                super().__init__(payload)
                self.headers = {'Content-Length': str(len(payload))}
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        song = self.song()
        song.update(source='酷狗', download_url='https://fs.youthandroid2.kugou.com/test.wav')
        song.pop('downloaded_contents')
        certificate_error = urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED'))
        opener = Mock()
        opener.open.return_value = Response(wav_bytes())
        with tempfile.TemporaryDirectory() as folder:
            with patch('urllib.request.urlopen', side_effect=certificate_error), patch('urllib.request.build_opener', return_value=opener):
                result = download(song, folder, threading.Event(), lambda *_: None)
            self.assertTrue(result.is_file())
            self.assertIn('酷狗证书兼容模式', song['_download_note'])
            opener.open.assert_called_once()
        other = dict(song, source='网易云')
        other.pop('_download_note', None)
        with tempfile.TemporaryDirectory() as folder:
            with patch('urllib.request.urlopen', side_effect=certificate_error), patch('urllib.request.build_opener') as build:
                with self.assertRaises(urllib.error.URLError):
                    download(other, folder, threading.Event(), lambda *_: None)
                build.assert_not_called()

    def test_quality(self):
        self.assertTrue(lossless({'ext': 'FLAC'}))
        self.assertFalse(lossless({'ext': 'm4a'}))
        self.assertFalse(lossless({'ext': 'mp3'}))

    def test_qq_catalog_row_exposes_unavailable_lossless(self):
        row = qq_catalog_row({'title': '<em>晴天</em>', 'mid': 'song-mid', 'interval': 269,
            'singer': [{'name': '<em>周杰伦</em>'}], 'album': {'title': '<em>叶惠美</em> &amp; 精选'},
            'file': {'size_flac': 55397039, 'size_new': [186980254, 31168013]}, 'pay': {'pay_play': 1}})
        self.assertEqual(row['song_name'], '晴天')
        self.assertEqual(row['singers'], '周杰伦')
        self.assertEqual(row['album'], '叶惠美 & 精选')
        self.assertEqual(row['duration'], '04:29')
        self.assertTrue(row['catalog_lossless'])
        self.assertEqual(row['file_size'], '178.32 MB')
        self.assertTrue(row['requires_rights'])
        self.assertFalse(row['downloadable'])
        self.assertEqual(row['catalog_item']['mid'], 'song-mid')

    def test_all_source_workers_only_return_catalog_rows(self):
        items = {
            '网易云': {'name': '晴天', 'id': 'ne', 'dt': 269000, 'ar': [{'name': '周杰伦'}], 'al': {'name': '叶惠美'}, 'sq': {'size': 55397039}},
            'QQ音乐': {'title': '晴天', 'mid': 'qq', 'interval': 269, 'singer': [{'name': '周杰伦'}], 'album': {'title': '叶惠美'}, 'file': {'size_flac': 55397039}},
            '酷狗': {'SongName': '晴天', 'FileHash': 'kg', 'Duration': 269, 'SingerName': '周杰伦', 'AlbumName': '叶惠美', 'SQFileSize': 55397039},
            '酷我': {'SONGNAME': '晴天', 'MUSICRID': 'MUSIC_kw', 'DURATION': 269, 'ARTIST': '周杰伦', 'ALBUM': '叶惠美', 'FORMATS': 'mp3|flac'},
            '咪咕': {'name': '晴天', 'contentId': 'mg', 'copyrightId': 'cp', 'singers': [{'name': '周杰伦'}], 'album': '叶惠美', 'rateFormats': [{'formatType': 'SQ', 'size': '52MB'}]},
            '千千': {'title': '晴天', 'TSID': 'qy', 'artist': [{'name': '周杰伦'}], 'albumTitle': '叶惠美'},
            '街声': {'title': '晴天', 'song_id': 'sv', 'artist': '周杰伦'},
            'ccMixter': {'title': 'Piano', 'identifier': 'cc', 'creator': 'Artist', 'album': 'Album', 'duration': 269, 'download_url': 'https://example.test/a.mp3'},
        }
        class FakeSong:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
            @property
            def with_valid_download_url(self): return True
        for source, item in items.items():
            with self.subTest(source=source):
                class Client:
                    def __init__(self):
                        self.source = source
                        self.search_size_per_source = self.search_size_per_page = 5
                    def search(self, *args, **kwargs):
                        return [self._parsewithofficialapiv1(search_result=item)]
                client = Client()
                fake_modules = types.ModuleType('musicdl.modules')
                fake_modules.BuildMusicClient = lambda module_cfg: client
                fake_modules.SongInfo = FakeSong
                channel = queue.Queue()
                with patch.dict(sys.modules, {'musicdl': types.ModuleType('musicdl'), 'musicdl.modules': fake_modules}):
                    source_worker('', source, '测试', 5, 5, channel)
                done = channel.get_nowait()
                self.assertEqual(done[0], 'done')
                self.assertEqual(done[1][0]['source'], source)
                self.assertFalse(done[1][0]['downloadable'])
                self.assertEqual(done[1][0]['catalog_item'], item)
                self.assertIn('等待按需解析', done[2])

    def test_window_and_events(self):
        from app import App
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        self.assertFalse(app.queue_panel.winfo_manager())
        app.toggle_queue(True)
        self.assertEqual(app.queue_panel.winfo_manager(), 'pack')
        app.toggle_queue(False)
        self.assertFalse(app.queue_panel.winfo_manager())
        song = self.song()
        app.events.put(('source', ('酷我', [song], '1 首')))
        app.poll()
        root.update()
        self.assertEqual(len(app.table.get_children()), 1)
        app.rows.append(dict(song, ext='mp3'))
        app.only_lossless.set(True)
        app.render()
        self.assertEqual(len(app.table.get_children()), 1)
        app.only_lossless.set(False)
        catalog = qq_catalog_row({'title': '目录歌曲', 'mid': 'catalog-mid', 'file': {'size_flac': 1234}})
        app.operation = 'search'
        app.set_busy(True)
        app.events.put(('source_partial', ('QQ音乐', [catalog], '1 首目录结果')))
        app.poll()
        self.assertEqual(len(app.table.get_children()), 2)
        self.assertEqual(str(app.download_button['state']), 'normal')
        self.assertEqual('无损 · FLAC', app.table.set(str(app.rows.index(catalog)), 3))
        app.table.selection_set(str(app.rows.index(catalog)))
        with patch.object(app, 'save_settings'):
            app.begin_download()
        self.assertTrue(app.cancel.is_set())
        self.assertEqual(app.pending_download[0][0]['identifier'], 'catalog-mid')
        with patch.object(app, 'start_download') as start_download:
            app.events.put(('done', None))
            app.poll()
            start_download.assert_called_once()
        self.assertIsNone(app.pending_download)
        app.operation = 'search'
        app.set_busy(True)
        app.table.selection_set(str(app.rows.index(catalog)))
        with patch.object(app, 'save_settings'):
            app.begin_choose_quality()
        self.assertEqual(app.pending_quality[0]['identifier'], 'catalog-mid')
        with patch.object(app, 'start_quality_lookup') as start_quality_lookup:
            app.events.put(('done', None))
            app.poll()
            start_quality_lookup.assert_called_once()
        self.assertIsNone(app.pending_quality)
        app.events.put(('source', ('QQ音乐', [], '超时，请稍后重试')))
        app.poll()
        self.assertEqual(len(app.table.get_children()), 2)
        self.assertEqual(app.source_rows['QQ音乐'][0]['identifier'], 'catalog-mid')
        self.assertIn('链接解析失败', app.source_status['QQ音乐'])
        app.events.put(('source', ('QQ音乐', [dict(song, source='QQ音乐')], '1 首可下载')))
        app.poll()
        self.assertEqual(len(app.table.get_children()), 2)
        app.events.put(('source', ('咪咕', [], '不可用：测试')))
        app.events.put(('source', ('咪咕', [], '不可用：测试')))
        app.poll()
        self.assertEqual(app.source_health['咪咕']['failures'], 2)
        self.assertGreater(app.source_health['咪咕']['cooldown_until'], time.monotonic())
        app.operation = 'download'
        app.cancel.clear()
        app.events.put(('done', None))
        app.poll()
        self.assertEqual(app.status.get(), '下载任务结束，结果见下方记录。')
        failed = app.queue_table.insert('', 'end', values=('失败歌曲', '失败', 'HTTP Error 403'))
        app.download_meta[failed] = dict(song_name='失败歌曲', singers='歌手', source='酷我', ext='flac')
        report = app.failure_report()
        self.assertIn('失败数量：1', report)
        self.assertIn('错误：HTTP Error 403', report)
        self.assertNotIn('download_url', report)
        app.copy_failures()
        root.update()
        copied = root.clipboard_get()
        self.assertIn('失败数量：1', copied)
        self.assertIn('错误：HTTP Error 403', copied)
        self.assertEqual(str(app.limit['state']), 'normal')
        app.query.set('测试')
        app.limit.set('17')
        with tempfile.TemporaryDirectory() as folder:
            app.settings_path = Path(folder) / 'settings.json'
            with patch('app.search') as run_search, patch.object(app, 'launch', side_effect=lambda job, operation: job()):
                app.begin_search()
            self.assertEqual(run_search.call_args.kwargs['catalog_limit'], 17)
            self.assertEqual(json.loads(app.settings_path.read_text('utf-8'))['limit'], '17')
        for invalid in ('0', '1.5', '无效'):
            app.limit.set(invalid)
            with patch('app.messagebox.showinfo') as showinfo, patch('app.search') as run_search:
                app.begin_search()
            showinfo.assert_called_once()
            run_search.assert_not_called()
        root.destroy()


if __name__ == '__main__':
    import sys
    if '--catalog-live' in sys.argv:
        report, started = {}, time.monotonic()
        def emit_catalog(kind, data):
            source, songs, status = data
            if kind == 'source': report[source] = songs
            print(f'{time.monotonic() - started:.1f}秒', source, status, len(songs), flush=True)
        search(DEFAULT_REPO, [source for source in SOURCES if source != 'ccMixter'], '晴天 周杰伦', 3, threading.Event(), emit_catalog, timeout=45, catalog_limit=3)
        search(DEFAULT_REPO, ['ccMixter'], 'piano', 3, threading.Event(), emit_catalog, timeout=45, catalog_limit=3)
        for source, songs in report.items():
            if any(song.get('downloadable', True) for song in songs):
                raise RuntimeError(f'{source}在搜索阶段提前生成了下载地址')
        print(json.dumps({source: len(songs) for source, songs in report.items()}, ensure_ascii=False), flush=True)
    elif '--qq-fast-live' in sys.argv:
        final_rows, started = [], time.monotonic()
        def emit_fast(kind, data):
            source, songs, status = data
            print(f'{time.monotonic() - started:.1f}秒', kind, status, len(songs), flush=True)
            if kind == 'source': final_rows.extend(songs)
        search(DEFAULT_REPO, ['QQ音乐'], '晴天 周杰伦', 3, threading.Event(), emit_fast, timeout=45, catalog_limit=10)
        target = next((song for song in final_rows if not song.get('downloadable')), None)
        if not target: raise RuntimeError('未找到待按需解析的 QQ 目录结果')
        resolved = resolve_catalog_song(DEFAULT_REPO, target, threading.Event(), timeout=45)
        print('按需解析成功', resolved['song_name'], resolved['ext'], resolved['file_size'],
            '封面', bool(resolved.get('cover_url')), '歌词', bool(resolved.get('lyric')), flush=True)
    elif '--qq-live' in sys.argv:
        candidates, started = [], time.monotonic()
        def emit_qq(kind, data):
            source, songs, status = data
            candidates.extend(songs)
            print(f'{time.monotonic() - started:.1f}秒', kind, source, status, [(s.get('song_name'), s.get('ext'), s.get('file_size'), s.get('downloadable')) for s in songs], flush=True)
        search(DEFAULT_REPO, ['QQ音乐'], '晴天 周杰伦', 3, threading.Event(), emit_qq, timeout=45, catalog_limit=10)
        target = next((song for song in candidates if not song.get('downloadable')), None)
        if not target: raise RuntimeError('未找到 QQ 目录结果')
        resolved = resolve_catalog_song(DEFAULT_REPO, target, threading.Event(), timeout=45)
        path = download(resolved, Path('verification-downloads'), threading.Event(), lambda *_: None)
        print('下载成功', path.name, path.stat().st_size, flush=True)
    elif '--kugou-live' in sys.argv:
        candidates = []
        def emit_kugou(kind, data):
            source, songs, status = data
            candidates.extend(songs)
            print(source, status, flush=True)
        search(DEFAULT_REPO, ['酷狗'], '晴天 周杰伦', 3, threading.Event(), emit_kugou, timeout=75)
        target = next((song for song in candidates if song.get('song_name') == '晴天'), None)
        if not target: raise RuntimeError('未找到酷狗目录结果')
        resolved = resolve_catalog_song(DEFAULT_REPO, target, threading.Event(), timeout=45)
        path = download(resolved, Path('verification-downloads'), threading.Event(), lambda *_: None)
        print('下载成功', path.name, path.stat().st_size, resolved.get('_download_note', ''), flush=True)
    elif '--live' in sys.argv:
        report = []
        candidates = []
        def emit(kind, data):
            source, songs, status = data
            report.append(dict(source=source, status=status, count=len(songs), lossless=sum(lossless(s) for s in songs)))
            candidates.extend(songs)
            print(source, status, flush=True)
        search(DEFAULT_REPO, ['网易云', 'QQ音乐', '酷狗', '酷我', 'ccMixter'], 'piano', 3, threading.Event(), emit, timeout=75)
        for song in sorted(candidates, key=lambda s: not s.get('catalog_lossless'))[:3]:
            try:
                resolved = resolve_catalog_song(DEFAULT_REPO, song, threading.Event(), timeout=45)
                path = download(resolved, Path('verification-downloads'), threading.Event(), lambda *_: None)
                report.append(dict(download='成功', source=resolved['source'], format=resolved['ext'], size=path.stat().st_size, file=str(path)))
                print('下载成功', resolved['source'], resolved['ext'], path.stat().st_size, flush=True)
                break
            except Exception as exc:
                report.append(dict(download='失败', source=song['source'], error=str(exc)))
                print('下载失败', song['source'], str(exc), flush=True)
        Path('validation-live.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    else:
        unittest.main()
