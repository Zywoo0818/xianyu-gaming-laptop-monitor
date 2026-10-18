"""行情表：读取你自己的《笔电行情.xlsx》，作为每条商品的参考价。

表结构（每个 sheet 按显卡分）：名称 / CPU / GPU / 内存 / 硬盘 / 电池 / 价格 / 其他
价格是你实际收机的价格，所以“比它低 100 以上”才是值得看的货。
"""
from __future__ import annotations

import json
import re
import statistics
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

HEADER_ALIASES = {
    "名称": "name",
    "型号": "name",
    "cpu": "cpu",
    "gpu": "gpu",
    "显卡": "gpu",
    "内存": "ram",
    "硬盘": "disk",
    "电池": "battery",
    "价格": "price",
    "其他": "note",
}

SKIP_SHEETS = {"内存", "硬盘", "Sheet1", "说明"}

# 行情表里没有的显卡，用相邻档位估算（倍数）
PROXY_GPU = {
    "3050ti": ("3050", 1.05),
    "1650": ("1060", 1.0),
    "1070": ("1060", 1.25),
    "1080": ("1060", 1.45),
    "2070": ("2060", 1.2),
    "2080": ("2060", 1.4),
    "3070": ("3060", 1.25),
    "3080": ("3060", 1.5),
    "4070": ("4060", 1.3),
    "4080": ("4060", 1.6),
    "4090": ("4060", 2.0),
    "5050": ("4060", 1.1),
    "5060": ("4060", 1.2),
    "5070": ("4060", 1.5),
}


@dataclass
class PriceRow:
    name: str
    gpu: str
    price: float
    cpu: str = ""
    ram: str = ""
    disk: str = ""
    battery: str = ""
    note: str = ""
    sheet: str = ""
    norm: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "gpu": self.gpu,
            "price": self.price,
            "cpu": self.cpu,
            "ram": self.ram,
            "disk": self.disk,
            "battery": self.battery,
            "note": self.note,
            "sheet": self.sheet,
        }


def _text(node) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def _column_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - 64)
    return max(0, index - 1)


def read_xlsx(path: Path) -> dict[str, list[list[str]]]:
    """用标准库读 xlsx，不依赖 openpyxl。"""
    result: dict[str, list[list[str]]] = {}
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append(_text(si))

        rels: dict[str, str] = {}
        rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        for rel in rel_root:
            rels[rel.get("Id", "")] = rel.get("Target", "")

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        for sheet in workbook.find("m:sheets", NS) or []:
            name = sheet.get("name", "")
            rel_id = sheet.get(f"{R_NS}id", "")
            target = rels.get(rel_id, "")
            if not target:
                continue
            target = target.lstrip("/")
            path_in_zip = target if target.startswith("xl/") else f"xl/{target}"
            if path_in_zip not in archive.namelist():
                continue
            rows: list[list[str]] = []
            sheet_root = ET.fromstring(archive.read(path_in_zip))
            for row in sheet_root.iter(f"{{{NS['m']}}}row"):
                values: dict[int, str] = {}
                for cell in row.findall("m:c", NS):
                    index = _column_index(cell.get("r", ""))
                    kind = cell.get("t", "")
                    if kind == "inlineStr":
                        value = _text(cell.find("m:is", NS))
                    else:
                        raw = cell.find("m:v", NS)
                        value = _text(raw)
                        if kind == "s" and value.isdigit():
                            position = int(value)
                            value = shared[position] if position < len(shared) else ""
                    values[index] = value
                if values:
                    width = max(values) + 1
                    rows.append([values.get(i, "") for i in range(width)])
            result[name] = rows
    return result


def _norm_gpu(raw: str) -> str:
    text = str(raw or "").strip().lower()
    match = re.search(r"(10[5-8]0|16[5-6]0|20[5-8]0|30[5-8]0|40[5-9]0|50[5-9]0)", text)
    if not match:
        return text
    suffix = "ti" if "ti" in text else ""
    return match.group(1) + suffix


def normalize_name(name: str) -> str:
    cleaned = str(name or "").lower()
    for token in (" ", "\t", "-", "_", "(", ")", "（", "）", "/", "\\", "+", "＋", "·", ".", "，", ","):
        cleaned = cleaned.replace(token, "")
    return cleaned


BRAND_FOLDS = (
    ("机械革命", "机革"),
    ("神舟战神", "战神"),
    ("玩家国度", "rog"),
    ("神舟", ""),
    ("华硕", ""),
    ("联想", ""),
    ("戴尔", ""),
    ("微星", ""),
    ("七彩虹", ""),
    ("宏碁", ""),
    ("惠普", ""),
)


def fold_name(name: str) -> str:
    """去掉品牌前缀，方便「机械革命蛟龙」对上「机革蛟龙」。"""
    text = normalize_name(name)
    for source, target in BRAND_FOLDS:
        text = text.replace(source, target)
    return text


