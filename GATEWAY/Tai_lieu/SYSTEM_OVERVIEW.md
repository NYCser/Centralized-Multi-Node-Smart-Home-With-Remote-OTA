# SmartHome Gateway - System Overview

## 1. Mục tiêu tài liệu
Tài liệu này mô tả kiến trúc hiện tại của hệ thống Gateway và frontend quản trị, theo mô hình cũ mà bạn muốn khôi phục. Mục đích là giúp bạn đọc hiểu cấu trúc và chỉ đạo sửa đổi tiếp theo.

## 2. Thành phần chính

### 2.1 GATEWAY
- `gateway_main.py`
  - Entry point chính.
  - Khởi tạo CSDL SQLite và bảng bằng `storage/db_schema.sql`.
  - Khởi động các worker nền:
    - `safety_watchdog`
    - `network_watchdog`
    - `data_syncer`
    - `automation_engine`
  - Đăng ký các Blueprint API và chạy server Flask/SocketIO.

- `app/main.py`
  - Tạo `Flask` app.
  - Khởi tạo `SocketIO` với `cors_allowed_origins="*"` và `async_mode='eventlet'`.
  - Đây là phần trái tim của realtime socket service.

- `app/api/routes/all_routes.py`
  - Nơi đăng ký các Blueprint API như `auth_bp`, `sensors_bp`, `devices_bp`, `automation_bp`, `logs_bp`, `rfid_bp`, `wifi_bp`, `ota_bp`, `system_bp`.
  - Endpoint API chạy dưới đường dẫn `/api/*`.

- `bridge/message_bus.py`
  - Quản lý message bus nội bộ.
  - Được dùng bởi worker để kết nối các thành phần với nhau.

- `workers/`
  - Các tiến trình nền xử lý tự động hóa, đồng bộ dữ liệu, giám sát an toàn, giám sát mạng.

## 3. Luồng dữ liệu hiện tại
- Frontend `NhaThongMinh-Web` dùng Firebase để xác thực người dùng.
- Dựa trên quyền đăng nhập Firebase, admin mới được truy cập trang `admin.html`.
- `admin.js` đọc dữ liệu người dùng và phòng từ Firestore qua `roomService`.
- Backend Gateway cung cấp API và realtime SocketIO cho các chức năng nội bộ.

## 4. Mô hình cũ (đã khôi phục)
- Backend vẫn dùng Flask + Flask-SocketIO.
- CORS được cấu hình trong `gateway_main.py` bằng `Flask-CORS`.
- `gateway_main.py` không phải launcher chuyên biệt tách rời, nó vừa cấu hình CORS vừa chạy server.
- Không có logic kết nối Cloudflare Tunnel trong `admin.js`.

## 5. Chạy hệ thống
1. Kích hoạt môi trường ảo:
   ```bash
   source venv/bin/activate
   ```
2. Cài dependencies:
   ```bash
   pip install -r GATEWAY/requirements.txt
   ```
3. Chạy Gateway:
   ```bash
   cd GATEWAY
   python3 gateway_main.py
   ```

## 6. Ghi chú quan trọng
- Dữ liệu thẻ SD hiện tại chưa rõ định dạng trong mã nguồn đã đọc. Bạn cần xác nhận:
  - JSON
  - text
  - CSV
- Sau khi xác nhận định dạng, tôi sẽ giúp bạn chỉnh sửa `admin.js` hoặc backend để hiển thị đúng dạng.

## 7. Gợi ý bước tiếp theo
- Nếu muốn sửa theo mô hình Cloudflare/Netlify sau, hãy xác định rõ:
  - `frontend` chạy ở Netlify hay local?
  - `backend` có cần dùng Cloudflare Tunnel không?
  - `login/auth` có giữ Firebase hay chuyển sang cơ chế khác?
