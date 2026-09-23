"""Run local regression checks; --live also probes real musicdl providers."""
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
import ssl
import urllib.error
import wave
import tkinter as tk
from backend import download, Cancelled, lossless, qq_catalog_row, resolve_catalog_song, search, source_worker, DEFAULT_REPO, SOURCES


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
            'file': {'size_flac': 55397039, 'size_new': []}, 'pay': {'pay_play': 1}})
        self.assertEqual(row['song_name'], '晴天')
        self.assertEqual(row['singers'], '周杰伦')
        self.assertEqual(row['album'], '叶惠美 & 精选')
        self.assertEqual(row['duration'], '04:29')
        self.assertTrue(row['catalog_lossless'])
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
        print('按需解析成功', resolved['song_name'], resolved['ext'], resolved['file_size'], flush=True)
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
