from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


# Centralized regex and heuristic configuration.
PROJECT_FILENAME_PREFIX_PATTERNS = [
    r"^\(?发售稿\)?",
    r"^\(?\d+\.\d+PM\+?",
]

PROJECT_NAME_PATTERNS = [
    r"(?:项目名称|采购项目名称|招标项目名称)\s*[:：]?\s*([^\n\r|]{4,120})",
    r"(?:工程名称)\s*[:：]?\s*([^\n\r|]{4,120})",
]
PROJECT_NAME_REJECT_PREFIXES = ("项目类型", "采购标的名称")

CUSTOMER_PATTERNS = [
    r"(?m)^[ \t]*采购人(?:\s*[（(][^）)\r\n]{1,20}[）)])?\s*[:：]\s*([^\n\r|]{3,80})",
    r"(?:采购人|招标人|建设单位|采购单位)(?:\s*[（(][^）)\r\n]{1,20}[）)])?\s*[:：]?\s*([^\n\r|]{3,80})",
    r"(?:甲方)(?:\s*[（(][^）)\r\n]{1,20}[）)])?\s*[:：]?\s*([^\n\r|]{3,80})",
]
CUSTOMER_SPLIT_PATTERN = r"\s{2,}|地址|联系方式|联系人|联系电话"
CUSTOMER_NAME_PREFIX_PATTERN = r"^(单位名称|名称)\s*[:：]\s*"
CUSTOMER_STEM_YEAR_PATTERN = r"^\d{4}年?"
CUSTOMER_STEM_ORG_PATTERN = (
    r"([\u4e00-\u9fff]{2,30}"
    r"(?:局|厅|院|中心|学校|公司|实验室|委员会|大学|学院|报社|支队|总队|分局|政府办公室|医院|法院))"
)
JUNK_ORG_TERMS = [
    "质疑",
    "供应商",
    "投标",
    "响应",
    "招标代理",
    "采购代理",
    "保证金",
    "合同签订",
    "要求",
    "对此不承担",
    "规定",
    "答复",
]
JUNK_ORG_PREFIXES = ("为 ", "和", "及", "的", "要求", "对")
JUNK_ORG_MAX_LEN = 42

MONEY_PATTERN = r"((?:人民币)?\s*\d[\d,，]*(?:\.\d+)?\s*(?:万元|万|元|亿元|亿))"
GENERIC_MONEY_PATTERN = (
    r"(预算(?:金额)?|最高限价|采购预算|中标(?:金额|价)|成交(?:金额|价))"
    r"[^\n\r]{0,80}?(\d[\d,，]*(?:\.\d+)?\s*(?:万元|万|元|亿元|亿))"
)
BUDGET_LABELS = ["预算金额", "采购预算", "项目预算", "预算", "最高限价", "控制价"]
AWARD_AMOUNT_LABELS = ["中标金额", "成交金额", "中标价", "成交价", "投标报价"]
CONTRACT_AMOUNT_LABELS = ["合同金额", "合同总价", "合同价款"]
CONTRACT_AMOUNT_PATTERN = r"合同金额为人民币\s*([\d,，]+(?:\.\d+)?\s*元)"

DATE_PATTERNS = [
    r"(?:开标时间|响应文件提交截止时间|投标截止时间|提交投标文件截止时间)[^\d]{0,20}"
    r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日(?:\s*\d{1,2}[:：]\d{2})?)",
    r"(?:开标时间|投标截止时间)[^\d]{0,20}"
    r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}[:：]\d{2})?)",
]

WINNER_PATTERNS = [
    r"确定\s*([\u4e00-\u9fffA-Za-z0-9（）()·\-—、&＆,.，\s]{4,80}?有限公司)"
    r"\s*为[^\n\r]{0,80}?(?:中\s*标|成交|供应商)",
    r"(?m)^[ \t]*供应商(?:\s*[（(][^）)\r\n]{1,20}[）)])?\s*[:：]\s*([^\n\r|]{4,120})",
    r"(?:中标供应商|中标人|成交供应商|成交人|供应商名称)"
    r"(?:\s*[（(][^）)\r\n]{1,20}[）)])?\s*[:：]?\s*([^\n\r|]{4,120})",
    r"(?:中标候选人)\s*[:：]?\s*([^\n\r|]{4,120})",
]
WINNER_SPLIT_PATTERN = r"地址|金额|报价|得分|统一社会信用"

CONTENT_PATTERNS = [
    r"(?:采购需求|服务内容|建设内容|项目内容|采购内容)\s*[:：]?\s*([\s\S]{20,420})",
    r"(?:简要规格描述|项目概况)\s*[:：]?\s*([\s\S]{20,360})",
]
CONTENT_LABELS = ("采购需求", "服务内容", "建设内容", "项目内容", "采购内容")
CONTENT_STOP_LABELS = ("服务期限", "合同价格", "项目经理", "资金来源")
MAX_CONTENT_CHARS = 800

