"""Run local regression checks; --live also probes real musicdl providers."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave
import tkinter as tk
from backend import download, Cancelled, lossless, search, DEFAULT_REPO


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

    def test_quality(self):
        self.assertTrue(lossless({'ext': 'FLAC'}))
        self.assertFalse(lossless({'ext': 'm4a'}))
        self.assertFalse(lossless({'ext': 'mp3'}))

    def test_window_and_events(self):
        from app import App
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        song = self.song()
        app.events.put(('source', ('测试', [song], '1 首')))
        app.poll()
        root.update()
        self.assertEqual(len(app.table.get_children()), 1)
        app.rows.append(dict(song, ext='mp3'))
        app.only_lossless.set(True)
        app.render()
        self.assertEqual(len(app.table.get_children()), 1)
        failed = app.queue_table.insert('', 'end', values=('失败歌曲', '失败', 'HTTP Error 403'))
        app.download_meta[failed] = dict(song_name='失败歌曲', singers='歌手', source='酷我', ext='flac')
        report = app.failure_report()
        self.assertIn('失败数量：1', report)
        self.assertIn('错误：HTTP Error 403', report)
        self.assertNotIn('download_url', report)
        app.copy_failures()
        root.update()
        self.assertEqual(root.clipboard_get(), report)
        root.destroy()


if __name__ == '__main__':
    import sys
    if '--live' in sys.argv:
        report = []
        candidates = []
        def emit(kind, data):
            source, songs, status = data
            report.append(dict(source=source, status=status, count=len(songs), lossless=sum(lossless(s) for s in songs)))
            candidates.extend(songs)
            print(source, status, flush=True)
        search(DEFAULT_REPO, ['网易云', 'QQ音乐', '酷狗', '酷我', 'ccMixter'], 'piano', 3, threading.Event(), emit, timeout=75)
        for song in sorted(candidates, key=lambda s: not lossless(s))[:3]:
            try:
                path = download(song, Path('verification-downloads'), threading.Event(), lambda *_: None)
                report.append(dict(download='成功', source=song['source'], format=song['ext'], size=path.stat().st_size, file=str(path)))
                print('下载成功', song['source'], song['ext'], path.stat().st_size, flush=True)
                break
            except Exception as exc:
                report.append(dict(download='失败', source=song['source'], error=str(exc)))
                print('下载失败', song['source'], str(exc), flush=True)
        Path('validation-live.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    else:
        unittest.main()
