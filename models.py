from datetime import datetime, timezone

STATUS = [
    "INBOUND",     # 已录入（未到货）
    "REPARING",    # 护理中
    "RECEIVED",    # 已到货
    "ON_SHELF",    # 上架在售
    "RESERVED",    # 预留
    "SOLD",        # 已售
    "MISSING",     # 缺件
    "DAMAGED",     # 到货损坏
    "RETURNED",    # 退货（可选）
]

def now():
    return datetime.now(timezone.utc)
