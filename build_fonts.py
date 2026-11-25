import os
import sys
import time
import csv
import argparse
import zipfile
import re
import unicodedata

from pathlib import PurePosixPath
from collections import defaultdict

import fontforge



EM_SIZE = 1000
ASCENT = 800
DESCENT = 200



MARGIN = 20                  
REF_CANVAS = 900              
FIT_SIZE = 900               


def parse_svg_contours(svg_text):
    if "transform=" in svg_text:
        raise ValueError("SVG 含 transform，請先展平")
    contours = []
    for d in re.findall(r'\sd="([^"]*)"', svg_text):
        cur = None
        tokens = re.findall(r"[A-Za-z]|-?\d*\.?\d+(?:[eE][-+]?\d+)?", d)
        i = 0
        cmd = None
        while i < len(tokens):
            t = tokens[i]
            if t.isalpha():
                cmd = t
                i += 1
                if cmd in ("Z", "z"):
                    if cur and len(cur) >= 3:
                        contours.append(cur)
                    cur = None
                elif cmd == "M":
                    if cur and len(cur) >= 3:
                        contours.append(cur)
                    cur = []
                elif cmd != "L":
                    raise ValueError("不支援的 path 指令：" + cmd)
                continue
            if cmd not in ("M", "L"):
                raise ValueError("path 格式異常")
            x, y = float(tokens[i]), float(tokens[i + 1])
            i += 2
            if cur is None:
                cur = []
            cur.append((x, y))
        if cur and len(cur) >= 3:
            contours.append(cur)
    if not contours:
        raise ValueError("SVG 裡沒有可用的輪廓")
    return contours


