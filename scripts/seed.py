"""Seed demo data: 1 buyer + 3 sellers + 25 items (local placeholder photos), then rebuild vector + BM25 indexes.

    python scripts/seed.py            # wipe & rebuild
    python scripts/seed.py --keep     # keep existing data, only rebuild indexes
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

import _bootstrap  # noqa: F401  # isort:skip

from app.config import LOCATION_BY_NAME, settings
from app.db import SessionLocal, engine, init_db
from app.models import (
    Base,
    Item,
    ItemStatus,
    Notification,
    Reservation,
    ReservationStatus,
    StatusLog,
    TokenUsageLog,
    User,
    utcnow,
)
from app.security import hash_password
from app.services.images import make_placeholder
from app.services.item_index import get_item_index

BUYER = ("buyer@campus.edu", "Nine East 33rd")
SELLERS = [
    ("seller1@campus.edu", "The Marylander (3501 St Paul St)"),
    ("seller2@campus.edu", "Wyman Park Apartments (W 40th St)"),
    ("seller3@campus.edu", "St Paul St 2900 block townhouses"),
]
PASSWORD = "demo1234"

# (name, summary, description, price, category, brand, condition, color, location, time_start, time_end)
ITEMS: list[tuple] = [
    ("Giant ATX 山地自行车", "黑色 Giant 山地自行车，骑了约两年，刹车最近更换过。",
     "Giant ATX mountain bike with 26-inch wheels, aluminum frame, front suspension fork and V-brakes. "
     "Ridden for about two years; brake pads were replaced recently, shifting is smooth and the tires show "
     "light wear. U-lock included.",
     220, "自行车与滑板车", "Giant", "成色良好", "Black", "Nine East 33rd", "周六 10:00", "周日 18:00"),
    ("Schwinn 20 英寸折叠自行车", "20 英寸通勤折叠自行车，轻便，适合校园短途出行。",
     "Schwinn 20-inch folding bike with a blue frame that folds small enough to stash in a dorm room. "
     "Single speed, chain freshly oiled, rides smooth. Perfect for getting around campus and the nearby blocks.",
     130, "自行车与滑板车", "Schwinn", "成色很好", "Blue", "The Carlyle (500 W University Pkwy)",
     "工作日 18:00", "工作日 21:00"),
    ("Trek FX 混合通勤自行车", "Trek 700C 通勤自行车，成色很好，附前后挡泥板。",
     "Trek FX 700C hybrid commuter bike, silver frame, 7-speed drivetrain, with front and rear fenders and a "
     "rear rack. Used for daily commuting, never crashed, brakes are responsive.",
     290, "自行车与滑板车", "Trek", "成色很好", "Silver", "Campus Square Apartments (E 29th St)",
     "周末全天", "周末全天"),
    ("JBL 便携式蓝牙音箱", "JBL 便携式蓝牙音箱，续航不错，附充电线。",
     "JBL portable Bluetooth speaker, black cylinder design, Bluetooth 5.0, battery tested at around 8 hours. "
     "Comes with the original charging cable, no retail box. Sound is on the bright, clear side for its class.",
     25, "电子产品", "JBL", "成色很好", "Black", "The Marylander (3501 St Paul St)",
     "每天 12:00", "每天 20:00"),
    ("Logitech M330 静音无线鼠标", "Logitech 静音无线鼠标，带 USB 接收器，每个按键都正常。",
     "Logitech M330 silent wireless mouse in black, USB receiver included. The silent switches click cleanly and "
     "the scroll wheel has no skipping. Battery compartment is intact; bring your own AA battery.",
     12, "电子产品", "Logitech", "成色很好", "Black", "The Allston (St Paul & 32nd)",
     "工作日 15:00", "工作日 19:00"),
    ("白色 87 键机械键盘", "白色 87 键无数字区机械键盘，使用约一年，键帽略微打油。",
     "White compact 87-key tenkeyless mechanical keyboard with clicky blue-style switches and white backlight. "
     "About a year of use, keycaps have a light shine, every key tested and none are dead. USB-C cable included.",
     45, "电子产品", None, "成色良好", "White", "Jefferson House (N Calvert St)",
     "工作日晚上", "周末下午"),
    ("Kindle Paperwhite 8GB", "Kindle Paperwhite，8GB 防水版，屏幕无划痕。",
     "Kindle Paperwhite e-reader, 8GB storage, waterproof version. The e-ink screen has no scratches or bright "
     "spots, battery health is good, and a cover is included. Already factory reset.",
     60, "电子产品", "Kindle", "成色很好", "Black", "The Marylander (3501 St Paul St)",
     "每天 14:00", "每天 21:00"),
    ("Apple iPad 第 7 代 32GB", "iPad 第 7 代，32GB Wi-Fi 型号，边框有一处很小的磕碰。",
     "Apple iPad 7th generation, 10.2-inch, 32GB Wi-Fi model in silver. Screen is flawless with no scratches; "
     "there is a tiny ding on the lower-left corner of the frame. Battery lasts as expected, charger included. "
     "Great for lectures, notes and reading.",
     180, "电子产品", "Apple", "成色良好", "Silver", "St Paul St 2900 block townhouses",
     "周末 10:00", "周末 17:00"),
    ("Sony WH-CH510 头戴式耳机", "Sony 贴耳式蓝牙耳机，耳罩完好，续航长。",
     "Sony WH-CH510 wireless Bluetooth headphones in black. The earpads are intact with no flaking, Bluetooth "
     "stays connected, and battery life tests long. USB-C charging cable included.",
     30, "电子产品", "Sony", "成色很好", "Black", "The Broadview (105 W 39th St)",
     "每天 13:00", "每天 21:00"),
    ("Nintendo Switch V2", "Switch V2 游戏机，带一对 Joy-Cons 和底座。",
     "Nintendo Switch V2 (extended battery) console with both Joy-Cons, dock, HDMI cable and power adapter. "
     "Screen protector on since day one, zero stick drift. No game cartridges included.",
     210, "电子产品", "Nintendo", "成色很好", "Black", "Jefferson House (N Calvert St)",
     "周末 11:00", "周末 19:00"),
    ("Anker 20000mAh 充电宝", "Anker 20000mAh 充电宝，双向快充，测试正常。",
     "Anker 20000mAh power bank with 18W fast charging in and out, dual USB-A output. The shell has light "
     "scratches from use; charge and discharge both test fine. Cable included.",
     22, "电子产品", "Anker", "成色良好", "White", "Campus Square Apartments (E 29th St)",
     "每天 12:00", "每天 22:00"),
    ("可折叠床上电脑桌", "宿舍床上用可折叠电脑桌，结实且易收纳。",
     "Folding lap desk with a wood-grain top and metal folding legs. The surface is flat with no scratches, "
     "the folding latches work, and it holds a laptop plus books without wobbling. Awkward to carry far, so "
     "pickup is easiest.",
     15, "家具与电器", None, "成色很好", "Brown", "Wyman Park Apartments (W 40th St)",
     "周末下午", "周末傍晚"),
    ("LED 台灯，3 档亮度", "护眼 3 档调光台灯，USB 供电，无频闪。",
     "LED clip-on desk lamp in white with three brightness levels, USB powered, clips to a bed rail or desk "
     "edge. All LEDs light up with no flicker and the clamp still grips firmly. Great for late-night reading.",
     10, "家具与电器", None, "成色很好", "White", "Nine East 33rd",
     "每天 19:00", "每天 22:30"),
    ("Hamilton Beach 0.9 cu ft 微波炉", "Hamilton Beach 紧凑型微波炉，加热均匀，内部已清洁。",
     "Hamilton Beach 0.9 cu ft compact microwave in silver, 900W. Interior has been scrubbed clean, turntable "
     "spins fine and it heats evenly. Perfect size for a dorm or studio kitchen counter.",
     35, "家具与电器", "Hamilton Beach", "成色良好", "Silver", "Wyman Park Apartments (W 40th St)",
     "工作日 18:30", "工作日 21:30"),
    ("USB 夹式桌面风扇", "静音 USB 夹式风扇，3 档风速，带夹式底座。",
     "USB-powered quiet clip-on fan with three speed settings; the clamp base fixes onto a bed frame or desk. "
     "Blades have no cracks and it spins without rattling. A dorm essential for Baltimore summers.",
     10, "家具与电器", None, "成色很好", "Blue", "Jefferson House (N Calvert St)",
     "每天 18:00", "每天 22:00"),
    ("Stewart Calculus 第 8 版", "Stewart Calculus: Early Transcendentals，全书有笔记，无缺页。",
     "Calculus: Early Transcendentals by James Stewart, 8th edition, hardcover. Pencil notes and highlights on "
     "key chapters, no missing pages, no water damage. Covers Calc I through III — ideal if you're taking the "
     "sequence.",
     45, "图书与教材", None, "成色良好", "Red", "The Marylander (3501 St Paul St)",
     "每天 10:00", "每天 20:00"),
    ("Kaplan MCAT 备考套装", "Kaplan MCAT 7 本科目复习套装，几乎未使用。",
     "Kaplan MCAT Complete 7-Book Subject Review with online practice code (unused). Only skimmed the first "
     "two books; the rest are brand new with no notes. Great deal for anyone starting their MCAT prep.",
     60, "图书与教材", None, "几乎全新", "Blue", "The Allston (St Paul & 32nd)",
     "工作日 12:00", "工作日 18:00"),
    ("Introduction to Algorithms (CLRS)", "CS 算法教材，内页有少量铅笔标注。",
     "Introduction to Algorithms by Cormen, Leiserson, Rivest and Stein (CLRS), 3rd edition. Spine is intact, "
     "a few pencil annotations inside that erase cleanly. Ideal for CS and software engineering majors.",
     40, "图书与教材", None, "成色良好", "Green", "The Allston (St Paul & 32nd)",
     "工作日 13:00", "工作日 19:00"),
    ("Uniqlo Ultra Light Down 羽绒服，M 码", "Uniqlo 男款 Ultra Light Down 羽绒服，M 码，可收进自带收纳袋。",
     "Uniqlo men's Ultra Light Down jacket, size M, navy blue, packs into its own little pouch for travel. "
     "No holes or stains, zipper runs smooth. Works great as a mid-layer through the Baltimore winter.",
     30, "服装与鞋靴", "Uniqlo", "成色很好", "Blue", "St Paul St 2900 block townhouses",
     "周末 10:00", "周末 20:00"),
    ("Nike 跑鞋，9 码", "Nike 跑鞋，男款 9 码，鞋底轻微磨损，刚洗过。",
     "Nike running shoes, men's US size 9, black and white colorway. Tread is still sharp with light wear, "
     "insides are clean with no odor — freshly washed. Selling because I bought a size too big.",
     40, "服装与鞋靴", "Nike", "成色良好", "Black", "The Baltimorean (3121 St Paul St)",
     "周末 09:00", "周末 18:00"),
    ("15.6 英寸笔记本电脑背包", "15.6 英寸笔记本电脑背包，有很多隔层，拉链都能正常使用。",
     "Gray 15.6-inch laptop backpack with a padded laptop sleeve and side pockets. Zippers all work, straps "
     "have no fraying, bottom shows no wear-through. Plenty for classes and the daily commute.",
     18, "服装与鞋靴", None, "成色很好", "Gray", "The Broadview (105 W 39th St)",
     "每天 12:00", "每天 21:00"),
    ("一对 15 lb 橡胶哑铃", "橡胶包胶哑铃，每只 15 lb，宿舍锻炼够用。",
     "Pair of rubber-coated hex dumbbells, 15 lb each (30 lb total). The rubber coating is intact with no "
     "cracks and the grips are non-slip. Enough for arm and shoulder work in your room. Heavy — pickup only.",
     25, "运动与户外", None, "成色很好", "Black", "The Baltimorean (3121 St Paul St)",
     "周末 09:00", "周末 17:00"),
    ("Wilson 网球拍", "网球拍，刚换过握把，球线状态良好。",
     "Wilson tennis racket, red frame, slightly flexible shaft that suits beginners. Freshly regripped, string "
     "tension is fine with no breaks, frame has no warping. Cover included.",
     30, "运动与户外", "Wilson", "成色良好", "Red", "The Baltimorean (3121 St Paul St)",
     "周末 15:00", "周末 20:00"),
    ("6mm 瑜伽垫", "6mm 厚瑜伽垫，防滑，含背带。",
     "6mm thick TPE yoga mat in purple with non-slip texture on both sides. No tears, no smell, freshly wiped "
     "down. Carrying strap included — great for core workouts in your room or classes at the rec center.",
     12, "运动与户外", None, "成色很好", "Purple", "The Broadview (105 W 39th St)",
     "每天 17:00", "每天 21:00"),
    ("Fender 原声吉他", "全尺寸 Fender 原声吉他，音准保持良好，含琴包和背带。",
     "Fender full-size acoustic guitar with a spruce top, natural brown finish. Frets have no sprouting, it "
     "holds tune well, and the action is comfortable for learning. Comes with a gig bag, strap and capo. "
     "Great starter guitar.",
     110, "乐器", "Fender", "成色良好", "Brown", "St Paul St 2900 block townhouses",
     "周末 14:00", "周末 20:00"),
]

#: Preset statuses: item index -> list of user emails reserving it (demos reserved / sold states)
RESERVED_PLAN = {
    0: ["buyer@campus.edu", "seller2@campus.edu"],   # mountain bike: buyer is #1 in queue
    5: ["buyer@campus.edu"],                          # mechanical keyboard: buyer is #1 in queue
    9: ["seller1@campus.edu", "seller3@campus.edu"],  # Switch: buyer not in queue, card shows "2 people reserved"
    16: ["buyer@campus.edu"],                         # MCAT prep set: near expiry, demos the countdown
}
#: Confirmed deals (the buyer sees "deal confirmed" in the cart)
SOLD_PLAN = {19: "buyer@campus.edu"}                  # Nike running shoes
#: Items about to expire (2 hours left) — demos the countdown and the expiry poll
NEAR_EXPIRY_ITEMS = {16}


def wipe() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    for path in settings.upload_path.glob("*.jpg"):
        path.unlink(missing_ok=True)
    index = get_item_index()
    index.vector.reset()
    index.bm25.reset({})
    index.records.clear()
    print("已清空商业数据库，上传目录和索引")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留现有数据，仅重建索引")
    args = parser.parse_args()

    init_db()
    if not args.keep:
        wipe()

    index = get_item_index()

    with SessionLocal() as db:
        if args.keep:
            index.rebuild_from_db(db)
            print("索引已重建：", index.stats())
            return 0

        users: dict[str, User] = {}
        for email, location_name in [BUYER, *SELLERS]:
            loc = LOCATION_BY_NAME[location_name]
            user = User(
                email=email,
                password_hash=hash_password(PASSWORD),
                avatar=f"avatar-{len(users) % 8}",
                location_name=loc["name"],
                lat=loc["lat"],
                lng=loc["lng"],
                pickup_range_enabled=False,
                pickup_range_km=settings.default_pickup_range_km,
            )
            db.add(user)
            users[email] = user
        db.flush()
        print(f"Created {len(users)} users (shared password {PASSWORD})")

        seller_emails = [e for e, _ in SELLERS]
        items: list[Item] = []
        for idx, row in enumerate(ITEMS):
            (name, summary, description, price, category, brand, condition,
             color, location_name, t_start, t_end) = row
            loc = LOCATION_BY_NAME[location_name]
            seller = users[seller_emails[idx % len(seller_emails)]]
            photo = make_placeholder(name, idx)
            item = Item(
                seller_id=seller.id,
                name=name,
                description=description,
                summary=summary,
                attributes={
                    "category": category,
                    "brand": brand,
                    "condition": condition,
                    "color": color,
                },
                price=float(price),
                photo_path=photo,
                pickup_location_name=loc["name"],
                lat=loc["lat"],
                lng=loc["lng"],
                pickup_time_start=t_start,
                pickup_time_end=t_end,
                status=ItemStatus.SELLING,
                created_at=utcnow() - timedelta(hours=len(ITEMS) - idx),
            )
            db.add(item)
            items.append(item)
        db.flush()
        for item in items:
            db.add(StatusLog(item_id=item.id, from_status=None,
                             to_status=ItemStatus.SELLING, reason="种子商品"))
        db.flush()
        print(f"Created {len(items)} items")

        # ---- Preset reservations / deals ----
        for idx, buyer_emails in RESERVED_PLAN.items():
            item = items[idx]
            for order, email in enumerate(buyer_emails):
                db.add(
                    Reservation(
                        item_id=item.id,
                        buyer_id=users[email].id,
                        status=ReservationStatus.ACTIVE,
                        created_at=utcnow() - timedelta(minutes=30 - order * 5),
                    )
                )
            item.status = ItemStatus.RESERVED
            hours = 2 if idx in NEAR_EXPIRY_ITEMS else settings.confirm_window_hours
            item.confirm_deadline = utcnow() + timedelta(hours=hours)
            db.add(StatusLog(item_id=item.id, from_status=ItemStatus.SELLING,
                             to_status=ItemStatus.RESERVED, reason="种子预订"))

        for idx, email in SOLD_PLAN.items():
            item = items[idx]
            db.add(
                Reservation(
                    item_id=item.id,
                    buyer_id=users[email].id,
                    status=ReservationStatus.ACTIVE,
                    created_at=utcnow() - timedelta(hours=3),
                )
            )
            item.status = ItemStatus.SOLD_OUT
            item.confirm_deadline = None
            db.add(StatusLog(item_id=item.id, from_status=ItemStatus.RESERVED,
                             to_status=ItemStatus.SOLD_OUT, reason="种子交易已确认"))

        db.commit()

        stats = index.rebuild_from_db(db)
        counts = {
            "在售": sum(1 for i in items if i.status == ItemStatus.SELLING),
            "已预留": sum(1 for i in items if i.status == ItemStatus.RESERVED),
            "售罄": sum(1 for i in items if i.status == ItemStatus.SOLD_OUT),
        }

    print("状态分布：", counts)
    print("索引统计：", stats)
    print()
    print("登录账户：")
    print(f"  buyer   {BUYER[0]} / {PASSWORD}")
    for email, _ in SELLERS:
        print(f"  seller  {email} / {PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
