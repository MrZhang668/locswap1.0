"""Build the photo variants the image-recognition families need.

    python scripts/badcase_eval/make_photo_fixtures.py --build
    python scripts/badcase_eval/make_photo_fixtures.py --check

The image families ask what the assistant does with a photo that is dark,
blurry, holds two products, or contradicts the seller's words. None of that
needed new photography: the corpus already carries 788 real product photos,
and the four conditions are derivable from them.

* **dark** — the same photo at 28% luminance with the contrast pulled in, which
  is what a dorm-room shot at night actually looks like: not uniformly black,
  just starved of light with the highlights surviving.
* **blur** — a 2.6px Gaussian, the range a phone produces from a half-pressed
  shutter. Strong enough to lose fine print, weak enough that the object is
  still identifiable, which is the interesting case: the assistant should read
  it *or* say it cannot, and not guess.
* **multi** — two products from the same seller composited side by side on a
  neutral ground, the second one smaller and offset, so "which of these am I
  selling" is a real question with a defensible answer (the larger, centred
  one) rather than a coin flip.
* **conflict** — no processing at all. The case pairs product A's real photo
  with text naming product B; the conflict is between the two inputs, not
  inside the image.

Written into data/uploads/ alongside the real photos, under a `bcfix_` prefix
so `--check` can find and remove them. Paths are stored the way item rows store
them (`uploads/<name>`), which is what the chat endpoint expects.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import _bootstrap  # noqa: E402,F401

from PIL import Image, ImageEnhance, ImageFilter  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Item  # noqa: E402

import cases_data as CD  # noqa: E402

DATA = Path(settings.data_dir)
PREFIX = "bcfix_"
INDEX = _HERE / "photo_fixtures.json"

#: A product name in the case set matched against the corpus item names. A few
#: of the case set's names carry qualifiers the corpus spells differently.
ALIASES = {
    # Aliases exist because the case set and the corpus spell the same product
    # differently. They must stay specific: a one-character alias matches the
    # wrong thing — "桌" pulled a 桌面麦克风 for 书桌, "垫" pulled a 床垫 for
    # 野餐垫, "架" pulled a 床架 for 置物架. Every entry here is at least two
    # characters and names the product, not a part of it.
    "iPad平板": "平板", "Switch游戏机": "Switch", "Kindle阅读器": "Kindle",
    "微单相机": "相机", "TI-84图形计算器": "计算器", "科学计算器": "计算器",
    "人体工学办公椅": "办公椅", "双人沙发": "沙发", "布衣柜": "衣柜",
    "手持吸尘器": "吸尘器", "加厚被子": "被子", "记忆棉枕头": "枕头",
    "遮光窗帘": "窗帘", "胶囊咖啡机": "咖啡机", "马克笔套装": "马克笔",
    "小白板": "白板", "手账本": "手账", "水彩颜料": "颜料",
    "呢子大衣": "大衣", "连帽卫衣": "卫衣", "羊毛围巾": "围巾",
    "露营帐篷": "帐篷", "野餐垫": "野餐垫", "弹力带": "阻力带",
    "通勤单车": "自行车", "折叠自行车": "自行车", "儿童自行车": "自行车",
    "电动自行车": "电动车", "原版小说": "小说", "英汉词典": "词典",
    "线性代数习题册": "习题", "经济学原理教材": "教材", "统计学教材": "教材",
    "微积分教材": "教材", "有机化学课本": "课本", "GRE复习资料": "GRE",
    "MCAT备考书": "MCAT", "解题手册": "手册", "电热水壶": "电热水壶",   # 不能用「水壶」：会匹配到滤水壶
    "不粘锅": "不粘锅", "置物架": "置物架", "收纳箱": "收纳箱",
    "空气炸锅": "炸锅", "电饭煲": "电饭", "保温杯": "保温",
    "无线鼠标": "鼠标", "机械键盘": "键盘", "蓝牙耳机": "耳机",
    "蓝牙音箱": "音箱", "智能手表": "手表", "笔记本电脑": "笔记本",
    "充电宝": "充电宝", "投影仪": "投影", "路由器": "路由",
    "麦克风": "麦克风", "显示器": "显示器", "摄像头": "摄像头",
    "转换器": "转换器", "分线器": "分线器",
    "木吉他": "吉他", "电吉他": "电吉他", "尤克里里": "尤克",
    "电钢琴": "钢琴", "电子琴": "电子琴", "小提琴": "提琴",
    "口琴": "口琴", "电鼓": "架子鼓",
    "羽绒服": "羽绒", "冲锋衣": "冲锋衣", "雪地靴": "雪地靴",
    "双肩包": "双肩包", "行李箱": "行李箱", "牛仔裤": "牛仔", "跑鞋": "跑鞋",
    "瑜伽垫": "瑜伽垫", "网球拍": "网球拍", "羽毛球拍": "羽毛球拍",
    "篮球": "篮球", "哑铃": "哑铃", "壶铃": "壶铃", "跳绳": "跳绳",
    "滑板": "滑板", "落地灯": "落地灯", "台灯": "台灯",
    "取暖器": "取暖", "电风扇": "风扇", "微波炉": "微波炉",
    "小冰箱": "冰箱", "床垫": "床垫", "床架": "床架",
    "书桌": "书桌", "书架": "书架", "斗柜": "斗柜", "地毯": "地毯",
    "活页夹": "活页", "便利贴": "便利贴", "画板": "画板", "破壁机": "破壁",
    "山地车": "山地车", "公路车": "公路车", "电动滑板车": "滑板车",
}


#: Products paired with a genuine photograph, built by `scan_real_photos`.
REAL = _HERE / "real_photos.json"


def _is_photograph(path: Path) -> bool:
    """Tell a real photo from the corpus's placeholder cards.

    Most of the 788 listing images are generated placeholders — a flat colour
    block with the product name set in it. They are useless here: "can the
    assistant read this in low light" is not a question you can ask of a
    rectangle with text on it. Photographs have a long tail of colours;
    placeholders put most of their pixels in a handful. A real corpus photo
    measures ~12k distinct colours with the top eight covering 7% of the
    frame; a placeholder measures ~1k with the top eight covering 85%.
    """
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((160, 160))
        cols = im.getcolors(maxcolors=1_000_000) or []
        if len(cols) < 400:
            return False
        top8 = sorted(c[0] for c in cols)[-8:]
        return sum(top8) / (im.width * im.height) < 0.55
    except Exception:  # noqa: BLE001  an unreadable file is not a photograph
        return False


def find_photos(db) -> dict[str, str]:
    """product -> a genuine photograph, from the prepared index.

    Built once by `scan_real_photos` across two sources: the corpus's own
    listing images (78 of 788 are real), and the annotated training set
    (82 of 84 are real, and it carries Chinese labels so the mapping is
    reliable). Together they cover 34 of the 101 products, across six of the
    nine categories — instruments, school supplies and bikes have no
    photograph anywhere, so the image families cannot cover them.
    """
    if not REAL.exists():
        raise SystemExit("缺 real_photos.json，先跑 --scan")
    idx = json.loads(REAL.read_text(encoding="utf-8"))
    return {p: v for p, v in idx.items() if (DATA / v).exists() or Path(v).exists()}


def _open(rel: str) -> Image.Image:
    """Sources come from two roots: data/ for corpus images, repo-relative for
    the training set."""
    p = DATA / rel
    return Image.open(p if p.exists() else Path(rel))


def _save(img: Image.Image, name: str) -> str:
    rel = f"uploads/{PREFIX}{name}.jpg"
    dest = DATA / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(dest, "JPEG", quality=88)
    return rel


def make_dark(src: str, key: str) -> str:
    img = _open(src).convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(0.28)
    img = ImageEnhance.Contrast(img).enhance(0.75)
    return _save(img, f"dark_{key}")


def make_blur(src: str, key: str) -> str:
    img = _open(src).convert("RGB")
    return _save(img.filter(ImageFilter.GaussianBlur(radius=2.6)), f"blur_{key}")


def make_multi(a: str, b: str, key: str) -> str:
    """Two products in one frame; the larger centred one is the defensible
    answer to 'which am I selling', so the case has a right answer."""
    W, H = 900, 600
    canvas = Image.new("RGB", (W, H), (238, 238, 236))
    main = _open(a).convert("RGB")
    main.thumbnail((520, 460))
    canvas.paste(main, (40, (H - main.height) // 2))
    side = _open(b).convert("RGB")
    side.thumbnail((260, 230))
    canvas.paste(side, (W - side.width - 50, H - side.height - 60))
    return _save(canvas, f"multi_{key}")


def scan_real_photos() -> None:
    """Rebuild real_photos.json from both sources."""
    import csv
    out: dict[str, str] = {}
    labels: dict[str, str] = {}
    want = {p for _, p in CD.FLAT}
    with SessionLocal() as db:
        items = db.scalars(select(Item).where(Item.photo_path.isnot(None))).all()
    for it in items:
        f = DATA / (it.photo_path or "")
        if not f.exists() or not _is_photograph(f):
            continue
        for prod in want:
            if prod not in out and ALIASES.get(prod, prod) in (it.name or ""):
                out[prod] = it.photo_path
                labels[prod] = it.name or ""
    tdir = Path("training_source_image_product/商品图训练集_images")
    if (tdir / "annotations.csv").exists():
        for r in csv.DictReader(open(tdir / "annotations.csv", encoding="utf-8-sig")):
            f = tdir / r["filename"]
            if not f.exists():
                continue
            lab = " ".join(filter(None, (r.get("label_cn"), r.get("label_en"),
                                         r.get("matched_base"), r.get("notes"))))
            for prod in want:
                if prod in out:
                    continue
                if ALIASES.get(prod, prod) in lab or prod in lab:
                    out[prod] = str(f).replace("\\", "/")
                    labels[prod] = (r.get("label_cn") or "") + " / " + (r.get("label_en") or "")
    REAL.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (_HERE / "real_photos_audit.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"实拍照覆盖 {len(out)} / {len(want)} 件商品 -> {REAL.name}")
    print("  每件对应的照片标签见 real_photos_audit.json")


def build() -> None:
    with SessionLocal() as db:
        photos = find_photos(db)
    print(f"语料里能配到实拍照的用例商品：{len(photos)} / {len(CD.FLAT)}")

    by_cat: dict[str, list[str]] = {}
    for cat, prod in CD.FLAT:
        if prod in photos:
            by_cat.setdefault(cat, []).append(prod)

    index: dict[str, dict[str, str]] = {}
    made = 0
    for prod, rel in photos.items():
        src = rel
        key = str(abs(hash(prod)) % 10_000_000)
        entry = {"plain": rel}
        try:
            entry["dark"] = make_dark(src, key)
            entry["blur"] = make_blur(src, key)
            made += 2
        except Exception as exc:  # noqa: BLE001  a broken source file is skippable
            print(f"  跳过 {prod}: {type(exc).__name__}")
            continue
        index[prod] = entry

    # one composite per product, pairing it with a sibling in its category
    for cat, prods in by_cat.items():
        for n, prod in enumerate(prods):
            if prod not in index or len(prods) < 2:
                continue
            other = prods[(n + 1) % len(prods)]
            try:
                index[prod]["multi"] = make_multi(
                    photos[prod], photos[other],
                    str(abs(hash(prod + other)) % 10_000_000))
                index[prod]["multi_with"] = other
                made += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  合成跳过 {prod}+{other}: {type(exc).__name__}")

    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"生成图片 {made} 张，覆盖 {len(index)} 件商品 -> {INDEX.name}")


def check() -> None:
    if not INDEX.exists():
        raise SystemExit("还没生成，先 --build")
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    missing = []
    kinds = {"plain": 0, "dark": 0, "blur": 0, "multi": 0}
    for prod, e in index.items():
        for k in kinds:
            if k in e:
                kinds[k] += 1
                if not ((DATA / e[k]).exists() or Path(e[k]).exists()):
                    missing.append(e[k])
    print(f"覆盖商品 {len(index)} 件")
    for k, v in kinds.items():
        print(f"  {k:<7}{v:>4} 张")
    print(f"文件缺失 {len(missing)} 个" + (f"：{missing[:3]}" if missing else ""))
    n_files = len(list((DATA / "uploads").glob(f"{PREFIX}*")))
    print(f"磁盘上 {PREFIX}* 文件 {n_files} 个")


def clean() -> None:
    n = 0
    for f in (DATA / "uploads").glob(f"{PREFIX}*"):
        f.unlink()
        n += 1
    if INDEX.exists():
        INDEX.unlink()
    print(f"删除生成图片 {n} 张")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="重新扫描实拍照来源")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--clean", action="store_true")
    a = ap.parse_args()
    if a.scan:
        scan_real_photos()
    if a.clean:
        clean()
    if a.build:
        build()
    if a.check or a.build:
        check()
    if not (a.build or a.check or a.clean):
        ap.error("要 --build / --check / --clean 之一")


if __name__ == "__main__":
    main()
