"""拾音 · musicdl 桌面版。"""
import ctypes
from datetime import datetime
import json
import multiprocessing
import os
import platform
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from backend import ROOT, DEFAULT_REPO, SOURCES, lossless, search, download, Cancelled


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.rows = []
        self.tasks = {}
        self.download_meta = {}
        self.source_status = {}
        self.settings_path = ROOT / 'settings.json'
        try: settings = json.loads(self.settings_path.read_text('utf-8'))
        except (OSError, ValueError): settings = {}
        self.repo = settings.get('repo', str(DEFAULT_REPO))
        self.directory = tk.StringVar(value=settings.get('directory', str(ROOT / '下载音乐')))
        self.query = tk.StringVar()
        self.only_lossless = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='输入歌名或歌手，开始寻找你的下一首收藏。')
        self.count = tk.StringVar(value='尚未搜索')
        root.title('拾音 · 无损音乐桌面版')
        scale = max(1, float(root.tk.call('tk', 'scaling')) / (96 / 72))
        px = lambda value: round(value * scale)
        width = min(px(1180), root.winfo_screenwidth() - px(40))
        height = min(px(860), root.winfo_screenheight() - px(80))
        root.geometry(f'{width}x{height}')
        root.minsize(min(px(960), width), min(px(750), height))
        root.configure(bg='#f3f5f7')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background='#f3f5f7')
        style.configure('TLabel', background='#f3f5f7', foreground='#243c46')
        style.configure('TButton', padding=(14, 8))
        style.configure('Accent.TButton', background='#177d72', foreground='white')
        style.map('Accent.TButton', background=[('active', '#11675e'), ('disabled', '#8ca9a4')])
        style.configure('Treeview', rowheight=px(30), background='white', fieldbackground='white', borderwidth=0)
        style.configure('Treeview.Heading', padding=8, font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#d4eee8')], foreground=[('selected', '#124a42')])
        banner = tk.Frame(root, bg='#142e37', padx=28, pady=18)
        banner.pack(fill='x')
        tk.Label(banner, text='拾音  /  MUSIC LIBRARY', font=('Microsoft YaHei UI', 24, 'bold'), bg='#142e37', fg='white').pack(anchor='w')
        tk.Label(banner, text='搜索 · 发现无损 · 收藏到本地', font=('Microsoft YaHei UI', 11), bg='#142e37', fg='#a7d9ce').pack(anchor='w', pady=(5, 0))
        body = ttk.Frame(root, padding=22)
        body.pack(fill='both', expand=True)
        bar = ttk.Frame(body)
        bar.pack(fill='x')
        self.entry = ttk.Entry(bar, textvariable=self.query, font=('Microsoft YaHei UI', 13))
        self.entry.pack(side='left', fill='x', expand=True, ipady=7)
        self.entry.bind('<Return>', lambda _: self.begin_search())
        self.search_button = ttk.Button(bar, text='搜索音乐', style='Accent.TButton', command=self.begin_search)
        self.search_button.pack(side='left', padx=10)
        self.cancel_button = ttk.Button(bar, text='取消任务', command=self.cancel.set, state='disabled')
        self.cancel_button.pack(side='left')
        sources = ttk.Frame(body)
        sources.pack(fill='x', pady=(12, 8))
        self.source_vars = {}
        selected = settings.get('sources', ['网易云', 'QQ音乐', '酷狗', '酷我'])
        for source in SOURCES:
            value = tk.BooleanVar(value=source in selected)
            self.source_vars[source] = value
            ttk.Checkbutton(sources, text=source, variable=value).pack(side='left', padx=(0, 8))
        filters = ttk.Frame(body)
        filters.pack(fill='x', pady=(0, 8))
        ttk.Checkbutton(filters, text='仅显示无损', variable=self.only_lossless, command=self.render).pack(side='left')
        ttk.Label(filters, text='每个音源最多').pack(side='left', padx=(20, 5))
        self.limit = ttk.Combobox(filters, values=['10', '20', '30'], state='readonly', width=4)
        self.limit.set('10')
        self.limit.pack(side='left')
        ttk.Label(filters, text='首  ·  无损优先排列').pack(side='left', padx=6)
        ttk.Label(filters, textvariable=self.count).pack(side='right')
        self.table = self.make_table(body, ['歌曲', '歌手', '专辑', '音质 / 格式', '时长', '大小', '来源'], [250, 160, 180, 120, 75, 85, 90], height=8)
        self.table.tag_configure('lossless', foreground='#087566')
        self.table.tag_configure('unavailable', foreground='#8a7770')
        self.table.bind('<Double-1>', lambda _: self.begin_download())
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=10)
        self.download_button = ttk.Button(actions, text='下载选中歌曲', style='Accent.TButton', command=self.begin_download)
        self.download_button.pack(side='left')
        ttk.Button(actions, text='全选当前结果', command=lambda: self.table.selection_set(self.table.get_children())).pack(side='left', padx=8)
        ttk.Label(actions, text='Ctrl / Shift 多选 · 双击下载').pack(side='left', padx=8)
        ttk.Button(actions, text='项目位置', command=self.choose_repo).pack(side='right')
        savebar = ttk.Frame(body)
        savebar.pack(fill='x', pady=(0, 10))
        ttk.Label(savebar, text='保存到').pack(side='left', padx=(0, 10))
        ttk.Entry(savebar, textvariable=self.directory).pack(side='left', fill='x', expand=True, ipady=5)
        ttk.Button(savebar, text='更改目录', command=self.choose_directory).pack(side='left', padx=8)
        ttk.Button(savebar, text='打开文件夹', command=self.open_directory).pack(side='left')
        queue_header = ttk.Frame(body)
        queue_header.pack(fill='x', pady=(0, 6))
        ttk.Label(queue_header, text='下载记录', font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
        ttk.Button(queue_header, text='一键复制失败信息', command=self.copy_failures).pack(side='right')
        self.queue_table = self.make_table(body, ['歌曲', '状态', '进度 / 位置'], [300, 140, 600], height=4, expand=False)
        self.progress = ttk.Progressbar(body, mode='determinate')
        self.progress.pack(fill='x', pady=(10, 6))
        ttk.Label(body, textvariable=self.status, wraplength=1090).pack(fill='x')
        self.source_label = ttk.Label(body, text='音质由来源标注，下载后检查音频格式；不进行 MP3 转无损。', foreground='#73838a', wraplength=1090)
        self.source_label.pack(fill='x', pady=(5, 0))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)
        self.entry.focus_set()

    def make_table(self, parent, names, widths, height, expand=True):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=expand)
        table = ttk.Treeview(frame, columns=list(range(len(names))), show='headings', height=height)
        for index, (name, width) in enumerate(zip(names, widths)):
            table.heading(index, text=name)
            table.column(index, width=width, minwidth=55)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        table.pack(fill='both', expand=True)
        return table

    def save_settings(self):
        data = dict(repo=self.repo, directory=self.directory.get(), sources=[s for s, v in self.source_vars.items() if v.get()])
        temporary = self.settings_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
        temporary.replace(self.settings_path)

    def choose_repo(self):
        path = filedialog.askdirectory(title='选择包含 musicdl 文件夹的原项目目录', initialdir=self.repo)
        if path:
            self.repo = path
            self.save_settings()

    def choose_directory(self):
        path = filedialog.askdirectory(title='音乐保存位置')
        if path:
            self.directory.set(path)
            self.save_settings()

    def open_directory(self):
        try:
            path = Path(self.directory.get()).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except OSError as exc: messagebox.showerror('无法打开目录', str(exc))

    def failure_report(self):
        failures = []
        for key in self.queue_table.get_children():
            if self.queue_table.set(key, 1) != '失败':
                continue
            song = self.download_meta.get(key, {})
            failures.append('\n'.join((
                f"歌曲：{song.get('song_name') or self.queue_table.set(key, 0) or '未知'}",
                f"歌手：{song.get('singers') or '未知'}",
                f"来源：{song.get('source') or '未知'}",
                f"格式：{str(song.get('ext') or '未知').upper()}",
                f"错误：{self.queue_table.set(key, 2) or '未提供详情'}",
            )))
        if not failures:
            return ''
        header = '\n'.join((
            '拾音下载失败报告',
            f'生成时间：{datetime.now().astimezone().isoformat(timespec="seconds")}',
            f'系统：{platform.platform()}',
            f'Python：{sys.version.split()[0]}',
            f'musicdl 项目：{self.repo}',
            f'失败数量：{len(failures)}',
        ))
        return header + '\n\n' + '\n\n---\n\n'.join(failures)

    def copy_failures(self):
        report = self.failure_report()
        if not report:
            messagebox.showinfo('复制失败信息', '当前下载记录中没有失败项目。')
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
            self.root.update_idletasks()
            self.status.set(f'已复制 {report.count("歌曲：")} 条下载失败信息，可以直接粘贴发给我。')
        except tk.TclError as exc:
            messagebox.showerror('复制失败', str(exc))

    def set_busy(self, value):
        self.busy = value
        self.search_button.configure(state='disabled' if value else 'normal')
        self.download_button.configure(state='disabled' if value else 'normal')
        self.cancel_button.configure(state='normal' if value else 'disabled')

    def launch(self, job):
        self.cancel.clear()
        self.set_busy(True)
        def worker():
            try: job()
            except Exception as exc: self.events.put(('error', str(exc)))
            finally: self.events.put(('done', None))
        threading.Thread(target=worker, daemon=True).start()

    def begin_search(self):
        if self.busy: return
        keyword = self.query.get().strip()
        sources = [s for s, v in self.source_vars.items() if v.get()]
        if not keyword or not sources:
            messagebox.showinfo('搜索音乐', '请输入关键词，并勾选至少一个音源。')
            return
        if not (Path(self.repo) / 'musicdl' / 'musicdl.py').is_file():
            messagebox.showerror('未找到 musicdl', '请点击“项目位置”选择下载的 musicdl-master 文件夹。')
            return
        self.save_settings()
        self.rows.clear()
        self.render()
        self.source_status = {s: '搜索中' for s in sources}
        self.update_sources()
        self.status.set('正在搜索，各音源完成后陆续显示；单音源最多等待 90 秒。')
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)
        repo, limit = self.repo, int(self.limit.get())
        self.launch(lambda: search(repo, sources, keyword, limit, self.cancel, lambda kind, payload: self.events.put((kind, payload))))

    def render(self):
        self.table.delete(*self.table.get_children())
        indexes = sorted(range(len(self.rows)), key=lambda i: not lossless(self.rows[i]))
        for i in indexes:
            s = self.rows[i]
            if self.only_lossless.get() and not lossless(s): continue
            downloadable = s.get('downloadable', True)
            if not downloadable:
                quality = '目录有 FLAC · 暂不可下载' if s.get('catalog_lossless') else '暂无可用直链'
            else:
                quality = ('无损 · ' if lossless(s) else '') + str(s.get('ext') or '未知').upper()
                if s.get('catalog_lossless') and not lossless(s): quality += ' · 目录有 FLAC'
            tags = ('lossless',) if lossless(s) else (('unavailable',) if not downloadable else ())
            self.table.insert('', 'end', iid=str(i), values=[s.get('song_name'), s.get('singers'), s.get('album'), quality, s.get('duration'), s.get('file_size'), s['source']], tags=tags)
        available = sum(s.get('downloadable', True) for s in self.rows)
        self.count.set(f"显示 {len(self.table.get_children())} / {len(self.rows)} 首 · 可下载 {available} 首 · 无损 {sum(lossless(s) for s in self.rows)} 首")

    def update_sources(self):
        self.source_label.configure(text='  /  '.join(f'{s}：{v}' for s, v in self.source_status.items()))

    def begin_download(self):
        if self.busy: return
        selected = [self.rows[int(i)] for i in self.table.selection()]
        if not selected:
            messagebox.showinfo('选择歌曲', '请先选中需要下载的歌曲。')
            return
        songs = [song for song in selected if song.get('downloadable', True)]
        if not songs:
            messagebox.showinfo('暂无可用下载', 'QQ 已找到歌曲，但当前没有可下载直链。目录标注有 FLAC 不代表未登录账号可以下载；请改选其他音源。')
            return
        if len(songs) != len(selected):
            self.status.set(f'已跳过 {len(selected) - len(songs)} 首仅有目录信息的歌曲。')
        directory = self.directory.get().strip()
        if not directory:
            self.choose_directory()
            return
        self.save_settings()
        tasks = []
        for song in songs:
            key = self.queue_table.insert('', 'end', values=(song['song_name'], '等待下载', ''))
            self.download_meta[key] = song
            tasks.append((key, song))
        self.progress.stop()
        self.progress.configure(mode='determinate', value=0)
        self.status.set(f'正在下载 {len(tasks)} 首歌曲；取消会停止当前文件及后续队列。')
        def job():
            for key, song in tasks:
                if self.cancel.is_set():
                    self.events.put(('task', (key, '已取消', '')))
                    continue
                self.events.put(('task', (key, '下载中', '正在连接…')))
                try:
                    def progress(done, total): self.events.put(('progress', (key, done, total)))
                    path = download(song, directory, self.cancel, progress)
                    note = song.pop('_download_note', '')
                    detail = str(path) + (f' · {note}' if note else '')
                    self.events.put(('task', (key, '已完成', detail)))
                except Cancelled:
                    self.events.put(('task', (key, '已取消', '临时文件已清理')))
                except Exception as exc:
                    self.events.put(('task', (key, '失败', str(exc))))
        self.launch(job)

    def poll(self):
        for _ in range(200):
            try: kind, data = self.events.get_nowait()
            except queue.Empty: break
            if kind == 'source':
                source, songs, status = data
                self.rows.extend(songs)
                self.source_status[source] = status
                self.update_sources()
                self.render()
            elif kind == 'task':
                key, state, detail = data
                self.queue_table.set(key, 1, state)
                self.queue_table.set(key, 2, detail)
                self.queue_table.see(key)
            elif kind == 'progress':
                key, done, total = data
                detail = f'{done / 1048576:.1f} MB' + (f' / {total / 1048576:.1f} MB' if total else '')
                self.queue_table.set(key, 2, detail)
                self.progress.configure(value=done / total * 100 if total else 0)
            elif kind == 'error':
                messagebox.showerror('任务失败', data)
            elif kind == 'done':
                self.progress.stop()
                self.set_busy(False)
                self.status.set('任务已取消。' if self.cancel.is_set() else '任务结束。搜索详情见音源状态；下载结果见记录中的“已完成 / 失败”。')
        self.root.after(100, self.poll)

    def close(self):
        self.cancel.set()
        self.save_settings()
        if self.busy:
            self.status.set('正在取消任务并清理临时文件，请稍候…')
            self.root.after(200, self.finish_close)
        else: self.root.destroy()

    def finish_close(self):
        if self.busy: self.root.after(200, self.finish_close)
        else: self.root.destroy()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError): pass
    window = tk.Tk()
    App(window)
    window.mainloop()
