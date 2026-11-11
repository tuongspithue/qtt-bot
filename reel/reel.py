# -*- coding: utf-8 -*-
"""
Dựng Reel 20 giây và đăng lên Trang Facebook (dùng chung cho nhiều trang: Quang Tri Teen, Quảng Trị Vươn Mình…).
Chạy trên GitHub Actions, do bot Apps Script gọi (repository_dispatch "make_reel").

Đầu vào (client_payload): job, folder_id (thư mục Drive chứa frame_1.png..frame_N.png),
music_folder_id (thư mục nhạc nền .mp3), caption, link (link bài gốc để bình luận), seconds.
Tuỳ chọn kind: "reel" (mặc định, đăng Reel) hoặc "story" (đăng Tin dạng video có nhạc).
Tuỳ chọn để chọn trang: page_id, sheet_id, token_secret (tên secret chứa Page token, dạng FB_PAGE_TOKEN hoặc FB_PAGE_TOKEN_XXX).
Không gửi các trường này → dùng mặc định của Quang Tri Teen (biến FB_PAGE_ID, SHEET_ID, secret FB_PAGE_TOKEN).
Bí mật (Secrets): GOOGLE_CREDENTIALS_JSON (tài khoản dịch vụ), FB_PAGE_TOKEN, FB_PAGE_TOKEN_KTXH (Quảng Trị Vươn Mình).
Biến: FB_PAGE_ID, SHEET_ID.
"""
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

GRAPH = "https://graph.facebook.com/v23.0"
PAGE_ID = os.environ.get("FB_PAGE_ID") or "1113107338547087"                      # Trang Quang Tri Teen
SHEET_ID = os.environ.get("SHEET_ID") or "1h2egAXIXIDgRrpNZCzvqakL_FMrDvV-3lYLGe9GhYNs"  # sheet của bot
VN = timezone(timedelta(hours=7))
W, H, FPS, FADE = 1080, 1920, 30, 0.5


def log(*a):
    print(datetime.now(VN).strftime("%H:%M:%S"), *a, flush=True)


# ─────────────────────────── đầu vào ───────────────────────────
def load_payload():
    p = {}
    ev = os.environ.get("GITHUB_EVENT_PATH")
    if ev and os.path.exists(ev):
        with open(ev, encoding="utf-8") as f:
            data = json.load(f)
        p = data.get("client_payload") or data.get("inputs") or {}
    for k in ("job", "folder_id", "music_folder_id", "caption", "link", "seconds",
              "page_id", "sheet_id", "token_secret", "kind"):                                # chạy tay / thử cục bộ
        v = os.environ.get("REEL_" + k.upper())
        if v and not p.get(k):
            p[k] = v
    if not p.get("job") or not p.get("folder_id"):
        sys.exit("Thiếu job / folder_id")
    p["seconds"] = max(5, min(60, float(p.get("seconds") or 20)))
    p["kind"] = "story" if str(p.get("kind") or "").strip().lower() == "story" else "reel"
    nar = p.get("narration")                              # lời đọc, mỗi khung một câu (không bắt buộc)
    if isinstance(nar, str):
        try:
            nar = json.loads(nar)
        except ValueError:
            nar = [nar]
    p["narration"] = [str(x).strip() for x in nar] if isinstance(nar, list) else []
    global VOICE                                          # phần tử đầu "@voice:<tên giọng>" chọn giọng (nam/nữ), không phải lời đọc
    if p["narration"] and p["narration"][0].startswith("@voice:"):
        v = p["narration"].pop(0)[7:].strip()
        if v in VOICES:
            VOICE = v
    return p


