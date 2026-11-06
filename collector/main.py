# -*- coding: utf-8 -*-
"""
ỨNG DỤNG THU THẬP TIN TRƯỜNG HỌC → GOOGLE SHEET (RAW) + ẢNH LÊN GOOGLE DRIVE
Dùng cho Quang Tri Teen Bot.

Cách làm việc (mỗi bài một vòng, xong bài nào lưu bài đó):
  1. Đọc danh sách trường trong dstruong.xlsx (cột school, website)
  2. Vào trang chủ từng trường, lấy link bài viết
  3. Với MỖI bài: đọc nội dung + ngày đăng → tải ảnh về máy → đổi sang JPEG nếu cần
     → tải ảnh lên Google Drive → ghi NGAY 1 dòng vào sheet RAW (cột linkimage = link ảnh trên Drive)
  4. Chương trình có bị ngắt giữa chừng thì các bài đã ghi vẫn còn; chạy lại sẽ bỏ qua bài đã có
     trong RAW và tiếp tục từ bài chưa có.

Không xoá dữ liệu cũ trong RAW (bản cũ dùng worksheet.clear() làm mất trạng thái xử lý của bot).
"""

import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from PIL import Image

import gspread
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as SACredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

try:                                   # dùng kho chứng chỉ của Windows/macOS (đỡ lỗi SSL ở một số web trường)
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

# ═════════════════════════════ CẤU HÌNH ═════════════════════════════
SPREADSHEET_ID = "1h2egAXIXIDgRrpNZCzvqakL_FMrDvV-3lYLGe9GhYNs"   # Sheet của Quang Tri Teen Bot
SHEET_NAME = "RAW"
# Thư mục gốc App_Fb_Giaoduc (theo ID nên di chuyển/đổi tên thoải mái); ảnh vào thư mục con theo tháng, vd 2026-11.
# Đổi được không cần sửa mã: đặt biến môi trường DRIVE_FOLDER_ID (trên GitHub: Settings → Variables → Actions).
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID") or "1ruJGHT7qd7zWC4StL4028LoIZ0ww-Bg1"
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID") or SPREADSHEET_ID
EXCEL_FILE = "dstruong.xlsx"

OAUTH_FILE = "credentials_oauth.json"   # OAuth (tài khoản Google của thầy) — dùng để tải ảnh lên Drive và ghi Sheet
TOKEN_FILE = "token.json"               # tự tạo sau lần đăng nhập đầu tiên
SA_FILE = "credentials.json"            # tài khoản dịch vụ — chỉ dùng ghi Sheet khi không có OAuth

RECENT_DAYS = 7          # chỉ lấy bài trong 7 ngày gần đây
KEEP_UNDATED = False     # True = vẫn lấy bài không tìm được ngày đăng
MAX_POSTS_PER_SITE = 20  # tối đa số bài đọc mỗi trường mỗi lần chạy
MAX_IMAGES = 3           # tối đa số ảnh mỗi bài
MIN_IMG_W, MIN_IMG_H = 400, 300
MAX_IMG_SIDE = 2000      # ảnh lớn hơn sẽ thu nhỏ trước khi tải lên Drive
DELAY = 1.0              # nghỉ giữa các bài (giây) cho đỡ nặng web trường

ARTICLE_KEYS = ["/tin-tuc", "/chi-tiet", "/thong-bao", "/view", "/news", "/article", "/bai-viet",
                "/hoat-dong", "/tin-", "/su-kien"]
BAD_IMG_KEYS = ["logo", "icon", "banner", "avatar", "thumb", "ads", "favicon", "loading", "spacer", "blank"]

STATE_FILE = "collector_state.json"     # nhớ link bài cũ đã bỏ qua + ảnh đã tải lên (đỡ làm lại)
LOG_FILE = "collector.log"

VN_TZ = timezone(timedelta(hours=7))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
COLUMNS = ["post_id", "source", "title", "content", "time", "url", "hash", "saved_at", "status", "linkimage"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.6",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")])
log = logging.getLogger("collector")


def now_vn():
    return datetime.now(VN_TZ)