DOC_TYPE_KEYWORDS = {
    "合同": ["合同"],
    "结果公告": ["结果", "成交", "中标", "公示"],
    "采购需求": ["需求"],
    "报价/明细": ["报价", "明细", "分项表"],
    "采购公告/文件": ["招标", "采购文件", "磋商", "单一来源"],
}
WINNER_ALLOWED_DOC_TYPES = {"结果公告", "合同"}
NOTE_DOC_TYPES_WITH_KIND = {"合同", "采购需求", "报价/明细", "资料"}


@dataclass
class ExtractedRecord:
    source_file: str
    source_document_id: str
    doc_type: str
    customer: str
    project_name: str
    content: str
    budget: str
    bid_open_time: str
    winner: str
    award_amount: str
    note: str
    text_chars: int
    error: str


def normalize_project_from_filename(name: str) -> str:
    stem = Path(name).stem
    for pattern in PROJECT_FILENAME_PREFIX_PATTERNS:
        stem = re.sub(pattern, "", stem)
    stem = stem.replace("+", " ")
    stem = re.sub(r"\s+", " ", stem)
    return stem.strip(" _-")


def first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            value = next((g for g in m.groups() if g), "")
            value = re.sub(r"\s+", " ", value).strip(" ：:;；，,。")
            if value:
                return value[:180]
    return ""


def find_money(text: str, labels: list[str], allow_generic: bool = True) -> str:
    for label in labels:
        window_pat = rf"{label}[^\n\r]{{0,120}}"
        for wm in re.finditer(window_pat, text):
            m = re.search(MONEY_PATTERN, wm.group(0))
            if m:
                return re.sub(r"\s+", "", m.group(1))
    if not allow_generic:
        return ""
    m = re.search(GENERIC_MONEY_PATTERN, text)
    return re.sub(r"\s+", "", m.group(2)) if m else ""


def find_contract_amount(text: str) -> str:
    m = re.search(CONTRACT_AMOUNT_PATTERN, text)
    if m:
        return re.sub(r"\s+", "", m.group(1))
    return find_money(text, CONTRACT_AMOUNT_LABELS, allow_generic=False)


def find_date(text: str) -> str:
    return first_match(text, DATE_PATTERNS)


def find_project_name(text: str, filename: str) -> str:
    value = first_match(text, PROJECT_NAME_PATTERNS)
    if value and not value.startswith(PROJECT_NAME_REJECT_PREFIXES):
        return value
    return normalize_project_from_filename(filename)


def find_customer(text: str, filename: str) -> str:
    value = first_match(text, CUSTOMER_PATTERNS)
    if value:
        value = re.split(CUSTOMER_SPLIT_PATTERN, value)[0].strip()
        value = re.sub(CUSTOMER_NAME_PREFIX_PATTERN, "", value).strip()
        if not looks_junk_org(value):
            return value[:80]
    stem = normalize_project_from_filename(filename)
    stem = re.sub(CUSTOMER_STEM_YEAR_PATTERN, "", stem)
    m = re.search(CUSTOMER_STEM_ORG_PATTERN, stem)
    return m.group(1) if m else ""


def looks_junk_org(value: str) -> bool:
    if len(value) > JUNK_ORG_MAX_LEN:
        return True
    if value.startswith(JUNK_ORG_PREFIXES):
        return True
    return any(term in value for term in JUNK_ORG_TERMS)


def find_winner(text: str) -> str:
    value = first_match(text, WINNER_PATTERNS)
    if value:
        return re.split(WINNER_SPLIT_PATTERN, value)[0].strip()
    if "废标" in text:
        return "废标"
    return ""


