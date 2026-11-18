import os
import sys
import time
import csv
import argparse
import zipfile
import tempfile
import unicodedata

from pathlib import PurePosixPath
from collections import defaultdict

import fontforge




EM_SIZE = 1000
ASCENT = 800
DESCENT = 200

os.makedirs(OUTPUT_DIR, exist_ok=True)

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

        if not info.filename.startswith(folder + "/"):
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
                glyph.width = get_glyph_width(cp)
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

    os.makedirs(args.output_dir, exist_ok=True)

    output_path = os.path.join(
        args.output_dir,
        args.output
    )

    font.generate(output_path)

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("TTF 產生失敗或檔案為空")

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

        imported, failed = build_font(
            z,
            folder,
            items,
            args
        )

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
