# SD2 Storage Configuration Guide

## Tình Huống

- **Vấn đề:** USB/SD card được cắm vào Raspberry Pi nhưng dữ liệu gateway không được lưu vào thẻ này
- **Nguyên nhân:** Thiết bị không được tự động mount (gắn) vào `/mnt/sd2`
- **Giải pháp:** Cấu hình auto-mount cho USB/SD card

## Cách Cài Đặt

### Cách 1: Tự động (Khuyên dùng)

Chạy setup.sh sẽ tự động cấu hình:

```bash
cd ~/GATEWAY
bash setup.sh
```

Điều này sẽ:
- Tạo thư mục `/mnt/sd2`
- Cài đặt systemd service để auto-mount
- Cài đặt udev rules để tự động nhận diện USB

### Cách 2: Thủ công Mount USB

Nếu USB không được tự động mount, chạy:

```bash
sudo /home/pi/GATEWAY/scripts/mount_sd2.sh
```

Script này sẽ:
1. Detect USB device
2. Tìm partition đầu tiên
3. Mount vào `/mnt/sd2` với quyền read/write

## Kiểm Tra SD2 Có Mount Không

```bash
# Kiểm tra mountpoint
mount | grep sd2

# Hoặc
df -h | grep sd2

# Chi tiết hơn
lsblk

# Kiểm tra thư mục
ls -la /mnt/sd2/
```

**Kết quả thành công:**
- `/mnt/sd2` trống hoặc chứa thư mục `exports`
- Có quyền read/write

## Dữ Liệu Được Lưu Ở Đâu

Khi SD2 được mount, gateway sẽ tự động lưu:

### 1. CSV Export (hàng ngày)
```
/mnt/sd2/exports/
├── sensor_20260415_0200.csv
├── sensor_20260414_0200.csv
└── ...
```
- **Thời gian:** 02:00 sáng mỗi ngày (cấu hình bằng `EXPORT_HOUR` trong data_syncer.py)
- **Dữ liệu:** Dữ liệu cảm biến 24 giờ qua

### 2. Database Backup (hàng ngày)
```
/mnt/sd2/
└── smarthome_backup.db
```
- **Thời gian:** Cùng lúc CSV export
- **Nội dung:** Backup toàn bộ database

### 3. Log Files (option)
```
/var/log/sd2_mount.log
```
- Mount script logs

## Cấu Hình Chi Tiết

### Config Files

**Mount point:** `/mnt/sd2`
**Script mount:** `/home/pi/GATEWAY/scripts/mount_sd2.sh`
**Systemd service:** `/etc/systemd/system/sd2-mount@.service`
**Udev rules:** `/etc/udev/rules.d/99-sd2-mount.rules`
**Data syncer:** `/home/pi/GATEWAY/workers/data_syncer.py`

### Tùy chỉnh thời gian export

Sửa trong `data_syncer.py`:

```python
EXPORT_HOUR = 2  # Thay đổi thành giờ mong muốn (0-23)
```

Sau đó restart gateway:
```bash
python gateway_main.py
```

## Troubleshooting

### 1. USB không được detect

```bash
# Kiểm tra lsblk
lsblk

# Kiểm tra dmesg logs
dmesg | tail -20
```

**Giải pháp:**
- USB device có hỗ trợ không? (thử USB khác)
- Cổng USB bị sao? (thử cổng khác)
- Cải tổ Raspberry Pi

### 2. Mount script báo lỗi

```bash
# Chạy script với debug
sudo bash -x /home/pi/GATEWAY/scripts/mount_sd2.sh
```

### 3. Dữ liệu vẫn không được lưu

```bash
# Kiểm tra gateway logs
grep "SYNCER" /var/log/syslog

# Hoặc chạy gateway và xem real-time output
python /home/pi/GATEWAY/gateway_main.py
```

Tìm dòng:
- `[SYNCER] SD2 not available` - USB chưa mount
- `[SYNCER] CSV exported` - Đang lưu thành công
- `[SYNCER] DB backup OK` - Database backup thành công

### 4. Quyền truy cập bị từ chối

```bash
# Cấp quyền rw cho /mnt/sd2
sudo chmod 777 /mnt/sd2
```

## Flow Dữ Liệu

```
[Sensors/Devices]
        ↓
[Bridge/Message Bus]
        ↓
[Redis Buffer] → Flush to [SQLite DB (/data/smarthome.db)]
        ↓
[Daily Export 02:00]
        ↓
[/mnt/sd2/exports/sensor_YYYYMMDD_HHMM.csv]
[/mnt/sd2/smarthome_backup.db]
```

## Manual Export (nếu cần)

Gửi command đến Redis:

```bash
redis-cli PUBLISH log_commands '{"action": "export_now"}'
```

Gateway sẽ ngay lập tức export CSV và backup database.

## Auto-Mount Bị Disabled?

Nếu muốn disable auto-mount:

```bash
# Disable udev rules
sudo rm /etc/udev/rules.d/99-sd2-mount.rules
sudo udevadm control --reload-rules

# Hoặc disable systemd service
sudo systemctl disable sd2-mount@.service
```

## Hardware Requirements

- **USB/SD Card Reader:** Có driver trên Raspberry Pi (chuẩn)
- **USB Drive/SD Card:** Format FAT32, NTFS, ext4, exFAT
- **Dung lượng tối thiểu:** 1GB (tùy khối lượng dữ liệu)

---

**Để được hỗ trợ**, kiểm tra logs:
```bash
tail -f /var/log/sd2_mount.log
tail -f /var/log/syslog | grep SYNCER
```
