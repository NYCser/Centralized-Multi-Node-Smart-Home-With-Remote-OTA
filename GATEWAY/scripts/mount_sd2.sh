#!/bin/bash
################################################################################
# mount_sd2.sh - Script tự động detect và mount USB/SD2 card
# Chạy lệnh: sudo /home/pi/GATEWAY/scripts/mount_sd2.sh
################################################################################

MOUNT_POINT="/mnt/sd2"
LOGFILE="/var/log/sd2_mount.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOGFILE"
}

log "Starting SD2 mount detection..."

# 1. Tạo mount point nếu chưa có
if [ ! -d "$MOUNT_POINT" ]; then
    log "Creating mount point: $MOUNT_POINT"
    mkdir -p "$MOUNT_POINT"
    chmod 755 "$MOUNT_POINT"
fi

# 2. Kiểm tra xem có device USB nào không
# Loại bỏ devices là partitions của mmcblk0 (OS)
DEVICES=$(lsblk -d -n -o NAME,SIZE | grep -v mmcblk0 | grep -v loop | awk '{print "/dev/" $1}')

if [ -z "$DEVICES" ]; then
    log "WARNING: No external storage device detected!"
    exit 1
fi

log "Found device(s): $DEVICES"

# 3. Mount device đầu tiên (thường là sda hoặc sdb)
for DEVICE in $DEVICES; do
    # Lấy partition đầu tiên (e.g., /dev/sda1)
    PARTITION=$(lsblk -l -n -o NAME "$DEVICE" | tail -1)
    PARTITION_PATH="/dev/$PARTITION"
    
    if [ ! -b "$PARTITION_PATH" ]; then
        log "Device not found: $PARTITION_PATH"
        continue
    fi
    
    log "Attempting to mount: $PARTITION_PATH"
    
    # Umount nếu đã mount rồi
    if mountpoint -q "$MOUNT_POINT"; then
        log "Already mounted at $MOUNT_POINT, unmounting first..."
        umount "$MOUNT_POINT" || true
    fi
    
    # Thử mount với các filesystem types phổ biến
    for FSTYPE in auto vfat ntfs ext4 exfat; do
        if mount -t "$FSTYPE" -o rw,uid=1000,gid=1000 "$PARTITION_PATH" "$MOUNT_POINT" 2>/dev/null; then
            log "✓ SUCCESS: Mounted $PARTITION_PATH ($FSTYPE) at $MOUNT_POINT"
            
            # # Tạo thư mục exports
            # mkdir -p "$MOUNT_POINT/exports"
            # chmod 755 "$MOUNT_POINT/exports"
            # Thành dòng mới để khớp với logic Layer 4:
            mkdir -p "$MOUNT_POINT/data"
            chmod 755 "$MOUNT_POINT/data"
            
            # Kiểm tra quyền
            if [ -w "$MOUNT_POINT" ]; then
                log "✓ Write permission verified"
            else
                log "WARNING: No write permission on $MOUNT_POINT"
                chmod 777 "$MOUNT_POINT"
            fi
            
            exit 0
        fi
    done
    
    log "ERROR: Could not mount $PARTITION_PATH with any filesystem type"
done

log "ERROR: No external storage could be mounted"
exit 1