def apply_target(p):
    """Chọn trang đích theo lệnh gửi sang: page_id, sheet_id, token_secret (không có → mặc định Quang Tri Teen)."""
    global PAGE_ID, SHEET_ID
    if str(p.get("page_id") or "").strip():
        PAGE_ID = str(p["page_id"]).strip()
    if str(p.get("sheet_id") or "").strip():
        SHEET_ID = str(p["sheet_id"]).strip()
    name = str(p.get("token_secret") or "FB_PAGE_TOKEN").strip()
    if not re.fullmatch(r"FB_PAGE_TOKEN(_[A-Z0-9]+)?", name):       # chỉ nhận tên secret token Trang
        sys.exit("token_secret không hợp lệ: " + name)
    token = os.environ.get(name, "")
    if not token and os.environ.get("DRY_RUN") != "1":
        sys.exit("Thiếu secret %s trên GitHub (Settings → Secrets → Actions)" % name)
    os.environ["FB_PAGE_TOKEN"] = token
    log("🎯 Trang %s · sheet %s… · token %s" % (PAGE_ID, SHEET_ID[:10], name))


def google_services():
    info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/spreadsheets"])
    return (build("drive", "v3", credentials=creds, cache_discovery=False),
            build("sheets", "v4", credentials=creds, cache_discovery=False))


def list_files(drive, folder_id):
    res = drive.files().list(q="'%s' in parents and trashed = false" % folder_id,
                             fields="files(id,name,mimeType,size)", pageSize=200,
                             supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    return res.get("files", [])


def download(drive, file_id, path):
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(path, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return path


# ─────────────────────────── dựng video ───────────────────────────
def durations(n, total):
    """Khung bìa dài hơn một chút, khung cuối ngắn hơn. Cộng bù phần chồng lúc chuyển cảnh."""
    if n == 1:
        return [total]
    span = total + (n - 1) * FADE
    w = [1.3] + [1.0] * (n - 2) + [0.8]
    s = sum(w)
    return [round(span * x / s, 2) for x in w]


# ─────────────────────────── nhạc nền ngẫu nhiên, không lặp ───────────────────────────
MUSIC_RECENT = 4          # không dùng lại bài đã dùng ở 4 Reel gần nhất (nếu thư mục đủ bài)


def audio_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30).stdout
        return float(out.strip())
    except Exception:
        return 0.0


def recent_music(sheets):
    """Tên các bài nhạc đã dùng ở những Reel gần đây (ghi trong cột note của sheet REELS: "♪ tên bài @ 37s")."""
    if not SHEET_ID:
        return []
    try:
        rows = sheets.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="REELS!A:Z").execute().get("values", [])
        if not rows or "note" not in rows[0]:
            return []
        nc = rows[0].index("note")
        used = []
        for r in reversed(rows[1:]):
            note = r[nc] if len(r) > nc else ""
            if "♪" in note:
                used.append(note.split("♪", 1)[1].split(" @ ")[0].strip())
            if len(used) >= MUSIC_RECENT:
                break
        return used
    except Exception as e:
        log("⚠️ Không đọc được lịch sử nhạc:", str(e)[:120])
        return []


def pick_music(tracks, used):
    """Chọn ngẫu nhiên 1 bài, tránh các bài vừa dùng; nếu thư mục ít bài thì chỉ tránh bài dùng gần nhất."""
    fresh = [t for t in tracks if t["name"] not in used]
    if not fresh and len(tracks) > 1 and used:
        fresh = [t for t in tracks if t["name"] != used[0]]
    return random.choice(fresh or tracks)


def music_start(dur, total):
    """Điểm bắt đầu ngẫu nhiên trong bài: bỏ đoạn dạo đầu (~8% hoặc tối đa 10s), không chạm đoạn kết."""
    if dur <= total + 4:
        return 0.0
    lo = min(10.0, dur * 0.08)
    hi = dur - total - 2.0
    return round(random.uniform(lo, hi), 1) if hi > lo else 0.0


# ─────────────────────────── giọng đọc (không bắt buộc) ───────────────────────────
VOICES = ("vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural")   # nữ, nam (edge-tts chỉ có 2 giọng tiếng Việt)
VOICE = os.environ.get("REEL_VOICE") or VOICES[0]


