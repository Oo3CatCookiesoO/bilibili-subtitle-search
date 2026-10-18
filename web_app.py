#!/usr/bin/env python3
"""Local web UI for bili_sub_search."""

from __future__ import annotations

import contextlib
import html
import io
import json
import queue
import re
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import bili_sub_search as core


APP_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
jobs: dict[str, "Job"] = {}
completed_jobs: dict[str, "Job"] = {}
local_jobs: dict[str, "Job"] = {}
login_jobs: dict[str, "Job"] = {}
job_lock = threading.Lock()
run_lock = threading.Lock()
local_search_lock = threading.Lock()


@dataclass
class CacheLine:
    uid: str
    up_name: str
    title: str
    bvid: str
    page: int
    part_title: str
    pubdate_text: str
    timestamp: str
    seconds: float
    subtitle: str


class Job:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.started_at = time.time()
        self.scan_uid = ""
        self.events: queue.Queue[str | None] = queue.Queue()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

    def log(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                self.events.put(line)

    def close(self) -> None:
        self.events.put(None)


class QueueWriter(io.TextIOBase):
    def __init__(self, job: Job):
        self.job = job
        self.buf = ""

    def write(self, text: str) -> int:
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.job.log(line)
        return len(text)

    def flush(self) -> None:
        if self.buf:
            self.job.log(self.buf)
            self.buf = ""


def parse_int(value: str, default: int | None = None) -> int | None:
    value = value.strip()
    return default if not value else int(value)


def parse_float(value: str, default: float) -> float:
    value = value.strip()
    return default if not value else float(value)


def parse_cache_filename(path: Path) -> tuple[str, int, str] | None:
    # Current format: title P1 BVxxxx.txt
    match = re.match(r"^(?P<title>.+?) P(?P<page>\d+) (?P<bvid>BV[0-9A-Za-z]+)\.txt$", path.name)
    if not match:
        # Old format: title_P1_BVxxxx.txt
        match = re.match(r"^(?P<title>.+?)_P(?P<page>\d+)_(?P<bvid>BV[0-9A-Za-z]+)\.txt$", path.name)
    if not match:
        return None
    return match.group("title"), int(match.group("page")), match.group("bvid")


def meta_path_for_subtitle(path: Path) -> Path:
    return path.parent / "meta" / path.with_suffix(".meta.json").name


def load_subtitle_meta(path: Path) -> dict[str, object]:
    for meta_path in (meta_path_for_subtitle(path), path.with_suffix(".meta.json")):
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
    return {}


def read_uploader_profile(uid_dir: Path) -> dict[str, str]:
    path = uid_dir / "uploader.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "uid": str(data.get("uid") or uid_dir.name),
        "name": str(data.get("name") or ""),
    }


