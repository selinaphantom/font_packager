
import os
import sys
import time
import csv
import zipfile
import tempfile
import unicodedata
from pathlib import PurePosixPath
from collections import defaultdict

import fontforge



ZIP_PATH = sys.argv[1] if len(sys.argv) > 1 else "font.zip"

OUTPUT_DIR = sys.argv[2] if len(sys.argv) > 2 else "output_fonts"

STYLES = {
    "工整": {
        "family": "MyFont Neat",
        "fontname": "MyFontNeat",
        "output": "MyFont_Neat.ttf"
    },
    "潦草": {
        "family": "MyFont Handwriting",
        "fontname": "MyFontHandwriting",
        "output": "MyFont_Handwriting.ttf"
    }
}

EM_SIZE = 1000
ASCENT = 800
DESCENT = 200

os.makedirs(OUTPUT_DIR, exist_ok=True)


def get_unicode(filename):


    stem = PurePosixPath(filename).stem

    if len(stem) != 1:
        return None

    codepoint = ord(stem)

    if 0xD800 <= codepoint <= 0xDFFF:
        return None

    return codepoint


def glyph_name(codepoint):
    if codepoint <= 0xFFFF:
        return "uni{:04X}".format(codepoint)

    return "u{:05X}".format(codepoint)



def load_overrides():


    overrides = {}

    path = os.path.join(OUTPUT_DIR, "overrides.csv")

    if not os.path.exists(path):
        return overrides

    with open(path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            value = row["codepoint"].strip().upper()

            if value.startswith("U+"):
                cp = int(value[2:], 16)
            else:
                cp = int(value, 0)

            overrides[
                (row["style"], row["filename"])
            ] = cp

    return overrides



def scan_svg(zip_file, overrides):

    result = defaultdict(list)
    report = []

    for info in zip_file.infolist():

        if info.is_dir():
            continue

        path = PurePosixPath(info.filename)

        if path.suffix.lower() != ".svg":
            continue

        style = path.parent.name

        if style not in STYLES:
            continue

        filename = path.name

        cp = overrides.get(
            (style, filename),
            get_unicode(filename)
        )

        if cp is None:
            report.append([
                style,
                info.filename,
                "",
                "無法從檔名判斷 Unicode"
            ])
            continue

        if not (0 <= cp <= 0x10FFFF):
            report.append([
                style,
                info.filename,
                "",
                "Unicode 超出範圍"
            ])
            continue

        if 0xD800 <= cp <= 0xDFFF:
            report.append([
                style,
                info.filename,
                "",
                "Unicode 為非法代理碼"
            ])
            continue

        result[style].append({
            "info": info,
            "filename": filename,
            "codepoint": cp
        })

    return result, report



def remove_conflicts(items, style, report):

    groups = defaultdict(list)

    for item in items:
        groups[item["codepoint"]].append(item)

    valid = []

    for cp, group in groups.items():

        if len(group) > 1:

            for item in group:
                report.append([
                    style,
                    item["info"].filename,
                    "U+{:04X}".format(cp),
                    "Unicode 重複，需人工確認"
                ])

            continue

        valid.append(group[0])

    return valid



def build_font(zip_file, style, items):

    
    config = STYLES[style]

    total = len(items)

    print("\n" + "=" * 60)
    print("開始製作：", style)
    print("總 SVG 數量：", total)
    print("=" * 60)

    font = fontforge.font()

    font.encoding = "UnicodeFull"
    font.familyname = config["family"]
    font.fontname = config["fontname"]
    font.fullname = config["family"]

    font.version = "1.0"
    font.weight = "Regular"

    font.ascent = ASCENT
    font.descent = DESCENT

    imported = 0
    failed = 0
    start_time = time.time()

    with tempfile.TemporaryDirectory() as temp_dir:

        for index, item in enumerate(items, start=1):

            info = item["info"]
            cp = item["codepoint"]

            temp_svg = os.path.join(
                temp_dir,
                "glyph_{:06d}.svg".format(index)
            )

            try:

                with zip_file.open(info) as source:
                    data = source.read()

                with open(temp_svg, "wb") as target:
                    target.write(data)

                name = glyph_name(cp)

                glyph = font.createChar(cp, name)

                glyph.importOutlines(temp_svg)

                glyph.autoWidth()

                imported += 1

                print(
                    "[成功] {} -> U+{:04X}".format(
                        info.filename, cp
                    ),
                    flush=True
                )

            except Exception as e:

                failed += 1
                print(
                    "[失敗] {} : {}".format(
                        info.filename, e
                    ),
                    flush=True
                )

            percent = index / total * 100

            elapsed = time.time() - start_time

            speed = index / elapsed if elapsed > 0 else 0

            remaining = (
                (total - index) / speed
                if speed > 0 else 0
            )

            bar_length = 30

            filled = int(
                bar_length * index / total
            )

            bar = (
                "#" * filled
                + "-" * (bar_length - filled)
            )

            filename = os.path.basename(info.filename)

            print(
                "\r[{}] {:6.2f}% | {}/{} | 成功:{} | 失敗:{} | 剩餘:{} | {}".format(
                    bar,
                    percent,
                    index,
                    total,
                    imported,
                    failed,
                    int(remaining),
                    filename[:25]
                ),
                end="",
                flush=True
            )

    print("\n\nSVG 匯入完成！")

    print("成功：", imported)
    print("失敗：", failed)

    # 輪廓整理
    print("\n[1/3] 正在整理字型輪廓...", flush=True)

    try:
        font.selection.all()
        font.removeOverlap()
        font.correctDirection()

    except Exception as e:
        print("\n輪廓整理警告：", e)

    # 輸出字型
    print("[2/3] 正在產生 TTF...", flush=True)

    output_path = os.path.join(
        OUTPUT_DIR,
        config["output"]
    )

    font.generate(output_path)

    print("[3/3] 正在關閉 FontForge...", flush=True)

    font.close()

    print("\n" + "=" * 60)
    print("字型輸出完成：", output_path)
    print("成功：", imported)
    print("失敗：", failed)
    print("耗時：{:.1f} 秒".format(
        time.time() - start_time
    ))
    print("=" * 60)

    return imported, failed



def save_report(report):

    path = os.path.join(
        OUTPUT_DIR,
        "import_report.csv"
    )

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "Style",
            "Original Filename",
            "Unicode",
            "Status"
        ])

        writer.writerows(report)

    print("\n檢查報告：", path)



def main():

    print("=" * 50)
    print("FontForge SVG Batch Import")
    print("=" * 50)

    overrides = load_overrides()

    with zipfile.ZipFile(ZIP_PATH, "r") as z:

        scanned, report = scan_svg(z, overrides)

        for style in STYLES:

            items = remove_conflicts(
                scanned[style],
                style,
                report
            )

            if not items:
                print(
                    "\n[警告] {} 沒有可匯入的 SVG".format(style)
                )
                continue

            build_font(
                z,
                style,
                items
            )

    save_report(report)

    print("\n所有字型製作流程結束！")


if __name__ == "__main__":
    main()