def synth_narration(lines, tmp):
    """Đọc từng câu bằng giọng AI (edge-tts). Trả về danh sách (đường dẫn, giây) hoặc None cho khung không đọc được.
    Lỗi ở đâu thì khung đó im lặng; video vẫn dựng bình thường với nhạc nền."""
    out = [None] * len(lines)
    try:
        import asyncio
        import edge_tts
    except Exception as e:
        log("⚠️ Không có thư viện giọng đọc:", str(e)[:120])
        return out

    async def one(text, path):
        await edge_tts.Communicate(text, VOICE, rate="+4%").save(path)

    for i, t in enumerate(lines):
        if not t:
            continue
        path = os.path.join(tmp, "voice_%d.mp3" % (i + 1))
        try:
            asyncio.run(asyncio.wait_for(one(t, path), 45))
            d = audio_duration(path)
            if d > 0.3:
                out[i] = (path, d)
        except Exception as e:
            log("⚠️ Không đọc được khung %d: %s" % (i + 1, str(e)[:120]))
    return out


def voice_durations(n, voices, total):
    """Độ dài khung theo lời đọc (lời đọc + 0,6s, tối thiểu 2,5s) nên video dài/ngắn theo nội dung, không ép đúng `total`;
    khung không có lời đọc giữ độ dài mặc định. Tổng nằm trong 12–60s. Trả (durs, tổng mới)."""
    base = durations(n, total)
    durs = []
    for i in range(n):
        v = voices[i] if i < len(voices) else None
        durs.append(round(max(2.5, v[1] + 0.6), 2) if v else base[i])
    tot = sum(durs) - (n - 1) * FADE
    if tot < 12.0:                                        # quá ngắn → kéo giãn đều các khung lên tối thiểu 12s
        k = (12.0 + (n - 1) * FADE) / max(1e-6, sum(durs))
        durs = [round(d * k, 2) for d in durs]
        tot = sum(durs) - (n - 1) * FADE
    return durs, round(min(60.0, tot), 2)


def build_video(frames, music, total, out, music_ss=0.0, voices=None, durs=None):
    n = len(frames)
    durs = durs or durations(n, total)
    vlist = [(i, v) for i, v in enumerate((voices or [])[:n]) if v]
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in frames:
        args += ["-i", f]
    if music:
        args += ["-stream_loop", "-1", "-ss", "%.1f" % music_ss, "-i", music]
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    for _, v in vlist:
        args += ["-i", v[0]]

    parts = []
    for i, d in enumerate(durs):
        nf = int(round(d * FPS))
        # Ảnh chuyển động nhẹ (Ken Burns): xen kẽ phóng to / thu nhỏ từ tâm
        z = "min(1+0.0009*on,1.10)" if i % 2 == 0 else "max(1.10-0.0009*on,1.0)"
        if n == 1:                                       # 1 ảnh (bản tin): phóng rất nhẹ để không cắt mất chữ sát mép
            z = "min(1+0.0001*on,1.03)"
        parts.append(
            "[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1,"
            "zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=%d:s=%dx%d:fps=%d,"
            "format=yuv420p,trim=duration=%.2f,setpts=PTS-STARTPTS[v%d]"
            % (i, W * 2, H * 2, W * 2, H * 2, z, nf, W, H, FPS, d, i))
    last, off = "v0", 0.0
    for i in range(1, n):
        off += durs[i - 1] - FADE
        trans = ["fade", "slideleft", "smoothup", "fade", "circleopen"][i % 5]
        parts.append("[%s][v%d]xfade=transition=%s:duration=%.2f:offset=%.2f[x%d]" % (last, i, trans, FADE, off, i))
        last = "x%d" % i
    a = n
    if vlist:                                            # có giọng đọc: nhạc nền nhỏ lại, lời đọc vào đúng đầu từng khung
        parts.append("[%d:a]atrim=0:%.2f,afade=t=in:d=0.6,afade=t=out:st=%.2f:d=1.5,volume=0.22[bg]" % (a, total, max(0, total - 1.5)))
        starts, acc = [0.0], 0.0
        for d in durs[:-1]:
            acc += d - FADE
            starts.append(acc)
        labels = "[bg]"
        for k, (i, _) in enumerate(vlist):
            ms = int(round((starts[i] + 0.1) * 1000))
            parts.append("[%d:a]adelay=%d|%d,volume=1.7[vo%d]" % (a + 1 + k, ms, ms, k))
            labels += "[vo%d]" % k
        parts.append("%samix=inputs=%d:duration=first:dropout_transition=0:normalize=0,atrim=0:%.2f[a]" % (labels, 1 + len(vlist), total))
    else:
        parts.append("[%d:a]atrim=0:%.2f,afade=t=in:d=0.6,afade=t=out:st=%.2f:d=1.5,volume=0.85[a]" % (a, total, max(0, total - 1.5)))
    args += ["-filter_complex", ";".join(parts), "-map", "[%s]" % last, "-map", "[a]",
             "-t", "%.2f" % total, "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-movflags", "+faststart", out]
    t0 = time.time()
    subprocess.run(args, check=True)
    log("🎬 Dựng xong %.1fs video trong %ds (%.1f MB)" % (total, time.time() - t0, os.path.getsize(out) / 1e6))