def write_uploader_profile(uid: str, name: str) -> None:
    uid_dir = core.SUBTITLE_CACHE_DIR / core.safe_filename_part(uid, max_len=80)
    uid_dir.mkdir(parents=True, exist_ok=True)
    payload = {"uid": uid, "name": name}
    (uid_dir / "uploader.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_uploader_name(client: core.BiliClient, uid: str) -> str:
    payload = client.request_json(
        "https://api.bilibili.com/x/space/wbi/acc/info",
        {"mid": uid},
        signed=True,
    )
    data = payload.get("data") or {}
    return str(data.get("name") or "")


def cached_uploader_options(fetch_missing: bool = False) -> list[dict[str, str]]:
    root = core.SUBTITLE_CACHE_DIR
    if not root.exists():
        return []
    options: list[dict[str, str]] = []
    client: core.BiliClient | None = None
    for uid_dir in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
        profile = read_uploader_profile(uid_dir)
        uid = profile.get("uid") or uid_dir.name
        name = profile.get("name") or ""
        if fetch_missing and not name:
            try:
                if client is None:
                    client = core.BiliClient(core.COOKIE_FILE)
                name = fetch_uploader_name(client, uid)
                if name:
                    write_uploader_profile(uid, name)
            except Exception:
                name = ""
        options.append({"uid": uid, "name": name, "label": f"{name} ({uid})" if name else f"未知UP ({uid})"})
    return options


def iter_cache_lines(cache_dir: Path, uid_filter: str | None = None) -> list[CacheLine]:
    if not cache_dir.exists():
        return []
    roots = [cache_dir / uid_filter] if uid_filter else [p for p in cache_dir.iterdir() if p.is_dir()]
    rows: list[CacheLine] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        uid = root.name
        profile = read_uploader_profile(root)
        up_name = profile.get("name") or "未知UP"
        for path in root.glob("*.txt"):
            parsed = parse_cache_filename(path)
            if not parsed:
                continue
            title, page, bvid = parsed
            meta = load_subtitle_meta(path)
            title = str(meta.get("title") or title)
            page = int(meta.get("page") or page)
            bvid = str(meta.get("bvid") or bvid)
            part_title = str(meta.get("part_title") or "分P标题未知")
            pubdate_text = str(meta.get("pubdate_text") or "发布日期未知")
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                timestamp, sep, subtitle = raw.partition(" ")
                if not sep:
                    continue
                rows.append(
                    CacheLine(
                        uid=uid,
                        up_name=up_name,
                        title=title,
                        bvid=bvid,
                        page=page,
                        part_title=part_title,
                        pubdate_text=pubdate_text,
                        timestamp=timestamp,
                        seconds=core.parse_subtitle_ts(timestamp),
                        subtitle=subtitle,
                    )
                )
    return rows


def cache_text_paths(cache_dir: Path, uid_filter: str | None = None) -> list[Path]:
    if not cache_dir.exists():
        return []
    roots = [cache_dir / uid_filter] if uid_filter else [p for p in cache_dir.iterdir() if p.is_dir()]
    paths: list[Path] = []
    for root in roots:
        if root.exists() and root.is_dir():
            paths.extend(path for path in root.glob("*.txt") if parse_cache_filename(path))
    return sorted(paths, key=lambda path: (path.parent.name, path.name))


def read_cache_lines_from_path(path: Path) -> list[CacheLine]:
    parsed = parse_cache_filename(path)
    if not parsed:
        return []
    title, page, bvid = parsed
    uid = path.parent.name
    profile = read_uploader_profile(path.parent)
    up_name = profile.get("name") or "未知UP"
    meta = load_subtitle_meta(path)
    title = str(meta.get("title") or title)
    page = int(meta.get("page") or page)
    bvid = str(meta.get("bvid") or bvid)
    part_title = str(meta.get("part_title") or "分P标题未知")
    pubdate_text = str(meta.get("pubdate_text") or "发布日期未知")
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        raw_lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    rows: list[CacheLine] = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        timestamp, sep, subtitle = raw.partition(" ")
        if not sep:
            continue
        rows.append(
            CacheLine(
                uid=uid,
                up_name=up_name,
                title=title,
                bvid=bvid,
                page=page,
                part_title=part_title,
                pubdate_text=pubdate_text,
                timestamp=timestamp,
                seconds=core.parse_subtitle_ts(timestamp),
                subtitle=subtitle,
            )
        )
    return rows


def highlight_keyword(text: str, keyword: str) -> str:
    escaped = html.escape(text)
    if not keyword:
        return escaped
    pattern = re.compile(re.escape(keyword))
    return pattern.sub(lambda m: f'<mark class="kw">{html.escape(m.group(0))}</mark>', escaped)


def render_result_block(lines: list[CacheLine], keyword: str) -> str:
    first = lines[0]
    url = f"https://www.bilibili.com/video/{first.bvid}"
    if first.page > 1:
        url += f"?p={first.page}"
    gap = "&nbsp;&nbsp;&nbsp;"
    info = gap.join(
        [
            f"{{{html.escape(first.title)}}}",
            f'{{<span class="partTag">P{first.page}</span>}}',
            f"{{{html.escape(first.part_title)}}}",
            f"{{{html.escape(first.bvid)}}}",
            f"{{UP:{html.escape(first.up_name)} ({html.escape(first.uid)})}}",
            f"{{{html.escape(first.pubdate_text)}}}",
        ]
    )
    sentence_html = []
    for line in lines:
        joiner = "&" if "?" in url else "?"
        ts_url = f"{url}{joiner}t={int(line.seconds)}"
        sentence_html.append(
            f'(<a class="timestampLink" href="{ts_url}" target="_blank">[{html.escape(line.timestamp)}]</a>'
            f'{highlight_keyword(line.subtitle, keyword)})'
        )
    return (
        f'<div class="resultItem" data-video-key="{html.escape(first.bvid)}-P{first.page}">'
        f'<div class="metaBox">{info}</div>'
        f'<div class="link"><a class="videoLink" href="{url}" target="_blank">{url}</a></div>'
        f'<div class="sentences">{" ".join(sentence_html)}</div>'
        '</div>'
    )


def render_local_results(keyword: str, uid: str | None, cache_dir: Path) -> str:
    keyword = keyword.strip()
    if not keyword:
        return '<div class="empty">请输入字幕关键词。</div>'
    grouped: dict[tuple[str, int], list[CacheLine]] = {}
    for line in iter_cache_lines(cache_dir, uid_filter=uid or None):
        if keyword in line.subtitle:
            grouped.setdefault((line.bvid, line.page), []).append(line)
    if not grouped:
        return '<div class="empty">本地字幕缓存里没有找到命中句子。</div>'

    blocks: list[str] = []
    for (_bvid, _page), lines in sorted(grouped.items(), key=lambda item: (item[1][0].title, item[1][0].page)):
        blocks.append(render_result_block(lines, keyword))
    return "\n".join(blocks)


def run_local_search(
    job: Job,
    keyword: str,
    uid: str | None,
    title_keyword: str | None,
    include_part_title: bool,
) -> None:
    with local_search_lock:
        try:
            keyword = keyword.strip()
            if not keyword:
                job.log(json.dumps({"type": "error", "message": "请输入字幕关键词。"}, ensure_ascii=False))
                return
            paths = cache_text_paths(core.SUBTITLE_CACHE_DIR, uid_filter=uid or None)
            total = len(paths)
            job.log(json.dumps({"type": "start", "total": total}, ensure_ascii=False))
            hit_blocks = 0
            for index, path in enumerate(paths, 1):
                while job.pause_event.is_set() and not job.stop_event.is_set():
                    time.sleep(0.2)
                if job.stop_event.is_set():
                    job.log(json.dumps({"type": "stopped", "current": index - 1, "total": total, "hits": hit_blocks}, ensure_ascii=False))
                    return
                lines = read_cache_lines_from_path(path)
                if title_keyword and lines:
                    first = lines[0]
                    title_matched = title_keyword in first.title
                    part_matched = include_part_title and title_keyword in first.part_title
                    if not title_matched and not part_matched:
                        if index == total or index % 10 == 0:
                            job.log(json.dumps({"type": "progress", "current": index, "total": total, "hits": hit_blocks}, ensure_ascii=False))
                        continue
                hits = [line for line in lines if keyword in line.subtitle]
                if hits:
                    hit_blocks += 1
                    job.log(json.dumps({"type": "result", "html": render_result_block(hits, keyword)}, ensure_ascii=False))
                if index == total or index % 10 == 0 or hits:
                    job.log(json.dumps({"type": "progress", "current": index, "total": total, "hits": hit_blocks}, ensure_ascii=False))
            job.log(json.dumps({"type": "done", "current": total, "total": total, "hits": hit_blocks}, ensure_ascii=False))
        except Exception:
            job.log(json.dumps({"type": "error", "message": traceback.format_exc().replace("\n", "\n")}, ensure_ascii=False))
        finally:
            job.close()


def run_search(job: Job, form: dict[str, str]) -> None:
    with run_lock:
        writer = QueueWriter(job)
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                uid = form.get("uid", "").strip()
                job.scan_uid = uid
                if not uid:
                    raise core.BiliError("UID 不能为空。")
                scan_mode = "all" if form.get("scan_all") == "on" else "new"
                client = core.BiliClient(core.COOKIE_FILE)
                if form.get("login") == "on" and not client.logged_in():
                    client.login_by_qr()
                elif not client.logged_in():
                    print("提示：当前未登录或 Cookie 已失效，部分字幕可能拿不到。")

                cache = core.SubtitleCache(core.SUBTITLE_CACHE_DIR)
                history = core.ScanHistory(core.HISTORY_DIR, uid)
                try:
                    up_name = fetch_uploader_name(client, uid)
                    if up_name:
                        write_uploader_profile(uid, up_name)
                        print(f"当前 UP：{up_name} ({uid})")
                except Exception as exc:
                    print(f"提示：暂时没有拿到 UP 名字，仍会继续扫描。{type(exc).__name__}: {exc}")
                print("开始在线补扫/下载字幕。")
                rows = core.search_subtitles(
                    client,
                    uid=uid,
                    keyword="",
                    rate=parse_float(form.get("rate", ""), 2.0),
                    limit_videos=parse_int(form.get("limit_videos", ""), None),
                    start_ts=None,
                    end_ts=None,
                    start_bvid=None,
                    end_bvid=None,
                    start_page=parse_int(form.get("start_page", ""), 1) or 1,
                    end_page=parse_int(form.get("end_page", ""), None),
                    page_start_bvid=(form.get("page_start_bvid", "").strip() or None),
                    page_end_bvid=(form.get("page_end_bvid", "").strip() or None),
                    title_keyword=(form.get("title_keyword", "").strip() or None),
                    video_source=form.get("video_source", "uploads") or "uploads",
                    upload_filter=form.get("upload_filter", "all") or "all",
                    cache=cache,
                    refresh_cache=False,
                    history=history,
                    scan_mode=scan_mode,
                    stop_event=job.stop_event,
                    collect_matches=False,
                )
                print("在线补扫完成。本次只下载/更新字幕缓存，没有执行关键词匹配。")
                print("你可以点击“本地查询字幕”从已缓存字幕里快速检索结果。")
        except Exception:
            writer.flush()
            job.log("任务出错：")
            job.log(traceback.format_exc())
        finally:
            writer.flush()
            job.close()


def rewrite_without_lines(path: Path, should_remove) -> int:
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept: list[str] = []
    removed = 0
    for line in lines:
        if should_remove(line):
            removed += 1
        else:
            kept.append(line)
    if removed:
        path.write_text(("\n".join(kept) + ("\n" if kept else "")), encoding="utf-8")
    return removed


def cleanup_empty_run_history(job: Job) -> dict[str, object]:
    uid = job.scan_uid.strip()
    if not uid:
        return {"ok": False, "error": "找不到这次扫描的 UID，无法清理。"}
    uid_key = core.safe_filename_part(uid, max_len=80)
    cache_dir = core.SUBTITLE_CACHE_DIR / uid_key
    history_dir = core.HISTORY_DIR / uid_key
    if not cache_dir.exists():
        return {"ok": True, "empty_files": 0, "parts_removed": 0, "videos_removed": 0}

    cutoff = job.started_at - 2
    empty_parts: set[tuple[str, int]] = set()
    removed_files = 0
    removed_meta = 0
    for path in cache_dir.glob("*.txt"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size != 0 or stat.st_mtime < cutoff:
            continue
        parsed = parse_cache_filename(path)
        if not parsed:
            continue
        _title, page, bvid = parsed
        empty_parts.add((bvid, page))
        for target in (path, meta_path_for_subtitle(path), path.with_suffix(".meta.json")):
            if target.exists():
                target.unlink()
                if target.suffix == ".json":
                    removed_meta += 1
                else:
                    removed_files += 1

    if not empty_parts:
        return {"ok": True, "empty_files": 0, "parts_removed": 0, "videos_removed": 0, "meta_removed": 0}

    parts_removed = rewrite_without_lines(
        history_dir / "scanned_parts.tsv",
        lambda line: (
            (len(parts := line.split("\t")) >= 2)
            and parts[1].isdigit()
            and (parts[0], int(parts[1])) in empty_parts
        ),
    )
    affected_bvids = {bvid for bvid, _page in empty_parts}
    videos_removed = rewrite_without_lines(
        history_dir / "scanned.tsv",
        lambda line: bool(line.split("\t", 1)[0] in affected_bvids),
    )
    return {
        "ok": True,
        "empty_files": removed_files,
        "meta_removed": removed_meta,
        "parts_removed": parts_removed,
        "videos_removed": videos_removed,
    }


def cleanup_empty_subtitle_paths(
    empty_paths: list[Path],
    cache_root: Path,
    history_root: Path,
) -> dict[str, object]:
    empty_parts_by_uid: dict[str, set[tuple[str, int]]] = {}
    removed_files = 0
    removed_meta = 0
    for path in empty_paths:
        parsed = parse_cache_filename(path)
        if not parsed:
            continue
        _title, page, bvid = parsed
        try:
            uid = path.parent.relative_to(cache_root).parts[0]
        except ValueError:
            continue
        empty_parts_by_uid.setdefault(uid, set()).add((bvid, page))
        for target in (path, meta_path_for_subtitle(path), path.with_suffix(".meta.json")):
            if target.exists():
                target.unlink()
                if target.suffix == ".json":
                    removed_meta += 1
                else:
                    removed_files += 1

    parts_removed = 0
    videos_removed = 0
    for uid, empty_parts in empty_parts_by_uid.items():
        history_dir = history_root / uid
        parts_removed += rewrite_without_lines(
            history_dir / "scanned_parts.tsv",
            lambda line, empty_parts=empty_parts: (
                (len(parts := line.split("\t")) >= 2)
                and parts[1].isdigit()
                and (parts[0], int(parts[1])) in empty_parts
            ),
        )
        affected_bvids = {bvid for bvid, _page in empty_parts}
        videos_removed += rewrite_without_lines(
            history_dir / "scanned.tsv",
            lambda line, affected_bvids=affected_bvids: bool(line.split("\t", 1)[0] in affected_bvids),
        )

    return {
        "ok": True,
        "empty_files": removed_files,
        "meta_removed": removed_meta,
        "parts_removed": parts_removed,
        "videos_removed": videos_removed,
    }


def cleanup_all_empty_subtitles(
    cache_root: Path = core.SUBTITLE_CACHE_DIR,
    history_root: Path = core.HISTORY_DIR,
) -> dict[str, object]:
    if not cache_root.exists():
        return {"ok": True, "empty_files": 0, "parts_removed": 0, "videos_removed": 0, "meta_removed": 0}
    empty_paths: list[Path] = []
    for path in cache_root.glob("*/*.txt"):
        try:
            if path.stat().st_size == 0 and parse_cache_filename(path):
                empty_paths.append(path)
        except OSError:
            continue
    if not empty_paths:
        return {"ok": True, "empty_files": 0, "parts_removed": 0, "videos_removed": 0, "meta_removed": 0}
    return cleanup_empty_subtitle_paths(empty_paths, cache_root, history_root)


def detect_bv(form: dict[str, str]) -> dict[str, object]:
    bvid = form.get("detect_bvid", "").strip() or form.get("page_start_bvid", "").strip()
    if not bvid:
        return {"ok": False, "error": "请输入要检测的 BV 号。"}
    client = core.BiliClient(core.COOKIE_FILE)
    payload = client.request_json("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
    data = payload.get("data") or {}
    owner = data.get("owner") or {}
    return {
        "ok": True,
        "bvid": bvid,
        "title": data.get("title") or "",
        "pubdate": core.format_date(data.get("pubdate")),
        "author": owner.get("name") or "",
        "url": f"https://www.bilibili.com/video/{bvid}",
    }


def login_info() -> dict[str, object]:
    client = core.BiliClient(core.COOKIE_FILE)
    payload = client.request_json("https://api.bilibili.com/x/web-interface/nav")
    data = payload.get("data") or {}
    if not data.get("isLogin"):
        return {"ok": True, "logged_in": False}
    return {
        "ok": True,
        "logged_in": True,
        "name": data.get("uname") or data.get("name") or "",
        "mid": data.get("mid") or "",
    }


def logout() -> None:
    try:
        core.COOKIE_FILE.unlink()
    except FileNotFoundError:
        pass


def cookie_login(cookie_text: str) -> dict[str, object]:
    client = core.BiliClient(core.COOKIE_FILE)
    client.import_cookie_string(cookie_text)
    if not client.logged_in():
        logout()
        return {"ok": False, "error": "Cookie 登录校验未通过，未保存。请确认复制的是 bilibili.com 登录后的完整 Cookie。"}
    client.save_cookies(force=True)
    return {"ok": True, "message": "Cookie 登录成功。"}


def run_qr_login(job: Job) -> None:
    writer = QueueWriter(job)
    try:
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            client = core.BiliClient(core.COOKIE_FILE)
            if client.logged_in():
                print("当前已经登录。")
            else:
                client.login_by_qr()
    except Exception:
        writer.flush()
        job.log("扫码登录出错：")
        job.log(traceback.format_exc())
    finally:
        writer.flush()
        job.close()


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bilibili 字幕关键词搜索</title>
  <style>
    body { margin: 0; background: #f5f7fa; color: #1f2937; font-family: "Microsoft YaHei", Segoe UI, sans-serif; }
    header { height: 54px; display: flex; align-items: center; justify-content: space-between; padding: 0 24px; background: #fff; border-bottom: 1px solid #e5e7eb; }
    h1 { font-size: 18px; margin: 0; }
    .account { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #374151; }
    .account button { height: 30px; padding: 0 10px; font-size: 12px; }
    .loginPanel { display: none; position: fixed; right: 18px; top: 62px; z-index: 20; width: 420px; max-width: calc(100vw - 36px); background: #fff; border: 1px solid #d1d5db; border-radius: 8px; box-shadow: 0 12px 30px rgba(15, 23, 42, .18); padding: 12px; }
    .loginPanel textarea { width: 100%; height: 120px; box-sizing: border-box; border: 1px solid #d1d5db; border-radius: 6px; padding: 8px; resize: vertical; font: 12px/1.5 Consolas, monospace; }
    .loginPanel .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 8px; }
    .loginPanel pre { max-height: 150px; margin-top: 8px; border-radius: 6px; }
    main { display: grid; grid-template-columns: 420px minmax(0, 1fr); gap: 14px; padding: 14px; }
    form, .panel { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; }
    form { padding: 16px; display: grid; gap: 12px; align-content: start; }
    label { display: grid; gap: 5px; font-size: 13px; color: #374151; }
    input, select { height: 34px; border: 1px solid #d1d5db; border-radius: 6px; padding: 0 10px; font-size: 14px; }
    input:focus, select:focus { outline: 2px solid #93c5fd; border-color: #60a5fa; }
    input.hasValue, select.hasValue { background: #eff6ff; border-color: #38bdf8; box-shadow: 0 0 0 1px rgba(14, 165, 233, .12); }
    .primaryField { padding: 10px; border: 1px solid #bae6fd; background: #f0f9ff; border-radius: 8px; }
    .primaryField span { color: #0369a1; font-weight: 800; }
    .primaryField input { height: 40px; font-weight: 700; color: #0f172a; }
    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .checks { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; }
    .check { display: flex; gap: 8px; align-items: center; font-size: 13px; }
    .check input { width: 16px; height: 16px; }
    button { height: 38px; border: 0; border-radius: 6px; background: #0ea5e9; color: #fff; font-weight: 650; cursor: pointer; }
    button.secondary { background: #475569; }
    button.danger { background: #dc2626; }
    button.resume { background: #16a34a; }
    button:disabled { background: #9ca3af; cursor: wait; }
    .hint { color: #6b7280; font-size: 12px; line-height: 1.5; }
    .right { display: grid; grid-template-rows: minmax(260px, 42vh) minmax(320px, 1fr); gap: 14px; }
    .panel { display: grid; grid-template-rows: 42px minmax(0, 1fr); overflow: hidden; }
    .localPanel { grid-template-rows: 42px auto auto minmax(0, 1fr); }
    .head { display: flex; align-items: center; justify-content: space-between; padding: 0 14px; border-bottom: 1px solid #e5e7eb; }
    .logPager { display: flex; align-items: center; gap: 8px; color: #64748b; font-size: 12px; }
    .logPager button { height: 26px; padding: 0 8px; font-size: 12px; }
    pre { margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap; font: 13px/1.5 Consolas, monospace; background: #111827; color: #e5e7eb; }
    .localTools { display: grid; grid-template-columns: minmax(180px, 1fr) minmax(240px, 1.2fr) minmax(180px, .9fr) auto auto auto; gap: 10px; align-items: end; padding: 10px 14px; border-bottom: 1px solid #e5e7eb; }
    .titleFilterBox { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: end; }
    .tinyCheck { display: flex; align-items: center; gap: 6px; height: 34px; white-space: nowrap; color: #4b5563; }
    .tinyCheck input { width: 16px; height: 16px; }
    .progressWrap { padding: 8px 14px; border-bottom: 1px solid #e5e7eb; display: none; gap: 8px; align-items: center; grid-template-columns: 1fr auto; }
    .progressTrack { height: 8px; background: #e5e7eb; border-radius: 999px; overflow: hidden; }
    .progressFill { width: 0%; height: 100%; background: #0ea5e9; transition: width .15s ease; }
    .progressText { font-size: 12px; color: #4b5563; min-width: 120px; text-align: right; }
    #results { padding: 12px; overflow: auto; }
    .resultItem { border: 1px solid #e5e7eb; border-radius: 8px; margin: 0 0 10px; overflow: hidden; background: #fff; }
    .resultItem.viewed { background: #fff7ed; border-color: #fdba74; }
    .metaBox { padding: 8px 10px; background: #eef6ff; border-bottom: 1px solid #dbeafe; font-weight: 650; line-height: 1.8; }
    .resultItem.viewed .metaBox { background: #ffedd5; border-bottom-color: #fed7aa; }
    .partTag { color: #dc2626; font-weight: 800; }
    .link { margin: 8px 10px; font-size: 13px; }
    .sentences { line-height: 1.9; margin: 0 10px 10px; }
    .kw { background: #fef08a; color: #7c2d12; padding: 0 2px; border-radius: 3px; }
    .empty { color: #6b7280; padding: 20px; }
    #detectBox { border: 1px solid #e5e7eb; border-radius: 6px; padding: 8px; display: none; }
    @media (max-width: 980px) { main { grid-template-columns: 1fr; } .localTools { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Bilibili 字幕关键词搜索</h1>
    <div class="account">
      <span id="accountBox">检查登录中...</span>
      <button class="secondary" id="qrLoginBtn" type="button">扫码登录</button>
      <button class="secondary" id="cookieLoginBtn" type="button">Cookie 登录</button>
      <button class="danger" id="logoutBtn" type="button">退出登录</button>
    </div>
  </header>
  <div class="loginPanel" id="loginPanel">
    <strong id="loginPanelTitle">登录</strong>
    <div id="qrHint" class="hint" style="display:none">如果没有二维码图片，请优先使用 Cookie 登录；扫码链接会显示在下面日志里。</div>
    <textarea id="cookieText" placeholder="在这里粘贴 bilibili.com 的整段 Cookie"></textarea>
    <pre id="loginLog" style="display:none"></pre>
    <div class="row">
      <button class="secondary" id="closeLoginPanelBtn" type="button">关闭</button>
      <button id="submitCookieBtn" type="button">保存 Cookie</button>
    </div>
  </div>
  <main>
    <form id="searchForm" autocomplete="off">
      <label class="primaryField"><span>UP 主 UID</span><input name="uid" autocomplete="off"></label>
      <label>扫描来源<select name="video_source"><option value="uploads" selected>投稿视频</option><option value="collections">合集和系列</option><option value="all">投稿 + 合集和系列</option></select></label>
      <label>投稿筛选<select name="upload_filter"><option value="all" selected>全部类型</option><option value="charge">只扫充电专属</option></select></label>
      <label>标题关键词<input name="title_keyword" placeholder="可选：只扫描标题包含指定文字的视频"></label>
      <div class="grid2">
        <label>起始页<input name="start_page" type="number" min="1" placeholder="默认 1"></label>
        <label>结束页<input name="end_page" type="number" min="1" placeholder="不填则可扫到结束 BV 或最早"></label>
      </div>
      <label>页内起点 BV<input name="page_start_bvid" placeholder="从起始页内这个 BV 开始"></label>
      <label>结束 BV<input name="page_end_bvid" placeholder="不填结束页也可用：扫到这个 BV 后停止"></label>
      <div class="grid2">
        <label>检测 BV<input name="detect_bvid" placeholder="可填起点/终点 BV"></label>
        <button class="secondary" id="detectBtn" type="button">检测 BV</button>
      </div>
      <div id="detectBox"></div>
      <div class="grid2">
        <label>速度<input name="rate" type="number" min="0.1" step="0.1" placeholder="默认 2"></label>
        <label>最多视频<input name="limit_videos" type="number" min="1" placeholder="调试用"></label>
      </div>
      <div class="checks">
        <label class="check"><input type="checkbox" name="login">开始前若未登录就扫码</label>
        <label class="check"><input type="checkbox" name="scan_all">扫描全部视频（不勾选则只扫描未扫描的视频）</label>
      </div>
      <div class="grid2">
        <button id="startBtn" type="submit">在线补扫/下载字幕</button>
        <button class="danger" id="cleanupRunBtn" type="button" style="display:none">清理本次空字幕历史</button>
      </div>
      <button class="danger" id="cleanupAllEmptyBtn" type="button">清理全部0KB字幕和历史</button>
      <div class="hint" id="onlineHint">在线停止后，不做自动断点续扫；再次点“在线补扫”并保持“不扫描全部视频”即可按 history 补扫未完成内容。</div>
      <div class="hint">投稿视频按 B 站每页 42 个处理；合集和系列会先读取合集/系列列表，再展开里面的视频。页内起点 BV 只在起始页之后查找；结束页不填但填写结束 BV 时，会从起点向后扫到该 BV 后停止。</div>
    </form>
    <div class="right">
      <section class="panel"><div class="head"><strong>运行日志</strong><div class="logPager"><button class="secondary" id="prevLogPageBtn" type="button">上一页</button><span id="logPageInfo">第 1 页</span><button class="secondary" id="nextLogPageBtn" type="button">下一页</button></div><span id="status">空闲</span></div><pre id="log"></pre></section>
      <section class="panel localPanel">
        <div class="head"><strong>本地字幕搜索结果</strong><span id="resultStatus">空闲</span></div>
        <div class="localTools">
          <label>字幕关键词<input id="localKeyword" autocomplete="off"></label>
          <div class="titleFilterBox">
            <label>本地标题关键词<input id="localTitleKeyword" placeholder="可选：按视频标题筛选"></label>
            <label class="tinyCheck"><input id="includePartTitle" type="checkbox">包含分P标题</label>
          </div>
          <label>搜索已下载 UP<select id="localUid"><option value="">全部已下载 UP</option></select></label>
          <button class="secondary" id="refreshUploaderBtn" type="button">刷新UP</button>
          <button class="secondary" id="localBtn" type="button">本地查询字幕</button>
          <button class="resume" id="localRestartBtn" type="button" style="display:none">重新查询</button>
        </div>
        <div class="progressWrap" id="localProgress">
          <div class="progressTrack"><div class="progressFill" id="progressFill"></div></div>
          <div class="progressText" id="progressText">0 / 0</div>
        </div>
        <div id="results"><div class="empty">输入关键词后点击“本地查询字幕”显示结果。</div></div>
      </section>
    </div>
  </main>
  <script>
    const form = document.getElementById('searchForm');
    const log = document.getElementById('log');
    const statusEl = document.getElementById('status');
    const results = document.getElementById('results');
    const resultStatus = document.getElementById('resultStatus');
    const startBtn = document.getElementById('startBtn');
    const cleanupRunBtn = document.getElementById('cleanupRunBtn');
    const cleanupAllEmptyBtn = document.getElementById('cleanupAllEmptyBtn');
    const prevLogPageBtn = document.getElementById('prevLogPageBtn');
    const nextLogPageBtn = document.getElementById('nextLogPageBtn');
    const logPageInfo = document.getElementById('logPageInfo');
    const localBtn = document.getElementById('localBtn');
    const localRestartBtn = document.getElementById('localRestartBtn');
    const refreshUploaderBtn = document.getElementById('refreshUploaderBtn');
    const detectBtn = document.getElementById('detectBtn');
    const detectBox = document.getElementById('detectBox');
    const accountBox = document.getElementById('accountBox');
    const qrLoginBtn = document.getElementById('qrLoginBtn');
    const cookieLoginBtn = document.getElementById('cookieLoginBtn');
    const logoutBtn = document.getElementById('logoutBtn');
    const loginPanel = document.getElementById('loginPanel');
    const loginPanelTitle = document.getElementById('loginPanelTitle');
    const qrHint = document.getElementById('qrHint');
    const cookieText = document.getElementById('cookieText');
    const loginLog = document.getElementById('loginLog');
    const closeLoginPanelBtn = document.getElementById('closeLoginPanelBtn');
    const submitCookieBtn = document.getElementById('submitCookieBtn');
    const localKeyword = document.getElementById('localKeyword');
    const localTitleKeyword = document.getElementById('localTitleKeyword');
    const includePartTitle = document.getElementById('includePartTitle');
    const localUid = document.getElementById('localUid');
    const localProgress = document.getElementById('localProgress');
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    const params = () => new URLSearchParams(new FormData(form));
    const esc = (text) => String(text ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const localParams = () => {
      const data = new URLSearchParams();
      data.set('keyword', localKeyword.value || '');
      data.set('title_keyword', localTitleKeyword.value || '');
      data.set('include_part_title', includePartTitle.checked ? '1' : '0');
      data.set('uid', localUid.value || '');
      return data;
    };
    const viewedKey = (key) => `bili-sub-viewed:${key}`;
    function markViewed(item) {
      if (!item) return;
      item.classList.add('viewed');
      if (item.dataset.videoKey) localStorage.setItem(viewedKey(item.dataset.videoKey), '1');
    }
    function restoreViewed(scope = results) {
      const items = scope.matches && scope.matches('.resultItem') ? [scope] : Array.from(scope.querySelectorAll('.resultItem'));
      items.forEach((item) => {
        if (item.dataset.videoKey && localStorage.getItem(viewedKey(item.dataset.videoKey))) item.classList.add('viewed');
      });
    }
    function updateProgress(current, total, hits) {
      const pct = total ? Math.round(current * 100 / total) : 0;
      progressFill.style.width = `${pct}%`;
      progressText.textContent = `${current} / ${total}${hits !== undefined ? `，命中 ${hits}` : ''}`;
    }
    let onlineJobId = null;
    let lastOnlineJobId = null;
    let onlineEvents = null;
    let localJobId = null;
    let localEvents = null;
    let localPaused = false;
    let loginEvents = null;
    const LOG_PAGE_SIZE = 1000;
    let logPages = [[]];
    let currentLogPage = 0;
    let logHeaderLines = [];
    function shouldStickToBottom(el) {
      return el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    }
    function updateLogPager() {
      logPageInfo.textContent = `第 ${currentLogPage + 1} / ${logPages.length} 页`;
      prevLogPageBtn.disabled = currentLogPage <= 0;
      nextLogPageBtn.disabled = currentLogPage >= logPages.length - 1;
    }
    function renderLogPage(stick = false) {
      log.textContent = (logPages[currentLogPage] || []).join('\n');
      if (log.textContent) log.textContent += '\n';
      if (stick) log.scrollTop = log.scrollHeight;
      updateLogPager();
    }
    function resetLogPages() {
      logPages = [[]];
      currentLogPage = 0;
      logHeaderLines = [];
      renderLogPage(true);
    }
    function makeLogPageHeader(pageNumber) {
      const header = [`========== 日志第 ${pageNumber} 页 ==========`];
      if (logHeaderLines.length) header.push(...logHeaderLines);
      return header;
    }
    function appendLogLine(text) {
      const newestPage = logPages.length - 1;
      const stick = currentLogPage === newestPage && shouldStickToBottom(log);
      const line = String(text ?? '');
      if (logHeaderLines.length < 8) {
        logHeaderLines.push(line);
      }
      if (logHeaderLines.length < 8 && line.includes('开始在线')) {
        logHeaderLines.push(line);
      }
      let createdNewPage = false;
      if (logPages[newestPage].length >= LOG_PAGE_SIZE) {
        logPages.push(makeLogPageHeader(logPages.length + 1));
        createdNewPage = true;
      }
      logPages[logPages.length - 1].push(line);
      if (stick) {
        currentLogPage = logPages.length - 1;
        renderLogPage(stick);
      } else if (!createdNewPage && currentLogPage === newestPage) {
        renderLogPage(false);
      } else {
        updateLogPager();
      }
    }
    function postControl(path, jobId) {
      const data = new URLSearchParams();
      data.set('id', jobId || '');
      return fetch(path, { method: 'POST', body: data });
    }
    function setOnlineIdle(text = '在线补扫/下载字幕') {
      if (onlineJobId) lastOnlineJobId = onlineJobId;
      onlineJobId = null;
      if (onlineEvents) onlineEvents.close();
      onlineEvents = null;
      startBtn.textContent = text;
      startBtn.classList.remove('danger');
      startBtn.disabled = false;
      if (lastOnlineJobId) cleanupRunBtn.style.display = '';
    }
    function setLocalIdle(text = '本地查询字幕') {
      localJobId = null;
      localPaused = false;
      if (localEvents) localEvents.close();
      localEvents = null;
      localBtn.textContent = text;
      localBtn.classList.remove('danger', 'resume');
      localBtn.disabled = false;
      localRestartBtn.style.display = 'none';
    }
    function updateValueHighlights() {
      document.querySelectorAll('input, select').forEach((el) => {
        const hasValue = el.type === 'checkbox' ? el.checked : String(el.value || '').trim() !== '';
        el.classList.toggle('hasValue', hasValue);
      });
    }
    async function loadLoginInfo() {
      try {
        const res = await fetch('/login-info');
        const p = await res.json();
        if (p.ok && p.logged_in) {
          accountBox.textContent = `已登录：${p.name || '未知账号'}${p.mid ? ` (${p.mid})` : ''}`;
        } else {
          accountBox.textContent = '未登录或 Cookie 已失效';
        }
      } catch (e) {
        accountBox.textContent = '登录状态检测失败';
      }
    }
    function showCookiePanel() {
      loginPanel.style.display = 'block';
      loginPanelTitle.textContent = 'Cookie 登录';
      qrHint.style.display = 'none';
      cookieText.style.display = '';
      submitCookieBtn.style.display = '';
      loginLog.style.display = 'none';
      loginLog.textContent = '';
    }
    function showQrPanel() {
      loginPanel.style.display = 'block';
      loginPanelTitle.textContent = '扫码登录';
      qrHint.style.display = '';
      cookieText.style.display = 'none';
      submitCookieBtn.style.display = 'none';
      loginLog.style.display = 'block';
      loginLog.textContent = '正在创建扫码登录...\n';
    }
    async function loadUploaderOptions() {
      try {
        const previous = localUid.value || '';
        if (refreshUploaderBtn) refreshUploaderBtn.disabled = true;
        const res = await fetch('/cache-uploader-options?fetch_missing=1');
        const p = await res.json();
        if (!p.ok) return;
        localUid.innerHTML = '<option value="">全部已下载 UP</option>' + (p.options || []).map((item) => (
          `<option value="${esc(item.uid)}">${esc(item.label)}</option>`
        )).join('');
        if (previous && Array.from(localUid.options).some((option) => option.value === previous)) {
          localUid.value = previous;
        }
      } catch (e) {}
      finally {
        if (refreshUploaderBtn) refreshUploaderBtn.disabled = false;
      }
    }
    loadLoginInfo();
    loadUploaderOptions();
    updateValueHighlights();
    updateLogPager();
    document.addEventListener('input', updateValueHighlights);
    document.addEventListener('change', updateValueHighlights);
    refreshUploaderBtn.addEventListener('click', loadUploaderOptions);
    prevLogPageBtn.addEventListener('click', () => {
      if (currentLogPage > 0) {
        currentLogPage -= 1;
        renderLogPage(false);
      }
    });
    nextLogPageBtn.addEventListener('click', () => {
      if (currentLogPage < logPages.length - 1) {
        currentLogPage += 1;
        renderLogPage(true);
      }
    });
    localKeyword.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        localBtn.click();
      }
    });
    closeLoginPanelBtn.addEventListener('click', () => {
      loginPanel.style.display = 'none';
      if (loginEvents) loginEvents.close();
      loginEvents = null;
    });
    cookieLoginBtn.addEventListener('click', showCookiePanel);
    submitCookieBtn.addEventListener('click', async () => {
      const data = new URLSearchParams();
      data.set('cookie', cookieText.value || '');
      submitCookieBtn.disabled = true;
      const res = await fetch('/cookie-login', { method: 'POST', body: data });
      const p = await res.json();
      submitCookieBtn.disabled = false;
      if (p.ok) {
        loginPanel.style.display = 'none';
        cookieText.value = '';
        await loadLoginInfo();
      } else {
        loginLog.style.display = 'block';
        loginLog.textContent = p.error || 'Cookie 登录失败';
      }
    });
    qrLoginBtn.addEventListener('click', async () => {
      showQrPanel();
      const res = await fetch('/qr-login-start', { method: 'POST' });
      const p = await res.json();
      if (!p.ok) { loginLog.textContent += p.error || '扫码登录启动失败'; return; }
      if (loginEvents) loginEvents.close();
      loginEvents = new EventSource('/login-events?id=' + encodeURIComponent(p.job_id));
      loginEvents.onmessage = async (e) => {
        if (e.data === '__DONE__') {
          loginLog.textContent += '登录流程结束。\n';
          loginEvents.close();
          loginEvents = null;
          await loadLoginInfo();
          return;
        }
        loginLog.textContent += e.data + '\n';
        loginLog.scrollTop = loginLog.scrollHeight;
      };
      loginEvents.onerror = () => {
        loginLog.textContent += '登录状态连接断开。\n';
        if (loginEvents) loginEvents.close();
        loginEvents = null;
      };
    });
    logoutBtn.addEventListener('click', async () => {
      await fetch('/logout', { method: 'POST' });
      await loadLoginInfo();
      loginPanel.style.display = 'none';
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (onlineJobId) {
        startBtn.disabled = true;
        statusEl.textContent = '正在停止';
        await postControl('/stop', onlineJobId);
        startBtn.textContent = '停止中...';
        return;
      }
      resetLogPages();
      cleanupRunBtn.style.display = 'none';
      statusEl.textContent = '启动中';
      startBtn.disabled = true;
      const res = await fetch('/start', { method: 'POST', body: params() });
      const payload = await res.json();
      if (!payload.ok) { appendLogLine(payload.error || '启动失败'); statusEl.textContent = '失败'; setOnlineIdle(); return; }
      onlineJobId = payload.job_id;
      lastOnlineJobId = payload.job_id;
      startBtn.textContent = '停止在线补扫';
      startBtn.classList.add('danger');
      startBtn.disabled = false;
      statusEl.textContent = '运行中';
      onlineEvents = new EventSource('/events?id=' + encodeURIComponent(payload.job_id));
      onlineEvents.onmessage = (e) => {
        if (e.data === '__DONE__') {
          statusEl.textContent = '完成';
          setOnlineIdle();
          loadUploaderOptions();
          return;
        }
        appendLogLine(e.data);
      };
      onlineEvents.onerror = () => { statusEl.textContent = '连接断开'; setOnlineIdle(); };
    });
    cleanupRunBtn.addEventListener('click', async () => {
      if (!lastOnlineJobId) return;
      if (!confirm('确认清理本次扫描产生的 0KB 字幕文件和对应历史吗？这个操作不能撤销。')) return;
      cleanupRunBtn.disabled = true;
      const data = new URLSearchParams();
      data.set('id', lastOnlineJobId);
      const res = await fetch('/cleanup-empty-run-history', { method: 'POST', body: data });
      const p = await res.json();
      cleanupRunBtn.disabled = false;
      if (p.ok) {
        appendLogLine('');
        appendLogLine(`[清理] 删除本次 0KB 字幕 ${p.empty_files || 0} 个，删除分P历史 ${p.parts_removed || 0} 条，删除整BV历史 ${p.videos_removed || 0} 条。`);
        cleanupRunBtn.style.display = 'none';
        loadUploaderOptions();
      } else {
        appendLogLine('');
        appendLogLine(`[清理失败] ${p.error || '未知错误'}`);
      }
    });
    cleanupAllEmptyBtn.addEventListener('click', async () => {
      if (!confirm('确认删除当前已下载字幕缓存里的全部 0KB 字幕文件，并清理对应历史吗？这个操作不能撤销。')) return;
      cleanupAllEmptyBtn.disabled = true;
      const res = await fetch('/cleanup-all-empty-subtitles', { method: 'POST' });
      const p = await res.json();
      cleanupAllEmptyBtn.disabled = false;
      appendLogLine('');
      if (p.ok) {
        appendLogLine(`[清理全部0KB] 删除 0KB 字幕 ${p.empty_files || 0} 个，删除分P历史 ${p.parts_removed || 0} 条，删除整BV历史 ${p.videos_removed || 0} 条。`);
        loadUploaderOptions();
      } else {
        appendLogLine(`[清理全部0KB失败] ${p.error || '未知错误'}`);
      }
    });
    localBtn.addEventListener('click', async () => {
      if (localJobId && !localPaused) {
        await postControl('/local-search-pause', localJobId);
        localPaused = true;
        localBtn.textContent = '继续查询';
        localBtn.classList.remove('danger');
        localBtn.classList.add('resume');
        localRestartBtn.style.display = '';
        resultStatus.textContent = '已暂停';
        return;
      }
      if (localJobId && localPaused) {
        await postControl('/local-search-resume', localJobId);
        localPaused = false;
        localBtn.textContent = '暂停查询';
        localBtn.classList.remove('resume');
        localBtn.classList.add('danger');
        localRestartBtn.style.display = 'none';
        resultStatus.textContent = '查询中';
        return;
      }
      resultStatus.textContent = '查询中';
      results.innerHTML = '';
      localProgress.style.display = 'grid';
      updateProgress(0, 0, 0);
      localBtn.disabled = true;
      const res = await fetch('/local-search-start', { method: 'POST', body: localParams() });
      const payload = await res.json();
      if (!payload.ok) {
        results.innerHTML = `<div class="empty">${esc(payload.error || '启动失败')}</div>`;
        resultStatus.textContent = '失败';
        setLocalIdle();
        return;
      }
      localJobId = payload.job_id;
      localBtn.textContent = '暂停查询';
      localBtn.classList.add('danger');
      localBtn.disabled = false;
      localEvents = new EventSource('/local-search-events?id=' + encodeURIComponent(payload.job_id));
      localEvents.onmessage = (e) => {
        if (e.data === '__DONE__') { setLocalIdle('本地查询字幕'); return; }
        const msg = JSON.parse(e.data);
        if (msg.type === 'start') {
          updateProgress(0, msg.total || 0, 0);
        } else if (msg.type === 'progress') {
          updateProgress(msg.current || 0, msg.total || 0, msg.hits || 0);
        } else if (msg.type === 'result') {
          const wrap = document.createElement('div');
          wrap.innerHTML = msg.html || '';
          const item = wrap.firstElementChild;
          if (item) {
            results.appendChild(item);
            restoreViewed(item);
          }
        } else if (msg.type === 'done') {
          updateProgress(msg.current || 0, msg.total || 0, msg.hits || 0);
          resultStatus.textContent = `完成，命中 ${msg.hits || 0} 个视频`;
          if (!results.children.length) results.innerHTML = '<div class="empty">本地字幕缓存里没有找到命中句子。</div>';
        } else if (msg.type === 'stopped') {
          updateProgress(msg.current || 0, msg.total || 0, msg.hits || 0);
          resultStatus.textContent = '已停止';
        } else if (msg.type === 'error') {
          results.innerHTML = `<div class="empty">${esc(msg.message || '查询失败')}</div>`;
          resultStatus.textContent = '失败';
        }
      };
      localEvents.onerror = () => { resultStatus.textContent = '连接断开'; setLocalIdle(); };
    });
    localRestartBtn.addEventListener('click', async () => {
      if (localJobId) await postControl('/local-search-stop', localJobId);
      setLocalIdle();
      localBtn.click();
    });
    results.addEventListener('click', (event) => {
      if (event.target.closest('.timestampLink') || event.target.closest('.videoLink')) {
        markViewed(event.target.closest('.resultItem'));
      }
    });
    detectBtn.addEventListener('click', async () => {
      detectBox.style.display = 'block';
      detectBox.textContent = '检测中...';
      const res = await fetch('/detect-bv', { method: 'POST', body: params() });
      const p = await res.json();
      if (!p.ok) { detectBox.textContent = p.error || '检测失败'; return; }
      detectBox.innerHTML = `<strong>${p.title}</strong><br>BV：${p.bvid}<br>作者：${p.author}<br>发布日期：${p.pubdate}<br><a target="_blank" href="${p.url}">${p.url}</a>`;
    });
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML)
            return
        if parsed.path == "/login-info":
            try:
                self._json(login_info())
            except Exception as exc:
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if parsed.path == "/cache-uploader-options":
            qs = parse_qs(parsed.query)
            fetch_missing = (qs.get("fetch_missing") or ["0"])[0] == "1"
            self._json({"ok": True, "options": cached_uploader_options(fetch_missing=fetch_missing)})
            return
        if parsed.path == "/events":
            qs = parse_qs(parsed.query)
            job = jobs.get((qs.get("id") or [""])[0])
            if not job:
                self.send_error(404, "job not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            job_id = (qs.get("id") or [""])[0]
            while True:
                item = job.events.get()
                if item is None:
                    self.wfile.write(b"data: __DONE__\n\n")
                    self.wfile.flush()
                    with job_lock:
                        completed_jobs[job_id] = job
                        jobs.pop(job_id, None)
                    break
                self.wfile.write(f"data: {item.replace(chr(13), '')}\n\n".encode("utf-8"))
                self.wfile.flush()
            return
        if parsed.path == "/local-search-events":
            qs = parse_qs(parsed.query)
            job_id = (qs.get("id") or [""])[0]
            job = local_jobs.get(job_id)
            if not job:
                self.send_error(404, "local job not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                item = job.events.get()
                if item is None:
                    self.wfile.write(b"data: __DONE__\n\n")
                    self.wfile.flush()
                    with job_lock:
                        local_jobs.pop(job_id, None)
                    break
                self.wfile.write(f"data: {item.replace(chr(13), '')}\n\n".encode("utf-8"))
                self.wfile.flush()
            return
        if parsed.path == "/login-events":
            qs = parse_qs(parsed.query)
            job_id = (qs.get("id") or [""])[0]
            job = login_jobs.get(job_id)
            if not job:
                self.send_error(404, "login job not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                item = job.events.get()
                if item is None:
                    self.wfile.write(b"data: __DONE__\n\n")
                    self.wfile.flush()
                    with job_lock:
                        login_jobs.pop(job_id, None)
                    break
                self.wfile.write(f"data: {item.replace(chr(13), '')}\n\n".encode("utf-8"))
                self.wfile.flush()
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
        try:
            if self.path == "/start":
                job_id = str(int(time.time() * 1000))
                job = Job(job_id)
                with job_lock:
                    jobs[job_id] = job
                threading.Thread(target=run_search, args=(job, form), daemon=True).start()
                self._json({"ok": True, "job_id": job_id})
                return
            if self.path == "/stop":
                job = jobs.get(form.get("id", ""))
                if job:
                    job.stop_event.set()
                self._json({"ok": True})
                return
            if self.path == "/cleanup-empty-run-history":
                job_id = form.get("id", "")
                job = jobs.get(job_id) or completed_jobs.get(job_id)
                if not job:
                    self._json({"ok": False, "error": "找不到这次在线扫描任务，可能网页或服务已重启。"})
                    return
                result = cleanup_empty_run_history(job)
                self._json(result)
                return
            if self.path == "/cleanup-all-empty-subtitles":
                self._json(cleanup_all_empty_subtitles())
                return
            if self.path == "/logout":
                logout()
                self._json({"ok": True})
                return
            if self.path == "/cookie-login":
                self._json(cookie_login(form.get("cookie", "")))
                return
            if self.path == "/qr-login-start":
                job_id = str(int(time.time() * 1000))
                job = Job(job_id)
                with job_lock:
                    login_jobs[job_id] = job
                threading.Thread(target=run_qr_login, args=(job,), daemon=True).start()
                self._json({"ok": True, "job_id": job_id})
                return
            if self.path == "/local-search":
                uid = form.get("uid", "").strip() or None
                html_text = render_local_results(form.get("keyword", ""), uid, core.SUBTITLE_CACHE_DIR)
                self._json({"ok": True, "html": html_text})
                return
            if self.path == "/local-search-start":
                job_id = str(int(time.time() * 1000))
                job = Job(job_id)
                with job_lock:
                    local_jobs[job_id] = job
                uid = form.get("uid", "").strip() or None
                keyword = form.get("keyword", "")
                title_keyword = form.get("title_keyword", "").strip() or None
                include_part_title = form.get("include_part_title") == "1"
                threading.Thread(
                    target=run_local_search,
                    args=(job, keyword, uid, title_keyword, include_part_title),
                    daemon=True,
                ).start()
                self._json({"ok": True, "job_id": job_id})
                return
            if self.path == "/local-search-pause":
                job = local_jobs.get(form.get("id", ""))
                if job:
                    job.pause_event.set()
                self._json({"ok": True})
                return
            if self.path == "/local-search-resume":
                job = local_jobs.get(form.get("id", ""))
                if job:
                    job.pause_event.clear()
                self._json({"ok": True})
                return
            if self.path == "/local-search-stop":
                job = local_jobs.get(form.get("id", ""))
                if job:
                    job.stop_event.set()
                    job.pause_event.clear()
                self._json({"ok": True})
                return
            if self.path == "/detect-bv":
                self._json(detect_bv(form))
                return
        except Exception as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self.send_error(404)

    def _send_html(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Web UI: http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止。")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