def find_content(text: str) -> str:
    candidates: list[tuple[int, int, str]] = []
    labels = "|".join(map(re.escape, CONTENT_LABELS))

    numbered_pattern = re.compile(
        rf"(?m)^[ \t]*(\d+(?:\.\d+)+)[ \t]+(?:{labels})[ \t]*[:：][ \t]*"
    )
    for match in numbered_pattern.finditer(text):
        boundary = re.search(r"(?m)^[ \t]*\d+(?:\.\d+)+[ \t]+", text[match.end():])
        end = match.end() + boundary.start() if boundary else len(text)
        value = clean_content_value(text[match.end():end])
        if value:
            candidates.append((300, match.start(), value))

    explicit_pattern = re.compile(rf"(?m)^[ \t]*(?:{labels})[ \t]*[:：][ \t]*")
    stop_labels = "|".join(map(re.escape, CONTENT_STOP_LABELS))
    for match in explicit_pattern.finditer(text):
        boundary = re.search(
            rf"(?m)^[ \t]*(?:\d+(?:\.\d+)+[ \t]+|(?:{stop_labels})[ \t]*[:：])",
            text[match.end():],
        )
        end = match.end() + boundary.start() if boundary else min(len(text), match.end() + MAX_CONTENT_CHARS)
        value = clean_content_value(text[match.end():end])
        if value:
            candidates.append((200, match.start(), value))

    for pattern in CONTENT_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            value = clean_content_value(next((group for group in match.groups() if group), ""))
            if value:
                candidates.append((100, match.start(), value))

    if not candidates:
        return ""
    _score, _position, value = max(candidates, key=lambda item: (item[0], len(item[2]), -item[1]))
    return value[:MAX_CONTENT_CHARS]


def clean_content_value(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]*\n[ \t]*(?=[\u4e00-\u9fff])", "", value)
    value = re.sub(r"(?<=[，。；：、])[ \t]*\n[ \t]*", "", value)
    value = re.sub(r"[ \t]*\n[ \t]*", "；", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"；{2,}", "；", value)
    return value.strip(" ：:;；，,。")


def is_contract_document(text: str) -> bool:
    has_contract_language = any(token in text for token in ("合同编号", "合同总金额", "本合同", "政府采购合同"))
    role_pairs = (("甲方", "乙方"), ("采购人", "供应商"))
    has_parties = any(all(has_role_label(text, label) for label in pair) for pair in role_pairs)
    return has_contract_language and has_parties


def has_role_label(text: str, label: str) -> bool:
    pattern = rf"(?m)^[ \t]*{re.escape(label)}(?:[ \t]*[（(][^）)\r\n]{{1,20}}[）)])?[ \t]*[:：]"
    return bool(re.search(pattern, text))


def classify_doc(name: str, text: str = "") -> str:
    if is_contract_document(text):
        return "合同"
    for doc_type, keywords in DOC_TYPE_KEYWORDS.items():
        if any(keyword in name for keyword in keywords):
            return doc_type
    return "资料"


def note_for_record(kind: str, source_name: str, error: str, file_type: str) -> str:
    if kind in NOTE_DOC_TYPES_WITH_KIND:
        note = f"{kind}；来源：{source_name}"
    else:
        note = f"来源：{source_name}"
    if error:
        note += f"；解析提示：{error[:80]}"
    if file_type == ".zip":
        note += "；压缩包按文件名/目录线索整理"
    return note


def extract_document(document: dict[str, Any]) -> ExtractedRecord:
    text = document.get("text") or ""
    source_name = document.get("source_name") or ""
    file_type = document.get("file_type") or ""
    error = document.get("parse_error") or ""
    kind = classify_doc(source_name, text)

    winner = find_winner(text)
    award_amount = find_money(text, AWARD_AMOUNT_LABELS, allow_generic=False)
    if kind == "合同" and not award_amount:
        award_amount = find_contract_amount(text)
    if kind not in WINNER_ALLOWED_DOC_TYPES:
        winner = ""
        award_amount = ""

    return ExtractedRecord(
        source_file=source_name,
        source_document_id=document.get("document_id") or "",
        doc_type=kind,
        customer=find_customer(text, source_name),
        project_name=find_project_name(text, source_name),
        content=find_content(text),
        budget=find_money(text, BUDGET_LABELS),
        bid_open_time=find_date(text),
        winner=winner,
        award_amount=award_amount,
        note=note_for_record(kind, source_name, error, file_type),
        text_chars=len(text),
        error=error,
    )


def extract_records(documents: list[dict[str, Any]]) -> list[ExtractedRecord]:
    return [extract_document(document) for document in documents]


def load_parsed_documents(input_path: Path) -> list[dict[str, Any]]:
    return json.loads(input_path.read_text(encoding="utf-8"))


def write_json(records: list[ExtractedRecord], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records]
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract tender fields from ParsedDocument JSON.")
    parser.add_argument(
        "--input",
        default="data/cache/parsed_documents.json",
        help="Input ParsedDocument JSON path.",
    )
    parser.add_argument(
        "--output",
        default="data/cache/extracted_records.json",
        help="Output ExtractedRecord JSON path.",
    )
    args = parser.parse_args()

    documents = load_parsed_documents(Path(args.input))
    records = extract_records(documents)
    write_json(records, Path(args.output))

    unresolved = sum(1 for record in records if not record.customer or not record.project_name)
    print(f"wrote {args.output} records={len(records)} unresolved_key_fields={unresolved}")


if __name__ == "__main__":
    main()
