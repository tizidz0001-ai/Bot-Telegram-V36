# Telegram Bot Nhiệm Vụ → Nhận Xu → Đổi Quà

Bản này đã tích hợp menu người dùng + `/admin`, quản lý nhiệm vụ, xu, quà, key TXT, file Telegram, đơn đổi quà, thống kê, broadcast, API rút gọn và cơ chế session chống claim lặp.

## 1. Cài đặt

```bash
pip install -r requirements.txt
python bot.py
```

Bot dùng SQLite (`bot.db`) và tự tạo/migrate database khi chạy.

## 2. Admin

Gõ `/admin`. Nếu `ADMIN_ID=0`, lần đầu tài khoản có username đúng với `ADMIN_USERNAME` mở `/admin`, bot sẽ ghim numeric Telegram ID của admin vào database. Sau đó quyền admin theo ID, an toàn hơn username.

Các mục:

- Người dùng: tìm ID, khóa/mở khóa, reset session.
- Quản lý xu: cộng/trừ, tặng toàn bộ, top số dư.
- Nhiệm vụ: thêm, sửa, bật/tắt, xem thống kê.
- Quà: thêm, sửa giá/kho/kiểu giao, bật/tắt, đơn thủ công, duyệt/từ chối + hoàn xu.
- Key/File: upload TXT mỗi dòng 1 key, xem kho, xóa key chưa dùng hoặc gán file Telegram cho quà.
- Thống kê + lịch sử hệ thống: user, nhiệm vụ, xu, claim và đơn.
- Broadcast: xem trước trước khi gửi toàn bộ.
- Cài đặt: Public URL, kênh, hỗ trợ, hướng dẫn, bảo trì.
- Bảo mật: session một lần, thời gian tối thiểu, giới hạn IP/ngày, không cộng xu ở frontend.
- API rút gọn: thêm nhiều API và gán API cho nhiệm vụ.

## 3. Cấu hình vượt link an toàn

Bot chạy một HTTP endpoint:

`GET /complete/<session_token>`

Khi user bắt đầu nhiệm vụ, bot sinh session ngẫu nhiên, lấy URL đích dạng:

`https://YOUR-DOMAIN/complete/<token>`

sau đó gọi API rút gọn để tạo link cho user. Chỉ khi user đi đến endpoint cuối, session mới chuyển `pending → completed`. User quay lại bot bấm **Kiểm tra & Nhận xu** để chuyển `completed → claimed` và cộng xu đúng một lần.

### PUBLIC_BASE_URL

Khi deploy, đặt biến môi trường:

```env
PUBLIC_BASE_URL=https://bot.example.com
```

Domain này phải trỏ tới cùng service đang chạy `bot.py`, và service cần expose `PORT` (mặc định 8080).

### API rút gọn

Trong `/admin` → **API rút gọn** → **Thêm API**.

Format:

```text
Tên | API template | response_key
```

Template hỗ trợ:

- `{url}`: URL đích chưa encode.
- `{url_encoded}`: URL đích đã URL-encode.
- `{user_id}`
- `{token}`

Ví dụ:

```text
MyShortener | https://api.example.com/short?key=ABC&url={url_encoded} | short_url
```

Nếu API trả về plain-text là URL hoặc JSON có key phổ biến `shortenedUrl`, `short_url`, `shortUrl`, `url`, `link`, `result`, có thể để `response_key` là `-`.

**Không bật `Link trực tiếp` khi chạy thật**, vì tùy chọn đó chỉ dành cho test khi chưa có API rút gọn.

## 4. Thêm nhiệm vụ

`/admin` → Nhiệm vụ → Thêm:

```text
Tên | Xu thưởng | Giới hạn/ngày | Chờ giây | Shortener ID
```

Ví dụ:

```text
Vượt Link 1 | 500 | 2 | 20 | 1
```

## 5. Thêm quà

Format:

```text
Tên | Giá xu | Kho | Kiểu | Nội dung
```

Kiểu giao:

- `text`: gửi nội dung text ngay sau khi đổi.
- `keypool`: bot lấy 1 key chưa dùng từ kho TXT.
- `file_id`: bot tự gửi file Telegram.
- `manual`: tạo đơn chờ admin duyệt.

Kho `-1` = không giới hạn.

Ví dụ:

```text
Key VIP 1H | 5000 | -1 | keypool | -
```

Sau đó vào **Key / File** → **Upload TXT key**, gửi ID quà rồi upload file `.txt`, mỗi dòng một key.

## 6. Lưu ý bảo mật

- Secret/API key của dịch vụ rút gọn được lưu trong SQLite nếu nằm trong API template; bảo vệ file database và server.
- Không commit `.env` hoặc `bot.db` lên GitHub public.
- Token Telegram đã từng gửi qua chat nên nên tạo token mới bằng BotFather trước khi chạy production.
- Giới hạn IP chỉ là lớp chống abuse cơ bản; IP dùng chung/VPN có thể gây false positive hoặc né giới hạn.
- Telegram bot không thể lấy “device fingerprint” đáng tin cậy. Numeric Telegram ID + server session là lớp chính.

## Menu Telegram Cố Định

Menu người dùng chính đã được đổi sang **Reply Keyboard** của Telegram. Các nút `Tài Khoản`, `Làm Nhiệm Vụ`, `Đổi Quà`, `Lịch Sử`, `BXH`, `Hướng Dẫn`, `Hỗ Trợ / Liên Hệ` luôn nằm dưới ô nhập tin nhắn giống bàn phím trong app Telegram.

Các nút cần URL hoặc callback riêng theo từng nhiệm vụ/quà vẫn dùng Inline Button vì Telegram không cho Reply Keyboard mở URL tùy ý bằng `callback_data`.

## Deep Link Telegram Sau Khi Vượt Link

Luồng Nhiệm Vụ Hiện Tại:

1. Người Dùng Bấm **Vượt Link Lấy Xu**.
2. Bot Tạo Một `Session Token` Riêng Cho Telegram User ID.
3. API Rút Gọn Trỏ Đến `/complete/TOKEN` Trên Máy Chủ Bot.
4. Khi Người Dùng Đi Đến Trang Cuối, Máy Chủ Xác Nhận Session Và Chuyển Hướng Sang:

```text
https://t.me/TenBot?start=claim_TOKEN
```

5. Telegram Gửi `/start claim_TOKEN` Cho Bot Sau Khi Người Dùng Nhấn **Start** Nếu Ứng Dụng Yêu Cầu.
6. Bot Kiểm Tra Đúng User ID, Session Đã Hoàn Thành, Giới Hạn Trong Ngày Và Trạng Thái Chưa Nhận Xu.
7. Hợp Lệ Thì Bot Tự Cộng Xu Và Chuyển Session Sang `claimed`.
8. Một Session Không Thể Nhận Xu Lần Hai.

Nút **Kiểm Tra Thủ Công** Vẫn Được Giữ Làm Phương Án Dự Phòng Nếu Thiết Bị Không Tự Mở Telegram.

> Cần Cấu Hình `PUBLIC_BASE_URL` Bằng Domain HTTPS Công Khai Của Máy Chủ Đang Chạy Bot.