# ═════════════════════════════ GOOGLE: ĐĂNG NHẬP, SHEET, DRIVE ═════════════════════════════
class Google:
    def __init__(self):
        self.creds = self._user_creds()
        if self.creds:
            self.gc = gspread.authorize(self.creds)
            self.drive = build("drive", "v3", credentials=self.creds, cache_discovery=False)
        else:
            if not os.path.exists(SA_FILE):
                raise SystemExit("❌ Không có credentials_oauth.json hoặc credentials.json")
            sa = SACredentials.from_service_account_file(SA_FILE, scopes=SCOPES)
            self.gc = gspread.authorize(sa)
            self.drive = build("drive", "v3", credentials=sa, cache_discovery=False)
            log.warning("⚠️ Đang dùng tài khoản dịch vụ: Google không cho tài khoản dịch vụ lưu file vào Drive cá nhân, "
                        "nên tải ảnh có thể lỗi. Nên dùng credentials_oauth.json.")
        self.ws = self._worksheet()
        self.header = self._ensure_header()
        self._month_folders = {}

    @staticmethod
    def _user_creds():
        env_tok = os.environ.get("GOOGLE_OAUTH_TOKEN")       # chạy trên GitHub Actions: nội dung token.json cất trong Secrets
        if env_tok:
            creds = UserCredentials.from_authorized_user_info(json.loads(env_tok), SCOPES)
            if not creds.valid:
                creds.refresh(Request())
            return creds
        if not os.path.exists(OAUTH_FILE):
            return None
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = UserCredentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None
            if not creds or not creds.valid:
                from google_auth_oauthlib.flow import InstalledAppFlow
                log.info("🔑 Mở trình duyệt để đăng nhập Google (chỉ lần đầu)...")
                creds = InstalledAppFlow.from_client_secrets_file(OAUTH_FILE, SCOPES).run_local_server(port=0)
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        return creds

    def _worksheet(self):
        sh = self.gc.open_by_key(SPREADSHEET_ID)
        try:
            return sh.worksheet(SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            return sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(COLUMNS))

    def _ensure_header(self):
        row1 = [h.strip() for h in self.ws.row_values(1)]
        if not any(row1):
            self.ws.update(range_name="A1", values=[COLUMNS])
            return list(COLUMNS)
        missing = [c for c in COLUMNS if c not in row1]
        if missing:                                       # thêm cột còn thiếu vào cuối, không đụng dữ liệu cũ
            start = len(row1) + 1
            self.ws.update(range_name=gspread.utils.rowcol_to_a1(1, start), values=[missing])
            row1 += missing
        return row1

    def existing_keys(self):
        """Tập url / post_id / hash đã có trong RAW để không ghi trùng."""
        keys = set()
        rows = self._retry(self.ws.get_all_values)
        if not rows:
            return keys
        hdr = rows[0]
        idx = {c: hdr.index(c) for c in ("url", "post_id", "hash") if c in hdr}
        for r in rows[1:]:
            for c, i in idx.items():
                if i < len(r) and r[i].strip():
                    keys.add(r[i].strip())
        return keys

    def append(self, record):
        values = [str(record.get(h, "")) for h in self.header]
        self._retry(lambda: self.ws.append_row(values, value_input_option="RAW", insert_data_option="INSERT_ROWS"))

    def upload_image(self, data, name, mime):
        folder = self._month_folder()
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        f = self._retry(lambda: self.drive.files().create(
            body={"name": name, "parents": [folder]}, media_body=media,
            fields="id", supportsAllDrives=True).execute())
        return "https://drive.google.com/file/d/%s/view" % f["id"]

    def _month_folder(self):
        """Ảnh mỗi tháng vào 1 thư mục con (vd. 2026-11) cho gọn."""
        name = now_vn().strftime("%Y-%m")
        if name in self._month_folders:
            return self._month_folders[name]
        q = ("name = '%s' and '%s' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
             % (name, DRIVE_FOLDER_ID))
        res = self._retry(lambda: self.drive.files().list(q=q, fields="files(id)", supportsAllDrives=True,
                                                          includeItemsFromAllDrives=True).execute())
        if res.get("files"):
            fid = res["files"][0]["id"]
        else:
            fid = self._retry(lambda: self.drive.files().create(
                body={"name": name, "parents": [DRIVE_FOLDER_ID], "mimeType": "application/vnd.google-apps.folder"},
                fields="id", supportsAllDrives=True).execute())["id"]
        self._month_folders[name] = fid
        return fid

    @staticmethod
    def _retry(fn, tries=5):
        for i in range(tries):
            try:
                return fn()
            except Exception as e:                       # quá hạn mức API (429/5xx) → chờ rồi thử lại
                msg = str(e)
                if i == tries - 1 or not re.search(r"429|500|502|503|504|Quota|quota|rate|timed out|Connection", msg):
                    raise
                wait = 5 * (i + 1)
                log.info("   … Google bận (%s), chờ %ss", msg[:60], wait)
                time.sleep(wait)


