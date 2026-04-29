# SmartHome Gateway - Current System Status & Issues

## 1. Tổng quan hệ thống hiện tại
Hệ thống hiện tại đang ở trạng thái "lai" giữa hai mô hình:
- Backend Gateway chạy Flask + Flask-SocketIO.
- Frontend `NhaThongMinh-Web` vẫn dùng Firebase để xác thực và lấy dữ liệu `roomService`.
- Có tài liệu Cloudflare Tunnel và Socket.IO, nhưng mã frontend chưa thực sự nối với tunnel này.

## 2. Kiến trúc hiện tại

### 2.1 Backend
- `GATEWAY/app/main.py`
  - Khởi tạo `Flask` app.
  - Khởi tạo `SocketIO` với `cors_allowed_origins="*"` và `async_mode='eventlet'`.
- `GATEWAY/gateway_main.py`
  - Import `app` và `socketio` từ `app/main.py`.
  - Thiết lập CORS bằng `Flask-CORS` và header CORS thủ công.
  - Khởi tạo DB SQLite và worker.
  - Chạy SocketIO server trên `0.0.0.0:5000`.
- `GATEWAY/requirements.txt`
  - Chứa Flask, Flask-SocketIO, python-socketio, python-engineio.

### 2.2 Frontend
- `NhaThongMinh-Web/NhaThongMinh-Web/js/index.js`
  - Dùng Firebase auth để bảo vệ luồng truy cập trang Admin.
- `NhaThongMinh-Web/NhaThongMinh-Web/js/admin.js`
  - Dùng Firebase Firestore và `roomService` để hiển thị phòng, thiết bị, thông báo.
  - Không có kết nối `socket.io` tới backend.

### 2.3 Tài liệu và mô hình mong muốn
- `GATEWAY/CLOUDFLARE_TUNNEL_GUIDE.md` và các tài liệu tương tự đề cập đến Cloudflare Tunnel + Socket.IO.
- Nhưng mã hiện tại chưa đồng bộ với hướng dẫn đó.

## 3. Các mâu thuẫn và vấn đề chính

### 3.1 Thiếu dependency quan trọng
- `app/main.py` dùng `async_mode='eventlet'`.
- `gateway_main.py` dùng `Flask-CORS`.
- Nhưng `GATEWAY/requirements.txt` **không chứa**:
  - `eventlet`
  - `Flask-Cors`

> Kết quả: chạy trên môi trường mới có thể bị lỗi import hoặc lỗi `Invalid async_mode specified` nếu eventlet không được cài.

### 3.2 Cấu hình CORS chồng chéo
- `app/main.py` tạo `SocketIO` với `cors_allowed_origins="*"`.
- `gateway_main.py` lại tạo `CORS(app, ...)` và thêm header CORS sau mỗi response.
- Đây là một mâu thuẫn về trách nhiệm: CORS nên được cấu hình một chỗ, và SocketIO cần được xử lý đồng bộ với app.

### 3.3 Mô hình frontend/backend không nhất quán
- Frontend admin page hiện dùng Firebase Firestore làm nguồn dữ liệu chính.
- Nhưng tài liệu hệ thống lại đề xuất Netlify -> Cloudflare -> Pi -> SD card.
- `admin.js` không có bất kỳ kết nối Cloudflare Tunnel hoặc Socket.IO nào.

> Kết quả: dù frontend được xác thực, nó không thực sự đọc dữ liệu từ Raspberry Pi / SD card.

### 3.4 Tài liệu và mã không cùng nhịp
- Tài liệu Cloudflare nhắc đến Socket.IO real-time, port 5001, tunnel, v.v.
- Code thực tế chỉ chạy server trên port 5000 bằng `gateway_main.py`.
- `admin.js` không hiện thực tunnel connection, nên hướng dẫn document không áp dụng trực tiếp.

### 3.5 Dữ liệu SD card chưa rõ ràng
- Trong mã hiện tại không có bất kỳ logic nào cho `request_sd_data`, `sd_card_update` hay đọc SD card trực tiếp từ frontend.
- Nếu muốn hiện dữ liệu SD card, cần xác định rõ backend đang trả dữ liệu dạng gì:
  - JSON
  - text
  - CSV

### 3.6 Mã thừa / không cần thiết
- `gateway_main.py` import `request` từ Flask nhưng không dùng.
- `admin.js` hiện tại đóng gói tính năng Firebase + roomService, nhưng nếu đi theo mô hình Cloudflare thì cần refactor lớn.

## 4. Tác động khi chạy hệ thống hiện tại

### Nếu chạy như bây giờ
- Backend có thể chạy được nếu environment đã cài đầy đủ packages.
- Frontend admin sẽ hoạt động với Firebase/Firestore, nhưng không kết nối Raspberry Pi qua tunnel.
- Dữ liệu SD card sẽ không được hiển thị trừ khi có thêm logic backend/client phù hợp.

### Nếu bạn muốn Netlify đọc SD card từ Pi
Cần làm rõ hai bước lớn:
1. `backend` phải thực sự trả dữ liệu SD card qua API hoặc Socket.IO.
2. `frontend` phải kết nối tới backend qua Cloudflare Tunnel hoặc REST API.

## 5. Đề xuất khắc phục bước đầu

### 5.1 Nếu muốn giữ mô hình cũ (không dùng Cloudflare)
- Thêm `eventlet` và `Flask-Cors` vào `GATEWAY/requirements.txt`.
- Giữ CORS chỉ ở một chỗ:
  - tốt nhất là `gateway_main.py` hoặc `app/main.py`, không phải cả hai.
- Kiểm tra lại `socketio.run(...)` và `allow_unsafe_werkzeug=True` nếu cần.
- Bổ sung API trả dữ liệu SD card rõ ràng.

### 5.2 Nếu muốn chuyển sang mô hình Cloudflare/Netlify
- Đưa `admin.js` vào trạng thái thật sự dùng `socket.io` client hoặc HTTPS API.
- Đồng bộ `CLOUDFLARE_TUNNEL_GUIDE.md` với mã thực tế.
- Cập nhật backend để lắng nghe event Socket.IO và gửi `sd_card_update`.

### 5.3 Khắc phục ngay các lỗi rõ ràng
- Sửa `requirements.txt`:
  - thêm `eventlet`
  - thêm `Flask-Cors`
- Hoặc đổi `async_mode='eventlet'` thành `async_mode='threading'` nếu không muốn cài eventlet.
- Loại bỏ import `request` nếu không dùng.
- Chuẩn hóa CORS chỉ ở một file.

## 6. Kết luận
Hệ thống hiện tại đang hoạt động với hai luồng tư duy:
- một phía backend Flask/SocketIO,
- một phía frontend Firebase/Firestore,
- tài liệu thì hướng đến Cloudflare Tunnel.

Để hệ thống chạy mượt:
- cần chọn một luồng xử lý duy nhất và đồng bộ code + dependencies + tài liệu,
- hoặc tạm dừng Cloudflare nếu muốn chạy local nhanh.

---

## 7. Tài liệu tham khảo nhanh
- `GATEWAY/app/main.py`
- `GATEWAY/gateway_main.py`
- `GATEWAY/requirements.txt`
- `NhaThongMinh-Web/NhaThongMinh-Web/js/admin.js`
- `NhaThongMinh-Web/NhaThongMinh-Web/js/index.js`
- `GATEWAY/CLOUDFLARE_TUNNEL_GUIDE.md`