# ─────────────────────────── đăng Reel ───────────────────────────
def fb(method, url, **kw):
    r = requests.request(method, url, timeout=120, **kw)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError("Facebook %s: %s" % (r.status_code, err.get("message") or str(data)[:200]))
    return data


def publish_reel(path, caption, link):
    page, token = PAGE_ID, os.environ["FB_PAGE_TOKEN"]
    start = fb("POST", "%s/%s/video_reels" % (GRAPH, page), data={"upload_phase": "start", "access_token": token})
    vid = start["video_id"]
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        fb("POST", "https://rupload.facebook.com/video-upload/v23.0/%s" % vid,
           headers={"Authorization": "OAuth " + token, "offset": "0", "file_size": str(size)}, data=f.read())
    fb("POST", "%s/%s/video_reels" % (GRAPH, page), params={
        "access_token": token, "video_id": vid, "upload_phase": "finish",
        "video_state": "PUBLISHED", "description": caption})
    log("📤 Đã gửi Reel, video_id", vid, "- chờ Facebook xử lý…")
    status = {}
    for _ in range(30):                                   # chờ tối đa ~5 phút
        time.sleep(10)
        status = fb("GET", "%s/%s" % (GRAPH, vid), params={"fields": "status", "access_token": token}).get("status", {})
        vs = status.get("video_status")
        if vs == "error":
            raise RuntimeError("Facebook báo lỗi xử lý video: %s" % json.dumps(status)[:300])
        pub = (status.get("publishing_phase") or {}).get("status")
        if vs == "ready" and pub in ("complete", None):
            break
    if link:
        try:
            fb("POST", "%s/%s/comments" % (GRAPH, vid), data={"message": "📌 Chi tiết: " + link, "access_token": token})
        except Exception as e:                            # bình luận lỗi không làm hỏng việc đăng
            log("⚠️ Không bình luận được link:", str(e)[:150])
    return vid, status


def publish_story(path):
    """Đăng video lên Tin (Story) của Trang: start → tải lên (rupload) → finish."""
    page, token = PAGE_ID, os.environ["FB_PAGE_TOKEN"]
    start = fb("POST", "%s/%s/video_stories" % (GRAPH, page), data={"upload_phase": "start", "access_token": token})
    vid = start["video_id"]
    url = start.get("upload_url") or "https://rupload.facebook.com/video-upload/v23.0/%s" % vid
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        fb("POST", url, headers={"Authorization": "OAuth " + token, "offset": "0", "file_size": str(size)}, data=f.read())
    fin = fb("POST", "%s/%s/video_stories" % (GRAPH, page), params={
        "access_token": token, "video_id": vid, "upload_phase": "finish"})
    log("📤 Đã đăng Tin video, video_id", vid, "post_id", fin.get("post_id", ""))
    return vid, {"video_status": "story", "post_id": fin.get("post_id", "")}