# ═════════════════════════════ BỘ NHỚ TRẠNG THÁI (chạy lại không làm lại) ═════════════════════════════
class State:
    def __init__(self):
        self.data = {"old_urls": {}, "images": {}}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass
        cut = (now_vn() - timedelta(days=30)).strftime("%Y-%m-%d")   # quên link cũ sau 30 ngày
        self.data["old_urls"] = {u: d for u, d in self.data["old_urls"].items() if d >= cut}

    def save(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)

    def is_old(self, url):
        return url in self.data["old_urls"]

    def mark_old(self, url):
        self.data["old_urls"][url] = now_vn().strftime("%Y-%m-%d")
        self.save()

    def image_link(self, digest):
        return self.data["images"].get(digest)

    def remember_image(self, digest, link):
        self.data["images"][digest] = link
        if len(self.data["images"]) > 5000:              # giữ gọn file
            self.data["images"] = dict(list(self.data["images"].items())[-3000:])
        self.save()


# ═════════════════════════════ THU THẬP ═════════════════════════════
class SchoolScraper:
    def __init__(self, google, state):
        self.g = google
        self.state = state
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.stats = {"saved": 0, "images": 0, "old": 0, "skip": 0, "error": 0}

    # ---------- danh sách trường ----------
    @staticmethod
    def load_sources(path=EXCEL_FILE):
        df = pd.read_excel(path)
        df.columns = df.columns.str.strip().str.lower()
        if "school" not in df.columns or "website" not in df.columns:
            raise SystemExit("❌ File Excel phải có 2 cột: school, website")
        out, seen = [], set()
        for _, row in df.iterrows():
            school, site = str(row["school"]).strip(), str(row["website"]).strip()
            if not site or site.lower() == "nan":
                continue
            if not site.startswith("http"):
                site = "http://" + site
            key = urlparse(site).netloc.lower().replace("www.", "")
            if key in seen:                              # bỏ dòng trùng website
                continue
            seen.add(key)
            out.append({"school": school, "website": site.rstrip("/")})
        log.info("✅ Đã tải %d website từ %s", len(out), path)
        return out

    def get(self, url, timeout=20):
        for i in range(2):
            try:
                r = self.s.get(url, timeout=timeout, allow_redirects=True)
                r.raise_for_status()
                return r
            except requests.exceptions.SSLError as e:
                log.info("   ⚠️ Lỗi chứng chỉ SSL ở %s — bỏ qua (%s)", urlparse(url).netloc, str(e)[:80])
                return None
            except Exception as e:
                if i == 1:
                    log.info("   ⚠️ Không tải được %s (%s)", url[:80], str(e)[:80])
                    return None
                time.sleep(2)

    @staticmethod
    def soup_of(r):
        if r.encoding is None or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        return BeautifulSoup(r.text, "html.parser")

    # ---------- link bài viết trên trang chủ ----------
    def article_links(self, site):
        r = self.get(site)
        if not r:
            return []
        soup = self.soup_of(r)
        host = urlparse(r.url).netloc.lower().replace("www.", "")
        links, seen = [], set()
        for a in soup.find_all("a", href=True):
            title = " ".join(a.get_text(" ", strip=True).split()) or (a.get("title") or "").strip()
            if len(title) < 25:
                continue
            href = a["href"].strip()
            if not any(k in href.lower() for k in ARTICLE_KEYS):
                continue
            url = urljoin(r.url, href).split("#")[0]
            if urlparse(url).netloc.lower().replace("www.", "") != host:   # chỉ lấy bài cùng trang trường
                continue
            if url in seen:
                continue
            seen.add(url)
            links.append((url, title))
        return links[:MAX_POSTS_PER_SITE]

    # ---------- chi tiết bài ----------
    def article_detail(self, url):
        r = self.get(url, timeout=25)
        if not r:
            return None
        soup = self.soup_of(r)
        content_div = soup.find(["div", "article"], class_=re.compile(
            r"(^|\s)(content|post-content|detail-content|news-detail|entry-content|article-content|noidung|chitiet)(\s|$)", re.I))
        if content_div:
            text = "\n".join(t.strip() for t in content_div.get_text("\n").split("\n") if t.strip())
        else:
            text = "\n".join(p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 10)

        # Ảnh: ảnh đại diện (og:image) trước, rồi ảnh trong nội dung (kể cả ảnh tải chậm data-src)
        cands = []
        og = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "og:image"})
        if og and og.get("content"):
            cands.append(urljoin(r.url, og["content"].strip()))
        for img in (content_div or soup).find_all("img"):
            src = img.get("data-src") or img.get("data-original") or img.get("data-lazy-src") or img.get("src") or ""
            if not src and img.get("srcset"):
                src = img["srcset"].split(",")[-1].strip().split(" ")[0]
            src = src.strip()
            if src and not src.startswith("data:"):
                cands.append(urljoin(r.url, src))
        cands = [u for u in dict.fromkeys(cands) if not any(k in u.lower() for k in BAD_IMG_KEYS)]
        return {"content": text[:5000], "images": cands, "date": self.extract_date(soup)}

    # ---------- ngày đăng ----------
    def extract_date(self, soup):
        for attrs in ({"property": "article:published_time"}, {"name": "pubdate"}, {"name": "publish-date"},
                      {"name": "date"}, {"itemprop": "datePublished"}):
            m = soup.find("meta", attrs=attrs)
            if m and m.get("content"):
                d = self.parse_date(m["content"])
                if d:
                    return d
        for t in soup.find_all("time"):
            d = self.parse_date(t.get("datetime", "") or t.get_text(" ", strip=True))
            if d:
                return d
        for el in soup.find_all(class_=re.compile(r"date|time|ngay|post-meta|published", re.I)):
            d = self.parse_date(el.get_text(" ", strip=True))
            if d:
                return d
        return self.parse_date(soup.get_text(" ", strip=True)[:6000])

    @staticmethod
    def parse_date(text):
        text = str(text or "")
        for pat, fmt in ((r"(\d{4}-\d{1,2}-\d{1,2})", "%Y-%m-%d"), (r"(\d{1,2}/\d{1,2}/\d{4})", "%d/%m/%Y"),
                         (r"(\d{1,2}-\d{1,2}-\d{4})", "%d-%m-%Y"), (r"(\d{1,2}\.\d{1,2}\.\d{4})", "%d.%m.%Y")):
            m = re.search(pat, text)
            if m:
                try:
                    d = datetime.strptime(m.group(1), fmt)
                    if 2015 <= d.year <= now_vn().year + 1:
                        return d
                except ValueError:
                    pass
        return None

    # ---------- ảnh: tải về → kiểm tra → đổi JPEG → lên Drive ----------
    def fetch_image(self, url, referer):
        try:
            r = self.s.get(url, timeout=20, headers={"Referer": referer, "Accept": "image/*,*/*;q=0.5"})
            r.raise_for_status()
            data = r.content
        except Exception:
            return None
        if len(data) < 15000:
            return None
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:
            return None
        w, h = img.size
        if w < MIN_IMG_W or h < MIN_IMG_H or not (0.3 <= w / h <= 3):
            return None
        fmt = (img.format or "").upper()
        if fmt == "JPEG" and max(w, h) <= MAX_IMG_SIDE:
            return data, "image/jpeg", "jpg"
        if fmt == "PNG" and max(w, h) <= MAX_IMG_SIDE and len(data) < 3_000_000:
            return data, "image/png", "png"
        # WebP / GIF / BMP / ảnh quá lớn → JPEG (Facebook và Google Slides không nhận WebP)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img.thumbnail((MAX_IMG_SIDE, MAX_IMG_SIDE))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue(), "image/jpeg", "jpg"

    def upload_images(self, cands, post_id, referer):
        links = []
        for u in cands:
            if len(links) >= MAX_IMAGES:
                break
            got = self.fetch_image(u, referer)
            if not got:
                continue
            data, mime, ext = got
            digest = hashlib.md5(data).hexdigest()
            link = self.state.image_link(digest)
            if not link:
                try:
                    link = self.g.upload_image(data, "%s_%d.%s" % (post_id, len(links) + 1, ext), mime)
                except Exception as e:
                    log.info("      ⚠️ Lỗi tải ảnh lên Drive: %s", str(e)[:120])
                    continue
                self.state.remember_image(digest, link)
                self.stats["images"] += 1
            if link not in links:
                links.append(link)
        return links

    # ---------- chạy ----------
    def run(self, sources):
        existing = self.g.existing_keys()
        log.info("📄 RAW đang có %d khoá (url/post_id/hash) — các bài này sẽ được bỏ qua", len(existing))
        cutoff = now_vn().replace(tzinfo=None) - timedelta(days=RECENT_DAYS)
        for src in sources:
            school, site = src["school"], src["website"]
            log.info("\n🔎 %s — %s", school, site)
            links = self.article_links(site)
            if not links:
                log.info("   (không tìm thấy bài nào)")
                continue
            for url, title in links:
                post_id = hashlib.md5((url + title).encode("utf-8")).hexdigest()[:12]
                if url in existing or post_id in existing:
                    continue                                     # đã có trong RAW
                if self.state.is_old(url):
                    self.stats["old"] += 1
                    continue                                     # lần trước đã biết là bài cũ
                try:
                    det = self.article_detail(url)
                    if not det or len(det["content"]) < 40:
                        self.stats["skip"] += 1
                        log.info("   ⏩ Không đọc được nội dung: %s", title[:60])
                        continue
                    d = det["date"]
                    if (d and d < cutoff) or (not d and not KEEP_UNDATED):
                        self.stats["old"] += 1
                        self.state.mark_old(url)
                        log.info("   ⏩ Bài cũ/không rõ ngày: %s", title[:60])
                        continue
                    log.info("   + %s", title[:70])
                    images = self.upload_images(det["images"], post_id, url)
                    rec = {
                        "post_id": post_id, "source": school, "title": title, "content": det["content"],
                        "time": d.strftime("%Y-%m-%d") if d else "", "url": url,
                        "hash": hashlib.md5((url + title).encode("utf-8")).hexdigest(),
                        "saved_at": now_vn().isoformat(timespec="seconds"),
                        "status": "new", "linkimage": ",".join(images),
                    }
                    self.g.append(rec)                           # ghi ngay — ngắt chương trình cũng không mất bài này
                    existing.update([url, post_id, rec["hash"]])
                    self.stats["saved"] += 1
                    log.info("      ✅ Đã lưu vào RAW (%d ảnh)", len(images))
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self.stats["error"] += 1
                    log.info("      ❌ Lỗi: %s", str(e)[:150])
                time.sleep(DELAY)
        return self.stats


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    t0 = time.time()
    google = Google()
    bot = SchoolScraper(google, State())
    try:
        st = bot.run(bot.load_sources(EXCEL_FILE))
    except KeyboardInterrupt:
        st = bot.stats
        log.info("\n⏹ Đã dừng. Các bài đã lưu vẫn còn trong RAW; chạy lại sẽ tiếp tục.")
    log.info("\n══ XONG sau %ds: lưu %d bài, %d ảnh mới lên Drive, bỏ %d bài cũ, %d không đọc được, %d lỗi ══",
             time.time() - t0, st["saved"], st["images"], st["old"], st["skip"], st["error"])


if __name__ == "__main__":
    main()