def parse_canvas_size(svg_text):
    m = re.search(r'viewBox\s*=\s*"([^"]*)"', svg_text)
    if m:
        nums = re.findall(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?", m.group(1))
        if len(nums) == 4:
            w, h = abs(float(nums[2])), abs(float(nums[3]))
            if max(w, h) > 0:
                return max(w, h)

    sizes = []
    for attr in ("width", "height"):
        m = re.search(r'<svg[^>]*\s' + attr + r'\s*=\s*"\s*(-?\d*\.?\d+)', svg_text)
        if m:
            sizes.append(abs(float(m.group(1))))
    if sizes and max(sizes) > 0:
        return max(sizes)
    return None


def layout_contours(contours, width, scale=1.0, fit=False):
    xs = [p[0] for c in contours for p in c]
    ys = [p[1] for c in contours for p in c]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    em_center_y = (ASCENT - DESCENT) / 2

    if fit:
        s = FIT_SIZE / max(x1 - x0, y1 - y0)
        out = [[((x - cx) * s + width / 2,
                 em_center_y - (y - cy) * s) for x, y in c]
               for c in contours]
        return out, width

    needed = int(round((x1 - x0) * scale + 2 * MARGIN))
    width = max(width, needed)

    out = [[((x - cx) * scale + width / 2,
             em_center_y - (y - cy) * scale) for x, y in c]
           for c in contours]
    return out, width


def draw_contours(glyph, contours):
    pen = glyph.glyphPen()
    for pts in contours:
        pen.moveTo(pts[0])
        for p in pts[1:]:
            pen.lineTo(p)
        pen.closePath()
    pen = None


def apply_vertical_metrics(font):
    ymin, ymax = 0, 0
    for g in font.glyphs():
        bb = g.boundingBox()
        if bb == (0, 0, 0, 0):
            continue
        ymin = min(ymin, bb[1])
        ymax = max(ymax, bb[3])

    top = max(ASCENT, int(ymax) + 1)
    bottom = max(DESCENT, int(-ymin) + 1)

    font.hhea_ascent_add = False
    font.hhea_descent_add = False
    font.os2_typoascent_add = False
    font.os2_typodescent_add = False
    font.os2_winascent_add = False
    font.os2_windescent_add = False

    font.hhea_ascent = top
    font.hhea_descent = -bottom
    font.hhea_linegap = 0
    font.os2_typoascent = top
    font.os2_typodescent = -bottom
    font.os2_typolinegap = 0
    font.os2_winascent = top
    font.os2_windescent = bottom


def add_big5_cmap(ttf_path):
    try:
        from fontTools.ttLib import TTFont, newTable
        from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
    except ImportError:
        print("[提示] 未安裝 fontTools，略過 Big5 對照表（pip install fonttools 後重跑即可）")
        return

    font = TTFont(ttf_path)
    unicode_map = font.getBestCmap()

    big5 = {}
    for cp, glyph in unicode_map.items():
        try:
            raw = chr(cp).encode("cp950")
        except UnicodeEncodeError:
            continue
        code = raw[0] if len(raw) == 1 else (raw[0] << 8) | raw[1]
        big5[code] = glyph

    if not big5:
        font.close()
        return

    cmap = font["cmap"]
    cmap.tables = [t for t in cmap.tables if (t.platformID, t.platEncID) != (3, 4)]

    sub = CmapSubtable.newSubtable(4)
    sub.platformID = 3
    sub.platEncID = 4
    sub.language = 0
    sub.cmap = big5
    cmap.tables.append(sub)

    font.save(ttf_path)
    font.close()
    print("已加入 Big5(CP950) 對照表：{} 個字".format(len(big5)))


def parse_args():
    parser = argparse.ArgumentParser(
        description="FontForge ZIP 字型產生工具"
    )

    parser.add_argument("--zip", required=True,
                        help="輸入 ZIP 檔案")

    parser.add_argument("--folder",
                        help="ZIP 裡面要轉換的資料夾")

    parser.add_argument("--family",
                        help="字型家族名稱")

    parser.add_argument("--fontname",
                        help="PostScript 字型名稱")

    parser.add_argument("--output",
                        help="輸出的 TTF 檔名")

    parser.add_argument("--output-dir",
                        default="output_fonts",
                        help="輸出資料夾")

    parser.add_argument("--no-big5", action="store_true",
                        help="不要在 TTF 內加入 Big5(CP950) 對照表（預設會加入，與 Unicode 對照表並存於同一個檔案）")

    parser.add_argument("--scale", type=float, default=1.0,
                        help="整體等比例縮放（預設 1.0 = 與 SVG 稿紙相同大小）")

    parser.add_argument("--fit", action="store_true",
                        help="每個字各自放大到填滿字框（字會變大，但筆畫粗細會不一致）")

    parser.add_argument("--list-folders",
                        action="store_true",
                        help="列出 ZIP 裡的資料夾")

    return parser.parse_args()

def list_zip_folders(zip_path):
    folders = set()

    with zipfile.ZipFile(zip_path, "r") as z:
        for name in z.namelist():
            if name.lower().endswith(".svg"):
                folders.add(os.path.dirname(name))

    for folder in sorted(folders):
        print(folder)

def get_unicode(filename):
    stem = PurePosixPath(filename).stem

    if stem.upper().startswith("U+"):
        try:
            codepoint = int(stem[2:], 16)

            if not (0 <= codepoint <= 0x10FFFF):
                return None

            if 0xD800 <= codepoint <= 0xDFFF:
                return None

            return codepoint

        except ValueError:
            return None


    if len(stem) == 1:
        codepoint = ord(stem)

        if 0xD800 <= codepoint <= 0xDFFF:
            return None

        return codepoint

    return None


def glyph_name(codepoint):
    if codepoint <= 0xFFFF:
        return "uni{:04X}".format(codepoint)

    return "u{:05X}".format(codepoint)



def load_overrides(output_dir, folder):
    overrides = {}

    path = os.path.join(output_dir, "overrides.csv")

    if not os.path.exists(path):
        return overrides

    with open(path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["folder"] != folder:
                continue

            value = row["codepoint"].strip().upper()

            if value.startswith("U+"):
                cp = int(value[2:], 16)
            else:
                cp = int(value, 0)

            overrides[row["filename"]] = cp

    return overrides



def scan_svg(zip_file, folder, overrides):

    result = []
    report = []

    folder = folder.strip("/")

    for info in zip_file.infolist():

        if info.is_dir():
            continue

        path = PurePosixPath(info.filename)

        if path.suffix.lower() != ".svg":
            continue

        if str(path.parent) != folder:
            continue

        filename = path.name

        cp = overrides.get(filename, get_unicode(filename))

        if cp is None:
            report.append([
                folder,
                info.filename,
                "",
                "無法從檔名判斷 Unicode"
            ])
            continue

        if not (0 <= cp <= 0x10FFFF):
            report.append([
                folder, info.filename, "", "Unicode 超出範圍"
            ])
            continue

        if 0xD800 <= cp <= 0xDFFF:
            report.append([
                folder, info.filename, "", "Unicode 為非法代理碼"
            ])
            continue

        result.append({
            "info": info,
            "filename": filename,
            "codepoint": cp
        })

    return result, report



def remove_conflicts(items, folder, report):
    groups = defaultdict(list)

    for item in items:
        groups[item["codepoint"]].append(item)

    valid = []

    for cp, group in groups.items():
        if len(group) > 1:
            for item in group:
                report.append([
                    folder,
                    item["info"].filename,
                    "U+{:04X}".format(cp),
                    "Unicode 重複，需人工確認"
                ])
            continue

        valid.append(group[0])

    return valid

def get_glyph_width(cp):
    char = chr(cp)

    east_asian_width = unicodedata.east_asian_width(char)

    if east_asian_width in ("F", "W"):
        return 1000

    if east_asian_width == "A":
        return 600

    return 600

def build_font(zip_file, folder, items, args):

    total = len(items)

    print("\n" + "=" * 60)
    print("總 SVG 數量：", total)
    print("=" * 60)

    font = fontforge.font()

    font.encoding = "UnicodeFull"
    font.familyname = args.family
    font.fontname = args.fontname
    font.fullname = args.family

    font.version = "1.0"
    font.weight = "Regular"

    font.ascent = ASCENT
    font.descent = DESCENT

    imported = 0
    failed = 0
    shrunk_names = []
    start_time = time.time()

    for index, item in enumerate(items, start=1):

        info = item["info"]
        cp = item["codepoint"]
        glyph = None

        try:
            svg_text = zip_file.read(info).decode("utf-8")
            contours = parse_svg_contours(svg_text)

            canvas = parse_canvas_size(svg_text)
            unit = REF_CANVAS / canvas if canvas else 1.0

            width = get_glyph_width(cp)
            contours, width = layout_contours(contours, width, args.scale * unit, args.fit)
            widened = width > get_glyph_width(cp)

            glyph = font.createChar(cp, glyph_name(cp))
            glyph.clear()
            draw_contours(glyph, contours)
            glyph.width = width   

            imported += 1
            if widened:
                shrunk_names.append(info.filename)

            print(
                "[成功] {} -> U+{:04X}{}".format(
                    info.filename, cp,
                    ("（稿紙 {:g}，已換算 x{:.2f}）".format(canvas, unit)
                     if canvas and abs(unit - 1) > 1e-6 else "")
                    + ("（字較寬，已加寬字距）" if widened else "")
                ),
                flush=True
            )

        except Exception as e:

            if glyph is not None:
                try:
                    font.removeGlyph(glyph)
                except Exception:
                    pass

            failed += 1
            print(
                "[失敗] {} : {}".format(info.filename, e),
                flush=True
            )

        percent = index / total * 100
        elapsed = time.time() - start_time
        speed = index / elapsed if elapsed > 0 else 0
        remaining = (total - index) / speed if speed > 0 else 0

        bar_length = 30
        filled = int(bar_length * index / total)
        bar = "#" * filled + "-" * (bar_length - filled)

        print(
            "\r[{}] {:6.2f}% | {}/{} | 成功:{} | 失敗:{} | 剩餘:{} | {}".format(
                bar, percent, index, total, imported, failed,
                int(remaining), os.path.basename(info.filename)[:25]
            ),
            end="",
            flush=True
        )

    print("\n\nSVG 匯入完成！")
    print("成功：", imported)
    print("失敗：", failed)
    if shrunk_names:
        print("字較寬而加寬字距的字：", len(shrunk_names))

    print("\n[1/3] 正在整理字型輪廓...", flush=True)

    try:
        font.selection.all()
        font.removeOverlap()
        font.correctDirection()
        font.selection.none()

        for cp in range(0x0021, 0x007F):
            if cp in font:
                font.selection.select(("more", None), cp)
        font.autoWidth(100)
        font.selection.none()

        if 0x0020 in font:
            font[0x0020].width = get_glyph_width(0x0020)

        apply_vertical_metrics(font)

    except Exception as e:
        print("\n輪廓整理警告：", e)

    print("[2/3] 正在產生 TTF...", flush=True)

    os.makedirs(args.output_dir, exist_ok=True)
    output_path = os.path.join(args.output_dir, args.output)
    font.generate(output_path)

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("TTF 產生失敗或檔案為空")

    print("[3/3] 正在關閉 FontForge...", flush=True)
    font.close()

    if not args.no_big5:
        add_big5_cmap(output_path)

    print("\n" + "=" * 60)
    print("字型輸出完成：", output_path)
    print("成功：", imported)
    print("失敗：", failed)
    print("耗時：{:.1f} 秒".format(time.time() - start_time))
    print("=" * 60)

    return imported, failed



def save_report(report, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(
        output_dir,
        "import_report.csv"
    )

    with open(path,"w",newline="",encoding="utf-8-sig") as f:

        writer = csv.writer(f)

        writer.writerow([
            "Folder",
            "Original Filename",
            "Unicode",
            "Status"
        ])

        writer.writerows(report)

    print("\n檢查報告：", path)



def main(args):

    print("=" * 50)
    print("FontForge SVG Batch Import")
    print("=" * 50)

    os.makedirs(args.output_dir, exist_ok=True)

    folder = args.folder.strip("/")

    overrides = load_overrides(args.output_dir, folder)

    with zipfile.ZipFile(args.zip, "r") as z:

        scanned, report = scan_svg(
            z,
            folder,
            overrides
        )

        items = remove_conflicts(
            scanned,
            folder,
            report
        )

        print("\n指定資料夾：", folder)
        print("可匯入 SVG：", len(items))

        if not items:
            save_report(report, args.output_dir)
            print("[錯誤] 沒有可匯入的 SVG")
            return

        build_font(z, folder, items, args)

    save_report(report, args.output_dir)

    print("\n所有字型製作流程結束！")


if __name__ == "__main__":

    args = parse_args()

    if args.list_folders:
        list_zip_folders(args.zip)
        sys.exit(0)

    required = [
        args.folder,
        args.family,
        args.fontname,
        args.output
    ]

    if not all(required):
        raise ValueError(
            "請指定 --folder、--family、--fontname、--output"
        )

    if not os.path.isfile(args.zip):
        raise FileNotFoundError(
            f"找不到 ZIP 檔案：{args.zip}"
        )

    main(args)