def parse_price(raw: str) -> float | None:
    text = str(raw or "").replace(",", "").replace("¥", "").strip()
    if not text:
        return None
    if "万" in text:
        match = re.search(r"\d+(?:\.\d+)?", text)
        return float(match.group(0)) * 10_000 if match else None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    value = float(match.group(0))
    return value if value > 100 else None


def build_table(xlsx_path: Path) -> list[PriceRow]:
    sheets = read_xlsx(xlsx_path)
    rows: list[PriceRow] = []
    for sheet_name, matrix in sheets.items():
        if sheet_name in SKIP_SHEETS or not matrix:
            continue
        header_index = None
        mapping: dict[str, int] = {}
        for index, row in enumerate(matrix[:5]):
            lowered = [str(cell).strip().lower() for cell in row]
            if "名称" in lowered or "型号" in lowered:
                header_index = index
                for position, cell in enumerate(lowered):
                    key = HEADER_ALIASES.get(cell)
                    if key and key not in mapping:
                        mapping[key] = position
                break
        if header_index is None or "name" not in mapping or "price" not in mapping:
            continue
        for row in matrix[header_index + 1 :]:
            def cell(key: str) -> str:
                position = mapping.get(key)
                if position is None or position >= len(row):
                    return ""
                return str(row[position]).strip()

            name = cell("name")
            price = parse_price(cell("price"))
            if not name or price is None:
                continue
            gpu = _norm_gpu(cell("gpu")) if "gpu" in mapping else _norm_gpu(sheet_name)
            rows.append(
                PriceRow(
                    name=name,
                    gpu=gpu,
                    price=price,
                    cpu=cell("cpu"),
                    ram=cell("ram"),
                    disk=cell("disk"),
                    battery=cell("battery"),
                    note=cell("note"),
                    sheet=sheet_name,
                    norm=normalize_name(name),
                )
            )
    return rows


def save_table(rows: list[PriceRow], target: Path, source: str = "") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source,
        "count": len(rows),
        "rows": [row.to_dict() for row in rows],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def load_table(path: Path) -> list[PriceRow]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[PriceRow] = []
    for item in data.get("rows", []):
        rows.append(
            PriceRow(
                name=str(item.get("name") or ""),
                gpu=str(item.get("gpu") or ""),
                price=float(item.get("price") or 0),
                cpu=str(item.get("cpu") or ""),
                ram=str(item.get("ram") or ""),
                disk=str(item.get("disk") or ""),
                battery=str(item.get("battery") or ""),
                note=str(item.get("note") or ""),
                sheet=str(item.get("sheet") or ""),
                norm=normalize_name(item.get("name") or ""),
            )
        )
    return rows


def _gpu_key(gpu: str) -> str:
    return _norm_gpu(gpu)


def match_reference(
    title: str,
    gpu: str,
    rows: list[PriceRow],
    *,
    min_name_length: int = 3,
) -> tuple[float | None, str, str]:
    """返回 (参考价, 命中的行情表名称, 来源说明)。

    优先按“型号名包含”，其次按同显卡中位数。
    """
    if not rows:
        return None, "", "no-table"

    title_norm = normalize_name(title)
    title_fold = fold_name(title)
    key = _gpu_key(gpu)

    same_gpu = [row for row in rows if _gpu_key(row.gpu) == key] if key else []
    pool = same_gpu or rows

    best: PriceRow | None = None
    for row in pool:
        if not row.norm or len(row.norm) < min_name_length:
            continue
        if row.norm in title_norm:
            if best is None or len(row.norm) > len(best.norm):
                best = row
    if best is not None:
        return best.price, best.name, "table/model"

    # 品牌写法不同（机械革命 / 机革），去掉品牌再比一次
    for row in pool:
        folded = fold_name(row.name)
        if len(folded) >= min_name_length and folded in title_fold:
            if best is None or len(folded) > len(fold_name(best.name)):
                best = row
    if best is not None:
        return best.price, best.name, "table/alias"

    if same_gpu:
        prices = sorted(row.price for row in same_gpu if row.price > 0)
        if prices:
            return float(statistics.median(prices)), f"{gpu} 同档中位数", "table/gpu"

    proxy = PROXY_GPU.get(key)
    if proxy:
        proxy_gpu, factor = proxy
        proxy_rows = [row for row in rows if _gpu_key(row.gpu) == proxy_gpu]
        prices = sorted(row.price for row in proxy_rows if row.price > 0)
        if prices:
            middle = statistics.median(prices) * factor
            return float(middle), f"{gpu}（按 {proxy_gpu} 行情 ×{factor}）", "table/proxy"
    return None, "", "no-match"