# ─────────────────────────── ghi kết quả vào sheet REELS ───────────────────────────
def update_sheet(sheets, job, values):
    sid = SHEET_ID
    if not sid:
        return
    try:
        rows = sheets.spreadsheets().values().get(spreadsheetId=sid, range="REELS!A:Z").execute().get("values", [])
        if not rows:
            return
        hdr = rows[0]
        if "job" not in hdr:
            return
        jc = hdr.index("job")
        for i, r in enumerate(rows[1:], start=2):
            if len(r) > jc and r[jc] == job:
                data = []
                for k, v in values.items():
                    if k in hdr:
                        col = hdr.index(k)
                        a1 = "REELS!%s%d" % (col_letter(col), i)
                        data.append({"range": a1, "values": [[v]]})
                if data:
                    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={
                        "valueInputOption": "RAW", "data": data}).execute()
                return
    except Exception as e:
        log("⚠️ Không cập nhật được sheet REELS:", str(e)[:200])


def col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
    p = load_payload()
    apply_target(p)
    job = p["job"]
    log("▶ Reel", job)
    drive, sheets = google_services()
    try:
        tmp = tempfile.mkdtemp()
        files = sorted([f for f in list_files(drive, p["folder_id"]) if f["name"].lower().startswith("frame_")
                        and f["name"].lower().endswith((".png", ".jpg"))],
                       key=lambda f: int("".join(c for c in f["name"] if c.isdigit()) or 0))
        if not files:
            raise RuntimeError("Thư mục không có khung hình frame_*.png (đã chia sẻ thư mục cho tài khoản dịch vụ chưa?)")
        frames = [download(drive, f["id"], os.path.join(tmp, f["name"])) for f in files]
        log("🖼 Tải %d khung hình" % len(frames))

        voices, durs, total = [], None, p["seconds"]
        if p["kind"] == "reel" and any(p.get("narration") or []):
            voices = synth_narration(p["narration"][:len(frames)], tmp)
            if any(voices):
                durs, total = voice_durations(len(frames), voices, p["seconds"])
                log("🎙 Giọng đọc %d/%d khung · video %.1fs" % (sum(1 for v in voices if v), len(frames), total))
            else:
                voices = []
        music, music_ss, music_note = None, 0.0, ""
        if p.get("music_folder_id"):
            tracks = [f for f in list_files(drive, p["music_folder_id"]) if f["name"].lower().endswith((".mp3", ".m4a", ".wav"))]
            if tracks:
                used = recent_music(sheets)
                t = pick_music(tracks, used)
                music = download(drive, t["id"], os.path.join(tmp, "music_" + t["name"]))
                dur = audio_duration(music)
                music_ss = music_start(dur, total)
                music_note = "♪ %s @ %ds" % (t["name"], int(music_ss))
                log("🎵 Nhạc nền: %s (dài %ds, phát từ giây %.1f; tránh %d bài vừa dùng / %d bài)"
                    % (t["name"], dur, music_ss, len(used), len(tracks)))
        if voices:
            music_note = (music_note + " · 🎙 giọng đọc").strip(" ·")
        if not music:
            log("🔇 Không có nhạc nền (thêm file .mp3 vào thư mục nhạc trên Drive)")

        out = os.path.join(tmp, "reel_%s.mp4" % job)
        build_video(frames, music, total, out, music_ss, voices, durs)

        if os.environ.get("DRY_RUN") == "1":
            log("DRY_RUN=1 → không đăng")
            update_sheet(sheets, job, {"status": "rendered (dry run)", "note": music_note,
                                       "finished_at": datetime.now(VN).isoformat(timespec="seconds")})
            return
        if p["kind"] == "story":
            vid, st = publish_story(out)
        else:
            vid, st = publish_reel(out, p.get("caption") or "", p.get("link") or "")
        update_sheet(sheets, job, {"status": "published", "fb_video_id": vid,
                                   "finished_at": datetime.now(VN).isoformat(timespec="seconds"),
                                   "note": " · ".join(x for x in [st.get("video_status") or "", music_note] if x)})
        log("✅ Xong:", vid)
    except Exception as e:
        log("❌", e)
        update_sheet(sheets, job, {"status": "error", "note": str(e)[:300],
                                   "finished_at": datetime.now(VN).isoformat(timespec="seconds")})
        sys.exit(1)


if __name__ == "__main__":
    main()
