# Quang Tri Teen — máy chạy tự động trên GitHub Actions

Kho này chạy 2 việc, miễn phí trên GitHub Actions (kho công khai):

| Việc | Tệp | Khi nào chạy |
|---|---|---|
| Dựng Reel 20 giây và đăng lên Trang | `reel/reel.py`, `.github/workflows/reel.yml` | Bot Apps Script gửi lệnh (2–3 lần/ngày theo giờ trong Bảng điều khiển) |
| Thu thập tin web trường → sheet RAW + ảnh lên Drive | `collector/main.py` | **Chạy trên máy tính ở Việt Nam** (Task Scheduler + `collector/chay_thu_thap.bat`). Web trường chặn máy GitHub ở nước ngoài nên lịch trên GitHub đang tắt; vẫn bấm chạy tay được. |

## Khoá bí mật (Settings → Secrets and variables → Actions → New repository secret)

| Tên | Nội dung | Dùng cho |
|---|---|---|
| `GOOGLE_CREDENTIALS_JSON` | Toàn bộ nội dung file `credentials.json` (tài khoản dịch vụ) | Reel: đọc khung hình, nhạc trên Drive; ghi kết quả vào sheet REELS |
| `FB_PAGE_TOKEN` | Page Access Token của Trang | Reel: đăng lên Facebook |
| `GOOGLE_OAUTH_TOKEN` | Toàn bộ nội dung file `token.json` (tạo ra sau khi chạy `collector/main.py` lần đầu trên máy tính và đăng nhập) | Thu thập tin: ghi sheet và tải ảnh lên Drive bằng tài khoản của thầy |

Khoá chỉ nằm trong Secrets, GitHub tự che trong nhật ký chạy. **Không đưa file khoá vào kho** (đã chặn bằng `.gitignore`).

Khi đổi Page Access Token trong Bảng điều khiển của bot, nhớ cập nhật luôn `FB_PAGE_TOKEN` ở đây.

## Chạy thử bằng tay
Tab **Actions** → chọn workflow → **Run workflow**.
- *Dung Reel*: nhập `job` và `folder_id` (lấy ở sheet REELS), để `dry_run = 1` để chỉ dựng không đăng.
- *Thu thap tin*: bấm chạy và xem nhật ký.

## Đặt lịch thu thập tin trên Windows
1. Chép thư mục `collector` về máy (cùng `credentials_oauth.json`, `token.json`).
2. Task Scheduler → **Create Basic Task** → tên "QTT thu thập tin" → **Daily**, giờ 05:45.
3. Action **Start a program** → chọn file `chay_thu_thap.bat` trong thư mục `collector`.
4. Mở lại task → **Triggers** → thêm lần 12:45. Tab **Conditions**: bỏ tick "Start only if on AC power"; tab **Settings**: tick "Run task as soon as possible after a scheduled start is missed".

## Lưu ý
- Nhạc nền: thả file `.mp3` có giấy phép phù hợp (Pixabay Music, YouTube Audio Library…) vào thư mục Drive `QTT_NhacNen`.
- Một số web trường *.quangtri.edu.vn chặn truy cập từ nước ngoài; máy của GitHub đặt ở nước ngoài nên có thể không đọc được các trang này — xem nhật ký lần chạy đầu. Nếu bị chặn, chạy `collector/main.py` trên máy tính ở Việt Nam (Task Scheduler).
- Kho công khai không có hoạt động 60 ngày thì GitHub tạm tắt lịch chạy `collect.yml` (có email báo); vào tab Actions bấm bật lại.
