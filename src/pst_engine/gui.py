"""tkinter GUI. 표준 라이브러리 위젯만 쓴다(추가 패키지 설치가 불가능한
사내망 전제 — CLAUDE.md).

화면 구성은 검수받은 목업(검색 3분할 / Outlook 검색 문법 / 0건 진단 /
인덱싱 창)을 그대로 따른다. orchestrator/search를 직접 호출하되 인덱싱은
별도 스레드에서 돌리고 ``queue.Queue``를 ``after()``로 폴링해 UI를
갱신한다 — GUI 스레드를 블로킹하지 않는다(T9 계획).
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import Config
from .orchestrator import run_index
from .resolver import fetch_attachment
from .search import MailSearchEngine, SearchHit, SearchResult

_SEARCH_DEBOUNCE_MS = 120


class EmailQuickscanApp:
    def __init__(self, root: tk.Tk, config: Config, db_path: str, state_path: str, errors_path: str) -> None:
        self.root = root
        self.config = config
        self.db_path = db_path
        self.state_path = state_path
        self.errors_path = errors_path

        self._search_engine: MailSearchEngine | None = None
        self._last_result: SearchResult | None = None
        self._debounce_job: str | None = None
        self._index_queue: queue.Queue[tuple] = queue.Queue()
        self._index_thread: threading.Thread | None = None
        self._index_stop = threading.Event()

        root.title("EmailQuickscan")
        root.geometry("1100x650")

        self._build_menu()
        self._build_search_tab()

        self._open_engine()

    # -- 초기화 ----------------------------------------------------------

    def _open_engine(self) -> None:
        if self._search_engine is not None:
            self._search_engine.close()
        try:
            self._search_engine = MailSearchEngine(self.db_path, self.config)
        except Exception as exc:  # DB가 아직 없을 수 있다(첫 실행)
            self._search_engine = None
            self.status_var.set(f"DB를 열지 못했습니다: {exc}")
        self._refresh_folder_list()

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="인덱싱...", command=self._open_index_dialog)
        file_menu.add_separator()
        file_menu.add_command(label="종료", command=self.root.quit)
        menubar.add_cascade(label="파일", menu=file_menu)
        self.root.config(menu=menubar)

    # -- 검색 탭 ----------------------------------------------------------

    def _build_search_tab(self) -> None:
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=8, pady=6)

        self.query_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<KeyRelease>", self._on_query_changed)
        entry.focus_set()

        self.sort_var = tk.StringVar(value="date")
        sort_combo = ttk.Combobox(
            top, textvariable=self.sort_var, values=["date", "relevance"], width=10, state="readonly"
        )
        sort_combo.pack(side=tk.LEFT, padx=4)
        sort_combo.bind("<<ComboboxSelected>>", self._on_query_changed)

        # 폴더 필터: 검색창에 folder:/in:/폴더: 문법을 직접 타이핑하지
        # 않아도, 인덱싱된 폴더 목록에서 골라 같은 부분일치 검색을 쓸 수
        # 있게 한다(search.py의 기존 LIKE 로직을 그대로 재사용 — 새 필터
        # 방식을 만들지 않는다). 값은 _refresh_folder_list()가 채운다.
        self.folder_var = tk.StringVar(value="")
        self.folder_combo = ttk.Combobox(top, textvariable=self.folder_var, values=[""], width=22, state="readonly")
        self.folder_combo.pack(side=tk.LEFT, padx=4)
        self.folder_combo.bind("<<ComboboxSelected>>", self._on_query_changed)

        self.whole_word_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="단어 단위", variable=self.whole_word_var, command=self._on_query_changed).pack(
            side=tk.LEFT, padx=4
        )

        ttk.Button(top, text="내보내기", command=self._export_dialog).pack(side=tk.LEFT, padx=4)

        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        # -- 결과 목록 --
        left = ttk.Frame(paned)
        columns = ("date", "from", "subject", "att")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("date", text="날짜")
        self.tree.heading("from", text="보낸사람")
        self.tree.heading("subject", text="제목")
        self.tree.heading("att", text="첨부")
        self.tree.column("date", width=90, anchor=tk.W)
        self.tree.column("from", width=180, anchor=tk.W)
        self.tree.column("subject", width=320, anchor=tk.W)
        self.tree.column("att", width=40, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_row)
        paned.add(left, weight=6)

        # -- 미리보기 --
        right = ttk.Frame(paned)
        self.preview_header = ttk.Label(right, text="", wraplength=380, justify=tk.LEFT, font=("", 10, "bold"))
        self.preview_header.pack(fill=tk.X, padx=6, pady=(4, 2))
        self.preview_meta = tk.Text(right, height=6, wrap=tk.WORD, state=tk.DISABLED, font=("", 9))
        self.preview_meta.pack(fill=tk.X, padx=6)
        self.preview_body = tk.Text(right, wrap=tk.WORD)
        self.preview_body.tag_configure("hit", background="#fbead4", foreground="#c9761f")
        self.preview_body.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self.preview_body.config(state=tk.DISABLED)

        self.attach_frame = ttk.Frame(right)
        self.attach_frame.pack(fill=tk.X, padx=6, pady=(0, 6))

        paned.add(right, weight=5)

        # -- 상태줄 --
        self.status_var = tk.StringVar(value="검색어를 입력하세요")
        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W).pack(fill=tk.X, padx=8, pady=(0, 6))

        self._current_hits: list[SearchHit] = []
        self._current_full_by_id: dict[str, dict] = {}

    def _refresh_folder_list(self) -> None:
        # 첫 항목은 빈 문자열("전체 폴더" = 필터 없음). DB가 아직 없거나
        # 비어 있을 수 있으므로 조회 실패는 조용히 무시한다(드롭다운이
        # 비어 있는 채로 남을 뿐 검색 자체는 영향받지 않는다).
        folders = [""]
        if self._search_engine is not None:
            try:
                folders += self._search_engine.list_folders()
            except Exception:
                pass
        self.folder_combo["values"] = folders
        if self.folder_var.get() not in folders:
            self.folder_var.set("")

    def _on_query_changed(self, _event: object = None) -> None:
        if self._debounce_job is not None:
            self.root.after_cancel(self._debounce_job)
        self._debounce_job = self.root.after(_SEARCH_DEBOUNCE_MS, self._run_search)

    def _run_search(self) -> None:
        self._debounce_job = None
        if self._search_engine is None:
            self._open_engine()
        if self._search_engine is None:
            return

        query = self.query_var.get()
        folder = self.folder_var.get()
        if folder:
            # cli.py의 _compose_query와 동일한 따옴표 규칙 — 검색창에
            # 보이는 자유 텍스트 자체는 건드리지 않고, 폴더 필터만 검색
            # 시점에 별도로 합성한다(기존 folder: 부분일치 로직 재사용).
            quoted = f'"{folder}"' if " " in folder else folder
            query = f"{query} folder:{quoted}".strip()
        try:
            result = self._search_engine.search(
                query, limit=50, sort=self.sort_var.get(), whole_word=self.whole_word_var.get()
            )
        except Exception as exc:
            self.status_var.set(f"검색 오류: {exc}")
            return

        self._last_result = result
        self._populate_results(result)

    def _populate_results(self, result: SearchResult) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._current_hits = result.hits

        for h in result.hits:
            dt = datetime.fromtimestamp(h.date_utc, tz=timezone.utc).strftime("%m-%d %H:%M")
            att_mark = "📎" if h.has_attachment else ""
            self.tree.insert("", tk.END, iid=h.message_id, values=(dt, h.from_addr, h.subject, att_mark))

        if result.hits:
            first = result.hits[0].message_id
            self.tree.selection_set(first)
            self.tree.focus(first)
            self._show_preview(first)
        else:
            self._clear_preview()

        note = ""
        if result.warnings:
            note = " · " + " / ".join(result.warnings)
        more = " (더 있음)" if result.more_available else ""
        self.status_var.set(f"{len(result.hits)}건{more} · {result.elapsed_ms:.1f}ms{note}")

        if not result.hits and result.diagnosis:
            lines = [
                f"{d.label}: {d.count_display}" + (" <- 원인 후보" if d.is_culprit else "")
                for d in result.diagnosis
            ]
            self.status_var.set(self.status_var.get() + " | " + " / ".join(lines))

    def _on_select_row(self, _event: object = None) -> None:
        sel = self.tree.selection()
        if sel:
            self._show_preview(sel[0])

    def _show_preview(self, message_id: str) -> None:
        if self._search_engine is None:
            return
        full = self._current_full_by_id.get(message_id)
        if full is None:
            full = self._search_engine.get_full(message_id)
            if full is None:
                return
            self._current_full_by_id[message_id] = full

        self.preview_header.config(text=full["subject"])
        dt = datetime.fromtimestamp(full["date_utc"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        meta = (
            f"보낸사람: {full['from_addr']}\n"
            f"받는사람: {full['to_addr']}\n"
            + (f"참조: {full['cc_addr']}\n" if full["cc_addr"] else "")
            + f"날짜: {dt}\n"
            f"폴더: {full['folder_path']}"
        )
        self.preview_meta.config(state=tk.NORMAL)
        self.preview_meta.delete("1.0", tk.END)
        self.preview_meta.insert(tk.END, meta)
        self.preview_meta.config(state=tk.DISABLED)

        self.preview_body.config(state=tk.NORMAL)
        self.preview_body.delete("1.0", tk.END)
        self.preview_body.insert(tk.END, full["body_text"])
        self._highlight_terms(full["body_text"])
        self.preview_body.config(state=tk.DISABLED)

        for child in self.attach_frame.winfo_children():
            child.destroy()
        for i, name in enumerate(full["attachment_names"]):
            attachment_name = str(name)
            btn = ttk.Button(
                self.attach_frame,
                text=f"📎 {attachment_name}",
                command=self._make_attach_handler(message_id, i, attachment_name),
            )
            btn.pack(side=tk.LEFT, padx=(0, 4), pady=2)

    def _make_attach_handler(self, message_id: str, index: int, name: str) -> Callable[[], None]:
        # 람다 대신 별도 메서드로 분리한 이유: for 루프 변수를 클로저가
        # 그대로 캡처하는 실수(마지막 값만 쓰이는 흔한 버그)를 인자
        # 바인딩으로 원천 차단하고, 타입도 더 명확해진다.
        def handler() -> None:
            self._save_attachment(message_id, index, name)

        return handler

    def _highlight_terms(self, body_text: str) -> None:
        if self._last_result is None:
            return

        query = self.query_var.get()
        from .search import parse_query

        parsed = parse_query(query)
        terms = [t.text for t in parsed.positive_terms() if len(t.text) >= 2]
        if not terms:
            return
        for term in terms:
            start = "1.0"
            while True:
                pos = self.preview_body.search(term, start, tk.END, nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(term)}c"
                self.preview_body.tag_add("hit", pos, end)
                start = end

    def _clear_preview(self) -> None:
        self.preview_header.config(text="")
        self.preview_meta.config(state=tk.NORMAL)
        self.preview_meta.delete("1.0", tk.END)
        self.preview_meta.config(state=tk.DISABLED)
        self.preview_body.config(state=tk.NORMAL)
        self.preview_body.delete("1.0", tk.END)
        self.preview_body.config(state=tk.DISABLED)
        for child in self.attach_frame.winfo_children():
            child.destroy()

    def _save_attachment(self, message_id: str, index: int, suggested_name: str) -> None:
        full = self._current_full_by_id.get(message_id)
        if full is None or not full.get("locator_json"):
            messagebox.showerror("첨부 저장 실패", "이 메일에는 첨부 위치 정보가 없습니다.")
            return
        out_path = filedialog.asksaveasfilename(initialfile=suggested_name)
        if not out_path:
            return
        try:
            result = fetch_attachment(full["file_path"], full["locator_json"], index, self.config)
        except Exception as exc:
            messagebox.showerror("첨부 저장 실패", f"원본에서 첨부를 꺼내지 못했습니다:\n{exc}")
            return
        if result is None:
            messagebox.showerror("첨부 저장 실패", "원본 파일에서 첨부를 찾지 못했습니다 (이동/삭제됐을 수 있습니다).")
            return
        _, data = result
        Path(out_path).write_bytes(data)
        messagebox.showinfo("저장됨", f"{out_path}\n({len(data):,} bytes)")

    # -- 내보내기 ----------------------------------------------------------

    def _export_dialog(self) -> None:
        if not self._current_hits:
            messagebox.showinfo("내보내기", "내보낼 결과가 없습니다.")
            return
        out_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not out_path:
            return
        if self._search_engine is None:
            messagebox.showerror("내보내기 실패", "DB 연결이 없습니다.")
            return
        from .storage import MailStorageEngine

        cur = self._search_engine.conn.cursor()
        ids = [h.message_id for h in self._current_hits]
        placeholders = ",".join("?" for _ in ids)
        cur.execute(f"SELECT * FROM mails WHERE message_id IN ({placeholders})", ids)
        rows = cur.fetchall()

        exporter = object.__new__(MailStorageEngine)
        exporter.config = self.config  # type: ignore[attr-defined]
        columns = [
            "message_id", "file_path", "folder_path", "from_addr", "to_addr", "cc_addr",
            "date_utc", "subject", "snippet", "has_attachment", "attachment_names", "size_bytes",
        ]
        count = MailStorageEngine.export_csv(exporter, rows, out_path, columns)
        messagebox.showinfo("내보내기 완료", f"{count}행을 저장했습니다:\n{out_path}")

    # -- 인덱싱 다이얼로그 --------------------------------------------------

    def _open_index_dialog(self) -> None:
        paths = filedialog.askopenfilenames(
            title="PST/OST/MSG 선택", filetypes=[("메일 아카이브", "*.pst *.ost *.msg"), ("모든 파일", "*.*")]
        )
        if not paths:
            return
        dlg = IndexProgressDialog(self.root, self, list(paths))
        dlg.start()

    def start_index_thread(self, paths: list[str], on_event) -> None:
        self._index_stop.clear()

        def worker() -> None:
            run_index(
                paths, self.config, self.db_path, self.state_path, self.errors_path,
                recursive=True, on_event=on_event, stop_flag=self._index_stop.is_set,
            )

        self._index_thread = threading.Thread(target=worker, daemon=True)
        self._index_thread.start()

    def stop_index(self) -> None:
        self._index_stop.set()

    def on_index_finished(self) -> None:
        self._open_engine()
        self._run_search()


class IndexProgressDialog(tk.Toplevel):
    """목업 04번 화면: 진행률 + 파일별 상태 표."""

    def __init__(self, parent: tk.Tk, app: EmailQuickscanApp, paths: list[str]) -> None:
        super().__init__(parent)
        self.title("인덱싱")
        self.geometry("640x420")
        self.app = app
        self.paths = paths
        self._events: queue.Queue[tuple] = queue.Queue()
        self._file_rows: dict[str, str] = {}
        self._done_count = 0
        self._total = len(paths)

        self.progress = ttk.Progressbar(self, mode="determinate", maximum=max(self._total, 1))
        self.progress.pack(fill=tk.X, padx=10, pady=10)

        self.status_var = tk.StringVar(value=f"0 / {self._total} 완료")
        ttk.Label(self, textvariable=self.status_var).pack(anchor=tk.W, padx=10)

        columns = ("status", "mails")
        self.tree = ttk.Treeview(self, columns=columns, show="tree headings")
        self.tree.heading("#0", text="파일")
        self.tree.heading("status", text="상태")
        self.tree.heading("mails", text="메일 수")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(btns, text="중단", command=self._on_stop).pack(side=tk.RIGHT)

        for p in paths:
            name = Path(p).name
            iid = self.tree.insert("", tk.END, text=name, values=("대기", ""))
            self._file_rows[p] = iid

    def start(self) -> None:
        def on_event(kind: str, path: str, payload: object) -> None:
            self._events.put((kind, path, payload))

        self.app.start_index_thread(self.paths, on_event)
        self.after(100, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                kind, path, payload = self._events.get_nowait()
                self._handle_event(kind, path, payload)
        except queue.Empty:
            pass

        if self._done_count < self._total:
            self.after(150, self._poll)
        else:
            self.status_var.set(f"완료: {self._done_count} / {self._total}")
            self.app.on_index_finished()

    def _handle_event(self, kind: str, path: str, payload: object) -> None:
        iid = self._file_rows.get(path)
        if iid is None:
            return
        if kind == "resolved" and isinstance(payload, dict):
            self.tree.set(iid, "status", f"파싱중 ({payload['name']})")
        elif kind == "batch" and isinstance(payload, dict):
            self.tree.set(iid, "status", "쓰는중")
            self.tree.set(iid, "mails", str(payload["total"]))
        elif kind == "done":
            self.tree.set(iid, "status", "완료")
            self.tree.set(iid, "mails", str(payload))
            self._done_count += 1
            self.progress["value"] = self._done_count
        elif kind == "failed":
            self.tree.set(iid, "status", "실패")
            self._done_count += 1
            self.progress["value"] = self._done_count
        elif kind == "skipped":
            self.tree.set(iid, "status", "건너뜀")
            self._done_count += 1
            self.progress["value"] = self._done_count
        self.status_var.set(f"{self._done_count} / {self._total} 완료")

    def _on_stop(self) -> None:
        self.app.stop_index()
        self.status_var.set("중단 요청됨... 현재 배치까지 저장 후 종료합니다")


def run_gui(config: Config, db_path: str, state_path: str, errors_path: str) -> None:
    root = tk.Tk()
    EmailQuickscanApp(root, config, db_path, state_path, errors_path)
    root.mainloop()
