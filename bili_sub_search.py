#!/usr/bin/env python3
"""Search Bilibili auto subtitles by uploader UID and keyword.

This tool only calls Bilibili metadata and subtitle endpoints. It does not
download or inspect video, audio, or image content.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(__file__).resolve().parent
COOKIE_FILE = APP_DIR / "bili_cookies.txt"
SUBTITLE_CACHE_DIR = APP_DIR / "subtitle_cache"
HISTORY_DIR = APP_DIR / "history"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)
MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]


class BiliError(RuntimeError):
    pass


def stop_requested(stop_event: object | None) -> bool:
    return bool(stop_event is not None and getattr(stop_event, "is_set")())


def interruptible_sleep(seconds: float, stop_event: object | None = None) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if stop_requested(stop_event):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 0.5))


@dataclass
class Video:
    title: str
    bvid: str
    aid: int | None = None
    pubdate: int | None = None
    list_page: int | None = None
    list_index: int | None = None
    source: str = "投稿"


@dataclass
class Page:
    page: int
    cid: int
    part: str


@dataclass
class Match:
    uploader_uid: str
    keyword: str
    title: str
    bvid: str
    page: int
    part_title: str
    timestamp: str
    start_seconds: float
    end_seconds: float | None
    subtitle: str
    subtitle_url: str
    pubdate: int | None = None
    pubdate_text: str = ""


@dataclass
class SubtitleLine:
    uploader_uid: str
    title: str
    bvid: str
    page: int
    part_title: str
    cid: int
    pubdate: int | None
    subtitle_url: str
    start_seconds: float
    end_seconds: float | None
    subtitle: str


class BiliClient:
    AUTH_COOKIE_NAMES = {"SESSDATA", "DedeUserID", "DedeUserID__ckMd5", "bili_jct"}

    def __init__(self, cookie_file: Path = COOKIE_FILE):
        self.cookie_file = cookie_file
        self.cookie_jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
        if cookie_file.exists():
            self.cookie_jar.load(ignore_discard=True, ignore_expires=True)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self._mixin_key: str | None = None

    def has_auth_cookies(self) -> bool:
        names = {cookie.name for cookie in self.cookie_jar}
        return bool(names & self.AUTH_COOKIE_NAMES)

    def cookie_file_has_auth(self) -> bool:
        if not self.cookie_file.exists():
            return False
        try:
            text = self.cookie_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return any(name in text for name in self.AUTH_COOKIE_NAMES)

    def save_cookies(self, *, force: bool = False) -> None:
        if not force and not self.has_auth_cookies():
            return
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_jar.save(ignore_discard=True, ignore_expires=True)

    def import_cookie_string(self, cookie_string: str) -> None:
        parts = [part.strip() for part in cookie_string.split(";") if "=" in part]
        if not parts:
            raise BiliError("Cookie 字符串为空或格式不对。")

        now = int(time.time())
        for part in parts:
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            cookie = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=".bilibili.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=now + 60 * 60 * 24 * 180,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
            self.cookie_jar.set_cookie(cookie)
        self.save_cookies(force=True)

    def request_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        referer: str | None = "https://www.bilibili.com/",
        signed: bool = False,
    ) -> Any:
        if params:
            params = {k: v for k, v in params.items() if v is not None}
            if signed:
                params = self.sign_wbi(params)
            query = urllib.parse.urlencode(params)
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}{query}"
        data = self.request_bytes(url, referer=referer)
        try:
            payload = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BiliError(f"Response was not JSON: {url}") from exc
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            raise BiliError(f"Bilibili API error {payload.get('code')}: {payload.get('message')}")
        return payload

    def request_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        if url.startswith("//"):
            url = "https:" + url
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        max_attempts = 6
        for attempt in range(1, max_attempts + 1):
            try:
                with self.opener.open(req, timeout=30) as resp:
                    body = resp.read()
                self.save_cookies()
                return body
            except urllib.error.HTTPError as exc:
                if exc.code == 412 and attempt < max_attempts:
                    wait = 30 * attempt
                    print(
                        f"[风控退避] HTTP 412，第 {attempt}/{max_attempts} 次，"
                        f"等待 {wait} 秒后重试。",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                raise BiliError(f"HTTP {exc.code} while requesting {url}") from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.IncompleteRead,
            ) as exc:
                if attempt >= max_attempts:
                    raise BiliError(f"Network timeout/error while requesting {url}: {exc}") from exc
                wait = min(60, 5 * attempt)
                print(
                    f"[重试] 网络请求失败，第 {attempt}/{max_attempts} 次，"
                    f"等待 {wait} 秒：{exc}",
                    file=sys.stderr,
                )
                time.sleep(wait)
        raise BiliError(f"Network error while requesting {url}")

    def sign_wbi(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        params["wts"] = int(time.time())
        mixin_key = self.get_mixin_key()
        clean: dict[str, str] = {}
        for key, value in params.items():
            text = str(value)
            text = re.sub(r"[!'()*]", "", text)
            clean[key] = text
        query = urllib.parse.urlencode(sorted(clean.items()))
        clean["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return clean

    def get_mixin_key(self) -> str:
        if self._mixin_key:
            return self._mixin_key
        payload = self.request_json("https://api.bilibili.com/x/web-interface/nav")
        data = payload.get("data") or {}
        wbi_img = data.get("wbi_img") or {}
        img_url = wbi_img.get("img_url") or ""
        sub_url = wbi_img.get("sub_url") or ""
        img_key = Path(urllib.parse.urlparse(img_url).path).stem
        sub_key = Path(urllib.parse.urlparse(sub_url).path).stem
        raw = img_key + sub_key
        if len(raw) < 64:
            raise BiliError("Could not get WBI keys. Try logging in first.")
        self._mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        return self._mixin_key

    def logged_in(self) -> bool:
        try:
            payload = self.request_json("https://api.bilibili.com/x/web-interface/nav")
        except BiliError:
            return False
        data = payload.get("data") or {}
        return bool(data.get("isLogin"))

    def login_by_qr(self) -> None:
        payload = self.request_json(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            referer="https://passport.bilibili.com/",
        )
        data = payload.get("data") or {}
        qr_url = data.get("url")
        qr_key = data.get("qrcode_key")
        if not qr_url or not qr_key:
            raise BiliError("Could not create Bilibili QR login session.")

        self._write_qr_png_if_possible(qr_url)
        print("请用 Bilibili App 扫码登录：")
        print(qr_url)
        print("注意：不要直接在电脑浏览器点击这个链接，它需要生成二维码后用 Bilibili App 扫。")
        print("扫码确认后保持这个窗口打开，我会自动轮询登录状态。")

        for _ in range(180):
            time.sleep(1)
            poll = self.request_json(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                {"qrcode_key": qr_key},
                referer="https://passport.bilibili.com/",
            )
            result = poll.get("data") or {}
            code = result.get("code")
            message = result.get("message") or ""
            if code == 0:
                self.save_cookies(force=True)
                print("登录成功，Cookie 已保存。")
                return
            if code == 86038:
                raise BiliError("二维码已过期，请重新运行 login。")
            if code == 86090:
                print("已扫码，等待你在手机上确认...")
            elif code == 86101:
                pass
            else:
                print(f"等待登录：{message or code}")
        raise BiliError("登录超时，请重新运行 login。")

    def _write_qr_png_if_possible(self, qr_url: str) -> None:
        try:
            import qrcode  # type: ignore
        except Exception:
            return
        out = APP_DIR / "bili_login_qr.png"
        img = qrcode.make(qr_url)
        img.save(out)
        print(f"二维码图片已保存：{out}")

    def get_uploader_videos(
        self,
        uid: str,
        page_size: int = 42,
        start_page: int = 1,
        end_page: int | None = None,
        title_keyword: str | None = None,
        upload_filter: str = "all",
        stop_event: object | None = None,
    ) -> Iterable[Video]:
        if start_page < 1:
            raise BiliError("--start-page 必须大于等于 1。")
        if end_page is not None and end_page < start_page:
            raise BiliError("--end-page 不能小于 --start-page。")

        pn = start_page
        total: int | None = None
        yielded = 0
        while True:
            if stop_requested(stop_event):
                print("已请求停止，停止继续请求投稿列表。")
                break
            if end_page is not None and pn > end_page:
                break
            try:
                params = {
                    "mid": uid,
                    "pn": pn,
                    "ps": page_size,
                    "order": "pubdate",
                    "platform": "web",
                    "web_location": "1550101",
                    "keyword": title_keyword,
                }
                if upload_filter == "charge":
                    params.update(
                        {
                            "tid": 0,
                            "special_type": "charging",
                            "index": 0,
                            "order_avoided": "true",
                        }
                    )
                payload = self.request_json(
                    "https://api.bilibili.com/x/space/wbi/arc/search",
                    params,
                    signed=True,
                )
            except BiliError as exc:
                if "HTTP 412" in str(exc):
                    wait = 300
                    print(
                        f"[等待] 投稿列表第 {pn} 页触发 HTTP 412，"
                        f"等待 {wait} 秒后继续请求同一页。",
                        file=sys.stderr,
                    )
                    if not interruptible_sleep(wait, stop_event):
                        print("已请求停止，停止继续请求投稿列表。")
                        break
                    continue
                raise
            data = payload.get("data") or {}
            page = data.get("page") or {}
            if total is None:
                total = int(page.get("count") or 0)
                title_filter = f"；标题关键词：{title_keyword}" if title_keyword else ""
                upload_filter_text = "；投稿筛选：充电专属" if upload_filter == "charge" else ""
                if start_page == 1:
                    print(f"发现公开视频约 {total} 个{title_filter}{upload_filter_text}。")
                else:
                    print(f"发现公开视频约 {total} 个{title_filter}{upload_filter_text}；直接从投稿列表第 {start_page} 页开始。")
            vlist = ((data.get("list") or {}).get("vlist")) or []
            if not vlist:
                break
            for index, item in enumerate(vlist, 1):
                bvid = item.get("bvid")
                title = strip_html(item.get("title") or "")
                if title_keyword and title_keyword not in title:
                    continue
                if bvid:
                    yielded += 1
                    yield Video(
                        title=title,
                        bvid=bvid,
                        aid=item.get("aid"),
                        pubdate=item.get("created") or item.get("pubdate"),
                        list_page=pn,
                        list_index=index,
                        source="投稿",
                    )
            if yielded >= total:
                break
            pn += 1

    def _video_from_archive(
        self,
        item: dict[str, Any],
        *,
        list_page: int | None,
        source: str,
        title_keyword: str | None,
    ) -> Video | None:
        bvid = item.get("bvid") or item.get("bv_id")
        title = strip_html(item.get("title") or item.get("name") or "")
        if title_keyword and title_keyword not in title:
            return None
        if not bvid:
            return None
        return Video(
            title=title,
            bvid=bvid,
            aid=item.get("aid"),
            pubdate=item.get("pubdate") or item.get("ctime") or item.get("created"),
            list_page=list_page,
            source=source,
        )

    def get_collection_videos(
        self,
        uid: str,
        page_size: int = 20,
        start_page: int = 1,
        end_page: int | None = None,
        title_keyword: str | None = None,
        stop_event: object | None = None,
    ) -> Iterable[Video]:
        if start_page < 1:
            raise BiliError("--start-page 必须大于等于 1。")
        if end_page is not None and end_page < start_page:
            raise BiliError("--end-page 不能小于 --start-page。")

        yielded_bvids: set[str] = set()
        pn = start_page
        printed_total = False
        while True:
            if stop_requested(stop_event):
                print("已请求停止，停止继续请求合集/系列列表。")
                break
            if end_page is not None and pn > end_page:
                break
            payload = self.request_json(
                "https://api.bilibili.com/x/polymer/web-space/seasons_series_list",
                {
                    "mid": uid,
                    "page_num": pn,
                    "page_size": page_size,
                },
                signed=True,
            )
            data = payload.get("data") or {}
            items_lists = data.get("items_lists") or {}
            page_info = data.get("page") or {}
            if not printed_total:
                total = page_info.get("total") or page_info.get("count") or "未知"
                title_filter = f"；标题关键词：{title_keyword}" if title_keyword else ""
                print(f"发现合集/系列约 {total} 个{title_filter}；从合集/系列列表第 {start_page} 页开始。")
                printed_total = True

            metas: list[tuple[str, dict[str, Any]]] = []
            for item in items_lists.get("seasons_list") or []:
                meta = item.get("meta") or item
                metas.append(("合集", meta))
            for item in items_lists.get("series_list") or []:
                meta = item.get("meta") or item
                metas.append(("系列", meta))
            if not metas:
                break

            for kind, meta in metas:
                if stop_requested(stop_event):
                    break
                collection_name = strip_html(meta.get("name") or meta.get("title") or "")
                source = f"{kind}:{collection_name}" if collection_name else kind
                season_id = meta.get("season_id")
                series_id = meta.get("series_id")
                if kind == "合集" and season_id:
                    videos = self.get_season_archive_videos(
                        uid,
                        int(season_id),
                        source=source,
                        title_keyword=title_keyword,
                        stop_event=stop_event,
                    )
                elif kind == "系列" and series_id:
                    videos = self.get_series_archive_videos(
                        uid,
                        int(series_id),
                        source=source,
                        title_keyword=title_keyword,
                        stop_event=stop_event,
                    )
                else:
                    videos = iter(())
                for video in videos:
                    if video.bvid in yielded_bvids:
                        continue
                    yielded_bvids.add(video.bvid)
                    video.list_page = pn
                    yield video

            total_pages = int(page_info.get("total_page") or 0)
            if total_pages and pn >= total_pages:
                break
            pn += 1

    def get_season_archive_videos(
        self,
        uid: str,
        season_id: int,
        *,
        source: str,
        title_keyword: str | None,
        stop_event: object | None,
    ) -> Iterable[Video]:
        page_size = 100
        pn = 1
        while True:
            if stop_requested(stop_event):
                break
            payload = self.request_json(
                "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
                {
                    "mid": uid,
                    "season_id": season_id,
                    "page_num": pn,
                    "page_size": page_size,
                },
                signed=True,
            )
            data = payload.get("data") or {}
            archives = data.get("archives") or data.get("aids") or []
            if not archives:
                break
            for item in archives:
                if not isinstance(item, dict):
                    continue
                video = self._video_from_archive(item, list_page=pn, source=source, title_keyword=title_keyword)
                if video:
                    yield video
            page_info = data.get("page") or {}
            total_pages = int(page_info.get("total_page") or 0)
            if total_pages and pn >= total_pages:
                break
            if len(archives) < page_size and not total_pages:
                break
            pn += 1

    def get_series_archive_videos(
        self,
        uid: str,
        series_id: int,
        *,
        source: str,
        title_keyword: str | None,
        stop_event: object | None,
    ) -> Iterable[Video]:
        page_size = 100
        pn = 1
        while True:
            if stop_requested(stop_event):
                break
            payload = self.request_json(
                "https://api.bilibili.com/x/series/archives",
                {
                    "mid": uid,
                    "series_id": series_id,
                    "pn": pn,
                    "ps": page_size,
                    "only_normal": "true",
                    "sort": "desc",
                },
            )
            data = payload.get("data") or {}
            archives = data.get("archives") or data.get("aids") or []
            if not archives:
                break
            for item in archives:
                if not isinstance(item, dict):
                    continue
                video = self._video_from_archive(item, list_page=pn, source=source, title_keyword=title_keyword)
                if video:
                    yield video
            page_info = data.get("page") or {}
            total_pages = int(page_info.get("total_page") or 0)
            if total_pages and pn >= total_pages:
                break
            if len(archives) < page_size and not total_pages:
                break
            pn += 1

    def iter_video_source(
        self,
        uid: str,
        *,
        video_source: str,
        upload_filter: str,
        start_page: int,
        end_page: int | None,
        title_keyword: str | None,
        stop_event: object | None,
    ) -> Iterable[Video]:
        seen: set[str] = set()
        sources: list[Iterable[Video]] = []
        if video_source in ("uploads", "all"):
            sources.append(
                self.get_uploader_videos(
                    uid,
                    start_page=start_page,
                    end_page=end_page,
                    title_keyword=title_keyword,
                    upload_filter=upload_filter,
                    stop_event=stop_event,
                )
            )
        if video_source in ("collections", "all"):
            sources.append(
                self.get_collection_videos(
                    uid,
                    start_page=start_page,
                    end_page=end_page,
                    title_keyword=title_keyword,
                    stop_event=stop_event,
                )
            )
        for source_iter in sources:
            for video in source_iter:
                if video.bvid in seen:
                    continue
                seen.add(video.bvid)
                yield video

    def get_pages(self, bvid: str) -> tuple[str, list[Page]]:
        payload = self.request_json(
            "https://api.bilibili.com/x/web-interface/view",
            {"bvid": bvid},
        )
        data = payload.get("data") or {}
        title = data.get("title") or ""
        pages = [
            Page(
                page=int(item.get("page") or idx + 1),
                cid=int(item["cid"]),
                part=item.get("part") or f"P{idx + 1}",
            )
            for idx, item in enumerate(data.get("pages") or [])
            if item.get("cid")
        ]
        return title, pages

    def get_subtitle_urls(self, bvid: str, cid: int) -> list[str]:
        payload = self.request_json(
            "https://api.bilibili.com/x/player/wbi/v2",
            {"bvid": bvid, "cid": cid},
            referer=f"https://www.bilibili.com/video/{bvid}",
            signed=True,
        )
        data = payload.get("data") or {}
        subtitle = data.get("subtitle") or {}
        subtitles = subtitle.get("subtitles") or []
        urls = []
        for item in subtitles:
            url = item.get("subtitle_url")
            if url:
                urls.append(url)
        return urls

    def get_subtitle_body(self, subtitle_url: str, bvid: str) -> list[dict[str, Any]]:
        payload = self.request_json(
            subtitle_url,
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        return payload.get("body") or []


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def format_ts(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    seconds = float(seconds)
    total = int(seconds)
    ms = int(round((seconds - total) * 1000))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if ms:
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_subtitle_ts(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    seconds = float(seconds)
    total = int(seconds)
    ms = int(round((seconds - total) * 1000))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    if ms:
        return f"{m:02d}:{s:02d}.{ms:03d}"
    return f"{m:02d}:{s:02d}"


def parse_subtitle_ts(value: str) -> float:
    parts = value.split(":")
    try:
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return 0.0
    return 0.0


def safe_filename_part(text: str, max_len: int = 120) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_len].rstrip(" .")


def format_date(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def parse_date_arg(value: str | None, label: str) -> int | None:
    if not value:
        return None
    clean = value.strip().replace("-", "")
    if not re.fullmatch(r"\d{8}", clean):
        raise BiliError(f"{label} 日期格式应为 YYYYMMDD 或 YYYY-MM-DD。")
    parsed = time.strptime(clean, "%Y%m%d")
    return int(time.mktime(parsed))


def end_of_day(timestamp: int | None) -> int | None:
    if timestamp is None:
        return None
    return timestamp + 24 * 60 * 60 - 1


def normalize_date_range(
    start_date: str | None,
    end_date: str | None,
) -> tuple[int | None, int | None]:
    start_day = parse_date_arg(start_date, "--start-date")
    end_day = parse_date_arg(end_date, "--end-date")
    if start_day is not None and end_day is not None and start_day > end_day:
        start_day, end_day = end_day, start_day
    start = start_day
    end = end_of_day(end_day)
    return start, end


def in_date_range(video: Video, start_ts: int | None, end_ts: int | None) -> bool:
    if start_ts is None and end_ts is None:
        return True
    if video.pubdate is None:
        return True
    if start_ts is not None and video.pubdate < start_ts:
        return False
    if end_ts is not None and video.pubdate > end_ts:
        return False
    return True


class SubtitleCache:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir

    def part_path(self, uid: str, title: str, bvid: str, page: Page) -> Path:
        uid_dir = self.root_dir / safe_filename_part(uid, max_len=80)
        filename = f"{safe_filename_part(title)} P{page.page} {bvid}.txt"
        return uid_dir / filename

    def meta_path_for_part(self, part_path: Path) -> Path:
        return part_path.parent / "meta" / part_path.with_suffix(".meta.json").name

    def find_part_path(
        self,
        uid: str,
        title: str,
        bvid: str,
        page: Page,
    ) -> Path | None:
        expected = self.part_path(uid, title, bvid, page)
        if expected.exists():
            return expected
        uid_dir = self.root_dir / safe_filename_part(uid, max_len=80)
        if not uid_dir.exists():
            return None
        matches = sorted(uid_dir.glob(f"* P{page.page} {bvid}.txt"))
        if not matches:
            matches = sorted(uid_dir.glob(f"*_P{page.page}_{bvid}.txt"))
        return matches[0] if matches else None

    def get_part(
        self,
        uid: str,
        title: str,
        bvid: str,
        page: Page,
        pubdate: int | None,
    ) -> list[SubtitleLine] | None:
        path = self.find_part_path(uid, title, bvid, page)
        if path is None:
            return None
        lines: list[SubtitleLine] = []
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.rstrip("\n")
                if not raw.strip():
                    continue
                timestamp, sep, subtitle = raw.partition(" ")
                if not sep:
                    continue
                start = parse_subtitle_ts(timestamp)
                lines.append(
                    SubtitleLine(
                        uploader_uid=uid,
                        title=title,
                        bvid=bvid,
                        page=page.page,
                        part_title=page.part,
                        cid=page.cid,
                        pubdate=pubdate,
                        subtitle_url="",
                        start_seconds=start,
                        end_seconds=None,
                        subtitle=subtitle,
                    )
                )
        return lines

    def save_part(
        self,
        uid: str,
        title: str,
        bvid: str,
        page: Page,
        pubdate: int | None,
        lines: list[SubtitleLine],
    ) -> None:
        path = self.part_path(uid, title, bvid, page)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(f"{format_subtitle_ts(line.start_seconds)} {line.subtitle}\n")
        meta = {
            "uid": uid,
            "title": title,
            "bvid": bvid,
            "page": page.page,
            "part_title": page.part,
            "cid": page.cid,
            "pubdate": pubdate,
            "pubdate_text": format_date(pubdate),
        }
        meta_path = self.meta_path_for_part(path)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)


class ScanHistory:
    def __init__(self, root_dir: Path, uid: str):
        self.root_dir = root_dir
        self.uid = uid
        self.uid_dir = root_dir / safe_filename_part(uid, max_len=80)
        self.path = self.uid_dir / "scanned.tsv"
        self.parts_path = self.uid_dir / "scanned_parts.tsv"
        self.global_parts_path = root_dir / "_global" / "scanned_parts.tsv"
        self.scanned_bvids: set[str] = set()
        self.scanned_parts: set[tuple[str, int]] = set()
        self.bvids_with_part_history: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    bvid = raw.split("\t", 1)[0]
                    if bvid:
                        self.scanned_bvids.add(bvid)

        for path in (self.global_parts_path, self.parts_path):
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    parts = raw.split("\t")
                    if len(parts) < 3:
                        continue
                    bvid = parts[0]
                    try:
                        cid = int(parts[2])
                    except ValueError:
                        continue
                    if bvid and cid:
                        self.scanned_parts.add((bvid, cid))
                        self.bvids_with_part_history.add(bvid)

    def has_video(self, bvid: str) -> bool:
        return bvid in self.scanned_bvids

    def has_part(self, bvid: str, cid: int) -> bool:
        return (bvid, cid) in self.scanned_parts

    def filter_unscanned_pages(self, bvid: str, pages: list[Page]) -> list[Page]:
        if bvid in self.scanned_bvids and bvid not in self.bvids_with_part_history:
            return []
        return [page for page in pages if not self.has_part(bvid, page.cid)]

    def mark_video(self, video: Video) -> None:
        if video.bvid in self.scanned_bvids:
            return
        self.uid_dir.mkdir(parents=True, exist_ok=True)
        scanned_at = int(time.time())
        pubdate = video.pubdate or ""
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"{video.bvid}\t{pubdate}\t{scanned_at}\n")
        self.scanned_bvids.add(video.bvid)

    def mark_part(self, video: Video, page: Page) -> None:
        key = (video.bvid, page.cid)
        if key in self.scanned_parts:
            return
        self.uid_dir.mkdir(parents=True, exist_ok=True)
        scanned_at = int(time.time())
        pubdate = video.pubdate or ""
        with self.parts_path.open("a", encoding="utf-8") as f:
            f.write(f"{video.bvid}\t{page.page}\t{page.cid}\t{pubdate}\t{scanned_at}\n")
        self.scanned_parts.add(key)
        self.bvids_with_part_history.add(video.bvid)

    def record_stuck(
        self,
        video: Video,
        page: Page | None,
        stage: str,
        error: BaseException,
    ) -> None:
        self.uid_dir.mkdir(parents=True, exist_ok=True)
        recorded_at = int(time.time())
        page_no = page.page if page else ""
        cid = page.cid if page else ""
        part = page.part if page else ""
        message = str(error).replace("\t", " ").replace("\n", " ")
        with (self.uid_dir / "stuck.tsv").open("a", encoding="utf-8") as f:
            f.write(
                f"{recorded_at}\t{video.bvid}\t{page_no}\t{cid}\t"
                f"{stage}\t{part}\t{message}\n"
            )


def iter_selected_videos(
    videos: Iterable[Video],
    *,
    start_ts: int | None,
    end_ts: int | None,
    start_bvid: str | None,
    end_bvid: str | None,
    page_start_bvid: str | None = None,
    page_end_bvid: str | None = None,
) -> Iterable[Video]:
    if page_start_bvid and (start_bvid or end_bvid):
        raise BiliError("--page-start-bvid 不能和 --start-bvid/--end-bvid 同时使用。")
    if page_end_bvid and (start_bvid or end_bvid):
        raise BiliError("--page-end-bvid 不能和 --start-bvid/--end-bvid 同时使用。")
    if bool(start_bvid) != bool(end_bvid):
        raise BiliError("--start-bvid 和 --end-bvid 需要同时提供。")

    in_bv_range = not start_bvid and not end_bvid
    found_first_endpoint = False
    found_page_start = not page_start_bvid
    for video in videos:
        if not found_page_start:
            if video.bvid == page_start_bvid:
                found_page_start = True
            else:
                continue
        if not start_bvid and not end_bvid and start_ts is not None:
            if video.pubdate is not None and video.pubdate < start_ts:
                return
        if start_bvid and end_bvid:
            if video.bvid in (start_bvid, end_bvid):
                if not found_first_endpoint:
                    found_first_endpoint = True
                    in_bv_range = True
                    if in_date_range(video, start_ts, end_ts):
                        yield video
                    continue
                else:
                    if in_date_range(video, start_ts, end_ts):
                        yield video
                    return
            if not in_bv_range:
                continue

        if in_date_range(video, start_ts, end_ts):
            yield video
            if page_end_bvid and video.bvid == page_end_bvid:
                return


def lines_to_matches(lines: list[SubtitleLine], keyword: str) -> list[Match]:
    matches: list[Match] = []
    for line in lines:
        if keyword in line.subtitle:
            matches.append(
                Match(
                    uploader_uid=line.uploader_uid,
                    keyword=keyword,
                    title=line.title,
                    bvid=line.bvid,
                    page=line.page,
                    part_title=line.part_title,
                    timestamp=format_ts(line.start_seconds),
                    start_seconds=line.start_seconds,
                    end_seconds=line.end_seconds,
                    subtitle=line.subtitle,
                    subtitle_url=line.subtitle_url,
                    pubdate=line.pubdate,
                    pubdate_text=format_date(line.pubdate),
                )
            )
    return matches


def video_log_suffix(video: Video) -> str:
    parts = []
    if video.source:
        parts.append(video.source)
    if video.pubdate:
        parts.append(f"发布日期 {format_date(video.pubdate)}")
    if video.list_page:
        page_text = f"第 {video.list_page} 页"
        if video.list_index:
            page_text += f"；本页第 {video.list_index} 个"
        parts.append(page_text)
    return f" | {' | '.join(parts)}" if parts else ""


def search_subtitles(
    client: BiliClient,
    uid: str,
    keyword: str,
    *,
    rate: float,
    limit_videos: int | None = None,
    start_ts: int | None = None,
    end_ts: int | None = None,
    start_bvid: str | None = None,
    end_bvid: str | None = None,
    start_page: int = 1,
    end_page: int | None = None,
    page_start_bvid: str | None = None,
    page_end_bvid: str | None = None,
    title_keyword: str | None = None,
    video_source: str = "uploads",
    upload_filter: str = "all",
    cache: SubtitleCache | None = None,
    refresh_cache: bool = False,
    history: ScanHistory | None = None,
    scan_mode: str = "all",
    stop_event: object | None = None,
    collect_matches: bool = True,
) -> list[Match]:
    delay = 1.0 / rate if rate > 0 else 0.0
    matches: list[Match] = []
    processed_parts = 0
    video_count = 0

    videos = iter_selected_videos(
        client.iter_video_source(
            uid,
            video_source=video_source,
            upload_filter=upload_filter,
            start_page=start_page,
            end_page=end_page,
            title_keyword=title_keyword,
            stop_event=stop_event,
        ),
        start_ts=start_ts,
        end_ts=end_ts,
        start_bvid=start_bvid,
        end_bvid=end_bvid,
        page_start_bvid=page_start_bvid,
        page_end_bvid=page_end_bvid,
    )
    for video in videos:
        if stop_requested(stop_event):
            print("已请求停止，在线补扫在当前进度处结束。")
            break
        video_count += 1
        if limit_videos and video_count > limit_videos:
            break
        while True:
            try:
                title, pages = client.get_pages(video.bvid)
                title = title or video.title
                break
            except BiliError as exc:
                if history:
                    history.record_stuck(video, None, "pages", exc)
                wait = 300
                print(
                    f"[等待] {video.bvid} 获取分 P 失败：{exc}。"
                    f"已记录 stuck.tsv，等待 {wait} 秒后重试同一视频。",
                    file=sys.stderr,
                )
                if not interruptible_sleep(wait, stop_event):
                    print("已请求停止，在线补扫在当前进度处结束。")
                    return matches

        if scan_mode == "new" and history:
            original_count = len(pages)
            pages = history.filter_unscanned_pages(video.bvid, pages)
            skipped_count = original_count - len(pages)
            if not pages:
                print(f"[跳过整BV] {video.bvid} {title}{video_log_suffix(video)}：{original_count} 个分 P 都已在历史记录中")
                if delay > 0:
                    if not interruptible_sleep(delay, stop_event):
                        print("已请求停止，在线补扫在当前进度处结束。")
                        break
                continue
            if skipped_count:
                remaining = ", ".join(f"P{page.page}" for page in pages[:12])
                if len(pages) > 12:
                    remaining += f" ... 共 {len(pages)} 个"
                print(
                    f"[分P过滤] {video.bvid} {title}{video_log_suffix(video)}：跳过已扫 {skipped_count}/{original_count} 个，"
                    f"本次扫描 {remaining}"
                )

        video_ok = True
        for page in pages:
            if stop_requested(stop_event):
                print("已请求停止，在线补扫在当前进度处结束。")
                return matches
            processed_parts += 1
            print(f"[{processed_parts}] {video.bvid} P{page.page} {title}{video_log_suffix(video)}")
            while True:
                started = time.monotonic()
                try:
                    cached_lines = None
                    if cache and not refresh_cache:
                        cached_lines = cache.get_part(uid, title, video.bvid, page, video.pubdate)
                    if cached_lines is not None:
                        print(f"  使用缓存字幕 {len(cached_lines)} 行")
                        if collect_matches:
                            matches.extend(lines_to_matches(cached_lines, keyword))
                    else:
                        part_lines: list[SubtitleLine] = []
                        subtitle_urls = client.get_subtitle_urls(video.bvid, page.cid)
                        for subtitle_url in subtitle_urls:
                            body = client.get_subtitle_body(subtitle_url, video.bvid)
                            for raw_line in body:
                                content = str(raw_line.get("content") or "")
                                start = float(raw_line.get("from") or 0)
                                end_raw = raw_line.get("to")
                                end = float(end_raw) if end_raw is not None else None
                                part_lines.append(
                                    SubtitleLine(
                                        uploader_uid=uid,
                                        title=title,
                                        bvid=video.bvid,
                                        page=page.page,
                                        part_title=page.part,
                                        cid=page.cid,
                                        pubdate=video.pubdate,
                                        subtitle_url=subtitle_url,
                                        start_seconds=start,
                                        end_seconds=end,
                                        subtitle=content,
                                    )
                                )
                        if cache:
                            cache.save_part(uid, title, video.bvid, page, video.pubdate, part_lines)
                        if collect_matches:
                            matches.extend(lines_to_matches(part_lines, keyword))
                    if history:
                        history.mark_part(video, page)
                    break
                except Exception as exc:
                    if history:
                        history.record_stuck(video, page, "subtitle", exc)
                    wait = 300
                    print(
                        f"[等待] {video.bvid} P{page.page} 字幕失败："
                        f"{type(exc).__name__}: {exc}。已记录 stuck.tsv，"
                        f"等待 {wait} 秒后重试同一分 P。",
                        file=sys.stderr,
                    )
                    if not interruptible_sleep(wait, stop_event):
                        print("已请求停止，在线补扫在当前进度处结束。")
                        return matches
                finally:
                    elapsed = time.monotonic() - started
                    if delay > elapsed:
                        if not interruptible_sleep(delay - elapsed, stop_event):
                            print("已请求停止，在线补扫在当前进度处结束。")
                            return matches
        if history and video_ok:
            history.mark_video(video)

    return matches


def write_csv(path: Path, rows: list[Match]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "uploader_uid",
                "keyword",
                "title",
                "bvid",
                "page",
                "part_title",
                "pubdate",
                "pubdate_text",
                "timestamp",
                "start_seconds",
                "end_seconds",
                "subtitle",
                "subtitle_url",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def write_json(path: Path, rows: list[Match]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([row.__dict__ for row in rows], f, ensure_ascii=False, indent=2)


def command_login(args: argparse.Namespace) -> int:
    client = BiliClient(Path(args.cookie_file))
    if client.logged_in():
        print("当前 Cookie 已登录。")
        return 0
    client.login_by_qr()
    return 0


def command_cookie_login(args: argparse.Namespace) -> int:
    client = BiliClient(Path(args.cookie_file))
    cookie_string = args.cookie or ""
    if not cookie_string:
        print("请粘贴 bilibili.com 的 Cookie 字符串，然后回车：")
        cookie_string = input().strip()
    client.import_cookie_string(cookie_string)
    if client.logged_in():
        print("Cookie 导入成功，当前已登录。")
        return 0
    print("Cookie 已保存，但登录校验未通过。请确认粘贴的是 bilibili.com 登录后的完整 Cookie。")
    return 1


def command_search(args: argparse.Namespace) -> int:
    if args.scan_mode == "new" and args.no_history:
        raise BiliError("--scan-mode new 需要读取 history，不能同时使用 --no-history。")

    client = BiliClient(Path(args.cookie_file))
    if args.login and not client.logged_in():
        client.login_by_qr()
    elif not client.logged_in():
        print("提示：当前未登录或 Cookie 已失效，部分字幕可能拿不到。可先运行 login。", file=sys.stderr)

    start_ts, end_ts = normalize_date_range(args.start_date, args.end_date)
    cache = None if args.no_cache else SubtitleCache(Path(args.cache_dir))
    history = None if args.no_history else ScanHistory(Path(args.history_dir), args.uid)

    rows = search_subtitles(
        client,
        uid=args.uid,
        keyword=args.keyword,
        rate=args.rate,
        limit_videos=args.limit_videos,
        start_ts=start_ts,
        end_ts=end_ts,
        start_bvid=args.start_bvid,
        end_bvid=args.end_bvid,
        start_page=args.start_page,
        end_page=args.end_page,
        page_start_bvid=args.page_start_bvid,
        page_end_bvid=args.page_end_bvid,
        title_keyword=args.title_keyword,
        video_source=args.video_source,
        upload_filter=args.upload_filter,
        cache=cache,
        refresh_cache=args.refresh_cache,
        history=history,
        scan_mode=args.scan_mode,
    )

    output = Path(args.output)
    if output.suffix.lower() == ".json":
        write_json(output, rows)
    else:
        write_csv(output, rows)
    print(f"完成：命中 {len(rows)} 条，已写入 {output.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search Bilibili uploader subtitles by UID and keyword."
    )
    parser.add_argument(
        "--cookie-file",
        default=str(COOKIE_FILE),
        help=f"Cookie 保存路径，默认 {COOKIE_FILE}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="扫码登录并保存 Cookie")
    login.set_defaults(func=command_login)

    cookie_login = sub.add_parser("cookie-login", help="粘贴浏览器 Cookie 并保存登录态")
    cookie_login.add_argument(
        "--cookie",
        default="",
        help="bilibili.com 的完整 Cookie 字符串；不传则运行后交互粘贴",
    )
    cookie_login.set_defaults(func=command_cookie_login)

    search = sub.add_parser("search", help="按 UP 主 UID 搜索字幕关键词")
    search.add_argument("--uid", required=True, help="UP 主 UID/mid")
    search.add_argument("--keyword", required=True, help="要搜索的字幕关键词")
    search.add_argument(
        "--output",
        default="bili_subtitle_matches.csv",
        help="输出路径，支持 .csv 或 .json",
    )
    search.add_argument(
        "--start-date",
        default=None,
        help="发布日期范围开始，格式 YYYYMMDD 或 YYYY-MM-DD，包含当天",
    )
    search.add_argument(
        "--end-date",
        default=None,
        help="发布日期范围结束，格式 YYYYMMDD 或 YYYY-MM-DD，包含当天",
    )
    search.add_argument(
        "--start-bvid",
        default=None,
        help="BV 范围端点之一，按 UP 主页从新到旧顺序扫描，包含端点",
    )
    search.add_argument(
        "--end-bvid",
        default=None,
        help="BV 范围端点之二，按 UP 主页从新到旧顺序扫描，包含端点",
    )
    search.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="UP 投稿列表起始页，直接从这一页开始请求，默认 1",
    )
    search.add_argument(
        "--end-page",
        type=int,
        default=None,
        help="UP 投稿列表结束页，包含该页；不填则一直扫到最早视频",
    )
    search.add_argument(
        "--page-start-bvid",
        default=None,
        help="配合 --start-page 使用：从该页内指定 BV 开始扫描，不请求前面的页面",
    )
    search.add_argument(
        "--page-end-bvid",
        default=None,
        help="配合 --end-page 使用：扫描到该页内指定 BV 后停止，包含该 BV",
    )
    search.add_argument(
        "--title-keyword",
        default=None,
        help="只扫描标题包含该关键词的视频，例如 --title-keyword 北；会使用 B 站空间投稿搜索接口",
    )
    search.add_argument(
        "--video-source",
        choices=("uploads", "collections", "all"),
        default="uploads",
        help="扫描来源：uploads 投稿视频；collections 合集和系列；all 两者都扫",
    )
    search.add_argument(
        "--upload-filter",
        choices=("all", "charge"),
        default="all",
        help="投稿筛选：all 全部类型；charge 只扫描充电专属投稿视频。仅对 uploads 投稿来源生效",
    )
    search.add_argument(
        "--cache-dir",
        default=str(SUBTITLE_CACHE_DIR),
        help=f"字幕缓存文件夹，默认 {SUBTITLE_CACHE_DIR}",
    )
    search.add_argument(
        "--history-dir",
        default=str(HISTORY_DIR),
        help=f"扫描历史文件夹，默认 {HISTORY_DIR}",
    )
    search.add_argument(
        "--scan-mode",
        choices=("all", "new"),
        default="all",
        help="all 扫描范围内全部视频；new 只扫描 history 未记录过的视频",
    )
    search.add_argument(
        "--no-history",
        action="store_true",
        help="不读取也不写入扫描历史",
    )
    search.add_argument(
        "--no-cache",
        action="store_true",
        help="不读取也不写入字幕缓存",
    )
    search.add_argument(
        "--refresh-cache",
        action="store_true",
        help="忽略已有字幕缓存，重新请求并追加保存",
    )
    search.add_argument(
        "--rate",
        type=float,
        default=2.0,
        help="每秒推进几个视频/分 P，跳过历史和扫描字幕共用这个速度，默认 2.0",
    )
    search.add_argument(
        "--limit-videos",
        type=int,
        default=None,
        help="调试用：最多处理多少个视频",
    )
    search.add_argument(
        "--login",
        action="store_true",
        help="搜索前如果未登录，自动进入扫码登录",
    )
    search.set_defaults(func=command_search)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except BiliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
