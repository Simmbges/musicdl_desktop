"""拾音 · musicdl 桌面版。"""
import copy
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
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from backend import ROOT, DEFAULT_REPO, SOURCES, lossless, search, resolve_catalog_song, resolve_quality_options, download, Cancelled


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.rows = []
        self.source_rows = {}
        self.tasks = {}
        self.download_meta = {}
        self.source_status = {}
        self.source_health = {}
        self.source_started = {}
        self.search_cache = {}
        self.active_search_key = None
        self.operation = None
        self.pending_download = None
        self.pending_quality = None
        self.quality_result = None
        self.settings_path = ROOT / 'settings.json'
        try: settings = json.loads(self.settings_path.read_text('utf-8'))
        except (OSError, ValueError): settings = {}
        self.repo = settings.get('repo') or str(DEFAULT_REPO)
        if getattr(sys, 'frozen', False) and not (Path(self.repo) / 'musicdl' / 'musicdl.py').is_file():
            self.repo = str(DEFAULT_REPO)
        self.directory = tk.StringVar(value=settings.get('directory', str(ROOT / '下载音乐')))
        self.query = tk.StringVar()
        self.only_lossless = tk.BooleanVar(value=False)
        self.search_mode = tk.StringVar(value=settings.get('search_mode', '快速搜索'))
        self.mode_help = tk.StringVar()
        self.status = tk.StringVar(value='输入歌名或歌手，开始寻找你的下一首收藏。')
        self.count = tk.StringVar(value='尚未搜索')
        self.queue_summary = tk.StringVar(value='暂无下载任务')
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
        self.cancel_button = ttk.Button(bar, text='取消任务', command=self.cancel_current, state='disabled')
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
        self.mode = ttk.Combobox(filters, textvariable=self.search_mode, values=['快速搜索', '完整搜索'], state='readonly', width=9)
        self.mode.pack(side='left', padx=(20, 6))
        self.mode.bind('<<ComboboxSelected>>', lambda _: self.update_mode_help())
        ttk.Label(filters, text='每个音源最多').pack(side='left', padx=(20, 5))
        self.limit = ttk.Combobox(filters, values=['5', '10', '20', '30'], state='readonly', width=4)
        self.limit.set(str(settings.get('limit', '10')))
        self.limit.pack(side='left')
        ttk.Label(filters, text='首').pack(side='left', padx=(6, 14))
        ttk.Label(filters, textvariable=self.mode_help, foreground='#60747c').pack(side='left')
        results_header = ttk.Frame(body)
        results_header.pack(fill='x', pady=(5, 6))
        ttk.Label(results_header, text='搜索结果', font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
        ttk.Label(results_header, textvariable=self.count).pack(side='right')
        self.table = self.make_table(body, ['歌曲', '歌手', '专辑', '音质 / 格式', '时长', '预计大小', '来源'], [250, 160, 180, 120, 75, 85, 90], height=12)
        self.table.tag_configure('lossless', foreground='#087566')
        self.table.tag_configure('unavailable', foreground='#8a7770')
        self.table.bind('<Double-1>', lambda _: self.begin_download())
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=10)
        self.download_button = ttk.Button(actions, text='下载最高无损', style='Accent.TButton', command=self.begin_download)
        self.download_button.pack(side='left')
        self.quality_button = ttk.Button(actions, text='选择音质…', command=self.begin_choose_quality)
        self.quality_button.pack(side='left', padx=(8, 0))
        ttk.Button(actions, text='全选当前结果', command=lambda: self.table.selection_set(self.table.get_children())).pack(side='left', padx=8)
        ttk.Label(actions, text='可多选一键下载 · 双击下载最高无损').pack(side='left', padx=8)
        ttk.Button(actions, text='项目位置', command=self.choose_repo).pack(side='right')
        savebar = ttk.Frame(body)
        savebar.pack(fill='x', pady=(0, 10))
        ttk.Label(savebar, text='保存到').pack(side='left', padx=(0, 10))
        ttk.Entry(savebar, textvariable=self.directory).pack(side='left', fill='x', expand=True, ipady=5)
        ttk.Button(savebar, text='更改目录', command=self.choose_directory).pack(side='left', padx=8)
        ttk.Button(savebar, text='打开文件夹', command=self.open_directory).pack(side='left')
        queue_header = ttk.Frame(body)
        queue_header.pack(fill='x', pady=(0, 6))
        self.queue_button = ttk.Button(queue_header, text='▸ 下载队列', command=self.toggle_queue)
        self.queue_button.pack(side='left')
        ttk.Label(queue_header, textvariable=self.queue_summary, foreground='#60747c').pack(side='left', padx=10)
        ttk.Button(queue_header, text='一键复制失败信息', command=self.copy_failures).pack(side='right')
        self.queue_panel = ttk.Frame(body)
        self.queue_table = self.make_table(self.queue_panel, ['歌曲', '状态', '进度 / 位置'], [300, 140, 600], height=5, expand=False)
        self.progress = ttk.Progressbar(body, mode='determinate')
        self.progress.pack(fill='x', pady=(10, 6))
        ttk.Label(body, textvariable=self.status, wraplength=1090).pack(fill='x')
        self.source_label = ttk.Label(body, text='音质由来源标注，下载后检查音频格式；不进行 MP3 转无损。', foreground='#73838a', wraplength=1090)
        self.source_label.pack(fill='x', pady=(5, 0))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)
        self.entry.focus_set()
        self.update_mode_help()

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

    def toggle_queue(self, expanded=None):
        if expanded is None: expanded = not self.queue_panel.winfo_manager()
        if expanded:
            self.queue_panel.pack(fill='x', before=self.progress)
        else:
            self.queue_panel.pack_forget()
        self.queue_button.configure(text=('▾' if expanded else '▸') + ' 下载队列')

    def update_queue_summary(self):
        keys = self.queue_table.get_children()
        completed = sum(self.queue_table.set(key, 1) == '已完成' for key in keys)
        failed = sum(self.queue_table.set(key, 1) == '失败' for key in keys)
        self.queue_summary.set(f'{len(keys)} 首 · 已完成 {completed} · 失败 {failed}' if keys else '暂无下载任务')

    def save_settings(self):
        data = dict(repo='' if getattr(sys, 'frozen', False) and self.repo == str(DEFAULT_REPO) else self.repo,
            directory=self.directory.get(),
            sources=[s for s, v in self.source_vars.items() if v.get()],
            search_mode=self.search_mode.get(), limit=self.limit.get())
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
        can_download_during_search = value and self.operation == 'search' and bool(self.rows) and not self.pending_download
        self.download_button.configure(state='normal' if not value or can_download_during_search else 'disabled')
        self.quality_button.configure(state='normal' if not value or can_download_during_search else 'disabled')
        self.cancel_button.configure(state='normal' if value else 'disabled')

    def launch(self, job, operation):
        self.cancel.clear()
        self.operation = operation
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
        mode, catalog_limit = self.search_mode.get(), int(self.limit.get())
        resolve_limit = min(5, catalog_limit) if mode == '快速搜索' else catalog_limit
        cache_key = (str(Path(self.repo).resolve()), keyword.casefold(), tuple(sources), mode, catalog_limit)
        cached = self.search_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 15 * 60:
            self.source_rows = copy.deepcopy(cached[1])
            self.source_status = dict(cached[2])
            self.rebuild_rows()
            self.update_sources()
            self.status.set('已使用 15 分钟内的搜索缓存；需要刷新时切换模式或更改关键词。')
            return
        now = time.monotonic()
        active_sources, skipped = [], []
        for source in sources:
            health = self.source_health.get(source, {})
            if mode == '快速搜索' and health.get('cooldown_until', 0) > now:
                skipped.append(source)
            else:
                active_sources.append(source)
        if not active_sources:
            active_sources = sources[:1]
            skipped = sources[1:]
        self.source_rows = {}
        self.rows.clear()
        self.render()
        self.source_status = {s: ('近期连续失败，快速模式暂时跳过' if s in skipped else '等待搜索') for s in sources}
        self.source_started = {s: time.monotonic() for s in active_sources}
        self.active_search_key = cache_key
        self.update_sources()
        timeout = 45 if mode == '快速搜索' else 90
        self.status.set(f'{mode}中：所有音源只获取目录词条，点击下载后才解析；单个音源最多等待 {timeout} 秒。')
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)
        self.launch(lambda: search(self.repo, active_sources, keyword, resolve_limit, self.cancel,
            lambda kind, payload: self.events.put((kind, payload)), timeout=timeout,
            catalog_limit=catalog_limit), 'search')

    def update_mode_help(self):
        text = '全部点击后解析 · 单源 45 秒 · 跳过近期失败源' if self.search_mode.get() == '快速搜索' else '全部点击后解析 · 单源 90 秒 · 尝试所有源'
        self.mode_help.set(text)

    def rebuild_rows(self):
        self.rows = [song for source in SOURCES for song in self.source_rows.get(source, [])]
        self.render()
        if self.busy: self.set_busy(True)

    def render(self):
        self.table.delete(*self.table.get_children())
        is_lossless = lambda song: lossless(song) or bool(song.get('catalog_lossless'))
        indexes = sorted(range(len(self.rows)), key=lambda i: not is_lossless(self.rows[i]))
        for i in indexes:
            s = self.rows[i]
            if self.only_lossless.get() and not is_lossless(s): continue
            downloadable = s.get('downloadable', True)
            if not downloadable:
                quality = ('无损 · ' if s.get('catalog_lossless') else '') + (s.get('catalog_format') or '未知')
            else:
                quality = ('无损 · ' if lossless(s) else '') + str(s.get('ext') or '未知').upper()
                if s.get('catalog_lossless') and not lossless(s): quality += ' · 目录有 FLAC'
            tags = ('lossless',) if is_lossless(s) else (('unavailable',) if not downloadable else ())
            self.table.insert('', 'end', iid=str(i), values=[s.get('song_name'), s.get('singers'), s.get('album'), quality, s.get('duration'), s.get('file_size'), s['source']], tags=tags)
        direct = sum(s.get('downloadable', True) for s in self.rows)
        resolvable = sum(bool(s.get('catalog_item')) and not s.get('downloadable', True) for s in self.rows)
        self.count.set(f"显示 {len(self.table.get_children())} / {len(self.rows)} 首 · 可下载 {direct + resolvable} 首（待解析 {resolvable}）· 无损 {sum(is_lossless(s) for s in self.rows)} 首")

    def update_sources(self):
        self.source_label.configure(text='  /  '.join(f'{s}：{v}' for s, v in self.source_status.items()))

    def begin_download(self):
        if self.busy and self.operation != 'search': return
        selected = [self.rows[int(i)] for i in self.table.selection()]
        if not selected:
            messagebox.showinfo('选择歌曲', '请先选中需要下载的歌曲。')
            return
        songs = [song for song in selected if song.get('downloadable', True) or song.get('catalog_item')]
        if not songs:
            messagebox.showinfo('暂无可用下载', '选中的词条缺少按需解析信息，请重新搜索或改选其他音源。')
            return
        directory = self.directory.get().strip()
        if not directory:
            self.choose_directory()
            return
        self.save_settings()
        if self.busy:
            self.pending_download = (copy.deepcopy(songs), directory)
            self.download_button.configure(state='disabled')
            self.cancel.set()
            self.status.set(f'已选择 {len(songs)} 首歌曲；正在停止剩余搜索，随后自动解析并下载。')
            return
        self.start_download(songs, directory)

    def begin_choose_quality(self):
        if self.busy and self.operation != 'search': return
        selected = [self.rows[int(i)] for i in self.table.selection()]
        if len(selected) != 1:
            messagebox.showinfo('选择音质', '请先选中一首歌曲；多首歌曲可使用“下载最高无损”。')
            return
        song = selected[0]
        if not song.get('catalog_item'):
            messagebox.showinfo('选择音质', '该词条没有可查询的音质信息，请重新搜索。')
            return
        directory = self.directory.get().strip()
        if not directory:
            self.choose_directory()
            return
        self.save_settings()
        if self.busy:
            self.pending_quality = (copy.deepcopy(song), directory)
            self.quality_button.configure(state='disabled')
            self.cancel.set()
            self.status.set('正在停止剩余搜索，随后查询这首歌可用的无损档位。')
            return
        self.start_quality_lookup(song, directory)

    def start_quality_lookup(self, song, directory):
        self.quality_result = None
        self.status.set(f"正在查询《{song['song_name']}》可用的无损档位…")
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)
        def job():
            try:
                options = resolve_quality_options(self.repo, song, self.cancel)
                self.events.put(('quality_options', (options, directory)))
            except Cancelled:
                pass
            except Exception as exc:
                self.events.put(('error', f'查询音质失败：{exc}'))
        self.launch(job, 'quality')

    def show_quality_options(self, options, directory):
        dialog = tk.Toplevel(self.root)
        dialog.title('选择无损音质')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='以下档位已解析到可用的无损地址。实际位深和采样率以下载后核验为准。',
                  wraplength=540).pack(anchor='w', pady=(0, 10))
        choices = self.make_table(body, ['音质档位', '格式', '预计大小'], [280, 80, 120], height=min(7, len(options)), expand=False)
        for index, option in enumerate(options):
            choices.insert('', 'end', iid=str(index), values=(option['quality_label'], str(option.get('ext') or '').upper(), option.get('file_size') or '未知'))
        choices.selection_set('0')
        choices.focus('0')
        def choose():
            selected = choices.selection()
            if not selected: return
            song = options[int(selected[0])]
            dialog.destroy()
            self.start_download([song], directory)
        buttons = ttk.Frame(body)
        buttons.pack(fill='x', pady=(12, 0))
        ttk.Button(buttons, text='下载所选档位', style='Accent.TButton', command=choose).pack(side='left')
        ttk.Button(buttons, text='取消', command=dialog.destroy).pack(side='right')
        choices.bind('<Double-1>', lambda _: choose())
        dialog.grab_set()
        dialog.focus_set()

    def start_download(self, songs, directory):
        tasks = []
        for song in songs:
            key = self.queue_table.insert('', 'end', values=(song['song_name'], '等待下载', ''))
            self.download_meta[key] = song
            tasks.append((key, song))
        self.update_queue_summary()
        self.toggle_queue(True)
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
                    current = song
                    if not song.get('downloadable', True):
                        self.events.put(('task', (key, '解析中', f"正在按需解析{song.get('source') or '音源'}下载地址…")))
                        current = resolve_catalog_song(self.repo, song, self.cancel, timeout=45)
                        self.download_meta[key] = current
                        self.events.put(('task', (key, '下载中', '解析成功，正在连接…')))
                    if not lossless(current): raise ValueError('该档位不是无损音频，已停止下载')
                    def progress(done, total): self.events.put(('progress', (key, done, total)))
                    path = download(current, directory, self.cancel, progress)
                    note = current.pop('_download_note', '')
                    spec = current.pop('_audio_spec', '')
                    detail = str(path) + (f' · {spec}' if spec else '') + (f' · {note}' if note else '')
                    self.events.put(('task', (key, '已完成', detail)))
                except Cancelled:
                    self.events.put(('task', (key, '已取消', '临时文件已清理')))
                except Exception as exc:
                    self.events.put(('task', (key, '失败', str(exc))))
        self.launch(job, 'download')

    def cancel_current(self):
        self.pending_download = None
        self.cancel.set()

    def poll(self):
        for _ in range(200):
            try: kind, data = self.events.get_nowait()
            except queue.Empty: break
            if kind == 'source_partial':
                source, songs, status = data
                self.source_rows[source] = songs
                self.source_status[source] = status
                self.update_sources()
                self.rebuild_rows()
            elif kind == 'source':
                source, songs, status = data
                elapsed = time.monotonic() - self.source_started.get(source, time.monotonic())
                failed = status.startswith('不可用') or status.startswith('超时') or status.startswith('搜索进程退出')
                health = self.source_health.setdefault(source, {'failures': 0, 'cooldown_until': 0})
                if failed:
                    health['failures'] += 1
                    if health['failures'] >= 2: health['cooldown_until'] = time.monotonic() + 10 * 60
                else:
                    health.update(failures=0, cooldown_until=0)
                existing = self.source_rows.get(source, [])
                if failed and not songs and existing:
                    songs = existing
                    status = f'{len(existing)} 首目录结果 · 链接解析失败，双击下载时可重试'
                self.source_rows[source] = songs
                self.source_status[source] = f'{status} · {elapsed:.1f} 秒'
                self.update_sources()
                self.rebuild_rows()
            elif kind == 'task':
                key, state, detail = data
                self.queue_table.set(key, 1, state)
                self.queue_table.set(key, 2, detail)
                self.queue_table.see(key)
                self.update_queue_summary()
            elif kind == 'progress':
                key, done, total = data
                detail = f'{done / 1048576:.1f} MB' + (f' / {total / 1048576:.1f} MB' if total else '')
                self.queue_table.set(key, 2, detail)
                self.progress.configure(value=done / total * 100 if total else 0)
            elif kind == 'error':
                messagebox.showerror('任务失败', data)
            elif kind == 'quality_options':
                self.quality_result = data
            elif kind == 'done':
                completed_operation = self.operation
                queued_download = self.pending_download if completed_operation == 'search' else None
                queued_quality = self.pending_quality if completed_operation == 'search' else None
                self.pending_download = None
                self.pending_quality = None
                self.progress.stop()
                self.set_busy(False)
                if self.operation == 'search' and self.active_search_key and not self.cancel.is_set():
                    self.search_cache[self.active_search_key] = (time.monotonic(), copy.deepcopy(self.source_rows), dict(self.source_status))
                    if len(self.search_cache) > 10:
                        oldest = min(self.search_cache, key=lambda key: self.search_cache[key][0])
                        self.search_cache.pop(oldest, None)
                self.active_search_key = None
                self.operation = None
                if queued_download:
                    self.start_download(*queued_download)
                elif queued_quality:
                    self.start_quality_lookup(*queued_quality)
                elif completed_operation == 'quality' and self.quality_result:
                    options, directory = self.quality_result
                    self.quality_result = None
                    self.status.set(f'找到 {len(options)} 个可用无损档位，请选择。')
                    self.show_quality_options(options, directory)
                else:
                    if self.cancel.is_set():
                        message = '任务已取消。'
                    elif completed_operation == 'search':
                        message = '搜索结束，结果已缓存 15 分钟。'
                    else:
                        message = '下载任务结束，结果见下方记录。'
                    self.status.set(message)
        self.root.after(100, self.poll)

    def close(self):
        self.pending_download = None
        self.pending_quality = None
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
