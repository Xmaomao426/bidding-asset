from __future__ import annotations

import sys


def _refuse_direct_module() -> None:
    print(
        "请使用 start-local.cmd 启动本地 Web 应用；"
        "src.operator_ui.app 不再提供服务启动参数。",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    _refuse_direct_module()

import getpass
import json
import os
import re
import secrets
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import (
    Flask, abort, current_app, flash, jsonify, redirect, render_template_string,
    request, url_for,
)

from src.intake.asset_intake import (
    DEFAULT_ASSET_INTAKE_OUTPUT,
    DEFAULT_INTAKE_AUDIT_OUTPUT,
    build_asset_intake,
    latest_decision_by_asset,
    write_intake_outputs,
)
from src.operator.operator_workflow import (
    DEFAULT_OPERATOR_WORKFLOW_RESULT,
    apply_reviewed,
    link_documents,
    show_project,
)
from src.operator.acquisition_workflow import (
    AcquisitionWorkflowPaths,
    SUPPORTED_UPLOAD_SUFFIXES,
)
from src.acquisition.inbox.acquisition_inbox import (
    AcquisitionInboxPaths,
    COMPLETED,
    FAILED,
    PROCESSING,
    RECEIVED,
    apply_manual_remediation,
    capture_headless_browser_dom,
    create_file_item,
    create_url_items,
    fail_item,
    is_operator_visible,
    is_manual_remediation_target,
    load_inbox_items,
    normalize_url,
    parse_item_attachments,
    process_item,
    retry_item_downstream_refresh,
    update_business_sync_result,
    update_confirmation_result,
    write_inbox,
)
from src.operator.inbox_excel_sync import (
    CONFLICT as EXCEL_CONFLICT,
    FAILED as EXCEL_FAILED,
    NOT_APPLICABLE as EXCEL_NOT_APPLICABLE,
    NOT_WRITTEN as EXCEL_NOT_WRITTEN,
    WRITTEN as EXCEL_WRITTEN,
    InboxExcelSyncPaths,
    normalize_key as normalize_business_key,
    sync_confirmed_project,
    sync_confirmed_projects_batch,
)
from src.project_number import validated_project_number
from src.acquisition.xlsx_adapter import FORMAL_WORKBOOK_NAME
from src.award_detail import award_detail_id, business_group_id, business_sequence
from src.project_relation.project_document_relation import (
    DEFAULT_PROJECT_DOCUMENT_LINKS,
    create_link,
    find_existing_link,
    load_json_array as load_relation_json_array,
    write_links,
)
from src.query.repository_query import RepositoryQueryService
from src.repository.asset_repository import (
    CONFIRMED,
    DEFAULT_DOCUMENTS_REPOSITORY,
    DEFAULT_PROJECTS_REPOSITORY,
    DEFAULT_REPOSITORY_AUDIT,
    build_asset_repository,
    merge_repository_rows,
    merge_project_award_detail,
    project_business_group_id,
    update_entity_status,
    write_repository_outputs,
    write_json as write_repository_json,
)
from src.review.review_decision import (
    DEFAULT_REVIEW_DECISIONS_OUTPUT,
    DEFAULT_REVIEW_QUEUE_INPUT,
    append_review_decision,
    create_review_decision,
    load_review_decisions,
    load_review_queue,
)
MAX_BATCH_UPLOAD_FILES = 20
MAX_BATCH_URLS = 20
FILE_BATCH_CONCURRENCY_ENV = "BIDDING_ASSET_FILE_BATCH_CONCURRENCY"
DEFAULT_FILE_BATCH_CONCURRENCY = 3

_PERSISTED_CONFIRMATION_MAINLINES = frozenset({
    "notice_content_dom_qwen/v1",
    "unstructured_document_qwen/v1",
})
_MANUAL_REVIEW_FIELD_LABELS = {
    "project_name": "项目名称",
    "customer": "采购人",
    "bid_open_time": "开标时间",
    "content": "项目内容",
}
_MANUAL_REVIEW_ISSUE_LABELS = {
    "field_missing": "字段缺失",
    "field_evidence_missing": "缺少字段证据",
    "field_evidence_invalid_section_index": "证据位置无效",
    "field_evidence_section_not_found": "证据位置不存在",
    "field_evidence_quote_missing": "缺少证据摘录",
    "field_evidence_quote_not_in_section": "证据摘录与章节不符",
    "field_evidence_value_not_in_quote": "字段值不在证据摘录中",
    "field_evidence_role_label_missing": "角色标签不明确",
    "project_name_truncated": "项目名称疑似截断",
    "customer_agency_role_mismatch": "误绑定代理机构",
    "customer_role_conflict": "采购人角色冲突",
    "customer_role_unverified": "采购人角色未确认",
    "bid_opening_ambiguous": "存在多个开标时间",
    "bid_opening_deadline_only": "只有截止时间证据",
    "bid_opening_reference_unresolved": "开标时间引用未解析",
    "bid_opening_label_missing": "缺少开标事件标签",
    "content_scope_insufficient": "项目内容范围证据不足",
}
_BASE_CONFIRMATION_FIELDS = frozenset({"project_name", "customer"})
_OPTIONAL_CONFIRMATION_FIELDS = frozenset({
    "project_number", "content", "budget", "bid_open_time", "winner", "award_amount",
})
_AMOUNT_NUMBER_PATTERN = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_WAN_AMOUNT_PATTERN = re.compile(
    rf"(?<![\d.,])(?:[¥￥]\s*)?([+-]?{_AMOUNT_NUMBER_PATTERN})"
    r"\s*(?:[（(]\s*)?万元\s*(?:[）)])?"
)
_YUAN_AMOUNT_PATTERN = re.compile(
    r"^\s*(?:人民币\s*)?(?:[¥￥]\s*)?"
    rf"([+-]?{_AMOUNT_NUMBER_PATTERN})\s*元(?:整)?\s*$"
)


def display_amount(value: Any, *, include_unit: bool = True) -> str:
    """Normalize recognizable amounts for display without changing their stored value."""
    original = str(value or "").strip()
    if not original:
        return ""
    wan_matches = list(_WAN_AMOUNT_PATTERN.finditer(original))
    if wan_matches:
        numbers = [_decimal_text(match.group(1)) for match in wan_matches]
        if all(number is not None for number in numbers):
            suffix = " 万元" if include_unit else ""
            return "；".join(f"{number}{suffix}" for number in numbers)
    yuan_match = _YUAN_AMOUNT_PATTERN.fullmatch(original)
    if yuan_match:
        number = _decimal_text(yuan_match.group(1), divisor=Decimal("10000"))
        if number is not None:
            return f"{number} 万元" if include_unit else number
    return original


def _decimal_text(value: str, *, divisor: Decimal = Decimal("1")) -> str | None:
    try:
        number = Decimal(value.replace(",", "")) / divisor
    except (InvalidOperation, ValueError):
        return None
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


@dataclass(frozen=True)
class OperatorUiPaths:
    """File locations passed to existing workflow functions; tests inject isolated paths."""

    review_queue: Path = DEFAULT_REVIEW_QUEUE_INPUT
    review_decisions: Path = DEFAULT_REVIEW_DECISIONS_OUTPUT
    intake_output: Path = DEFAULT_ASSET_INTAKE_OUTPUT
    intake_audit: Path = DEFAULT_INTAKE_AUDIT_OUTPUT
    documents: Path = DEFAULT_DOCUMENTS_REPOSITORY
    projects: Path = DEFAULT_PROJECTS_REPOSITORY
    repository_audit: Path = DEFAULT_REPOSITORY_AUDIT
    links: Path = DEFAULT_PROJECT_DOCUMENT_LINKS
    workflow_result: Path = DEFAULT_OPERATOR_WORKFLOW_RESULT
    asset_candidates: Path = Path("data/diagnostics/asset_candidates.json")
    deduped_candidates: Path = Path("data/diagnostics/asset_candidates_deduped.json")
    dedup_summary: Path = Path("data/diagnostics/candidate_dedup_summary.json")
    lifecycle: Path = Path("data/diagnostics/asset_lifecycle.json")
    upload_dir: Path = Path("data/web_capture/operator_uploads")
    acquisition_inbox: Path = Path("data/diagnostics/acquisition_inbox.json")
    acquisition_inbox_summary: Path = Path("data/diagnostics/acquisition_inbox_summary.json")
    manual_remediation_backup_root: Path = Path("data/backups/manual_remediation")
    excel: Path = Path("招投标.xlsx")
    excel_sync_records: Path = Path("data/cache/inbox_excel_sync_records")
    excel_sync_summary: Path = Path("data/diagnostics/inbox_excel_sync_summary")
    excel_backup_dir: Path = Path("data/backups")
    excel_sync_runtime: Path = Path("data/cache/inbox_excel_sync_runtime")


BASE_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>招投标信息资产管理系统</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: "Microsoft YaHei", sans-serif; max-width: 1800px; margin: 1.25rem auto; padding: 0 1rem; color: #1f2937; }
    nav { display: flex; flex-wrap: wrap; gap: .4rem; margin: 1rem 0 1.5rem; }
    nav a { color: #1f4f82; padding: .45rem .65rem; text-decoration: none; }
    nav a.active { background: #1f4f82; color: white; border-radius: .25rem; }
    table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
    th, td { border: 1px solid #ccc; padding: .45rem; text-align: left; vertical-align: top; }
    label { display: block; margin: .6rem 0; } input, select, textarea { min-width: 20rem; padding: .35rem; }
    button { padding: .4rem .8rem; } .flash { background: #eef7ee; padding: .7rem; }
    .error { background: #fff0f0; padding: .7rem; } pre { white-space: pre-wrap; overflow-wrap: anywhere; }
    .muted { color: #666; } .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .8rem; }
    .stat { border: 1px solid #ccc; border-radius: .25rem; padding: .8rem; } .stat strong { display: block; font-size: 1.6rem; }
    .review-table { table-layout: fixed; } .review-table .asset { width: 15%; } .review-table .title { width: 25%; }
    .review-table .action { width: 27%; } .truncate { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    details { margin-top: .25rem; } .action form label { font-size: .85rem; } .action form input, .action form select { min-width: 0; width: 100%; box-sizing: border-box; }
    section { border: 1px solid #ddd; border-radius: .25rem; margin: 1rem 0; padding: .8rem; }
    .table-scroll { width: 100%; border: 1px solid #d1d5db; border-radius: .35rem; }
    .inbox-table { width: 100%; table-layout: fixed; margin: 0; }
    .inbox-table th, .inbox-table td { overflow-wrap: anywhere; padding: .35rem; }
    .inbox-table .col-select { width: 42px; text-align: center; white-space: nowrap; }
    .inbox-select { min-width: 0; width: 16px; height: 16px; margin: 0; }
    .inbox-table .col-source { width: 125px; }
    .inbox-table .col-type { width: 58px; white-space: nowrap; word-break: keep-all; }
    .inbox-table .col-project { width: 245px; }
    .inbox-table .col-customer, .inbox-table .col-winner { width: 130px; }
    .inbox-table .col-amount { width: 90px; }
    .inbox-table .col-repository { width: 90px; word-break: keep-all; }
    .inbox-table .col-excel { width: 76px; word-break: keep-all; }
    .inbox-table .col-reason { width: 140px; }
    .inbox-table .col-action { width: 96px; }
    .inbox-table .col-action a, .inbox-table .col-action button { display: block; max-width: 100%; margin: 0 0 .3rem; padding: .25rem .35rem; white-space: normal; }
    .two-line { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; word-break: normal; white-space: normal; line-height: 1.35; max-height: 2.7em; }
    .group-hint { display: block; margin-top: .2rem; color: #64748b; font-size: .82rem; font-weight: 400; }
    .batch-toolbar { position: sticky; top: 0; z-index: 2; display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; background: #f8fafc; border: 1px solid #d1d5db; border-radius: .35rem; padding: .65rem; margin: .75rem 0; }
    .batch-toolbar button { white-space: nowrap; }
    .status-ok { color: #166534; font-weight: 600; } .status-error { color: #b91c1c; font-weight: 600; }
    .status-pending { color: #92400e; } .filter-note { font-size: .9rem; color: #64748b; }
    .asset-table { table-layout: fixed; }
    .asset-table th, .asset-table td { padding: .35rem .45rem; overflow-wrap: anywhere; }
    .project-fields th { width: 10rem; white-space: nowrap; }
    .asset-action { width: 5.5rem; white-space: nowrap; }
    .remediation-table { table-layout: fixed; }
    .remediation-table th:nth-child(1) { width: 9%; }
    .remediation-table th:nth-child(2) { width: 43%; }
    .remediation-table th:nth-child(3) { width: 29%; }
    .remediation-table th:nth-child(4) { width: 19%; }
    .remediation-table td:nth-child(2) { white-space: pre-wrap; overflow-wrap: anywhere; }
    .remediation-table td:nth-child(3) input { min-width: 0; width: 100%; }
    .remediation-table td:nth-child(4) { word-break: keep-all; overflow-wrap: normal; }
    .remediation-table td:nth-child(4) button { margin: 0 .25rem .3rem 0; white-space: nowrap; }
    @media (max-width: 1400px) { body { margin-top: .75rem; } }
  </style>
</head>
<body>
  <h1>招投标资产管理</h1>
  <p class="muted">本地业务工作台；资料确认后进入资产库。</p>
  <nav>
    <a class="{% if request.endpoint == 'dashboard_page' %}active{% endif %}" href="{{ url_for('dashboard_page') }}">首页</a>
    <a class="{% if request.endpoint == 'acquisition_page' %}active{% endif %}" href="{{ url_for('acquisition_page') }}">资料采集</a>
    <a class="{% if request.endpoint in ('acquisition_inbox_page', 'acquisition_inbox_detail', 'retry_inbox_item') %}active{% endif %}" href="{{ url_for('acquisition_inbox_page') }}">资料待办</a>
    <a class="{% if request.endpoint == 'project_assets_page' %}active{% endif %}" href="{{ url_for('project_assets_page') }}">资产库</a>
  </nav>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, message in messages %}<p class="{{ category }}">{{ message }}</p>{% endfor %}
  {% endwith %}
  {{ body | safe }}
  <script>
  function formatBrowserLocalTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || '未识别';
    const pad = number => String(number).padStart(2, '0');
    const offsetMinutes = -date.getTimezoneOffset();
    const sign = offsetMinutes >= 0 ? '+' : '-';
    const absolute = Math.abs(offsetMinutes);
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())} ` +
      `UTC${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`;
  }
  document.querySelectorAll('[data-utc-time]').forEach(element => {
    element.textContent = formatBrowserLocalTime(element.dataset.utcTime);
  });
  </script>
  <footer class="muted">版本 {{ app_version }}</footer>
</body>
</html>
"""


REVIEW_TEMPLATE = """
<h2>Review Queue</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<table class="review-table"><thead><tr><th class="asset">asset_id</th><th class="title">title</th><th>source_type</th><th>confidence</th><th>lifecycle_status</th><th>priority</th><th class="action">操作</th></tr></thead>
<tbody>{% for item in items %}<tr>
<td class="asset"><span class="truncate" title="{{ item.asset_id }}">{{ item.asset_id_short }}</span><details><summary>完整值</summary><code>{{ item.asset_id }}</code></details></td>
<td class="title"><span class="truncate" title="{{ item.title }}">{{ item.title_short }}</span><details><summary>完整值</summary>{{ item.title }}</details></td><td>{{ item.source_type }}</td><td>{{ item.confidence }}</td><td>{{ item.lifecycle_status }}</td><td>{{ item.priority }}</td>
<td class="action"><form method="post" action="{{ url_for('submit_review', asset_id=item.asset_id) }}">
  <label>决策 <select name="decision"><option>ACCEPT</option><option>REJECT</option></select></label>
  <label>审核人 <input name="reviewer" value="operator"></label>
  <label>理由 <input name="reason" value="运营人员手动确认"></label>
  <label>关联 project_id（可选，需人工确认）<input name="related_project_id"></label>
  <button type="submit">提交</button>
</form></td></tr>{% endfor %}</tbody></table>
"""


APPLY_TEMPLATE = """
<h2>Apply Reviewed</h2>
<p>当前最新 ACCEPT 资产数量：<strong>{{ accepted_count }}</strong></p>
<form method="post"><label>操作人 <input name="operator" value="operator"></label><button type="submit">执行 Apply</button></form>
{% if result %}<h3>执行结果</h3><pre>{{ result }}</pre>{% endif %}
"""


PROJECT_TEMPLATE = """
<h2>资产库</h2>
<form method="get"><label>输入项目名称、客户、中标厂商或项目编号 <input name="query" value="{{ query }}"></label><button type="submit">查询</button></form>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
{% if query and not project_asset and not search_results %}<p>未找到相关项目</p>{% endif %}
{% if search_results %}<section><h3>项目列表</h3><div class="table-scroll"><table class="asset-table"><thead><tr><th>客户名称</th><th>项目名称</th><th>项目编号</th><th>预算（万元）</th><th>中标厂商</th><th>中标金额（万元）</th><th class="asset-action">操作</th></tr></thead><tbody>{% for row in search_results %}<tr><td>{{ row.customer or '未识别' }}</td><td>{{ row.project_name or '未识别' }}</td><td>{{ row.project_number or '未识别' }}</td><td>{{ row.budget or '未识别' }}</td><td>{{ row.winner_company or '未识别' }}</td><td>{{ row.award_amount or '未识别' }}</td><td class="asset-action"><a href="{{ url_for('project_assets_page', project_id=row.project_id) }}">查看详情</a></td></tr>{% endfor %}</tbody></table></div></section>{% endif %}
{% if project_asset %}
<section><h3>项目基础信息</h3><table class="project-fields"><tbody>{% for label, value in project_fields %}<tr><th>{{ label }}</th><td>{{ value or '未识别' }}</td></tr>{% endfor %}</tbody></table></section>
<section><h3>关联资料</h3>{% if project_documents %}<table class="asset-table"><thead><tr><th>文档名称</th>{% if show_document_type %}<th>文档类型</th>{% endif %}<th>来源</th><th>创建时间</th><th>关系类型</th></tr></thead><tbody>{% for item in project_documents %}<tr><td>{{ item.name }}</td>{% if show_document_type %}<td>{{ item.document_type or '—' }}</td>{% endif %}<td>{% if item.source_is_url %}<a href="{{ item.source }}">{{ item.source }}</a>{% else %}{{ item.source or '未识别' }}{% endif %}</td><td>{{ item.created_time or '未识别' }}</td><td>{{ item.relation_type }}</td></tr>{% endfor %}</tbody></table>{% else %}<p class="muted">暂无关联文档。</p>{% endif %}</section>
{% endif %}
"""


RELATION_TEMPLATE = """
<h2>Document Relation</h2>
<form method="post">
  <label>project_id <select name="project_id" required><option value="">请选择</option>{% for project in projects %}<option value="{{ project.project_id }}">{{ project.project_id }} — {{ project.project_name }}</option>{% endfor %}</select></label>
  {% for relation_type, label in relation_types %}
  <label>{{ label }} <select name="{{ relation_type }}"><option value="">不关联</option>{% for document in documents %}<option value="{{ document.document_id }}">{{ document.document_id }} — {{ document.asset_id }}</option>{% endfor %}</select></label>
  {% endfor %}
  <label>操作人 <input name="operator" value="operator"></label><button type="submit">创建关系</button>
</form>
{% if result %}<h3>执行结果</h3><pre>{{ result }}</pre>{% endif %}
"""


ACQUISITION_TEMPLATE = """
<h2>资料采集</h2>
<p>提交后为每个文件或 URL 创建独立待办任务；这里只采集和处理，不会写入资产库或 Excel。</p>
{% if workbook_summaries %}<section><h3>Excel导入批次摘要</h3>
{% for summary in workbook_summaries %}<details open><summary><strong>{{ summary.file_name }}</strong> · <span data-utc-time="{{ summary.import_time }}">{{ summary.import_time }}</span> · 处理状态：{{ summary.processing_status }}</summary>
{% if summary.error_message %}<p class="status-error"><strong>失败原因：</strong>{{ summary.error_message }}</p>{% endif %}
<div class="stats"><div class="stat">工作表<strong>{{ summary.sheet_count }}</strong></div><div class="stat">扫描数据行<strong>{{ summary.scanned_rows }}</strong></div><div class="stat">成功行<strong>{{ summary.success_count }}</strong></div><div class="stat">失败行<strong>{{ summary.failure_count }}</strong></div><div class="stat">跳过行<strong>{{ summary.skipped_count }}</strong></div><div class="stat">项目数<strong>{{ summary.project_count }}</strong></div><div class="stat">中标明细<strong>{{ summary.award_detail_count }}</strong></div><div class="stat">多明细项目<strong>{{ summary.multi_detail_project_count }}</strong></div><div class="stat">单项目最大明细<strong>{{ summary.max_details_per_project }}</strong></div></div>
</details>{% endfor %}</section>{% endif %}
<form method="post">
  <label>URL（每行一个，一次最多 {{ max_batch_urls }} 条）
    <textarea name="urls" rows="5" placeholder="https://example.com/notice-1&#10;https://example.com/notice-2"></textarea>
  </label>
  <button type="submit" name="action" value="url">提交 URL 采集</button>
</form>
<form method="post" enctype="multipart/form-data">
  <label>文件（PDF、DOC、DOCX、XLSX、ZIP，一次最多 {{ max_batch_files }} 个；当前不支持旧版 XLS）<input id="acquisition-files" name="files" type="file" accept=".pdf,.doc,.docx,.xlsx,.zip" multiple></label>
  <input type="hidden" name="action" value="file">
  <button id="acquisition-upload-submit" type="submit">上传并采集</button>
  <p id="acquisition-upload-status" role="status" hidden>文件已提交；批量文件将在资料待办中后台并发解析，请勿重复点击。</p>
</form>
<script>
const acquisitionForm = document.querySelector('form[enctype="multipart/form-data"]');
acquisitionForm.addEventListener('submit', function (event) {
  const fileInput = document.getElementById('acquisition-files');
  const submitButton = document.getElementById('acquisition-upload-submit');
  const status = document.getElementById('acquisition-upload-status');
  const count = fileInput.files.length;
  if (count === 0) {
    event.preventDefault();
    alert('请至少选择一个文件');
    return;
  }
  if (count > {{ max_batch_files }}) {
    event.preventDefault();
    alert('一次最多选择 {{ max_batch_files }} 个文件');
    return;
  }
  submitButton.disabled = true;
  submitButton.textContent = '处理中…';
  status.hidden = false;
});
</script>
"""


INBOX_TEMPLATE = """
<h2>资料待办</h2>
{% if upload_summary %}<section><strong>本批次任务：</strong>成功创建 {{ upload_summary.created }} 个，创建失败 {{ upload_summary.failed }} 个{% if upload_summary.skipped %}，重复 URL 跳过 {{ upload_summary.skipped }} 个{% endif %}。</section>{% endif %}
{% if batch_queue %}<section id="url-batch-progress" data-batch-id="{{ batch_queue.batch_id }}">
  <h3>本批任务处理进度</h3>
  <p><strong>已创建 <span id="batch-created">{{ batch_queue.created }}</span> / 总数 <span id="batch-total">{{ batch_queue.total }}</span></strong></p>
  <p>当前第 <span id="batch-current">{{ batch_queue.current }}</span> 条 · 完成 <span id="batch-completed">{{ batch_queue.completed }}</span> · 失败 <span id="batch-failed">{{ batch_queue.failed }}</span></p>
  <p>页面正文有效 {{ batch_queue.content_ready }} · 正文无效/不可用 {{ batch_queue.content_invalid }} · 字段完整 {{ batch_queue.fields_complete }} · 附件待解析 {{ batch_queue.attachments_pending }} · 可确认 {{ batch_queue.confirmable }} · AI 调用 {{ batch_queue.ai_invoked }} · AI 跳过 {{ batch_queue.ai_skipped }}</p>
  <p id="batch-progress-message">{% if batch_queue.server_processing and (batch_queue.received or batch_queue.processing) %}后台正在有界并发处理，可刷新查看每项状态。{% elif batch_queue.received %}正在按顺序处理；每次只运行一条。{% elif batch_queue.processing %}已有任务正在处理，可刷新查看进度。{% else %}本批处理已结束。{% endif %}</p>
</section>{% endif %}
<p>
  <a href="{{ url_for('acquisition_inbox_page', filter='all') }}">全部</a> ·
  <a href="{{ url_for('acquisition_inbox_page', filter='completed') }}">技术处理完成</a> ·
  <a href="{{ url_for('acquisition_inbox_page', filter='failed') }}">处理失败</a> ·
  <a href="{{ url_for('acquisition_inbox_page', filter='intaken') }}">已入库</a> ·
  <a href="{{ url_for('acquisition_inbox_page', filter='not_intaken') }}">未入库</a>
</p>
<form id="batch-confirm-form" method="post" action="{{ url_for('confirm_selected_inbox_items') }}">
<input type="hidden" name="filter" value="{{ active_filter }}">
<div class="batch-toolbar">
  <button type="button" id="select-filtered">全选当前筛选结果</button>
  <button type="button" id="select-confirmable">全选当前可确认项</button>
  <button type="button" id="clear-selection">取消全选</button>
  <strong id="selected-count">已选择 0 项</strong>
  <button type="submit" id="batch-confirm-button">确认选中项并导入资产库</button>
  <strong id="batch-processing" hidden>正在处理，请勿重复提交…</strong>
</div>
<p class="filter-note">切换筛选后选择会清空；全选只改变复选框，不会自动确认。</p>
<div class="table-scroll"><table class="inbox-table"><thead><tr><th class="col-select">选择</th><th class="col-customer">客户名称</th><th class="col-project">项目名称</th><th class="col-amount">预算（万元）</th><th class="col-winner">中标厂商</th><th class="col-amount">中标金额（万元）</th><th class="col-repository">资产库状态</th><th class="col-action">操作</th></tr></thead>
<tbody>{% for row in rows %}<tr>
<td class="col-select"><input class="inbox-select" type="checkbox" name="inbox_ids" value="{{ row.inbox_id }}" data-confirmable="{{ 'true' if row.confirmable else 'false' }}" {% if not row.selectable %}disabled title="{{ row.disabled_reason }}"{% endif %}></td>
<td class="col-customer"><span class="two-line">{{ row.customer }}</span></td><td class="col-project"><span class="two-line">{{ row.project_name }}</span></td><td class="col-amount">{{ row.budget or '—' }}</td><td class="col-winner"><span class="two-line">{{ row.winner or '—' }}</span></td><td class="col-amount">{{ row.award_amount or '—' }}</td>
<td class="col-repository {{ row.repository_class }}">{{ row.repository_label }}</td>
<td class="col-action"><a href="{{ url_for('acquisition_inbox_detail', inbox_id=row.inbox_id) }}">查看详情</a>{% if row.status == failed_status %}<button type="submit" formaction="{{ url_for('retry_inbox_item', inbox_id=row.inbox_id) }}" formmethod="post">重新采集</button>{% endif %}</td>
</tr>{% endfor %}</tbody></table></div>
</form>
{% if batch_result %}<section><h3>批量处理结果</h3>
<p>选中 {{ batch_result.selected_count }} 项；资产库成功 {{ batch_result.repository_success_count }} 项；已完成跳过 {{ batch_result.completed_skip_count }} 项；处理失败 {{ batch_result.failure_count }} 项。</p>
<table><thead><tr><th>文件/URL</th><th>资产库结果</th><th>说明</th><th>项目</th></tr></thead><tbody>{% for result in batch_result.results %}<tr><td>{{ result.file_name }}</td><td>{{ result.repository_label }}</td><td>{{ result.message }}</td><td>{% if result.project_id %}<a href="{{ url_for('project_assets_page', project_id=result.project_id) }}">查看项目</a>{% else %}—{% endif %}</td></tr>{% endfor %}</tbody></table>
</section>{% endif %}
<script>
const boxes = Array.from(document.querySelectorAll('.inbox-select'));
const count = document.getElementById('selected-count');
function updateCount() { count.textContent = `已选择 ${boxes.filter(box => box.checked).length} 项`; }
function choose(predicate) { boxes.forEach(box => { box.checked = !box.disabled && predicate(box); }); updateCount(); }
document.getElementById('select-filtered').addEventListener('click', () => choose(() => true));
document.getElementById('select-confirmable').addEventListener('click', () => choose(box => box.dataset.confirmable === 'true'));
document.getElementById('clear-selection').addEventListener('click', () => choose(() => false));
boxes.forEach(box => box.addEventListener('change', updateCount));
const batchForm = document.getElementById('batch-confirm-form');
const batchButton = document.getElementById('batch-confirm-button');
const batchProcessing = document.getElementById('batch-processing');
batchForm.addEventListener('submit', event => {
  if (event.submitter !== batchButton) return;
  if (batchForm.dataset.submitting === 'true') {
    event.preventDefault();
    return;
  }
  batchForm.dataset.submitting = 'true';
  batchButton.disabled = true;
  batchButton.textContent = '处理中…';
  batchProcessing.hidden = false;
});
{% if batch_queue and batch_queue.received_requests %}
const batchRequests = {{ batch_queue.received_requests | tojson }};
const batchCompleted = document.getElementById('batch-completed');
const batchFailed = document.getElementById('batch-failed');
const batchCurrent = document.getElementById('batch-current');
const batchMessage = document.getElementById('batch-progress-message');
let completedCount = {{ batch_queue.completed }};
let failedCount = {{ batch_queue.failed }};
async function processUrlBatchSequentially() {
  for (let index = 0; index < batchRequests.length; index += 1) {
    batchCurrent.textContent = String({{ batch_queue.completed + batch_queue.failed }} + index + 1);
    try {
      const response = await fetch(batchRequests[index].url, {
        method: 'POST',
        headers: {'Accept': 'application/json'}
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (result.status === 'COMPLETED') completedCount += 1;
      if (result.status === 'FAILED') failedCount += 1;
      batchCompleted.textContent = String(completedCount);
      batchFailed.textContent = String(failedCount);
    } catch (error) {
      batchMessage.textContent = '处理请求失败，已停止。可刷新页面继续剩余任务。';
      return;
    }
  }
  batchMessage.textContent = '本批处理已结束，正在刷新结果…';
  window.location.reload();
}
processUrlBatchSequentially();
{% endif %}
{% if batch_queue and batch_queue.server_processing and (batch_queue.received or batch_queue.processing) %}
setTimeout(() => window.location.reload(), 1000);
{% endif %}
</script>
"""


INBOX_DETAIL_TEMPLATE = """
<h2>资料处理详情</h2>
{% if item.status == completed_status %}<section><h3>{% if remediation_enabled %}提取结果与人工核实{% else %}提取结果{% endif %}</h3>
{% if remediation_enabled %}<p>人工核实只保存审计，不会自动确认或写入资产库。</p>{% endif %}
<table class="remediation-table"><thead><tr><th>字段</th><th>提取结果</th><th>人工核实</th><th>操作</th></tr></thead><tbody>
{% for field in detail_fields %}<tr>
  <th>{{ field.label }}</th><td>{{ field.display_value or '未识别' }}</td>
  {% if remediation_enabled %}<td><form id="remediation_{{ field.name }}" method="post" action="{{ url_for('remediate_inbox_item', inbox_id=item.inbox_id) }}">
    <input type="hidden" name="expected_revision" value="{{ remediation_revision }}">
    <input type="hidden" name="action_id" value="{{ remediation_action_id }}_{{ field.name }}">
    <input type="hidden" name="field" value="{{ field.name }}">
    <input name="new_value" value="{{ field.correction_value }}" placeholder="填写修正值" required aria-label="{{ field.label }}人工核实">
  </form></td><td>
    <button type="submit" form="remediation_{{ field.name }}" formnovalidate name="action_type" value="verify_current_value"{% if not field.original_value %} disabled{% endif %}>确认原值</button>
    <button type="submit" form="remediation_{{ field.name }}" name="action_type" value="correct_effective_value">保存修正</button>
    <small>{{ field.status }}{% if field.latest_timestamp %} · <time data-utc-time="{{ field.latest_timestamp }}">正在转换本地时间…</time>{% endif %}</small>
  </td>{% else %}<td>—</td><td>—</td>{% endif %}
</tr>{% endfor %}</tbody></table></section>
{% endif %}
<section><h3>处理详情</h3><table><tbody>{% for label, value in task_fields %}<tr><th>{{ label }}</th><td>{% if value is mapping and value.is_local_time %}<time data-utc-time="{{ value.utc_time }}">正在转换本地时间…</time>{% else %}{{ value or '未识别' }}{% endif %}</td></tr>{% endfor %}</tbody></table></section>
{% if can_parse_attachments %}
<section>
  <h3>网页关键字段不完整</h3>
  <p>已发现 {{ attachment_pending_count }} 个尚未解析的相关附件。附件不会自动下载或调用 AI。</p>
  <form method="post" action="{{ url_for('parse_inbox_attachments', inbox_id=item.inbox_id) }}"><button type="submit">解析相关附件</button></form>
  <p><a href="{{ url_for('acquisition_inbox_page') }}">暂不解析</a></p>
</section>
{% endif %}
<p><a href="{{ url_for('acquisition_inbox_page') }}">返回资料待办</a></p>
"""


DASHBOARD_TEMPLATE = """
<h2>首页</h2>
<div class="stats">
  <div class="stat"><span>待处理资料</span><strong>{{ stats.inbox_received }}</strong></div>
  <div class="stat"><span>处理中</span><strong>{{ stats.inbox_processing }}</strong></div>
  <div class="stat"><span>技术处理完成</span><strong>{{ stats.inbox_completed }}</strong></div>
  <div class="stat"><span>处理失败</span><strong>{{ stats.inbox_failed }}</strong></div>
  <div class="stat"><span>资产库项目</span><strong>{{ stats.projects }}</strong></div>
  <div class="stat"><span>资产库文档</span><strong>{{ stats.documents }}</strong></div>
</div>
<p class="muted">统计仅读取现有任务和资产库数据。</p>
"""


_RELEASE_ENDPOINT_METHODS = {
    "dashboard_page": {"GET"},
    "project_assets_page": {"GET"},
    "acquisition_page": {"GET", "POST"},
    "acquisition_inbox_page": {"GET"},
    "process_received_inbox_item": {"POST"},
    "confirm_selected_inbox_items": {"POST"},
    "acquisition_inbox_detail": {"GET"},
    "capture_inbox_headless_browser": {"POST"},
    "parse_inbox_attachments": {"POST"},
    "retry_inbox_downstream_refresh": {"POST"},
    "confirm_inbox_item": {"POST"},
    "remediate_inbox_item": {"POST"},
    "retry_inbox_item": {"POST"},
    "recollect_inbox_item": {"POST"},
}


def create_app(
    paths: OperatorUiPaths | None = None,
    *,
    release_mode: bool = False,
    app_version: str = "development",
    max_content_length: int | None = None,
) -> Flask:
    """Create a local-only UI that delegates all operations to existing V3 modules."""
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32) if release_mode else "local-operator-ui",
        OPERATOR_UI_PATHS=paths or OperatorUiPaths(),
        RELEASE_MODE=bool(release_mode),
        APP_VERSION=str(app_version),
        MAX_CONTENT_LENGTH=max_content_length,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict" if release_mode else "Lax",
        SESSION_COOKIE_SECURE=False,
    )
    if release_mode:
        app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]
    app.extensions["file_batch_executor"] = ThreadPoolExecutor(
        max_workers=configured_file_batch_concurrency(),
        thread_name_prefix="bidding-file-acquisition",
    )

    @app.before_request
    def enforce_release_boundary() -> None:
        if not app.config["RELEASE_MODE"]:
            return
        endpoint = request.endpoint
        allowed = _RELEASE_ENDPOINT_METHODS.get(str(endpoint))
        method = request.method.upper()
        if allowed is None or (
            method not in allowed
            and method != "OPTIONS"
            and not (method == "HEAD" and "GET" in allowed)
        ):
            abort(404)
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
            if fetch_site == "cross-site":
                abort(403)
            origin = request.headers.get("Origin")
            if origin is not None:
                try:
                    parsed = urlsplit(origin)
                    hostname = parsed.hostname
                except ValueError:
                    abort(403)
                if (
                    origin.strip().lower() == "null"
                    or parsed.scheme not in {"http", "https"}
                    or hostname not in {"127.0.0.1", "localhost"}
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    abort(403)

    @app.get("/")
    def dashboard_page() -> str:
        return render_page(DASHBOARD_TEMPLATE, stats=dashboard_stats(current_paths(app)))

    @app.get("/review-queue")
    def review_queue_page() -> str:
        try:
            queue = load_review_queue(current_paths(app).review_queue)
            return render_page(REVIEW_TEMPLATE, items=[review_row(item) for item in queue], error="")
        except Exception as exc:  # UI shows diagnostics rather than hiding invalid input files.
            return render_page(REVIEW_TEMPLATE, items=[], error=str(exc))

    @app.post("/review-queue/<asset_id>")
    def submit_review(asset_id: str) -> Any:
        paths = current_paths(app)
        try:
            review_queue = [
                _sanitized_review_queue_item(item)
                for item in load_review_queue(paths.review_queue)
            ]
            decision = create_review_decision(
                review_queue,
                asset_id,
                request.form.get("decision", ""),
                request.form.get("reason", ""),
                reviewer=request.form.get("reviewer", ""),
                related_project_id=request.form.get("related_project_id", ""),
            )
            append_review_decision(decision, paths.review_decisions)
            flash(f"已追加 {decision['decision']} 决策：{asset_id}", "flash")
        except Exception as exc:  # Existing review validation remains authoritative.
            flash(str(exc), "error")
        return redirect(url_for("review_queue_page"))

    @app.route("/apply-reviewed", methods=["GET", "POST"])
    def apply_reviewed_page() -> str:
        paths = current_paths(app)
        result: dict[str, Any] | None = None
        if request.method == "POST":
            result = apply_reviewed(
                operator=request.form.get("operator", "operator"),
                review_decisions_path=paths.review_decisions,
                review_queue_path=paths.review_queue,
                intake_output_path=paths.intake_output,
                intake_audit_path=paths.intake_audit,
                documents_path=paths.documents,
                projects_path=paths.projects,
                repository_audit_path=paths.repository_audit,
                result_path=paths.workflow_result,
            )
        accepted_count = accepted_asset_count(paths.review_decisions)
        return render_page(APPLY_TEMPLATE, accepted_count=accepted_count, result=pretty_json(result) if result else "")

    @app.get("/project-assets")
    def project_assets_page() -> str:
        paths = current_paths(app)
        project_id = request.args.get("project_id", "").strip()
        query = request.args.get("query", "").strip()
        service = RepositoryQueryService(paths.projects, paths.documents, paths.links)
        project_asset: dict[str, Any] = {}
        if project_id:
            project_asset = service.get_project_asset(project_id)
        document_rows = project_document_rows(project_asset)
        search_results = [
            project_search_row(row)
            for row in (service.search_projects(query) if query else service.list_projects())
        ] if not project_id else []
        return render_page(
            PROJECT_TEMPLATE,
            project_id=project_id,
            query=query,
            project_asset=project_asset,
            project_fields=project_detail_fields(project_asset),
            project_documents=document_rows,
            show_document_type=any(str(row.get("document_type") or "").strip() for row in document_rows),
            search_results=search_results,
            timeline=service.get_project_timeline(project_id) if project_id else [],
            error="",
        )

    @app.route("/document-relations", methods=["GET", "POST"])
    def document_relations_page() -> str:
        paths = current_paths(app)
        result: dict[str, Any] | None = None
        if request.method == "POST":
            result = link_documents(
                project_id=request.form.get("project_id", ""),
                bid_notice=request.form.get("bid_notice", ""),
                award_notice=request.form.get("award_notice", ""),
                contract=request.form.get("contract", ""),
                operator=request.form.get("operator", "operator"),
                projects_path=paths.projects,
                documents_path=paths.documents,
                links_path=paths.links,
                result_path=paths.workflow_result,
            )
        projects = load_relation_json_array(paths.projects, "projects")
        documents = load_relation_json_array(paths.documents, "documents")
        return render_page(
            RELATION_TEMPLATE,
            projects=projects,
            documents=documents,
            relation_types=[("bid_notice", "招标公告"), ("award_notice", "中标公告"), ("contract", "合同")],
            result=pretty_json(result) if result else "",
        )

    @app.route("/acquisition", methods=["GET", "POST"])
    def acquisition_page() -> str:
        paths = current_paths(app)
        result: dict[str, Any] | None = None
        sources: list[dict[str, str]] = []
        if request.method == "POST":
            try:
                if request.form.get("action") == "url":
                    raw_urls = request.form.get("urls", "") or request.form.get("url", "")
                    if len([line for line in str(raw_urls).splitlines() if line.strip()]) > MAX_BATCH_URLS:
                        raise ValueError(f"一次最多提交 {MAX_BATCH_URLS} 条 URL")
                    urls, skipped_duplicates = normalized_url_batch(raw_urls)
                    if not urls:
                        raise ValueError("URL is required")
                    batch_id = f"acquisition_batch_{uuid.uuid4().hex}"
                    created_items: list[dict[str, Any]] = []
                    created_count = 0
                    existing_count = 0
                    batch_items = create_url_items(urls, inbox_paths(paths), batch_id=batch_id)
                    for url, item in zip(urls, batch_items):
                        created = bool(item.pop("_created", True))
                        if created:
                            created_count += 1
                        elif str(item.get("status") or "") == COMPLETED:
                            existing_count += 1
                            created_items.append(item)
                            continue
                        if not is_valid_http_url(url):
                            processed = fail_item(item["inbox_id"], "URL 格式无效，仅支持 http/https。", inbox_paths(paths))
                        else:
                            processed = item
                        created_items.append(processed)
                    skipped_total = skipped_duplicates + existing_count
                    if len(urls) == 1 and existing_count == 1:
                        flash("该 URL 已采集，未创建新任务；已打开已有任务。", "flash")
                        return redirect(url_for(
                            "acquisition_inbox_detail", inbox_id=created_items[0]["inbox_id"]
                        ))
                    return redirect(url_for(
                        "acquisition_inbox_page",
                        batch_id=batch_id,
                        batch_created=created_count,
                        batch_failed=sum(1 for item in created_items if item.get("status") == FAILED),
                        batch_skipped=skipped_total,
                    ))
                elif request.form.get("action") == "file":
                    uploads = [upload for upload in request.files.getlist("files") if upload.filename]
                    if not uploads:
                        uploads = [upload for upload in request.files.getlist("file") if upload.filename]
                    if not uploads:
                        raise ValueError("Upload a supported file")
                    if len(uploads) > MAX_BATCH_UPLOAD_FILES:
                        raise ValueError(f"一次最多选择 {MAX_BATCH_UPLOAD_FILES} 个文件")
                    batch_id = f"acquisition_batch_{uuid.uuid4().hex}" if len(uploads) > 1 else ""
                    created_items: list[dict[str, Any]] = []
                    failed_creations: list[dict[str, str]] = []
                    for upload in uploads:
                        try:
                            file_name = safe_upload_filename(upload.filename)
                            suffix = Path(file_name).suffix.lower()
                            if suffix not in SUPPORTED_UPLOAD_SUFFIXES:
                                raise ValueError(f"Unsupported upload type: {suffix}")
                            if suffix == ".xlsx" and Path(file_name).name.casefold() == FORMAL_WORKBOOK_NAME.casefold():
                                raise ValueError(
                                    "根目录正式《招投标.xlsx》不允许作为普通资料采集来源；"
                                    "该文件属于未来 Historical Asset Import 范围。"
                                )
                            upload_path = paths.upload_dir / uuid.uuid4().hex / file_name
                            upload_path.parent.mkdir(parents=True, exist_ok=True)
                            upload.save(upload_path)
                            item = create_file_item(upload_path, inbox_paths(paths), batch_id=batch_id)
                            created_items.append(item)
                        except Exception as exc:  # A failed file must not stop the remaining batch.
                            failed_creations.append({"file_name": upload.filename, "reason": str(exc)})
                    if not created_items:
                        raise ValueError(failed_creations[0]["reason"])
                    if len(uploads) == 1:
                        result = process_item(
                            created_items[0]["inbox_id"],
                            acquisition_paths(paths),
                            inbox_paths(paths),
                            capture_root=paths.upload_dir.parent,
                        )
                        processed_item = dict(result.get("item") or created_items[0])
                        if str(processed_item.get("task_role") or "") == "xlsx_workbook_summary":
                            return redirect(url_for(
                                "acquisition_page",
                                import_summary_id=processed_item.get("inbox_id", ""),
                            ))
                        if str(processed_item.get("status") or "") == FAILED:
                            flash(
                                localized_error_message(
                                    str(processed_item.get("error_message") or "")
                                ) or "资料处理失败。",
                                "error",
                            )
                        return redirect(url_for(
                            "acquisition_inbox_detail",
                            inbox_id=processed_item["inbox_id"],
                        ))
                    dispatch_file_batch(app, paths, created_items)
                    created_names = [
                        Path(str(item.get("source_file") or "")).name or "未识别文件"
                        for item in created_items
                    ]
                    flash(
                        f"成功创建 {len(created_items)} 个"
                        f"{'：' + '、'.join(created_names) if created_names else ''}；"
                        f"创建失败 {len(failed_creations)} 个。",
                        "flash",
                    )
                    for failure in failed_creations:
                        reason = localized_error_message(str(failure.get("reason") or "")) or "文件创建失败。"
                        flash(f"{failure.get('file_name') or '未识别文件'}：{reason}", "error")
                    return redirect(url_for(
                        "acquisition_inbox_page",
                        batch_id=batch_id,
                        batch_created=len(created_items),
                        batch_failed=len(failed_creations),
                        batch_processing="server",
                    ))
                else:
                    raise ValueError("Unsupported acquisition action")
                workflow = dict(result.get("workflow") or {})
                sources = [dict(item) for item in workflow.get("sources", []) if isinstance(item, dict)]
                processed_item = dict(result.get("item") or {})
                if str(processed_item.get("status") or "") == FAILED:
                    flash(localized_error_message(str((result.get("item") or {}).get("error_message") or "")) or "资料处理失败。", "error")
                return redirect(url_for("acquisition_inbox_detail", inbox_id=processed_item["inbox_id"]))
            except Exception as exc:  # Existing module errors remain visible to the operator.
                flash(str(exc), "error")
        return render_page(
            ACQUISITION_TEMPLATE,
            result=pretty_json(result) if result else "",
            sources=sources,
            workbook_summaries=acquisition_workbook_summaries(
                paths,
                summary_id=request.args.get("import_summary_id", ""),
                batch_id=request.args.get("batch_id", ""),
            ),
            max_batch_files=MAX_BATCH_UPLOAD_FILES,
            max_batch_urls=MAX_BATCH_URLS,
        )

    @app.get("/acquisition/inbox")
    def acquisition_inbox_page() -> str:
        paths = current_paths(app)
        active_filter = request.args.get("filter", "all")
        inbox_items = load_inbox_items(paths.acquisition_inbox)
        rows = filter_inbox_rows(inbox_list_rows(paths), active_filter)
        upload_summary = None
        if request.args.get("batch_created") is not None:
            upload_summary = {
                "created": request.args.get("batch_created", "0"),
                "failed": request.args.get("batch_failed", "0"),
                "skipped": request.args.get("batch_skipped", "0"),
            }
        batch_queue = None
        batch_id = str(request.args.get("batch_id") or "")
        if batch_id:
            server_processing = request.args.get("batch_processing") == "server"
            batch_items = [
                item for item in inbox_items
                if str(item.get("batch_id") or "") == batch_id
            ]
            received_items = [item for item in batch_items if str(item.get("status") or "") == RECEIVED]
            completed_count = sum(1 for item in batch_items if str(item.get("status") or "") == COMPLETED)
            failed_count = sum(1 for item in batch_items if str(item.get("status") or "") == FAILED)
            processing_count = sum(1 for item in batch_items if str(item.get("status") or "") == PROCESSING)
            finished_count = completed_count + failed_count
            batch_queue = {
                "batch_id": batch_id,
                "created": len(batch_items),
                "total": len(batch_items),
                "current": min(len(batch_items), finished_count + (1 if received_items or processing_count else 0)),
                "completed": completed_count,
                "failed": failed_count,
                "processing": processing_count,
                "received": len(received_items),
                "server_processing": server_processing,
                "content_ready": sum(
                    1 for item in batch_items
                    if str((item.get("processing_result") or {}).get("content_status") or "") == "content_ready"
                ),
                "content_invalid": sum(
                    1 for item in batch_items
                    if str(item.get("status") or "") in {COMPLETED, FAILED}
                    and str((item.get("processing_result") or {}).get("content_status") or "") != "content_ready"
                ),
                "fields_complete": sum(
                    1 for item in batch_items
                    if bool(((item.get("processing_result") or {}).get("field_completeness") or {}).get("critical_fields_complete"))
                ),
                "attachments_pending": sum(
                    1 for item in batch_items
                    if int(((item.get("processing_result") or {}).get("attachments") or {}).get("pending_count") or 0) > 0
                ),
                "confirmable": sum(
                    1 for item in batch_items if resolve_inbox_confirmation_eligibility(item, paths).get("status") == "eligible"
                ),
                "ai_invoked": sum(
                    int(bool(((item.get("processing_result") or {}).get("ai") or {}).get("invoked"))) for item in batch_items
                ),
                "ai_skipped": sum(
                    1 for item in batch_items
                    if str(((item.get("processing_result") or {}).get("ai") or {}).get("status") or "").startswith("skipped")
                ),
                "received_requests": [
                    {
                        "inbox_id": str(item.get("inbox_id") or ""),
                        "url": url_for("process_received_inbox_item", inbox_id=str(item.get("inbox_id") or "")),
                    }
                    for item in received_items
                    if str(item.get("source_type") or "") == "url"
                ] if processing_count == 0 and not server_processing else [],
            }
        return render_page(
            INBOX_TEMPLATE,
            rows=rows,
            failed_status=FAILED,
            active_filter=active_filter,
            upload_summary=upload_summary,
            batch_queue=batch_queue,
            batch_result=None,
        )

    @app.post("/acquisition/inbox/<inbox_id>/process")
    def process_received_inbox_item(inbox_id: str):
        paths = current_paths(app)
        item = next(
            (row for row in load_inbox_items(paths.acquisition_inbox)
             if str(row.get("inbox_id") or "") == inbox_id),
            None,
        )
        if item is None:
            return jsonify({
                "inbox_id": inbox_id,
                "processed": False,
                "skipped": True,
                "reason": "not_found",
                "status": "NOT_FOUND",
            }), 404
        status = str(item.get("status") or "")
        if status != RECEIVED:
            return jsonify({
                "inbox_id": inbox_id,
                "processed": False,
                "skipped": True,
                "reason": f"status={status}",
                "status": status,
            })
        try:
            result = process_item(
                inbox_id,
                acquisition_paths(paths),
                inbox_paths(paths),
                attempt_origin="operator",
                capture_root=paths.upload_dir.parent,
            )
        except Exception:
            current = next(
                (row for row in load_inbox_items(paths.acquisition_inbox)
                 if str(row.get("inbox_id") or "") == inbox_id),
                item,
            )
            return jsonify({
                "inbox_id": inbox_id,
                "processed": False,
                "skipped": False,
                "reason": "processing_error",
                "status": str(current.get("status") or ""),
            }), 500
        processed = dict(result.get("item") or item)
        return jsonify({
            "inbox_id": inbox_id,
            "processed": not bool(result.get("skipped")),
            "skipped": bool(result.get("skipped")),
            "reason": str(result.get("reason") or ""),
            "status": str(processed.get("status") or ""),
        })

    @app.post("/acquisition/inbox/confirm-selected")
    def confirm_selected_inbox_items() -> str:
        paths = current_paths(app)
        active_filter = request.form.get("filter", "all")
        batch_result = confirm_inbox_batch(request.form.getlist("inbox_ids"), paths, operator="operator_ui")
        return render_page(
            INBOX_TEMPLATE,
            rows=filter_inbox_rows(inbox_list_rows(paths), active_filter),
            failed_status=FAILED,
            active_filter=active_filter,
            upload_summary=None,
            batch_result=batch_result,
        )

    @app.get("/acquisition/inbox/<inbox_id>")
    def acquisition_inbox_detail(inbox_id: str) -> str:
        paths = current_paths(app)
        items = load_inbox_items(inbox_paths(paths).inbox)
        item = next((row for row in items if str(row.get("inbox_id") or "") == inbox_id), None)
        if item is None:
            item = {"inbox_id": inbox_id, "status": "NOT_FOUND"}
        raw_remediation = item.get("manual_remediation")
        remediation = dict(raw_remediation) if isinstance(raw_remediation, dict) else {}
        remediation_enabled = is_manual_remediation_target(item)
        return render_page(INBOX_DETAIL_TEMPLATE, item=item, failed_status=FAILED, completed_status="COMPLETED",
                           task_fields=inbox_task_fields(item, paths),
                           can_parse_attachments=inbox_can_parse_attachments(item, paths),
                           attachment_pending_count=int(((item.get("processing_result") or {}).get("attachments") or {}).get("pending_count") or 0),
                           detail_fields=inbox_detail_field_rows(item),
                           remediation_enabled=remediation_enabled,
                           remediation_revision=int(remediation.get("revision") or 0),
                           remediation_action_id=f"manual_remediation_{uuid.uuid4().hex}")

    @app.post("/acquisition/inbox/<inbox_id>/headless-browser")
    def capture_inbox_headless_browser(inbox_id: str) -> Any:
        paths = current_paths(app)
        try:
            result = capture_headless_browser_dom(
                inbox_id,
                acquisition_paths(paths),
                inbox_paths(paths),
                capture_root=paths.upload_dir.parent,
            )
            item = dict(result.get("item") or {})
            if str(item.get("status") or "") == FAILED:
                flash(str(item.get("error_message") or "本机 Google Chrome 获取渲染正文失败。"), "error")
            else:
                eligibility = resolve_inbox_confirmation_eligibility(item, paths)
                if str(eligibility.get("content_status") or "") == "content_ready":
                    flash("已使用本机 Google Chrome 取得渲染正文，并在原 URL 任务中完成信息提取。", "flash")
                else:
                    flash("本机 Google Chrome 已返回渲染内容，但正文质量门禁仍未通过，当前不可确认。", "error")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("acquisition_inbox_detail", inbox_id=inbox_id))

    @app.post("/acquisition/inbox/<inbox_id>/attachments/parse")
    def parse_inbox_attachments(inbox_id: str) -> Any:
        paths = current_paths(app)
        result = parse_item_attachments(
            inbox_id,
            acquisition_paths(paths),
            inbox_paths(paths),
            capture_root=paths.upload_dir.parent,
        )
        if result.get("skipped"):
            flash("相关附件已经处理，未重复下载或调用附件 AI。", "flash")
        elif result.get("error"):
            flash(f"附件解析已隔离失败：{result['error']}", "error")
        else:
            item = dict(result.get("item") or {})
            eligibility = resolve_inbox_confirmation_eligibility(item, paths)
            flash(
                "相关附件解析完成；字段已补齐，可由用户确认。"
                if eligibility.get("status") == "eligible" else
                "相关附件解析完成，但字段仍不完整或存在冲突，当前不可确认。",
                "flash" if eligibility.get("status") == "eligible" else "error",
            )
        return redirect(url_for("acquisition_inbox_detail", inbox_id=inbox_id))

    @app.post("/acquisition/inbox/<inbox_id>/post-processing/retry")
    def retry_inbox_downstream_refresh(inbox_id: str) -> Any:
        paths = current_paths(app)
        result = retry_item_downstream_refresh(
            inbox_id, acquisition_paths(paths), inbox_paths(paths)
        )
        if result.get("skipped"):
            flash("当前任务没有需要重试的后处理。", "flash")
        elif dict(result.get("refresh") or {}).get("status") == "success":
            flash("候选后处理刷新完成；采集证据和候选未重复写入。", "flash")
        else:
            refresh = dict(result.get("refresh") or {})
            flash(
                f"采集和候选仍已保存；后处理阶段 {refresh.get('failed_stage') or 'unknown'} 再次失败。",
                "error",
            )
        return redirect(url_for("acquisition_inbox_detail", inbox_id=inbox_id))

    @app.post("/acquisition/inbox/<inbox_id>/confirm")
    def confirm_inbox_item(inbox_id: str) -> Any:
        try:
            result = confirm_inbox_with_audit(inbox_id, current_paths(app), operator="operator_ui")
            flash(str(result.get("message") or "入库完成"), "flash" if result.get("success") else "error")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("acquisition_inbox_detail", inbox_id=inbox_id))

    @app.post("/acquisition/inbox/<inbox_id>/remediate")
    def remediate_inbox_item(inbox_id: str) -> Any:
        paths = current_paths(app)
        try:
            result = apply_manual_remediation(
                inbox_id,
                field=request.form.get("field", ""),
                action_type=request.form.get("action_type", ""),
                new_value=request.form.get("new_value", ""),
                operator=local_operator_identity(),
                expected_revision=request.form.get("expected_revision", ""),
                action_id=request.form.get("action_id", ""),
                paths=inbox_paths(paths),
            )
            flash(
                "该人工修正已处理，未重复确认。" if result.get("replayed") else
                "人工核实已保存；请单独执行确认操作。",
                "flash",
            )
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("acquisition_inbox_detail", inbox_id=inbox_id))

    @app.post("/acquisition/inbox/<inbox_id>/retry")
    def retry_inbox_item(inbox_id: str) -> Any:
        paths = current_paths(app)
        result = process_item(
            inbox_id, acquisition_paths(paths), inbox_paths(paths), attempt_origin="operator_retry",
            capture_root=paths.upload_dir.parent,
        )
        item = dict(result.get("item") or {})
        if item.get("status") == FAILED:
            flash(localized_error_message(str(item.get("error_message") or "")) or "资料处理失败。", "error")
        elif result.get("skipped"):
            flash(str(result.get("reason") or "Item was not processed"), "error")
        else:
            eligibility = resolve_inbox_confirmation_eligibility(item, paths)
            flash(url_content_status_label(str(eligibility.get("content_status") or "unknown")), "flash")
        return redirect(url_for("acquisition_inbox_detail", inbox_id=inbox_id))

    @app.post("/acquisition/inbox/<inbox_id>/recollect")
    def recollect_inbox_item(inbox_id: str) -> Any:
        paths = current_paths(app)
        result = process_item(
            inbox_id,
            acquisition_paths(paths),
            inbox_paths(paths),
            force=True,
            attempt_origin="operator_recollect",
            capture_root=paths.upload_dir.parent,
        )
        item = dict(result.get("item") or {})
        if item.get("status") == FAILED:
            flash(localized_error_message(str(item.get("error_message") or "")) or "重新采集失败。", "error")
        else:
            flash("已在原任务中完成重新采集，未新增待办记录。", "flash")
        return redirect(url_for("acquisition_inbox_detail", inbox_id=inbox_id))

    @app.post("/acquisition/inbox/<inbox_id>/retry-excel")
    def retry_inbox_excel(inbox_id: str) -> Any:
        try:
            result = confirm_inbox_with_audit(inbox_id, current_paths(app), operator="operator_ui")
            flash(str(result.get("message") or "Excel 重试完成"), "flash" if result.get("success") else "error")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("acquisition_inbox_detail", inbox_id=inbox_id))

    return app


def current_paths(app: Flask) -> OperatorUiPaths:
    return app.config["OPERATOR_UI_PATHS"]


def acquisition_paths(paths: OperatorUiPaths) -> AcquisitionWorkflowPaths:
    return AcquisitionWorkflowPaths(
        asset_candidates=paths.asset_candidates,
        deduped_candidates=paths.deduped_candidates,
        dedup_summary=paths.dedup_summary,
        lifecycle=paths.lifecycle,
        review_queue=paths.review_queue,
    )


def inbox_paths(paths: OperatorUiPaths) -> AcquisitionInboxPaths:
    return AcquisitionInboxPaths(
        inbox=paths.acquisition_inbox,
        summary=paths.acquisition_inbox_summary,
        manual_remediation_backup_root=paths.manual_remediation_backup_root,
    )

def configured_file_batch_concurrency() -> int:
    raw = os.environ.get(FILE_BATCH_CONCURRENCY_ENV, "").strip()
    try:
        concurrency = int(raw) if raw else DEFAULT_FILE_BATCH_CONCURRENCY
    except ValueError as exc:
        raise ValueError("invalid_file_batch_concurrency") from exc
    if concurrency <= 0:
        raise ValueError("invalid_file_batch_concurrency")
    return min(concurrency, MAX_BATCH_UPLOAD_FILES)


def local_operator_identity() -> str:
    """Resolve a non-empty local audit identity without trusting form input."""
    try:
        username = str(getpass.getuser() or "").strip()
    except (KeyError, OSError):
        username = ""
    if not username:
        username = str(os.environ.get("USERNAME") or os.environ.get("USER") or "local_user").strip()
    domain = str(os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{username}" if domain else username


def dispatch_file_batch(
    app: Flask,
    paths: OperatorUiPaths,
    items: list[dict[str, Any]],
) -> None:
    """Submit only after the caller has persisted every valid file Inbox record."""

    executor = app.extensions["file_batch_executor"]
    for item in items:
        executor.submit(_process_file_batch_item, paths, str(item.get("inbox_id") or ""))


def _process_file_batch_item(paths: OperatorUiPaths, inbox_id: str) -> None:
    try:
        process_item(
            inbox_id,
            acquisition_paths(paths),
            inbox_paths(paths),
            attempt_origin="operator_batch",
            capture_root=paths.upload_dir.parent,
        )
    except Exception as exc:
        # Configuration/dispatch failures happen outside the normal per-item guard.
        current = next(
            (
                item for item in load_inbox_items(paths.acquisition_inbox)
                if str(item.get("inbox_id") or "") == inbox_id
            ),
            None,
        )
        if current is None or str(current.get("status") or "") in {COMPLETED, FAILED}:
            return
        fail_item(
            inbox_id,
            f"batch_processing_error:{type(exc).__name__}",
            inbox_paths(paths),
            stage="batch_processing",
        )


def excel_sync_paths(paths: OperatorUiPaths) -> InboxExcelSyncPaths:
    return InboxExcelSyncPaths(
        excel=paths.excel,
        records_dir=paths.excel_sync_records,
        summary_dir=paths.excel_sync_summary,
        backup_dir=paths.excel_backup_dir,
        runtime_dir=paths.excel_sync_runtime,
    )


def safe_upload_filename(raw_name: str) -> str:
    """Keep business-significant Chinese names while removing path and Windows-invalid characters."""
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")
    if not name or name in {".", ".."}:
        raise ValueError("Upload filename is invalid")
    return name


def normalized_url_batch(raw_value: str) -> tuple[list[str], int]:
    rows = [line.strip() for line in str(raw_value or "").splitlines() if line.strip()]
    unique = list(dict.fromkeys(normalize_url(row) for row in rows))
    return unique, len(rows) - len(unique)


def is_valid_http_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def inbox_list_rows(paths: OperatorUiPaths) -> list[dict[str, Any]]:
    items = sorted(
        [item for item in load_inbox_items(inbox_paths(paths).inbox) if is_business_inbox_item(item)],
        key=lambda item: str(item.get("created_time") or ""),
        reverse=True,
    )
    documents = load_relation_json_array(paths.documents, "documents")
    documented_asset_ids = {
        str(document.get("asset_id") or "")
        for document in documents if str(document.get("asset_id") or "")
    }
    group_positions = build_inbox_group_positions(items)
    return build_inbox_list_display_rows(items, documented_asset_ids, group_positions, paths)


def build_inbox_group_positions(
    items: list[dict[str, Any]],
) -> dict[str, tuple[str, int, int]]:
    grouped_items: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        group_id = business_group_id(dict(item.get("processing_result") or {}))
        if group_id:
            grouped_items.setdefault(group_id, []).append(item)
    group_positions: dict[str, tuple[str, int, int]] = {}
    for group_id, members in grouped_items.items():
        ordered = sorted(
            members,
            key=lambda member: (
                str((member.get("processing_result") or {}).get("sheet_name") or ""),
                int((member.get("processing_result") or {}).get("excel_row_number") or 0),
                str(member.get("inbox_id") or ""),
            ),
        )
        for position, member in enumerate(ordered, 1):
            group_positions[str(member.get("inbox_id") or "")] = (group_id, position, len(ordered))
    return group_positions


def build_inbox_list_display_rows(
    items: list[dict[str, Any]],
    documented_asset_ids: set[str],
    group_positions: dict[str, tuple[str, int, int]],
    paths: OperatorUiPaths,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        processing = dict(item.get("processing_result") or {})
        effective = confirmable_processing_payload(item)
        eligibility = resolve_inbox_confirmation_eligibility(item, paths)
        intake_result = any(
            str(asset_id) in documented_asset_ids for asset_id in item.get("generated_asset_ids", [])
        )
        task_role = str(item.get("task_role") or "")
        status = str(item.get("status") or "")
        repository_status = str(item.get("repository_status") or ("WRITTEN" if intake_result else "PENDING"))
        failure_reason = localized_error_message(str(
            item.get("error_message") or item.get("repository_error")
            or item.get("confirm_failure_reason") or ""
        ))
        has_asset = bool([value for value in item.get("generated_asset_ids", []) if str(value)])
        confirmable = (
            status == "COMPLETED" and repository_status in {"PENDING", "FAILED"}
            and has_asset and task_role != "xlsx_workbook_summary"
            and eligibility["status"] == "eligible"
        )
        selectable = confirmable
        inbox_id = str(item.get("inbox_id") or "")
        group_id, group_position, group_size = group_positions.get(inbox_id, ("", 0, 0))
        rows.append({
            "inbox_id": inbox_id,
            "task_type_short": inbox_task_type_short_label(item),
            "source_short": inbox_source_short_label(item),
            "project_name": str(effective.get("project_name") or "未识别"),
            "customer": str(effective.get("customer") or "未识别"),
            "budget": display_amount(effective.get("budget"), include_unit=False),
            "winner": str(effective.get("winner") or ""),
            "award_amount": display_amount(effective.get("award_amount"), include_unit=False),
            "business_group_id": group_id,
            "group_position": group_position,
            "group_size": group_size,
            "status": status,
            "is_url": str(item.get("source_type") or "") == "url",
            "content_status": eligibility["content_status"],
            "content_label": url_content_status_label(eligibility["content_status"])
            if str(item.get("source_type") or "") == "url" else "不适用",
            "extraction_label": extraction_status_label(str(processing.get("extract_status") or "")),
            "processing_label": processing_status_label(status),
            "repository_status": repository_status,
            "repository_label": derived_repository_status_label(item, eligibility, repository_status),
            "repository_class": status_class(
                repository_status == "WRITTEN",
                repository_status != "WRITTEN" and not (
                    status == "COMPLETED" and has_asset and eligibility["status"] == "eligible"
                ),
            ),
            "failure_reason": failure_reason or str(eligibility.get("block_reason") or ""),
            "selectable": selectable,
            "confirmable": confirmable,
            "retry_label": "重新采集" if eligibility["content_status"] == "fetch_failed" else "重新处理",
            "disabled_reason": (
                inbox_disabled_reason(status, repository_status, "", has_asset)
                if repository_status == "WRITTEN"
                else str(eligibility.get("block_reason") or "")
                or inbox_disabled_reason(status, repository_status, "", has_asset)
            ),
        })
    return rows


def is_business_inbox_item(item: dict[str, Any]) -> bool:
    return (
        is_operator_visible(item)
        and str(item.get("task_role") or "") != "xlsx_workbook_summary"
    )


def acquisition_workbook_summaries(
    paths: OperatorUiPaths,
    *,
    summary_id: str = "",
    batch_id: str = "",
) -> list[dict[str, Any]]:
    if not summary_id and not batch_id:
        return []
    summaries = []
    for item in load_inbox_items(inbox_paths(paths).inbox):
        if str(item.get("task_role") or "") != "xlsx_workbook_summary":
            continue
        if summary_id and str(item.get("inbox_id") or "") != summary_id:
            continue
        if batch_id and str(item.get("batch_id") or "") != batch_id:
            continue
        processing = dict(item.get("processing_result") or {})
        summaries.append({
            "file_name": Path(str(item.get("source_file") or "")).name or "未识别工作簿",
            "sheet_count": int(processing.get("sheet_count") or 0),
            "scanned_rows": int(processing.get("scanned_data_rows") or 0),
            "success_count": int(processing.get("successful_candidate_count") or 0),
            "failure_count": int(processing.get("failed_row_count") or processing.get("failed_row_task_count") or 0),
            "skipped_count": int(processing.get("skipped_row_count") or 0),
            "project_count": int(processing.get("project_count") or 0),
            "award_detail_count": int(processing.get("award_detail_count") or 0),
            "multi_detail_project_count": int(processing.get("multi_detail_project_count") or 0),
            "max_details_per_project": int(processing.get("max_award_details_per_project") or 0),
            "import_time": str(item.get("completed_time") or item.get("created_time") or ""),
            "processing_status": processing_status_label(str(item.get("status") or "")),
            "error_message": localized_error_message(str(item.get("error_message") or "")),
        })
    return summaries


def inbox_task_type_short_label(item: dict[str, Any]) -> str:
    role = str(item.get("task_role") or "")
    if role == "xlsx_workbook_summary":
        return "Excel摘要"
    if role == "xlsx_business_row" or str(item.get("source_type") or "") in {"xlsx_row", "xlsx_file_upload"}:
        return "Excel"
    processing = dict(item.get("processing_result") or {})
    suffix = str(processing.get("file_type") or Path(str(item.get("source_file") or "")).suffix).lower().lstrip(".")
    if suffix in {"pdf", "doc", "docx", "zip"}:
        return suffix.upper()
    if str(item.get("source_type") or "") in {"url", "url_acquisition"}:
        return "URL"
    return "资料"


def inbox_source_short_label(item: dict[str, Any]) -> str:
    processing = dict(item.get("processing_result") or {})
    source_file = str(item.get("source_file") or "")
    file_name = str(processing.get("file_name") or Path(source_file).name or "")
    if str(item.get("task_role") or "") == "xlsx_business_row":
        return f"{file_name or 'Excel'} · 第{processing.get('excel_row_number') or '?'}行"
    source_url = str(item.get("source_url") or processing.get("source_url") or "")
    if source_url:
        return urlsplit(source_url).netloc or source_url
    return file_name or "未识别"


def filter_inbox_rows(rows: list[dict[str, Any]], active_filter: str) -> list[dict[str, Any]]:
    if active_filter == "completed":
        return [row for row in rows if row["status"] == "COMPLETED"]
    if active_filter == "failed":
        return [row for row in rows if row["status"] == FAILED]
    if active_filter == "intaken":
        return [row for row in rows if row["repository_status"] == "WRITTEN"]
    if active_filter == "not_intaken":
        return [row for row in rows if row["repository_status"] != "WRITTEN"]
    return rows


def processing_status_label(status: str, content_status: str = "") -> str:
    if status == "COMPLETED" and content_status:
        return url_content_status_label(content_status)
    return {"RECEIVED": "待处理", "PROCESSING": "处理中", "COMPLETED": "技术处理完成", "FAILED": "处理失败"}.get(status, "未识别")


def url_content_status_label(status: str) -> str:
    return {
        "content_ready": "正文已取得",
        "content_partial": "正文不完整",
        "dynamic_shell": "未取得网页正文",
        "blocked": "页面访问受限",
        "soft_error": "未取得目标公告",
        "fetch_failed": "采集失败",
        "unknown": "内容状态待重新确认",
    }.get(status, "内容状态待重新确认")


def extraction_status_label(status: str) -> str:
    return {
        "success": "信息提取完成",
        "partial": "部分信息未提取",
        "empty": "未提取到业务信息",
        "not_run": "未执行信息提取",
        "failed": "信息提取失败",
    }.get(status.lower(), "信息提取状态待确认")


def derived_repository_status_label(
    item: dict[str, Any], eligibility: dict[str, Any], repository_status: str
) -> str:
    if repository_status == "WRITTEN":
        return "已入库"
    has_candidate = any(str(value) for value in item.get("generated_asset_ids", []))
    if (
        str(item.get("status") or "") == "COMPLETED"
        and has_candidate
        and eligibility["status"] == "eligible"
    ):
        return "待入库"
    return "待核实"


def localized_error_message(message: str) -> str:
    """Translate stable operator-facing acquisition errors while retaining useful details."""
    value = str(message or "").strip()
    if "pdf.password_required" in value:
        return "PDF 需要密码；请人工解密后重新上传或重试。"
    if "libreoffice_doc_conversion_timeout" in value:
        return "旧版 DOC 转换超时；请人工转为 DOCX 或 TXT 后重新处理。"
    unsupported = re.match(r"^Unsupported upload type:\s*(\.[^.\s]+)\.", value, flags=re.IGNORECASE)
    if unsupported:
        return f"暂不支持解析 {unsupported.group(1).lower()} 文件；当前可解析 PDF、DOC、DOCX 和 ZIP。"
    return value


def repository_status_label(status: str) -> str:
    return {"WRITTEN": "已入库", "PENDING": "待入库", "FAILED": "待入库"}.get(status, "待核实")


def excel_status_label(status: str) -> str:
    return {
        EXCEL_NOT_WRITTEN: "未写入", EXCEL_WRITTEN: "已写入", EXCEL_CONFLICT: "数据冲突",
        EXCEL_FAILED: "写入失败", EXCEL_NOT_APPLICABLE: "不适用",
    }.get(status, "未写入")


def status_class(success: bool, failed: bool) -> str:
    return "status-ok" if success else ("status-error" if failed else "status-pending")


def inbox_disabled_reason(status: str, repository_status: str, excel_status: str, has_asset: bool = True) -> str:
    if status != "COMPLETED":
        return "仅处理成功的任务可以确认。"
    if repository_status == "WRITTEN":
        return "资产库已完成。"
    if not has_asset:
        return "工作簿摘要不可确认；请确认对应的 Excel 行级任务。"
    return "当前任务不可确认。"


def confirm_inbox_with_audit(inbox_id: str, paths: OperatorUiPaths, *, operator: str) -> dict[str, Any]:
    item = next(
        (row for row in load_inbox_items(paths.acquisition_inbox) if str(row.get("inbox_id") or "") == inbox_id),
        None,
    )
    if item is None:
        raise ValueError(f"Inbox item not found: {inbox_id}")
    assert_inbox_confirmation_eligible(item, paths)
    if str(item.get("task_role") or "") == "xlsx_workbook_summary":
        raise ValueError("工作簿摘要不可确认；请选择对应的 Excel 行级任务。")
    existing = inbox_intake_result(item, paths)
    if existing:
        confirm_project_status(str(existing.get("project_id") or ""), paths.projects)
        merge_award_detail_in_repository(
            str(existing.get("project_id") or ""), confirmable_processing_payload(item), paths.projects
        )
        update_business_sync_result(
            inbox_id, repository_status="WRITTEN", repository_error="", paths=inbox_paths(paths)
        )
        update_confirmation_result(inbox_id, "CONFIRMED", "", inbox_paths(paths))
        existing.update({
            "success": True,
            "message": "资产库已完成，未重复执行。",
            "batch_outcome": "completed_skip",
            "repository_status": "WRITTEN",
        })
        return existing
    try:
        result = confirm_inbox_asset(inbox_id, paths, operator=operator)
    except Exception as exc:
        update_business_sync_result(
            inbox_id, repository_status="FAILED", repository_error=str(exc), paths=inbox_paths(paths)
        )
        update_confirmation_result(inbox_id, "FAILED", str(exc), inbox_paths(paths))
        raise
    if result.get("success"):
        update_business_sync_result(
            inbox_id, repository_status="WRITTEN", repository_error="", paths=inbox_paths(paths)
        )
        update_confirmation_result(inbox_id, "CONFIRMED", "", inbox_paths(paths))
        result.update({
            "success": True,
            "message": "确认完成：已进入资产库。",
            "batch_outcome": "written",
            "repository_status": "WRITTEN",
        })
        return result
    reason = str(result.get("message") or "资产库写入失败")
    update_business_sync_result(
        inbox_id,
        repository_status="FAILED",
        repository_error=reason,
        paths=inbox_paths(paths),
    )
    update_confirmation_result(inbox_id, "FAILED", reason, inbox_paths(paths))
    result.update({"batch_outcome": "failed", "repository_status": "FAILED"})
    return result


def sync_inbox_excel(
    inbox_id: str,
    item: dict[str, Any],
    repository_result: dict[str, Any],
    paths: OperatorUiPaths,
) -> dict[str, Any]:
    project_id = str(repository_result.get("project_id") or "")
    project = next(
        (row for row in load_relation_json_array(paths.projects, "projects") if str(row.get("project_id") or "") == project_id),
        {},
    )
    try:
        excel_result = sync_confirmed_project(
            inbox_id=inbox_id,
            extracted=confirmable_processing_payload(item),
            project=project,
            source_file=str(item.get("source_file") or ""),
            source_url=str(item.get("source_url") or ""),
            confirmed_time=str(item.get("confirm_time") or "") or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            paths=excel_sync_paths(paths),
        )
    except Exception as exc:
        reason = str(exc)
        update_business_sync_result(
            inbox_id,
            excel_status=EXCEL_FAILED,
            excel_error=reason,
            excel_action="",
            excel_conflicts=[],
            paths=inbox_paths(paths),
        )
        update_confirmation_result(inbox_id, "PARTIAL", reason, inbox_paths(paths))
        return {
            **repository_result,
            "success": False,
            "message": f"资产库已成功，Excel 写入失败：{reason}",
            "batch_outcome": "failed",
            "repository_status": "WRITTEN",
            "excel_status": EXCEL_FAILED,
            "excel_action": "",
        }

    excel_status = str(excel_result.get("excel_status") or EXCEL_FAILED)
    action = str(excel_result.get("excel_action") or "")
    error = "" if excel_status == EXCEL_WRITTEN else str(excel_result.get("message") or "")
    update_business_sync_result(
        inbox_id,
        excel_status=excel_status,
        excel_error=error,
        excel_action=action,
        excel_conflicts=[dict(row) for row in excel_result.get("conflicts", []) if isinstance(row, dict)],
        paths=inbox_paths(paths),
    )
    if excel_status == EXCEL_WRITTEN:
        update_confirmation_result(inbox_id, "CONFIRMED", "", inbox_paths(paths))
        message = f"确认完成：已进入资产库；{excel_result.get('message', 'Excel 已写入')}"
        outcome = action
        success = True
    elif excel_status == EXCEL_CONFLICT:
        update_confirmation_result(inbox_id, "PARTIAL", error, inbox_paths(paths))
        message = "资产库已成功，Excel 数据冲突，未覆盖原值。"
        outcome = "conflict"
        success = False
    else:
        update_confirmation_result(inbox_id, "PARTIAL", error, inbox_paths(paths))
        message = f"资产库已成功，Excel {excel_status_label(excel_status)}：{error}"
        outcome = "failed"
        success = False
    return {
        **repository_result,
        **excel_result,
        "success": success,
        "message": message,
        "batch_outcome": outcome,
        "repository_status": "WRITTEN",
    }


def confirm_inbox_batch_sequential(inbox_ids: list[str], paths: OperatorUiPaths, *, operator: str) -> dict[str, Any]:
    """Confirm a selection with one Repository commit and one Inbox commit."""
    selected_ids = list(dict.fromkeys(str(value) for value in inbox_ids if str(value)))
    inbox_items = load_inbox_items(paths.acquisition_inbox)
    items_by_id = {str(item.get("inbox_id") or ""): item for item in inbox_items}
    projects = load_relation_json_array(paths.projects, "projects")
    documents = load_relation_json_array(paths.documents, "documents")
    links = load_relation_json_array(paths.links, "project document links")
    repository_audit = load_relation_json_array(paths.repository_audit, "repository audit")
    review_queue = load_review_queue(paths.review_queue) if paths.review_queue.exists() else []
    review_decisions = load_review_decisions(paths.review_decisions)
    repository_results: dict[str, dict[str, Any]] = {}
    direct_results: dict[str, dict[str, Any]] = {}
    repository_dirty = False

    summary: dict[str, Any] = {
        "selected_count": len(selected_ids),
        "repository_success_count": 0,
        "completed_skip_count": 0,
        "success_count": 0,
        "already_intaken_count": 0,
        "ineligible_count": 0,
        "failure_count": 0,
        "results": [],
    }

    for inbox_id in selected_ids:
        item = items_by_id.get(inbox_id)
        file_name = inbox_item_file_name(item or {})
        if item is None:
            direct_results[inbox_id] = {
                "success": False, "message": "任务不存在。", "batch_outcome": "failed",
                "repository_status": "FAILED",
            }
            continue
        if not is_operator_visible(item):
            direct_results[inbox_id] = {
                "success": False, "message": "隐藏或重复任务不参与批量确认。", "batch_outcome": "ineligible",
                "repository_status": "FAILED",
            }
            continue
        if str(item.get("status") or "") != "COMPLETED":
            reason = "仅处理成功的任务可以确认。"
            set_confirmation_fields(item, "INELIGIBLE", reason)
            direct_results[inbox_id] = {
                "success": False, "message": reason, "batch_outcome": "ineligible",
                "repository_status": "FAILED",
            }
            continue
        eligibility = resolve_inbox_confirmation_eligibility(item, paths)
        if eligibility["status"] != "eligible":
            reason = str(eligibility.get("block_reason") or "URL 正文质量门禁未通过。")
            set_confirmation_fields(item, "INELIGIBLE", reason)
            direct_results[inbox_id] = {
                "success": False, "message": reason, "batch_outcome": "ineligible",
                "repository_status": "PENDING",
            }
            continue
        if (
            str(item.get("task_role") or "") == "xlsx_workbook_summary"
            or not any(str(value) for value in item.get("generated_asset_ids", []))
        ):
            reason = "工作簿摘要不可确认；请选择对应的 Excel 行级任务。"
            set_confirmation_fields(item, "INELIGIBLE", reason)
            direct_results[inbox_id] = {
                "success": False, "message": reason, "batch_outcome": "ineligible",
                "repository_status": "FAILED",
            }
            continue

        asset_id = next(str(value) for value in item.get("generated_asset_ids", []) if str(value))
        extracted = confirmable_processing_payload(item)
        try:
            existing = existing_intake_result_from_rows(asset_id, documents, links)
            if existing:
                if confirm_project_in_rows(str(existing.get("project_id") or ""), projects):
                    repository_dirty = True
                _detail_id, detail_changed = merge_award_detail_in_rows(
                    str(existing.get("project_id") or ""), extracted, projects
                )
                repository_dirty = repository_dirty or detail_changed
                set_business_sync_fields(item, repository_status="WRITTEN", repository_error="")
                repository_results[inbox_id] = existing
                set_confirmation_fields(item, "CONFIRMED", "")
                direct_results[inbox_id] = {
                    **existing,
                    "success": True,
                    "message": "资产库已完成，未重复执行。",
                    "batch_outcome": "completed_skip",
                    "repository_status": "WRITTEN",
                }
                continue
        except Exception as exc:
            reason = str(exc)
            set_business_sync_fields(
                item,
                repository_status="FAILED",
                repository_error=reason,
            )
            set_confirmation_fields(item, "FAILED", reason)
            direct_results[inbox_id] = {
                "success": False, "message": reason, "batch_outcome": "failed",
                "repository_status": "FAILED",
            }
            continue

        lengths = (len(review_decisions), len(projects), len(documents), len(links), len(repository_audit))
        try:
            resolution = resolve_project_match(
                extracted, build_project_match_index(projects)
            )
            matches = list(resolution["matches"])
            if resolution["status"] == "manual":
                reason = "项目关键字段冲突或存在多个可能项目，未自动入库。"
                set_business_sync_fields(
                    item,
                    repository_status="FAILED",
                    repository_error=reason,
                )
                set_confirmation_fields(item, "FAILED", reason)
                direct_results[inbox_id] = {
                    "success": False, "message": reason, "batch_outcome": "failed",
                    "repository_status": "FAILED",
                }
                continue
            project_id = str(matches[0].get("project_id") or "") if matches else ""
            if not project_id:
                project_decision = batch_accept_decision(asset_id, "", review_queue, operator, item)
                review_decisions.append(project_decision)
                project_intake, _project_intake_audit = build_asset_intake(
                    [project_decision], review_queue, operator=operator
                )
                _unused_documents, new_projects, new_audit = build_asset_repository(project_intake)
                projects, added_projects = merge_repository_rows(projects, new_projects, "project_id")
                repository_audit, _added_project_audit = merge_repository_rows(
                    repository_audit, new_audit, "repository_id"
                )
                project = next(
                    (row for row in added_projects if str(row.get("asset_id") or "") == asset_id), None
                )
                if project is None:
                    raise ValueError("ProjectEntity was not created")
                project_id = str(project.get("project_id") or "")

            detail_id, detail_changed = merge_award_detail_in_rows(
                project_id, extracted, projects
            )

            document_decision = batch_accept_decision(asset_id, project_id, review_queue, operator, item)
            review_decisions.append(document_decision)
            document_intake, _document_intake_audit = build_asset_intake(
                [document_decision], review_queue, operator=operator
            )
            new_documents, _unused_projects, new_audit = build_asset_repository(document_intake)
            documents, added_documents = merge_repository_rows(documents, new_documents, "document_id")
            repository_audit, _added_document_audit = merge_repository_rows(
                repository_audit, new_audit, "repository_id"
            )
            document = next(
                (row for row in documents if str(row.get("asset_id") or "") == asset_id), None
            )
            if document is None:
                raise ValueError("DocumentEntity was not created")
            document_id = str(document.get("document_id") or "")
            relation_type = relation_type_for(str((item.get("processing_result") or {}).get("doc_type") or ""))
            existing_link = find_existing_link(links, project_id, document_id, relation_type)
            relation_status = "existing"
            if existing_link is None:
                links.append(create_link(
                    links,
                    projects,
                    documents,
                    project_id=project_id,
                    document_id=document_id,
                    relation_type=relation_type,
                    source="phase35_operator_ui",
                ))
                relation_status = "created"
            confirm_project_in_rows(project_id, projects)
            result = {
                "success": True,
                "message": "确认入库成功。",
                "project_id": project_id,
                "document_id": document_id,
                "relation_status": relation_status,
                "award_detail_id": detail_id,
            }
            repository_results[inbox_id] = result
            set_business_sync_fields(item, repository_status="WRITTEN", repository_error="")
            set_confirmation_fields(item, "CONFIRMED", "")
            direct_results[inbox_id] = {
                **result,
                "success": True,
                "message": "确认完成：已进入资产库。",
                "batch_outcome": "written",
                "repository_status": "WRITTEN",
            }
            repository_dirty = True
        except Exception as exc:
            review_decisions[:] = review_decisions[:lengths[0]]
            projects[:] = projects[:lengths[1]]
            documents[:] = documents[:lengths[2]]
            links[:] = links[:lengths[3]]
            repository_audit[:] = repository_audit[:lengths[4]]
            reason = str(exc)
            set_business_sync_fields(
                item,
                repository_status="FAILED",
                repository_error=reason,
            )
            set_confirmation_fields(item, "FAILED", reason)
            direct_results[inbox_id] = {
                "success": False, "message": reason, "batch_outcome": "failed",
                "repository_status": "FAILED",
            }

    if repository_dirty:
        final_intake, final_intake_audit = build_asset_intake(review_decisions, review_queue, operator=operator)
        write_repository_json(paths.review_decisions, review_decisions)
        write_intake_outputs(final_intake, final_intake_audit, paths.intake_output, paths.intake_audit)
        write_repository_outputs(
            documents,
            projects,
            repository_audit,
            documents_path=paths.documents,
            projects_path=paths.projects,
            audit_path=paths.repository_audit,
        )
        write_links(links, paths.links)
        write_repository_json(paths.workflow_result, {
            "action": "confirm-inbox-batch",
            "operator": operator,
            "selected_count": len(selected_ids),
            "repository_success_count": len(repository_results),
        })

    write_inbox(inbox_items, inbox_paths(paths))
    for inbox_id in selected_ids:
        item = items_by_id.get(inbox_id)
        file_name = inbox_item_file_name(item or {})
        result = direct_results.get(inbox_id) or {
            "success": False, "message": "任务未处理。", "batch_outcome": "failed",
            "repository_status": "FAILED",
        }
        add_batch_result(summary, file_name, result)
    return summary


def confirm_inbox_batch(inbox_ids: list[str], paths: OperatorUiPaths, *, operator: str) -> dict[str, Any]:
    """Use indexed two-stage orchestration for structured XLSX uploads."""
    selected_ids = list(dict.fromkeys(str(value) for value in inbox_ids if str(value)))
    inbox_items = load_inbox_items(paths.acquisition_inbox)
    items_by_id = {str(item.get("inbox_id") or ""): item for item in inbox_items}
    eligible_new_items = [
        item for inbox_id in selected_ids
        if (item := items_by_id.get(inbox_id)) is not None
        and is_operator_visible(item)
        and str(item.get("status") or "") == "COMPLETED"
        and str(item.get("task_role") or "") != "xlsx_workbook_summary"
        and any(str(value) for value in item.get("generated_asset_ids", []))
        and str(item.get("repository_status") or "PENDING") != "WRITTEN"
    ]
    if any(not business_group_id(dict(item.get("processing_result") or {})) for item in eligible_new_items):
        return confirm_inbox_batch_sequential(selected_ids, paths, operator=operator)
    return confirm_inbox_batch_indexed_xlsx(
        selected_ids, paths, operator=operator, inbox_items=inbox_items, items_by_id=items_by_id
    )


def confirm_inbox_batch_indexed_xlsx(
    selected_ids: list[str],
    paths: OperatorUiPaths,
    *,
    operator: str,
    inbox_items: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    projects = load_relation_json_array(paths.projects, "projects")
    documents = load_relation_json_array(paths.documents, "documents")
    links = load_relation_json_array(paths.links, "project document links")
    repository_audit = load_relation_json_array(paths.repository_audit, "repository audit")
    review_queue = load_review_queue(paths.review_queue) if paths.review_queue.exists() else []
    review_decisions = load_review_decisions(paths.review_decisions)

    project_positions = {
        str(project.get("project_id") or ""): index
        for index, project in enumerate(projects)
        if str(project.get("project_id") or "")
    }
    project_match_index = build_project_match_index(projects)
    documents_by_asset = {
        str(document.get("asset_id") or ""): document
        for document in documents if str(document.get("asset_id") or "")
    }
    documents_by_id = {
        str(document.get("document_id") or ""): document
        for document in documents if str(document.get("document_id") or "")
    }
    links_by_document = {
        str(link.get("document_id") or ""): link
        for link in links if str(link.get("document_id") or "")
    }
    link_keys = {
        (
            str(link.get("project_id") or ""),
            str(link.get("document_id") or ""),
            str(link.get("relation_type") or ""),
        )
        for link in links
    }
    project_ids = set(project_positions)
    document_ids = set(documents_by_id)
    repository_ids = {
        str(row.get("repository_id") or "") for row in repository_audit if str(row.get("repository_id") or "")
    }
    queue_by_asset = {
        str(row.get("asset_id") or ""): row for row in review_queue if str(row.get("asset_id") or "")
    }

    repository_results: dict[str, dict[str, Any]] = {}
    direct_results: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    pending_project_decisions: list[dict[str, Any]] = []
    repository_dirty = False
    summary: dict[str, Any] = {
        "selected_count": len(selected_ids), "repository_success_count": 0,
        "completed_skip_count": 0, "success_count": 0,
        "already_intaken_count": 0, "ineligible_count": 0, "failure_count": 0, "results": [],
    }

    for inbox_id in selected_ids:
        item = items_by_id.get(inbox_id)
        if item is None:
            direct_results[inbox_id] = batch_failure("任务不存在。")
            continue
        if not is_operator_visible(item):
            direct_results[inbox_id] = batch_failure("隐藏或重复任务不参与批量确认。", "ineligible")
            continue
        if str(item.get("status") or "") != "COMPLETED":
            reason = "仅处理成功的任务可以确认。"
            set_confirmation_fields(item, "INELIGIBLE", reason)
            direct_results[inbox_id] = batch_failure(reason, "ineligible")
            continue
        eligibility = resolve_inbox_confirmation_eligibility(item, paths)
        if eligibility["status"] != "eligible":
            reason = str(eligibility.get("block_reason") or "URL 正文质量门禁未通过。")
            set_confirmation_fields(item, "INELIGIBLE", reason)
            direct_results[inbox_id] = {
                **batch_failure(reason, "ineligible"),
                "repository_status": "PENDING",
            }
            continue
        if str(item.get("task_role") or "") == "xlsx_workbook_summary" or not any(
            str(value) for value in item.get("generated_asset_ids", [])
        ):
            reason = "工作簿摘要不可确认；请选择对应的 Excel 行级任务。"
            set_confirmation_fields(item, "INELIGIBLE", reason)
            direct_results[inbox_id] = batch_failure(reason, "ineligible")
            continue

        asset_id = next(str(value) for value in item.get("generated_asset_ids", []) if str(value))
        extracted = confirmable_processing_payload(item)
        existing_document = documents_by_asset.get(asset_id)
        if existing_document is not None:
            document_id = str(existing_document.get("document_id") or "")
            link = links_by_document.get(document_id)
            existing = {
                "project_id": str((link or {}).get("project_id") or existing_document.get("project_id") or ""),
                "document_id": document_id,
                "relation_status": "existing",
            }
            project_id = str(existing["project_id"])
            repository_dirty = indexed_confirm_project(project_id, projects, project_positions) or repository_dirty
            _detail_id, detail_changed = indexed_merge_award_detail(
                project_id, extracted, projects, project_positions
            )
            repository_dirty = repository_dirty or detail_changed
            set_business_sync_fields(item, repository_status="WRITTEN", repository_error="")
            repository_results[inbox_id] = existing
            set_confirmation_fields(item, "CONFIRMED", "")
            direct_results[inbox_id] = {
                **existing, "success": True, "message": "资产库已完成，未重复执行。",
                "batch_outcome": "completed_skip", "repository_status": "WRITTEN",
            }
            continue

        resolution = resolve_project_match(extracted, project_match_index)
        matches = list(resolution["matches"])
        if resolution["status"] == "manual":
            reason = "项目关键字段冲突或存在多个可能项目，未自动入库。"
            set_business_sync_fields(
                item, repository_status="FAILED", repository_error=reason,
            )
            set_confirmation_fields(item, "FAILED", reason)
            direct_results[inbox_id] = batch_failure(reason)
            continue
        matched_project = matches[0] if matches else {}
        project_id = str(matched_project.get("project_id") or "")
        new_project_asset = str(matched_project.get("_pending_project_asset") or "")
        if not project_id and not new_project_asset:
            queue_item = queue_by_asset.get(asset_id)
            if queue_item is None:
                reason = f"Asset not found in review queue: {asset_id}"
                set_confirmation_fields(item, "FAILED", reason)
                direct_results[inbox_id] = batch_failure(reason)
                continue
            project_decision = indexed_accept_decision(asset_id, "", queue_item, operator, item)
            pending_project_decisions.append(project_decision)
            new_project_asset = asset_id
            add_project_to_match_index(
                project_match_index,
                pending_project_match_record(asset_id, extracted),
            )
        pending.append({
            "inbox_id": inbox_id, "item": item, "asset_id": asset_id,
            "project_id": project_id, "new_project_asset": new_project_asset,
        })

    new_intake_items: list[dict[str, Any]] = []
    new_intake_audit: list[dict[str, Any]] = []
    if pending_project_decisions:
        review_decisions.extend(pending_project_decisions)
        project_intake, project_intake_audit = build_asset_intake(
            pending_project_decisions, review_queue, operator=operator
        )
        new_intake_items.extend(project_intake)
        new_intake_audit.extend(project_intake_audit)
        _unused_documents, new_projects, new_project_audit = build_asset_repository(project_intake)
        append_unique_rows(projects, new_projects, "project_id", project_ids)
        append_unique_rows(repository_audit, new_project_audit, "repository_id", repository_ids)
        project_positions = {
            str(project.get("project_id") or ""): index
            for index, project in enumerate(projects) if str(project.get("project_id") or "")
        }
        new_project_ids_by_asset = {
            str(project.get("asset_id") or ""): str(project.get("project_id") or "")
            for project in new_projects
            if str(project.get("asset_id") or "") and str(project.get("project_id") or "")
        }
    else:
        new_project_ids_by_asset = {}

    document_decisions: list[dict[str, Any]] = []
    active_pending: list[dict[str, Any]] = []
    for plan in pending:
        project_id = str(plan.get("project_id") or "")
        if not project_id:
            project_id = new_project_ids_by_asset.get(str(plan.get("new_project_asset") or ""), "")
            if not project_id:
                reason = "ProjectEntity was not created"
                item = dict(plan["item"])
                set_confirmation_fields(item, "FAILED", reason)
                direct_results[str(plan["inbox_id"])] = batch_failure(reason)
                continue
            plan["project_id"] = project_id
        queue_item = queue_by_asset.get(str(plan["asset_id"]))
        if queue_item is None:
            direct_results[str(plan["inbox_id"])] = batch_failure(
                f"Asset not found in review queue: {plan['asset_id']}"
            )
            continue
        document_decisions.append(indexed_accept_decision(
            str(plan["asset_id"]), project_id, queue_item, operator, dict(plan["item"])
        ))
        active_pending.append(plan)

    if document_decisions:
        review_decisions.extend(document_decisions)
        document_intake, document_intake_audit = build_asset_intake(
            document_decisions, review_queue, operator=operator
        )
        new_intake_items.extend(document_intake)
        new_intake_audit.extend(document_intake_audit)
        new_documents, _unused_projects, new_document_audit = build_asset_repository(document_intake)
        append_unique_rows(documents, new_documents, "document_id", document_ids)
        append_unique_rows(repository_audit, new_document_audit, "repository_id", repository_ids)
        documents_by_asset.update({
            str(document.get("asset_id") or ""): document
            for document in new_documents if str(document.get("asset_id") or "")
        })
        documents_by_id.update({
            str(document.get("document_id") or ""): document
            for document in new_documents if str(document.get("document_id") or "")
        })

    for plan in active_pending:
        inbox_id = str(plan["inbox_id"])
        item = plan["item"]
        project_id = str(plan["project_id"])
        asset_id = str(plan["asset_id"])
        document = documents_by_asset.get(asset_id)
        if document is None:
            direct_results[inbox_id] = batch_failure("DocumentEntity was not created")
            continue
        document_id = str(document.get("document_id") or "")
        detail_id, detail_changed = indexed_merge_award_detail(
            project_id, confirmable_processing_payload(item), projects, project_positions
        )
        repository_dirty = repository_dirty or detail_changed
        relation_type = relation_type_for(str((item.get("processing_result") or {}).get("doc_type") or ""))
        link_key = (project_id, document_id, relation_type)
        relation_status = "existing"
        if link_key not in link_keys:
            project = projects[project_positions[project_id]]
            links.append(create_link(
                [], [project], [document], project_id=project_id, document_id=document_id,
                relation_type=relation_type, source="phase35_operator_ui",
            ))
            link_keys.add(link_key)
            links_by_document[document_id] = links[-1]
            relation_status = "created"
        repository_dirty = indexed_confirm_project(project_id, projects, project_positions) or repository_dirty
        result = {
            "success": True, "message": "确认入库成功。", "project_id": project_id,
            "document_id": document_id, "relation_status": relation_status, "award_detail_id": detail_id,
        }
        repository_results[inbox_id] = result
        set_business_sync_fields(item, repository_status="WRITTEN", repository_error="")
        set_confirmation_fields(item, "CONFIRMED", "")
        direct_results[inbox_id] = {
            **result, "success": True, "message": "确认完成：已进入资产库。",
            "batch_outcome": "written", "repository_status": "WRITTEN",
        }
        repository_dirty = True

    if repository_dirty:
        final_intake, final_intake_audit = build_asset_intake(review_decisions, review_queue, operator=operator)
        write_repository_json(paths.review_decisions, review_decisions)
        write_intake_outputs(final_intake, final_intake_audit, paths.intake_output, paths.intake_audit)
        write_repository_outputs(
            documents, projects, repository_audit,
            documents_path=paths.documents, projects_path=paths.projects, audit_path=paths.repository_audit,
        )
        write_links(links, paths.links)
        write_repository_json(paths.workflow_result, {
            "action": "confirm-inbox-batch", "operator": operator,
            "selected_count": len(selected_ids), "repository_success_count": len(repository_results),
        })

    write_inbox(inbox_items, inbox_paths(paths))
    for inbox_id in selected_ids:
        result = direct_results.get(inbox_id) or batch_failure("任务未处理。")
        add_batch_result(summary, inbox_item_file_name(items_by_id.get(inbox_id) or {}), result)
    return summary


def append_unique_rows(
    target: list[dict[str, Any]], incoming: list[dict[str, Any]], id_key: str, known_ids: set[str]
) -> None:
    for row in incoming:
        identifier = str(row.get(id_key) or "")
        if not identifier or identifier in known_ids:
            continue
        target.append(dict(row))
        known_ids.add(identifier)


def indexed_accept_decision(
    asset_id: str, project_id: str, queue_item: dict[str, Any], operator: str,
    inbox_item: dict[str, Any],
) -> dict[str, Any]:
    queue_item = _sanitized_review_queue_item(queue_item, inbox_item)
    return create_review_decision(
        [queue_item], asset_id, "ACCEPT", "用户在资料详情中确认入库。",
        reviewer=operator, related_project_id=project_id,
        review_time=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    )


def indexed_confirm_project(
    project_id: str, projects: list[dict[str, Any]], positions: dict[str, int]
) -> bool:
    index = positions.get(project_id)
    if index is None or str(projects[index].get("status") or "").lower() == CONFIRMED:
        return False
    projects[index] = update_entity_status(projects[index], CONFIRMED)
    return True


def indexed_merge_award_detail(
    project_id: str, extracted: dict[str, Any], projects: list[dict[str, Any]], positions: dict[str, int]
) -> tuple[str, bool]:
    detail_id = award_detail_id(extracted)
    index = positions.get(project_id)
    if index is None or not detail_id:
        return detail_id, False
    updated, added = merge_project_award_detail(projects[index], extracted)
    if added is None:
        return detail_id, False
    projects[index] = updated
    return detail_id, True


def indexed_excel_entry(
    inbox_id: str, item: dict[str, Any], repository_result: dict[str, Any],
    projects: list[dict[str, Any]], positions: dict[str, int],
) -> dict[str, Any]:
    project_id = str(repository_result.get("project_id") or "")
    index = positions.get(project_id)
    project = projects[index] if index is not None else {}
    return {
        "inbox_id": inbox_id, "extracted": confirmable_processing_payload(item),
        "project": project, "source_file": str(item.get("source_file") or ""),
        "source_url": str(item.get("source_url") or ""),
        "confirmed_time": str(item.get("confirm_time") or "")
        or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def finalize_batch_excel(
    excel_entries: list[dict[str, Any]], items_by_id: dict[str, dict[str, Any]],
    repository_results: dict[str, dict[str, Any]], direct_results: dict[str, dict[str, Any]],
    paths: OperatorUiPaths,
) -> None:
    if not excel_entries:
        return
    try:
        excel_results = sync_confirmed_projects_batch(
            excel_entries, paths=excel_sync_paths(paths), batch_id=f"confirm_batch_{uuid.uuid4().hex}"
        )
    except Exception as exc:
        excel_results = {
            str(entry.get("inbox_id") or ""): {
                "excel_status": EXCEL_FAILED, "excel_action": "", "message": str(exc),
            }
            for entry in excel_entries
        }
    for entry in excel_entries:
        inbox_id = str(entry.get("inbox_id") or "")
        item = items_by_id[inbox_id]
        repository_result = repository_results[inbox_id]
        excel_result = dict(excel_results.get(inbox_id) or {})
        excel_status = str(excel_result.get("excel_status") or EXCEL_FAILED)
        action = str(excel_result.get("excel_action") or "")
        error = "" if excel_status == EXCEL_WRITTEN else str(excel_result.get("message") or "")
        set_business_sync_fields(
            item, excel_status=excel_status, excel_error=error, excel_action=action,
            excel_conflicts=[dict(row) for row in excel_result.get("conflicts", []) if isinstance(row, dict)],
        )
        if excel_status == EXCEL_WRITTEN:
            set_confirmation_fields(item, "CONFIRMED", "")
            direct_results[inbox_id] = {
                **repository_result, **excel_result, "success": True,
                "message": f"确认完成：已进入资产库；{excel_result.get('message', 'Excel 已写入')}",
                "batch_outcome": action, "repository_status": "WRITTEN",
            }
        elif excel_status == EXCEL_CONFLICT:
            set_confirmation_fields(item, "PARTIAL", error)
            direct_results[inbox_id] = {
                **repository_result, **excel_result, "success": False,
                "message": "资产库已成功，Excel 数据冲突，未覆盖原值。",
                "batch_outcome": "conflict", "repository_status": "WRITTEN",
            }
        else:
            set_confirmation_fields(item, "PARTIAL", error)
            direct_results[inbox_id] = {
                **repository_result, **excel_result, "success": False,
                "message": f"资产库已成功，Excel {excel_status_label(excel_status)}：{error}",
                "batch_outcome": "failed", "repository_status": "WRITTEN",
            }


def batch_failure(reason: str, outcome: str = "failed") -> dict[str, Any]:
    return {
        "success": False, "message": reason, "batch_outcome": outcome,
        "repository_status": "FAILED",
    }


def existing_intake_result_from_rows(
    asset_id: str,
    documents: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve a persisted intake result from the batch's in-memory snapshot."""
    document = next(
        (row for row in documents if str(row.get("asset_id") or "") == asset_id),
        None,
    )
    if document is None:
        return None
    document_id = str(document.get("document_id") or "")
    link = next(
        (row for row in links if str(row.get("document_id") or "") == document_id),
        None,
    )
    return {
        "project_id": str((link or {}).get("project_id") or document.get("project_id") or ""),
        "document_id": document_id,
        "relation_status": "existing",
    }


def confirm_project_in_rows(project_id: str, projects: list[dict[str, Any]]) -> bool:
    """Apply the existing confirmed-state rule without writing the JSON file yet."""
    if not project_id:
        return False
    for index, project in enumerate(projects):
        if str(project.get("project_id") or "") != project_id:
            continue
        if str(project.get("status") or "").lower() == CONFIRMED:
            return False
        projects[index] = update_entity_status(project, CONFIRMED)
        return True
    return False


def merge_award_detail_in_rows(
    project_id: str,
    extracted: dict[str, Any],
    projects: list[dict[str, Any]],
) -> tuple[str, bool]:
    """Merge one XLSX physical row into its project without writing Repository JSON."""
    detail_id = award_detail_id(extracted)
    if not project_id or not detail_id:
        return detail_id, False
    for index, project in enumerate(projects):
        if str(project.get("project_id") or "") != project_id:
            continue
        updated, added = merge_project_award_detail(project, extracted)
        if added is None:
            return detail_id, False
        projects[index] = updated
        return detail_id, True
    return detail_id, False


def merge_award_detail_in_repository(project_id: str, extracted: dict[str, Any], projects_path: Path) -> str:
    projects = load_relation_json_array(projects_path, "projects")
    detail_id, changed = merge_award_detail_in_rows(project_id, extracted, projects)
    if changed:
        write_repository_json(projects_path, projects)
    return detail_id


def set_business_sync_fields(
    item: dict[str, Any],
    *,
    repository_status: str | None = None,
    repository_error: str | None = None,
    excel_status: str | None = None,
    excel_error: str | None = None,
    excel_action: str | None = None,
    excel_conflicts: list[dict[str, Any]] | None = None,
) -> None:
    """Mirror update_business_sync_result while deferring the shared Inbox write."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if repository_status is not None:
        item["repository_status"] = repository_status
        item["repository_time"] = timestamp
    if repository_error is not None:
        item["repository_error"] = repository_error
    if excel_status is not None:
        item["excel_status"] = excel_status
        item["excel_time"] = timestamp
    if excel_error is not None:
        item["excel_error"] = excel_error
    if excel_action is not None:
        item["excel_action"] = excel_action
    if excel_conflicts is not None:
        item["excel_conflicts"] = [dict(row) for row in excel_conflicts]


def set_confirmation_fields(item: dict[str, Any], status: str, reason: str = "") -> None:
    """Mirror update_confirmation_result while deferring the shared Inbox write."""
    item["confirm_status"] = status
    item["confirm_failure_stage"] = "confirm" if status == "FAILED" else ""
    item["confirm_failure_reason"] = reason
    item["confirm_time"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def batch_accept_decision(
    asset_id: str,
    project_id: str,
    review_queue: list[dict[str, Any]],
    operator: str,
    inbox_item: dict[str, Any],
) -> dict[str, Any]:
    review_queue = [
        _sanitized_review_queue_item(item, inbox_item)
        if str(item.get("asset_id") or "") == asset_id else _sanitized_review_queue_item(item)
        for item in review_queue
    ]
    return create_review_decision(
        review_queue,
        asset_id,
        "ACCEPT",
        "用户在资料详情中确认入库。",
        reviewer=operator,
        related_project_id=project_id,
        review_time=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    )


def batch_excel_entry(
    inbox_id: str,
    item: dict[str, Any],
    repository_result: dict[str, Any],
    projects: list[dict[str, Any]],
) -> dict[str, Any]:
    project_id = str(repository_result.get("project_id") or "")
    project = next(
        (row for row in projects if str(row.get("project_id") or "") == project_id),
        {},
    )
    return {
        "inbox_id": inbox_id,
        "extracted": confirmable_processing_payload(item),
        "project": project,
        "source_file": str(item.get("source_file") or ""),
        "source_url": str(item.get("source_url") or ""),
        "confirmed_time": str(item.get("confirm_time") or "")
        or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def add_batch_result(summary: dict[str, Any], file_name: str, result: dict[str, Any]) -> None:
    """Summarize Repository outcomes only."""
    outcome = str(result.get("batch_outcome") or "")
    repository_status = str(
        result.get("repository_status") or ("WRITTEN" if result.get("success") else "FAILED")
    )
    if repository_status == "WRITTEN":
        summary["repository_success_count"] += 1
    if outcome in {"completed_skip", "already_intaken"}:
        summary["completed_skip_count"] += 1
        summary["already_intaken_count"] += 1
    elif result.get("success"):
        summary["success_count"] += 1
    else:
        if outcome == "ineligible":
            summary["ineligible_count"] += 1
        summary["failure_count"] += 1
    summary["results"].append(batch_result_row(
        file_name,
        "待核实" if outcome == "ineligible" else repository_status_label(repository_status),
        str(result.get("message") or ""),
        str(result.get("project_id") or ""),
    ))


def batch_result_row(file_name: str, repository_label: str, message: str, project_id: str = "") -> dict[str, str]:
    return {
        "file_name": file_name,
        "repository_label": repository_label,
        "message": message,
        "project_id": project_id,
    }


def inbox_item_file_name(item: dict[str, Any]) -> str:
    processing = dict(item.get("processing_result") or {})
    source_file = str(item.get("source_file") or "")
    return str(processing.get("file_name") or Path(source_file).name or item.get("source_url") or "未识别")


def _bounded_review_value(value: Any, limit: int = 192) -> str:
    try:
        text = str(value or "")
    except Exception:
        return "不可用"
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _review_section_positions(
    integrity: dict[str, Any], field: str
) -> str:
    positions: set[int] = set()
    for issue in integrity.get("quality_issues") or []:
        if not isinstance(issue, dict) or str(issue.get("field") or "") != field:
            continue
        raw_index = issue.get("section_index")
        if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
            positions.add(raw_index)
    if not positions:
        return "不可用"
    return "、".join(f"第{index}节" for index in sorted(positions)[:4])


def _manual_review_fields(item: dict[str, Any]) -> list[tuple[str, str]]:
    processing = dict(item.get("processing_result") or {})
    completeness = dict(processing.get("field_completeness") or {})
    integrity = dict(processing.get("field_integrity") or {})
    missing = {
        str(field) for field in completeness.get("required_missing_fields") or []
        if str(field) in _MANUAL_REVIEW_FIELD_LABELS
    }
    suspect = {
        str(field) for field in integrity.get("suspect_fields") or []
        if str(field) in _MANUAL_REVIEW_FIELD_LABELS
    }
    if not suspect:
        suspect.update(
            str(field) for field in completeness.get("suspect_fields") or []
            if str(field) in _MANUAL_REVIEW_FIELD_LABELS
        )
    issues_by_field: dict[str, list[str]] = {}
    for issue in integrity.get("quality_issues") or []:
        if not isinstance(issue, dict):
            continue
        field = str(issue.get("field") or "")
        if field not in _MANUAL_REVIEW_FIELD_LABELS:
            continue
        code = str(issue.get("code") or "field_integrity")
        issues_by_field.setdefault(field, [])
        if code not in issues_by_field[field]:
            issues_by_field[field].append(code)
    for field in missing:
        if "field_missing" not in issues_by_field.setdefault(field, []):
            issues_by_field[field].insert(0, "field_missing")

    rows: list[tuple[str, str]] = []
    for field, label in _MANUAL_REVIEW_FIELD_LABELS.items():
        if field not in missing and field not in suspect:
            continue
        codes = issues_by_field.get(field) or ["field_integrity"]
        reasons = [
            f"{code}（{_MANUAL_REVIEW_ISSUE_LABELS.get(code, '需人工核验')}）"
            for code in codes[:3]
        ]
        current = _bounded_review_value(processing.get(field)) or "空"
        state = "字段缺失" if field in missing else "字段证据待核验"
        rows.append((
            f"人工检查：{label}",
            f"{state}；当前值：{current}；原因：{'；'.join(reasons)}；"
            f"证据位置：{_review_section_positions(integrity, field)}",
        ))
    return rows


def inbox_task_fields(item: dict[str, Any], paths: OperatorUiPaths | None = None) -> list[tuple[str, Any]]:
    """Return the exact compact processing-detail rows."""
    processing = dict(item.get("processing_result") or {})
    source_file = str(item.get("source_file") or "")
    attachments = dict(processing.get("attachments") or {})
    attachment_items = [row for row in attachments.get("items") or [] if isinstance(row, dict)]
    total_count = int(attachments.get("total_count") or len(attachment_items))
    attachment_result = (
        f"成功 {attachments.get('success_count', 0)} 个；"
        f"失败 {attachments.get('failed_count', 0)} 个；"
        f"跳过 {attachments.get('skipped_count', 0)} 个"
    ) if total_count else "无附件"
    attempts = item.get("attempt_history")
    return [
        ("任务类型", inbox_task_type_label(item)),
        ("来源文件或 URL", str(
            processing.get("file_name") or Path(source_file).name or item.get("source_url") or ""
        )),
        ("文件类型", str(processing.get("file_type") or Path(source_file).suffix)),
        ("创建时间", browser_local_time(str(item.get("created_time") or ""))),
        ("处理状态", processing_status_label(str(item.get("status") or ""))),
        ("失败原因", localized_error_message(str(item.get("error_message") or ""))
         if item.get("error_message") else ""),
        ("附件总数", str(total_count)),
        ("附件处理结果", attachment_result),
        ("历史采集记录", f"{len(attempts)}次" if isinstance(attempts, list) else "0次"),
    ]


def inbox_extraction_fields(item: dict[str, Any]) -> list[tuple[str, str]]:
    """Return the exact eight user-facing extraction fields."""
    row = dict(item.get("processing_result") or {})
    return [
        ("客户名称", str(row.get("customer") or "")),
        ("项目编号", validated_project_number(row.get("project_number"))),
        ("项目名称", str(row.get("project_name") or "")),
        ("项目内容", str(row.get("content") or "")),
        ("预算金额", str(row.get("budget") or "")),
        ("开标时间", str(row.get("bid_open_time") or "")),
        ("中标厂商", str(row.get("winner") or "")),
        ("中标金额", str(row.get("award_amount") or "")),
    ]

def inbox_confirmation_fields(item: dict[str, Any], paths: OperatorUiPaths) -> list[tuple[str, Any]]:
    intake_result = inbox_intake_result(item, paths)
    repository_status = str(item.get("repository_status") or ("WRITTEN" if intake_result else "PENDING"))
    excel_status = str(item.get("excel_status") or EXCEL_NOT_WRITTEN)
    action = str(item.get("excel_action") or "")
    detail = str(item.get("excel_error") or item.get("repository_error") or "")
    eligibility = resolve_inbox_confirmation_eligibility(item, paths)
    fields = [
        ("资产库状态", derived_repository_status_label(item, eligibility, repository_status)),
        ("Excel 状态", excel_status_label(excel_status)),
        ("Excel 操作", {"insert": "新增", "update": "更新", "unchanged": "数据未变化", "conflict": "冲突"}.get(action, "—")),
    ]
    candidate_ids = [str(value) for value in item.get("generated_asset_ids", []) if str(value)]
    if str(item.get("source_type") or "") == "url" and eligibility["status"] != "eligible" and candidate_ids:
        fields.append((
            "历史候选记录",
            f"保留 {len(candidate_ids)} 条（底层资产库状态：{repository_status_label(repository_status)}；当前不可确认）",
        ))
    if item.get("confirm_time"):
        fields.append(("确认时间", browser_local_time(str(item.get("confirm_time") or ""))))
    if detail:
        fields.append(("说明", detail))
    return fields


def browser_local_time(value: str) -> dict[str, Any] | str:
    return {"is_local_time": True, "utc_time": value} if value else ""


def _valid_manual_remediation_state(item: dict[str, Any]) -> dict[str, Any]:
    if not is_manual_remediation_target(item):
        return {
            "effective_fields": {}, "resolved_missing": set(), "resolved_suspect": set(),
            "latest_actions": {},
        }
    remediation = item.get("manual_remediation")
    if not isinstance(remediation, dict) or remediation.get("schema_version") != "inbox-manual-remediation/v1":
        return {
            "effective_fields": {}, "resolved_missing": set(), "resolved_suspect": set(),
            "latest_actions": {},
        }
    configured_fields = remediation.get("effective_fields")
    configured_fields = configured_fields if isinstance(configured_fields, dict) else {}
    latest: dict[str, dict[str, Any]] = {}
    for raw_action in remediation.get("history") or []:
        if not isinstance(raw_action, dict):
            continue
        action = dict(raw_action)
        field = str(action.get("field") or "")
        value = str(action.get("new_value") or "").strip()
        operator = str(action.get("operator") or "").strip()
        timestamp = str(action.get("timestamp") or "").strip()
        reference = str(action.get("evidence_reference") or "").strip()
        quote = str(action.get("evidence_quote") or "").strip()
        note = str(action.get("explanatory_note") or "").strip()
        action_type = str(action.get("action_type") or "")
        revision = action.get("revision")
        if (
            field not in (_BASE_CONFIRMATION_FIELDS | _OPTIONAL_CONFIRMATION_FIELDS)
            or not value
            or not operator
            or operator == "operator_ui"
            or not timestamp
            or not str(action.get("action_id") or "")
            or not isinstance(revision, int)
            or revision <= 0
        ):
            continue
        if action_type in {"verify_current_value", "correct_effective_value"}:
            pass
        elif action_type == "missing_field_completion" and ((reference and quote) or note):
            pass
        elif action_type == "existing_value_evidence_verification" and reference and quote:
            pass
        else:
            continue
        latest[field] = action
    effective_fields: dict[str, str] = {}
    resolved_missing: set[str] = set()
    resolved_suspect: set[str] = set()
    effective_latest: dict[str, dict[str, Any]] = {}
    for field, action in latest.items():
        value = str(action.get("new_value") or "").strip()
        if str(configured_fields.get(field) or "").strip() != value:
            continue
        effective_fields[field] = value
        effective_latest[field] = action
        resolved_issues = {str(issue) for issue in action.get("resolved_issues") or []}
        if "required_missing_field" in resolved_issues:
            resolved_missing.add(field)
        if "suspect_field" in resolved_issues:
            resolved_suspect.add(field)
    return {
        "effective_fields": effective_fields,
        "resolved_missing": resolved_missing,
        "resolved_suspect": resolved_suspect,
        "latest_actions": effective_latest,
    }


def manual_remediation_open_issues(item: dict[str, Any]) -> list[dict[str, str]]:
    if not is_manual_remediation_target(item):
        return []
    processing = dict(item.get("processing_result") or {})
    completeness = dict(processing.get("field_completeness") or {})
    integrity = dict(processing.get("field_integrity") or {})
    original_fields = processing.get("confirmable_fields")
    original_fields = original_fields if isinstance(original_fields, dict) else processing
    missing = {
        str(field) for field in completeness.get("required_missing_fields") or [] if str(field)
    } | {
        field for field in _BASE_CONFIRMATION_FIELDS
        if field in original_fields and not str(original_fields.get(field) or "").strip()
    }
    suspect = {
        str(field) for field in completeness.get("suspect_fields") or [] if str(field)
    } | {
        str(field) for field in completeness.get("integrity_blocked_fields") or [] if str(field)
    } | {
        str(field) for field in integrity.get("suspect_fields") or [] if str(field)
    }
    manual = _valid_manual_remediation_state(item)
    labels = {
        "customer": "客户名称", "project_number": "项目编号", "project_name": "项目名称",
        "content": "项目内容", "budget": "预算金额", "bid_open_time": "开标时间",
        "winner": "中标厂商", "award_amount": "中标金额",
    }
    rows = []
    for field in (
        "customer", "project_number", "project_name", "content", "budget",
        "bid_open_time", "winner", "award_amount",
    ):
        latest = dict(manual["latest_actions"].get(field) or {})
        action_type = str(latest.get("action_type") or "")
        if action_type in {"correct_effective_value", "missing_field_completion"}:
            status = "已修正"
        elif action_type in {"verify_current_value", "existing_value_evidence_verification"}:
            status = "已核实"
        elif field in missing or field in suspect:
            status = "待核实"
        else:
            status = "当前无需处理"
        original_value = str(original_fields.get(field) or "").strip()
        rows.append({
            "name": field,
            "label": labels[field],
            "original_value": original_value,
            "display_value": display_amount(original_value) if field in {"budget", "award_amount"} else original_value,
            "effective_value": str(
                manual["effective_fields"].get(field) or original_value
            ).strip(),
            "correction_value": (
                str(manual["effective_fields"].get(field) or "").strip()
                if action_type in {"correct_effective_value", "missing_field_completion"}
                else ""
            ),
            "status": status,
            "latest_operator": str(latest.get("operator") or ""),
            "latest_timestamp": str(latest.get("timestamp") or ""),
        })
    return rows


def inbox_detail_field_rows(item: dict[str, Any]) -> list[dict[str, str]]:
    remediation_rows = manual_remediation_open_issues(item)
    if remediation_rows:
        return remediation_rows
    names = (
        "customer", "project_number", "project_name", "content", "budget",
        "bid_open_time", "winner", "award_amount",
    )
    return [
        {
            "name": name,
            "label": label,
            "original_value": value,
            "display_value": display_amount(value) if name in {"budget", "award_amount"} else value,
            "effective_value": value,
            "correction_value": "",
            "status": "",
            "latest_operator": "",
            "latest_timestamp": "",
        }
        for name, (label, value) in zip(names, inbox_extraction_fields(item))
    ]


def confirmable_processing_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Return the persisted extraction with optional suspect values withheld."""
    processing = dict(item.get("processing_result") or {})
    payload = dict(processing.get("confirmable_fields") or processing)
    completeness = processing.get("field_completeness")
    completeness = completeness if isinstance(completeness, dict) else {}
    integrity = processing.get("field_integrity")
    integrity = integrity if isinstance(integrity, dict) else {}
    optional_fields = (
        {str(field) for field in processing.get("optional_unverified_fields") or []}
        | {str(field) for field in completeness.get("optional_unverified_fields") or []}
        | {str(field) for field in integrity.get("optional_unverified_fields") or []}
        | {str(field) for field in completeness.get("suspect_fields") or []}
        | {str(field) for field in integrity.get("suspect_fields") or []}
    ) & _OPTIONAL_CONFIRMATION_FIELDS
    for field in optional_fields:
        payload[field] = ""

    award_fields_unverified = optional_fields & {"winner", "award_amount"}
    details = [] if award_fields_unverified else [
        dict(detail) for detail in payload.get("award_details") or []
        if isinstance(detail, dict)
        and str(detail.get("winner") or "").strip()
        and re.search(r"\d", str(detail.get("award_amount") or ""))
    ]
    payload["award_details"] = details
    if not (
        str(payload.get("winner") or "").strip()
        and re.search(r"\d", str(payload.get("award_amount") or ""))
    ):
        if details:
            payload["winner"] = "；".join(str(detail.get("winner") or "").strip() for detail in details)
            payload["award_amount"] = "；".join(str(detail.get("award_amount") or "").strip() for detail in details)
        else:
            payload["winner"] = ""
            payload["award_amount"] = ""
    effective_fields = _valid_manual_remediation_state(item)["effective_fields"]
    payload.update(effective_fields)
    if {"winner", "award_amount"} & set(effective_fields):
        winner = str(payload.get("winner") or "").strip()
        award_amount = str(payload.get("award_amount") or "").strip()
        if winner and re.search(r"\d", award_amount):
            detail = dict(details[0]) if details else {}
            detail.update({"winner": winner, "award_amount": award_amount})
            payload["award_details"] = [detail]
        else:
            payload["award_details"] = []
    return payload


def _sanitized_review_queue_item(
    queue_item: dict[str, Any], inbox_item: dict[str, Any] | None = None
) -> dict[str, Any]:
    sanitized = dict(queue_item)
    detail = dict(sanitized.get("candidate_detail") or {})
    trace = dict(detail.get("source_trace") or {})
    extracted = dict(trace.get("extracted_fields") or {})
    if extracted:
        payload = confirmable_processing_payload({
            "processing_result": {
                **extracted,
                "field_completeness": trace.get("field_completeness"),
                "field_integrity": trace.get("field_integrity"),
            }
        })
        trace["extracted_fields"] = {
            **extracted,
            **{
                field: payload.get(field, "")
                for field in _OPTIONAL_CONFIRMATION_FIELDS | {"winner", "award_amount"}
            },
        }
        detail["source_trace"] = trace
        sanitized["candidate_detail"] = detail
    if inbox_item is not None:
        detail = dict(sanitized.get("candidate_detail") or {})
        trace = dict(detail.get("source_trace") or {})
        extracted = dict(trace.get("extracted_fields") or {})
        effective = confirmable_processing_payload(inbox_item)
        extracted.update({
            field: effective.get(field, "")
            for field in (_BASE_CONFIRMATION_FIELDS | _OPTIONAL_CONFIRMATION_FIELDS)
        })
        extracted["award_details"] = [dict(row) for row in effective.get("award_details") or []]
        trace["extracted_fields"] = extracted
        detail["source_trace"] = trace
        sanitized["candidate_detail"] = detail
    return sanitized


def resolve_inbox_confirmation_eligibility(
    item: dict[str, Any], paths: OperatorUiPaths | None = None
) -> dict[str, Any]:
    processing = dict(item.get("processing_result") or {})
    mainline = str(processing.get("mainline") or "")
    completeness = dict(processing.get("field_completeness") or {})
    integrity = processing.get("field_integrity")
    integrity = integrity if isinstance(integrity, dict) else {}
    field_values = processing.get("confirmable_fields")
    field_values = field_values if isinstance(field_values, dict) else processing
    completeness_suspect_fields = {
        str(field) for field in completeness.get("suspect_fields") or [] if str(field)
    }
    suspect_fields = {
        str(field) for field in integrity.get("suspect_fields") or [] if str(field)
    }
    original_base_missing_fields = {
        str(field) for field in completeness.get("required_missing_fields") or []
    } & _BASE_CONFIRMATION_FIELDS
    original_base_missing_fields.update(
        field for field in _BASE_CONFIRMATION_FIELDS
        if field in field_values and not str(field_values.get(field) or "").strip()
    )
    original_base_suspect_fields = (
        suspect_fields
        | completeness_suspect_fields
        | {
            str(field) for field in completeness.get("integrity_blocked_fields") or []
        }
    ) & _BASE_CONFIRMATION_FIELDS
    manual = _valid_manual_remediation_state(item)
    base_missing_fields = original_base_missing_fields - set(manual["resolved_missing"])
    base_suspect_fields = original_base_suspect_fields - set(manual["resolved_suspect"])
    optional_unverified_fields = sorted(
        (
            {str(field) for field in processing.get("optional_unverified_fields") or []}
            | {str(field) for field in completeness.get("optional_unverified_fields") or []}
            | {str(field) for field in integrity.get("optional_unverified_fields") or []}
            | (suspect_fields - _BASE_CONFIRMATION_FIELDS)
            | (completeness_suspect_fields - _BASE_CONFIRMATION_FIELDS)
        ) & _OPTIONAL_CONFIRMATION_FIELDS
    )
    if mainline == "notice_content_dom_qwen/v1":
        persisted_status = str(processing.get("confirmation_eligibility") or "blocked")
        content_status = str(processing.get("content_status") or "unknown")
        downstream = dict(processing.get("downstream_refresh") or {})
        non_field_blocker = (
            content_status != "content_ready"
            or str(processing.get("post_processing_status") or "").lower() == "failed"
            or str(downstream.get("status") or "").lower() == "failed"
        )
        original_field_block = bool(original_base_missing_fields or original_base_suspect_fields)
        field_block_resolved = original_field_block and not (
            base_missing_fields or base_suspect_fields
        )
        status = "eligible" if (
            not non_field_blocker
            and not (base_missing_fields or base_suspect_fields)
            and (persisted_status == "eligible" or field_block_resolved)
        ) else "blocked"
        return {
            "status": status,
            "content_status": content_status,
            "block_reason": "" if status == "eligible" else (
                str(processing.get("block_reason") or "")
                or "基础确认字段缺失或证据异常。"
            ),
            "next_action": "none" if status == "eligible" else "manual_review",
            "attachments": dict(processing.get("attachments") or {}),
            "content_quality": {},
            "optional_unverified_fields": optional_unverified_fields,
        }
    if mainline in _PERSISTED_CONFIRMATION_MAINLINES:
        persisted_status = str(processing.get("confirmation_eligibility") or "blocked")
        status = "eligible" if persisted_status == "eligible" else "blocked"
        block_reason = str(processing.get("block_reason") or "")
        is_active_file = (
            mainline == "unstructured_document_qwen/v1"
            and str(item.get("source_type") or "") in {"file", "file_upload"}
        )
        base_suspect = sorted(base_suspect_fields)
        original_field_block = bool(original_base_missing_fields or original_base_suspect_fields)
        if is_active_file:
            if base_missing_fields or base_suspect:
                status = "blocked"
                block_reason = block_reason or (
                    "基础确认字段缺失或证据异常："
                    + "、".join(sorted(base_missing_fields | set(base_suspect)))
                )
            elif persisted_status == "eligible" or original_field_block:
                status = "eligible"
                block_reason = ""
        if status != "eligible" and not block_reason:
            block_reason = "文件技术处理已完成，但关键字段需要人工检查。"
        return {
            "status": status,
            "content_status": str(processing.get("content_status") or "content_ready"),
            "block_reason": block_reason,
            "next_action": "none" if status == "eligible" else "manual_review",
            "attachments": dict(processing.get("attachments") or {}),
            "content_quality": {},
            "optional_unverified_fields": optional_unverified_fields,
        }
    if str(item.get("source_type") or "") in {"url", "url_acquisition"}:
        return {
            "status": "blocked",
            "content_status": "unknown",
            "block_reason": "旧 URL 任务不属于当前公告正文主线，不能确认。",
            "next_action": "manual_review",
            "attachments": {},
            "content_quality": {},
            "optional_unverified_fields": optional_unverified_fields,
        }
    return {
        "status": "eligible",
        "content_status": "content_ready",
        "block_reason": "",
        "next_action": "none",
        "attachments": {},
        "content_quality": {},
        "optional_unverified_fields": optional_unverified_fields,
    }


def assert_inbox_confirmation_eligible(item: dict[str, Any], paths: OperatorUiPaths) -> None:
    eligibility = resolve_inbox_confirmation_eligibility(item, paths)
    if eligibility["status"] != "eligible":
        reason = str(eligibility.get("block_reason") or "URL 正文质量门禁未通过。")
        raise ValueError(f"当前资料不可确认：{reason}")


def next_action_label(value: str, content_status: str = "") -> str:
    if value == "browser_dom" and content_status == "dynamic_shell":
        return "当前网页正文需要浏览器加载，系统暂时无法自动取得正文。"
    return {
        "none": "无需降级处理",
        "retry_http": "重新采集网页",
        "verified_site_adapter": "请使用系统已验证的站点采集方式重新获取正文。",
        "browser_dom": "请在浏览器中人工检查页面；系统暂时无法自动取得正文。",
        "parse_attachments": "由用户显式点击“解析相关附件”，或选择暂不解析。",
        "retry_downstream_refresh": "采集和候选保存成功；请重试候选去重、生命周期和复核视图刷新。",
        "manual_review": "人工检查来源页面",
    }.get(value, value or "人工检查来源页面")


def user_content_quality_summary(content_status: str, summary: str) -> str:
    if content_status == "dynamic_shell":
        return "页面仅返回站点外壳，未取得公告正文。"
    return summary


def inbox_recollect_label(item: dict[str, Any], paths: OperatorUiPaths) -> str:
    if (
        str(item.get("source_type") or "") != "url"
        or str(item.get("status") or "") != "COMPLETED"
        or not is_operator_visible(item)
    ):
        return ""
    content_status = str(resolve_inbox_confirmation_eligibility(item, paths).get("content_status") or "")
    if content_status in {"dynamic_shell", "blocked", "soft_error", "unknown"}:
        return ""
    if content_status == "content_partial":
        return "重新获取完整正文"
    return "明确重新采集"


def inbox_can_parse_attachments(item: dict[str, Any], paths: OperatorUiPaths) -> bool:
    if (
        str(item.get("source_type") or "") != "url"
        or str(item.get("status") or "") != "COMPLETED"
        or not is_operator_visible(item)
    ):
        return False
    processing = dict(item.get("processing_result") or {})
    attachments = dict(processing.get("attachments") or {})
    missing_fields = list(dict(processing.get("field_completeness") or {}).get("missing_fields") or [])
    eligibility = resolve_inbox_confirmation_eligibility(item, paths)
    retryable_count = (
        int(attachments.get("retryable_count") or 0) if "retryable_count" in attachments else
        sum(
            1 for row in attachments.get("items") or []
            if isinstance(row, dict)
            and (row.get("download_status") == "failed" or row.get("parse_status") == "failed")
        )
    )
    active_mainline = str(processing.get("mainline") or "") == "notice_content_dom_qwen/v1"
    return bool(
        eligibility.get("content_status") == "content_ready"
        and (active_mainline or eligibility.get("status") != "eligible")
        and bool(missing_fields)
        and attachments.get("requires_explicit_parse")
        and (int(attachments.get("pending_count") or 0) > 0 or retryable_count > 0)
    )


def inbox_can_retry_downstream(item: dict[str, Any]) -> bool:
    if str(item.get("status") or "") != "COMPLETED":
        return False
    downstream = dict((item.get("processing_result") or {}).get("downstream_refresh") or {})
    return bool(downstream.get("status") == "failed" and downstream.get("retryable"))


def inbox_failed_retry_label(item: dict[str, Any], paths: OperatorUiPaths) -> str:
    eligibility = resolve_inbox_confirmation_eligibility(item, paths)
    if (
        str(item.get("source_type") or "") == "url"
        and str(eligibility.get("content_status") or "") == "fetch_failed"
    ):
        return "重新采集"
    return "重新处理失败任务"


def inbox_can_confirm(item: dict[str, Any], paths: OperatorUiPaths) -> bool:
    if str(item.get("status") or "") != "COMPLETED":
        return False
    intake_result = inbox_intake_result(item, paths)
    repository_status = str(item.get("repository_status") or ("WRITTEN" if intake_result else "PENDING"))
    has_asset = bool([value for value in item.get("generated_asset_ids", []) if str(value)])
    eligibility = resolve_inbox_confirmation_eligibility(item, paths)
    return (
        repository_status in {"PENDING", "FAILED"} and has_asset
        and str(item.get("task_role") or "") != "xlsx_workbook_summary"
        and eligibility["status"] == "eligible"
    )


def inbox_can_retry_excel(item: dict[str, Any], paths: OperatorUiPaths) -> bool:
    if str(item.get("status") or "") != "COMPLETED":
        return False
    intake_result = inbox_intake_result(item, paths)
    repository_status = str(item.get("repository_status") or ("WRITTEN" if intake_result else "PENDING"))
    excel_status = str(item.get("excel_status") or EXCEL_NOT_WRITTEN)
    eligibility = resolve_inbox_confirmation_eligibility(item, paths)
    return (
        repository_status == "WRITTEN"
        and excel_status in {EXCEL_NOT_WRITTEN, EXCEL_FAILED}
        and eligibility["status"] == "eligible"
    )


def source_type_label(value: str) -> str:
    return {
        "file": "文件上传", "file_upload": "文件上传", "xlsx_row": "XLSX 行记录",
        "xlsx_file_upload": "XLSX 行记录", "url": "URL 采集", "url_acquisition": "URL 采集",
    }.get(value, "其他来源")


def inbox_task_type_label(item: dict[str, Any]) -> str:
    role = str(item.get("task_role") or "")
    if role == "xlsx_workbook_summary":
        return "工作簿摘要（不可确认）"
    if role == "xlsx_business_row" or str(item.get("source_type") or "") == "xlsx_row":
        return "Excel 行级任务"
    return "业务资料任务"


def user_processing_detail(value: str) -> str:
    return {
        "success": "成功", "partial": "部分抽取", "empty": "未抽取到业务字段",
        "not_run": "未运行", "failed": "失败",
    }.get(value.lower(), value or "未识别")


def confirm_inbox_asset(inbox_id: str, paths: OperatorUiPaths, *, operator: str) -> dict[str, Any]:
    item = next((row for row in load_inbox_items(paths.acquisition_inbox) if str(row.get("inbox_id") or "") == inbox_id), None)
    if item is None:
        raise ValueError(f"Inbox item not found: {inbox_id}")
    assert_inbox_confirmation_eligible(item, paths)
    if str(item.get("status") or "") != "COMPLETED":
        raise ValueError("Only COMPLETED tasks can be confirmed")
    extracted = confirmable_processing_payload(item)
    asset_ids = [str(value) for value in item.get("generated_asset_ids", []) if str(value)]
    if not asset_ids:
        raise ValueError("No extracted asset is available for confirmation")
    asset_id = asset_ids[0]
    existing = existing_intake_result(asset_id, paths)
    if existing:
        confirm_project_status(str(existing.get("project_id") or ""), paths.projects)
        existing["award_detail_id"] = merge_award_detail_in_repository(
            str(existing.get("project_id") or ""), extracted, paths.projects
        )
        existing.update({"success": True, "message": "该资料已经入库，未重复创建资产。"})
        return existing

    projects = load_relation_json_array(paths.projects, "projects")
    resolution = resolve_project_match(
        extracted, build_project_match_index(projects)
    )
    matches = list(resolution["matches"])
    if resolution["status"] == "manual":
        return {
            "success": False,
            "message": "项目关键字段冲突或存在多个可能项目，未自动入库。",
            "possible_projects": matches,
        }
    project_id = str(matches[0].get("project_id") or "") if matches else ""
    if not project_id:
        append_accept_decision(asset_id, "", paths, operator, item)
        apply_reviewed(operator=operator, review_decisions_path=paths.review_decisions, review_queue_path=paths.review_queue,
                       intake_output_path=paths.intake_output, intake_audit_path=paths.intake_audit,
                       documents_path=paths.documents, projects_path=paths.projects,
                       repository_audit_path=paths.repository_audit, result_path=paths.workflow_result)
        project = next((row for row in load_relation_json_array(paths.projects, "projects") if str(row.get("asset_id") or "") == asset_id), None)
        if project is None:
            raise ValueError("ProjectEntity was not created")
        project_id = str(project.get("project_id") or "")

    append_accept_decision(asset_id, project_id, paths, operator, item)
    apply_reviewed(operator=operator, review_decisions_path=paths.review_decisions, review_queue_path=paths.review_queue,
                   intake_output_path=paths.intake_output, intake_audit_path=paths.intake_audit,
                   documents_path=paths.documents, projects_path=paths.projects,
                   repository_audit_path=paths.repository_audit, result_path=paths.workflow_result)
    document = next((row for row in load_relation_json_array(paths.documents, "documents") if str(row.get("asset_id") or "") == asset_id), None)
    if document is None:
        raise ValueError("DocumentEntity was not created")
    document_id = str(document.get("document_id") or "")
    relation_type = relation_type_for(str((item.get("processing_result") or {}).get("doc_type") or ""))
    links = load_relation_json_array(paths.links, "project document links")
    existing_link = find_existing_link(links, project_id, document_id, relation_type)
    relation_status = "existing"
    if existing_link is None:
        links.append(create_link(links, load_relation_json_array(paths.projects, "projects"),
                                 load_relation_json_array(paths.documents, "documents"), project_id=project_id,
                                 document_id=document_id, relation_type=relation_type, source="phase35_operator_ui"))
        write_links(links, paths.links)
        relation_status = "created"
    confirm_project_status(project_id, paths.projects)
    detail_id = merge_award_detail_in_repository(
        project_id, extracted, paths.projects
    )
    return {"success": True, "message": "确认入库成功。", "project_id": project_id,
            "document_id": document_id, "relation_status": relation_status, "award_detail_id": detail_id}


def append_accept_decision(
    asset_id: str, project_id: str, paths: OperatorUiPaths, operator: str,
    inbox_item: dict[str, Any],
) -> None:
    review_queue = [
        _sanitized_review_queue_item(item, inbox_item)
        if str(item.get("asset_id") or "") == asset_id else _sanitized_review_queue_item(item)
        for item in load_review_queue(paths.review_queue)
    ]
    decision = create_review_decision(review_queue, asset_id, "ACCEPT", "用户在资料详情中确认入库。",
                                      reviewer=operator, related_project_id=project_id,
                                      review_time=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"))
    append_review_decision(decision, paths.review_decisions)


def exact_project_matches(extracted: dict[str, Any], projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolution = resolve_project_match(extracted, build_project_match_index(projects))
    return list(resolution["matches"]) if resolution["status"] == "matched" else []


PROJECT_STAGE_SUFFIXES = (
    "中标候选人公示", "中标结果公告", "成交结果公告", "中标公告", "成交公告",
    "招标公告", "采购公告", "竞争性磋商公告", "询价公告", "单一来源采购公告",
)
REMOVABLE_PROJECT_PREFIX_LABELS = {
    "新疆生产建设兵团·十二师",
    "公开招标", "邀请招标", "竞争性谈判", "竞争性磋商", "询价", "单一来源",
    "勘察",
    "招标公告", "采购公告", "中标公告", "成交公告", "中标候选人公示",
}
LABELED_PROJECT_NUMBER_PATTERN = re.compile(
    r"(?:项目|采购|招标)编号\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9._/-]{3,79})",
    re.I,
)
DATE_LIKE_PROJECT_NUMBER_PATTERN = re.compile(r"(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}")


def normalize_project_core_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    while True:
        prefix = re.match(r"^\s*(?:\[([^\]\r\n]{1,80})\]|【([^】\r\n]{1,80})】)\s*", text)
        if prefix is None:
            break
        label = unicodedata.normalize("NFKC", next(group for group in prefix.groups() if group)).strip()
        if label not in REMOVABLE_PROJECT_PREFIX_LABELS:
            break
        text = text[prefix.end():]
    text = re.sub(r"\(\s*(?:项目|采购|招标)?编号\s*[:：][^()]{1,100}\)", "", text, flags=re.I)
    changed = True
    while changed:
        changed = False
        for suffix in PROJECT_STAGE_SUFFIXES:
            stripped = re.sub(rf"\s*{re.escape(suffix)}\s*$", "", text, flags=re.I)
            if stripped != text:
                text = stripped
                changed = True
    return normalize_business_key(text)


def project_match_payload(project: dict[str, Any]) -> dict[str, Any]:
    extracted = dict(project_extracted(project))
    for key in ("project_name", "customer", "project_number", "source_file", "content", "note"):
        if not str(extracted.get(key) or "").strip() and str(project.get(key) or "").strip():
            extracted[key] = project.get(key)
    return extracted


def effective_project_numbers(payload: dict[str, Any]) -> set[str]:
    numbers: set[str] = set()
    explicit = safe_project_number(payload.get("project_number"))
    if explicit:
        numbers.add(explicit)
    project_name = unicodedata.normalize("NFKC", str(payload.get("project_name") or ""))
    for match in LABELED_PROJECT_NUMBER_PATTERN.finditer(project_name):
        labeled = safe_project_number(match.group(1))
        if labeled:
            numbers.add(labeled)
    return numbers


def safe_project_number(value: object) -> str:
    number = validated_project_number(value).strip(" ._/-").upper()
    if not number or DATE_LIKE_PROJECT_NUMBER_PATTERN.fullmatch(number):
        return ""
    return number


def project_name_customer_key(payload: dict[str, Any]) -> tuple[str, str] | None:
    name = normalize_project_core_name(payload.get("project_name"))
    customer = normalize_business_key(payload.get("customer"))
    return (name, customer) if name and customer else None


def build_project_match_index(projects: list[dict[str, Any]]) -> dict[str, dict[Any, list[dict[str, Any]]]]:
    index: dict[str, dict[Any, list[dict[str, Any]]]] = {
        "number": {}, "name_customer": {}, "group": {},
    }
    for project in projects:
        add_project_to_match_index(index, project)
    return index


def add_project_to_match_index(
    index: dict[str, dict[Any, list[dict[str, Any]]]], project: dict[str, Any]
) -> None:
    payload = project_match_payload(project)
    for number in effective_project_numbers(payload):
        index["number"].setdefault(number, []).append(project)
    name_customer = project_name_customer_key(payload)
    if name_customer:
        index["name_customer"].setdefault(name_customer, []).append(project)
    group_id = project_business_group_id(project)
    if group_id:
        index["group"].setdefault(group_id, []).append(project)


def pending_project_match_record(asset_id: str, extracted: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": "",
        "asset_id": asset_id,
        "project_name": str(extracted.get("project_name") or ""),
        "customer": str(extracted.get("customer") or ""),
        "business_group_id": business_group_id(extracted),
        "source_trace": {"extracted_fields": dict(extracted)},
        "_pending_project_asset": asset_id,
    }


def resolve_project_match(
    extracted: dict[str, Any], index: dict[str, dict[Any, list[dict[str, Any]]]]
) -> dict[str, Any]:
    numbers = effective_project_numbers(extracted)
    name_customer = project_name_customer_key(extracted)
    customer = normalize_business_key(extracted.get("customer"))
    group_id = business_group_id(extracted)
    group_matches = unique_project_rows(index["group"].get(group_id, [])) if group_id else []
    number_matches = unique_project_rows(
        project for number in numbers for project in index["number"].get(number, [])
    )
    name_matches = unique_project_rows(index["name_customer"].get(name_customer, [])) if name_customer else []

    if group_matches:
        if len(group_matches) != 1:
            return {"status": "manual", "matches": group_matches}
        grouped_project = group_matches[0]
        if project_identity_conflicts(extracted, project_match_payload(grouped_project)):
            return {"status": "manual", "matches": group_matches}
        identity_matches = number_matches if numbers else name_matches
        grouped_key = project_row_key(grouped_project)
        if any(project_row_key(project) != grouped_key for project in identity_matches):
            return {"status": "manual", "matches": unique_project_rows([grouped_project, *identity_matches])}
        return {"status": "matched", "matches": group_matches}

    if numbers:
        matches = number_matches
        if matches:
            customer_conflict = any(
                customer
                and normalize_business_key(project_match_payload(project).get("customer"))
                and customer != normalize_business_key(project_match_payload(project).get("customer"))
                for project in matches
            )
            status = "manual" if len(matches) != 1 or customer_conflict else "matched"
            return {"status": status, "matches": matches}
        if name_customer:
            conflicts = [
                project for project in index["name_customer"].get(name_customer, [])
                if effective_project_numbers(project_match_payload(project))
                and numbers.isdisjoint(effective_project_numbers(project_match_payload(project)))
            ]
            if conflicts:
                return {"status": "manual", "matches": unique_project_rows(conflicts)}
        return {"status": "new", "matches": []}

    if name_customer:
        matches = name_matches
        if matches:
            return {"status": "matched" if len(matches) == 1 else "manual", "matches": matches}
        return {"status": "new", "matches": []}

    return {"status": "new", "matches": []}


def project_identity_conflicts(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_numbers = effective_project_numbers(left)
    right_numbers = effective_project_numbers(right)
    if left_numbers and right_numbers and left_numbers.isdisjoint(right_numbers):
        return True
    left_name = normalize_project_core_name(left.get("project_name"))
    right_name = normalize_project_core_name(right.get("project_name"))
    if left_name and right_name and left_name != right_name:
        return True
    left_customer = normalize_business_key(left.get("customer"))
    right_customer = normalize_business_key(right.get("customer"))
    return bool(left_customer and right_customer and left_customer != right_customer)


def project_row_key(project: dict[str, Any]) -> str:
    return str(project.get("project_id") or project.get("_pending_project_asset") or id(project))


def unique_project_rows(rows: Any) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = project_row_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def project_extracted(project: dict[str, Any]) -> dict[str, str]:
    trace = dict(project.get("source_trace") or {})
    return {str(k): str(v or "") for k, v in dict(trace.get("extracted_fields") or {}).items()}


def relation_type_for(doc_type: str) -> str:
    if "结果" in doc_type or "中标" in doc_type:
        return "award_notice"
    if "合同" in doc_type:
        return "contract"
    if "公告" in doc_type or "采购" in doc_type:
        return "bid_notice"
    return "attachment"


def existing_intake_result(asset_id: str, paths: OperatorUiPaths) -> dict[str, Any] | None:
    document = next((row for row in load_relation_json_array(paths.documents, "documents") if str(row.get("asset_id") or "") == asset_id), None)
    if document is None:
        return None
    document_id = str(document.get("document_id") or "")
    link = next((row for row in load_relation_json_array(paths.links, "project document links") if str(row.get("document_id") or "") == document_id), None)
    project_id = str((link or {}).get("project_id") or document.get("project_id") or "")
    return {"project_id": project_id, "document_id": document_id, "relation_status": "existing"}


def confirm_project_status(project_id: str, projects_path: Path) -> None:
    if not project_id:
        return
    projects = load_relation_json_array(projects_path, "projects")
    changed = False
    for index, project in enumerate(projects):
        if str(project.get("project_id") or "") != project_id:
            continue
        if str(project.get("status") or "").lower() != CONFIRMED:
            projects[index] = update_entity_status(project, CONFIRMED)
            changed = True
        break
    if changed:
        write_repository_json(projects_path, projects)


def inbox_intake_result(item: dict[str, Any], paths: OperatorUiPaths) -> dict[str, Any] | None:
    for asset_id in item.get("generated_asset_ids", []):
        result = existing_intake_result(str(asset_id), paths)
        if result:
            result.update({"success": True, "message": "该资料已经入库。"})
            return result
    return None


def project_detail_fields(project_asset: dict[str, Any]) -> list[tuple[str, str]]:
    fields = dict(project_asset.get("project_fields") or {})
    return [
        ("客户名称", fields.get("customer", "")),
        ("项目编号", validated_project_number(fields.get("project_number", ""))),
        ("项目名称", fields.get("project_name", "")),
        ("项目内容", fields.get("content", "")),
        ("预算金额", display_amount(fields.get("budget"))),
        ("开标时间", fields.get("bid_open_time", "")),
        ("中标厂商", fields.get("winner_company", "")),
        ("中标金额", display_amount(fields.get("award_amount"))),
    ]


def project_award_detail_rows(project_asset: dict[str, Any]) -> list[dict[str, Any]]:
    details = project_asset.get("award_details")
    if not isinstance(details, list):
        details = (project_asset.get("project") or {}).get("award_details", [])
    if not isinstance(details, list):
        return []
    rows = [dict(item) for item in details if isinstance(item, dict)]
    for row in rows:
        row["award_amount"] = display_amount(row.get("award_amount"))
    return rows


def project_search_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["project_number"] = validated_project_number(result.get("project_number"))
    result["budget"] = display_amount(result.get("budget"), include_unit=False)
    result["award_amount"] = display_amount(result.get("award_amount"), include_unit=False)
    result["status_label"] = project_status_label(str(result.get("status") or ""))
    return result


def project_status_label(value: str) -> str:
    return {"confirmed": "已入库", "candidate": "待入库"}.get(value.lower(), "待核实")


def project_document_rows(project_asset: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in project_asset.get("documents", []):
        document = dict(item.get("document") or {})
        metadata = dict(document.get("document_metadata") or {})
        trace = dict(document.get("source_trace") or {})
        extracted = dict(trace.get("extracted_fields") or {})
        rows.append({
            "name": str(metadata.get("source_title") or metadata.get("file_name") or "未识别"),
            "document_type": str(metadata.get("file_type") or extracted.get("doc_type") or ""),
            "source": str(trace.get("source_file") or trace.get("source_url") or "未识别"),
            "source_is_url": str(trace.get("source_url") or "").startswith(("http://", "https://")),
            "created_time": str(document.get("created_time") or ""),
            "relation_type": {
                "bid_notice": "招标公告", "award_notice": "结果公告", "contract": "合同",
                "attachment": "附件", "other": "其他资料",
            }.get(str(item.get("relation_type") or ""), "其他资料"),
        })
    return rows


def accepted_asset_count(decisions_path: Path) -> int:
    decisions = load_review_decisions(decisions_path)
    latest = latest_decision_by_asset(decisions)
    return sum(1 for decision in latest.values() if str(decision.get("decision") or "") == "ACCEPT")


def review_row(item: dict[str, Any]) -> dict[str, str]:
    detail = item.get("candidate_detail") if isinstance(item.get("candidate_detail"), dict) else {}
    lifecycle = item.get("lifecycle") if isinstance(item.get("lifecycle"), dict) else {}
    asset_id = str(item.get("asset_id") or "")
    title = str(item.get("title") or detail.get("original_title") or "")
    return {
        "asset_id": asset_id,
        "asset_id_short": truncate(asset_id),
        "title": title,
        "title_short": truncate(title, 70),
        "source_type": str(item.get("source_type") or detail.get("source_type") or ""),
        "confidence": str(item.get("confidence") or detail.get("confidence") or ""),
        "lifecycle_status": str(item.get("lifecycle_status") or lifecycle.get("status") or ""),
        "priority": str(item.get("priority") or ""),
    }


def dashboard_stats(paths: OperatorUiPaths) -> dict[str, int]:
    candidates = load_relation_json_array(paths.asset_candidates, "asset candidates")
    queue = load_relation_json_array(paths.review_queue, "review queue")
    latest_decisions = latest_decision_by_asset(load_review_decisions(paths.review_decisions))
    inbox_items = [item for item in load_inbox_items(inbox_paths(paths).inbox) if is_business_inbox_item(item)]
    inbox_counts = {status: 0 for status in ("RECEIVED", "PROCESSING", "COMPLETED", "FAILED")}
    for item in inbox_items:
        status = str(item.get("status") or "")
        if status in inbox_counts:
            inbox_counts[status] += 1
    return {
        "asset_candidates": len(candidates),
        "pending_review": sum(1 for item in queue if str(item.get("asset_id") or "") not in latest_decisions),
        "accepted": sum(1 for decision in latest_decisions.values() if str(decision.get("decision") or "") == "ACCEPT"),
        "projects": len(load_relation_json_array(paths.projects, "projects")),
        "documents": len(load_relation_json_array(paths.documents, "documents")),
        "inbox_total": len(inbox_items),
        "inbox_received": inbox_counts["RECEIVED"],
        "inbox_processing": inbox_counts["PROCESSING"],
        "inbox_completed": inbox_counts["COMPLETED"],
        "inbox_failed": inbox_counts["FAILED"],
    }


def truncate(value: str, limit: int = 24) -> str:
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_page(body_template: str, **context: Any) -> str:
    body = render_template_string(body_template, **context)
    return render_template_string(
        BASE_TEMPLATE,
        body=body,
        app_version=current_app.config.get("APP_VERSION", "development"),
    )


def main() -> None:
    _refuse_direct_module()


if __name__ == "__main__":
    main()
