"""Content-aware diff for extracted PDF snapshots.

Two complementary views are produced:

* a page text diff based on normalised lines (works for paragraphs and notes);
* a table-aware diff that aligns pages, tables, rows and cells so a changed
  course code is reported as one cell change rather than a wall of text noise.
"""

from __future__ import annotations

import difflib
import json
from collections import defaultdict
from typing import Any, Callable, Sequence

from .extractor import page_signature


def _sequence_align(
    old_keys: Sequence[str],
    new_keys: Sequence[str],
    similarity: Callable[[int, int], float] | None = None,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Align two sequences, pairing modified items inside replace blocks.

    Returns (pairs, old_only, new_only). Exact matches are found by
    difflib; inside a replace block, items are greedily paired by a caller
    supplied similarity function.
    """
    matcher = difflib.SequenceMatcher(a=list(old_keys), b=list(new_keys), autojunk=False)
    pairs: list[tuple[int, int]] = []
    old_only: list[int] = []
    new_only: list[int] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            pairs.extend((i1 + offset, j1 + offset) for offset in range(i2 - i1))
            continue
        if tag == "delete":
            old_only.extend(range(i1, i2))
            continue
        if tag == "insert":
            new_only.extend(range(j1, j2))
            continue

        old_block = list(range(i1, i2))
        new_block = list(range(j1, j2))
        if not old_block:
            new_only.extend(new_block)
            continue
        if not new_block:
            old_only.extend(old_block)
            continue
        if len(old_block) == 1 and len(new_block) == 1:
            pairs.append((old_block[0], new_block[0]))
            continue

        if similarity is None:
            paired = min(len(old_block), len(new_block))
            pairs.extend(
                (old_block[offset], new_block[offset]) for offset in range(paired)
            )
            old_only.extend(old_block[paired:])
            new_only.extend(new_block[paired:])
            continue

        scored: list[tuple[float, int, int]] = []
        for old_index in old_block:
            for new_index in new_block:
                score = (
                    similarity(old_index, new_index)
                    if similarity is not None
                    else 0.0
                )
                scored.append((score, old_index, new_index))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        used_old: set[int] = set()
        used_new: set[int] = set()
        for score, old_index, new_index in scored:
            if old_index in used_old or new_index in used_new:
                continue
            if score < 0.2:
                continue
            pairs.append((old_index, new_index))
            used_old.add(old_index)
            used_new.add(new_index)
        old_only.extend(index for index in old_block if index not in used_old)
        new_only.extend(index for index in new_block if index not in used_new)

    pairs.sort()
    return pairs, sorted(old_only), sorted(new_only)


def _line_changes(
    old_text: str, new_text: str
) -> tuple[list[dict[str, Any]], list[str], int, int]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    changes: list[dict[str, Any]] = []
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        changes.append(
            {
                "op": tag,
                "old_start": i1 + 1 if i2 > i1 else None,
                "new_start": j1 + 1 if j2 > j1 else None,
                "old_lines": old_lines[i1:i2],
                "new_lines": new_lines[j1:j2],
            }
        )
    unified = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
            n=2,
        )
    )
    return changes, unified, added, removed


def _table_key(table: dict[str, Any]) -> str:
    return json.dumps(table.get("rows", []), ensure_ascii=False, separators=(",", ":"))


def _table_similarity(old_table: dict[str, Any], new_table: dict[str, Any]) -> float:
    old_flat = "\n".join(" | ".join(row) for row in old_table.get("rows", []))
    new_flat = "\n".join(" | ".join(row) for row in new_table.get("rows", []))
    return difflib.SequenceMatcher(a=old_flat, b=new_flat, autojunk=False).ratio()


def _row_key(row: Sequence[str]) -> str:
    return "\x1f".join(row)


def _row_similarity(old_row: Sequence[str], new_row: Sequence[str]) -> float:
    return difflib.SequenceMatcher(
        a=" | ".join(old_row), b=" | ".join(new_row), autojunk=False
    ).ratio()


def _row_context(row: Sequence[str], limit: int = 240) -> str:
    text = " | ".join(cell for cell in row if cell)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _count_cells(row: Sequence[str]) -> int:
    return sum(1 for cell in row if cell)


def _diff_tables(
    old_tables: Sequence[dict[str, Any]], new_tables: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    changes: list[dict[str, Any]] = []

    pairs, old_only, new_only = _sequence_align(
        [_table_key(table) for table in old_tables],
        [_table_key(table) for table in new_tables],
        similarity=lambda i, j: _table_similarity(old_tables[i], new_tables[j]),
    )

    for old_index, new_index in pairs:
        old_table = old_tables[old_index]
        new_table = new_tables[new_index]
        old_rows = old_table.get("rows", [])
        new_rows = new_table.get("rows", [])
        row_pairs, old_rows_only, new_rows_only = _sequence_align(
            [_row_key(row) for row in old_rows],
            [_row_key(row) for row in new_rows],
            similarity=lambda i, j, old=old_rows, new=new_rows: _row_similarity(
                old[i], new[j]
            ),
        )

        for row_old, row_new in row_pairs:
            old_row = old_rows[row_old]
            new_row = new_rows[row_new]
            if old_row == new_row:
                continue
            context = _row_context(new_row or old_row)
            common_columns = min(len(old_row), len(new_row))
            for column in range(common_columns):
                old_value = old_row[column]
                new_value = new_row[column]
                if old_value == new_value:
                    continue
                if not old_value:
                    change_type = "cell_added"
                    counts["table_cells_added"] += 1
                elif not new_value:
                    change_type = "cell_removed"
                    counts["table_cells_removed"] += 1
                else:
                    change_type = "cell_modified"
                    counts["table_cells_modified"] += 1
                changes.append(
                    {
                        "type": change_type,
                        "table_index": new_index + 1,
                        "old_row": row_old + 1,
                        "new_row": row_new + 1,
                        "column": column + 1,
                        "old": old_value,
                        "new": new_value,
                        "row_context": context,
                    }
                )
            for column in range(common_columns, len(old_row)):
                if old_row[column]:
                    counts["table_cells_removed"] += 1
                    changes.append(
                        {
                            "type": "cell_removed",
                            "table_index": new_index + 1,
                            "old_row": row_old + 1,
                            "new_row": row_new + 1,
                            "column": column + 1,
                            "old": old_row[column],
                            "new": "",
                            "row_context": context,
                        }
                    )
            for column in range(common_columns, len(new_row)):
                if new_row[column]:
                    counts["table_cells_added"] += 1
                    changes.append(
                        {
                            "type": "cell_added",
                            "table_index": new_index + 1,
                            "old_row": row_old + 1,
                            "new_row": row_new + 1,
                            "column": column + 1,
                            "old": "",
                            "new": new_row[column],
                            "row_context": context,
                        }
                    )

        for row_index in new_rows_only:
            row = new_rows[row_index]
            counts["rows_added"] += 1
            counts["table_cells_added"] += _count_cells(row)
            changes.append(
                {
                    "type": "row_added",
                    "table_index": new_index + 1,
                    "new_row": row_index + 1,
                    "cells": list(row),
                    "row_context": _row_context(row),
                }
            )
        for row_index in old_rows_only:
            row = old_rows[row_index]
            counts["rows_removed"] += 1
            counts["table_cells_removed"] += _count_cells(row)
            changes.append(
                {
                    "type": "row_removed",
                    "table_index": new_index + 1,
                    "old_row": row_index + 1,
                    "cells": list(row),
                    "row_context": _row_context(row),
                }
            )

    for table_index in new_only:
        table = new_tables[table_index]
        rows = table.get("rows", [])
        counts["tables_added"] += 1
        counts["rows_added"] += len(rows)
        changes.append(
            {
                "type": "table_added",
                "table_index": table_index + 1,
                "row_count": len(rows),
                "rows_preview": rows[:5],
            }
        )
    for table_index in old_only:
        table = old_tables[table_index]
        rows = table.get("rows", [])
        counts["tables_removed"] += 1
        counts["rows_removed"] += len(rows)
        changes.append(
            {
                "type": "table_removed",
                "table_index": table_index + 1,
                "row_count": len(rows),
                "rows_preview": rows[:5],
            }
        )

    return changes, dict(counts)


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _page_change(
    *,
    old_page: dict[str, Any] | None,
    new_page: dict[str, Any] | None,
    status: str,
) -> tuple[dict[str, Any], int, int, dict[str, int]]:
    old_text = old_page.get("text", "") if old_page else ""
    new_text = new_page.get("text", "") if new_page else ""
    line_changes, unified, added, removed = _line_changes(old_text, new_text)

    if status == "modified":
        table_changes, table_counts = _diff_tables(
            old_page.get("tables", []) if old_page else [],
            new_page.get("tables", []) if new_page else [],
        )
    elif status == "added":
        table_changes = []
        table_counts = defaultdict(int)
        for index, table in enumerate(new_page.get("tables", []) if new_page else [], start=1):
            rows = table.get("rows", [])
            table_counts["tables_added"] += 1
            table_counts["rows_added"] += len(rows)
            table_changes.append(
                {
                    "type": "table_added",
                    "table_index": index,
                    "row_count": len(rows),
                    "rows_preview": rows[:5],
                }
            )
        table_counts = dict(table_counts)
    else:
        table_changes = []
        table_counts = defaultdict(int)
        for index, table in enumerate(old_page.get("tables", []) if old_page else [], start=1):
            rows = table.get("rows", [])
            table_counts["tables_removed"] += 1
            table_counts["rows_removed"] += len(rows)
            table_changes.append(
                {
                    "type": "table_removed",
                    "table_index": index,
                    "row_count": len(rows),
                    "rows_preview": rows[:5],
                }
            )
        table_counts = dict(table_counts)

    page_change = {
        "status": status,
        "old_page": old_page.get("page") if old_page else None,
        "new_page": new_page.get("page") if new_page else None,
        "line_changes": line_changes,
        "table_changes": table_changes,
        "unified_diff": "\n".join(unified),
    }
    return page_change, added, removed, table_counts


def _headline(summary: dict[str, Any]) -> str:
    parts: list[str] = []
    if summary["pages_added"]:
        parts.append(f"新增 {summary['pages_added']} 页")
    if summary["pages_removed"]:
        parts.append(f"删除 {summary['pages_removed']} 页")
    if summary["pages_changed"]:
        parts.append(f"{summary['pages_changed']} 页内容变化")
    cell_total = (
        summary["table_cells_modified"]
        + summary["table_cells_added"]
        + summary["table_cells_removed"]
    )
    if cell_total:
        parts.append(f"{cell_total} 个表格单元格变化")
    if not cell_total and (summary["text_lines_added"] or summary["text_lines_removed"]):
        parts.append(
            f"文本 +{summary['text_lines_added']}/-{summary['text_lines_removed']} 行"
        )
    if not parts:
        return "可提取内容未变化"
    return "，".join(parts)


def _md_code(value: str) -> str:
    value = " ".join(value.split())
    if "`" in value:
        return "`` " + value + " ``"
    return "`" + value + "`"


def _build_markdown(
    *,
    document_key: str,
    detected_at: str,
    from_version_id: int | None,
    to_version_id: int,
    summary: dict[str, Any],
    headline: str,
    page_changes: Sequence[dict[str, Any]],
) -> str:
    lines = [
        "# PDF 内容变更报告",
        "",
        f"- 文档：`{document_key}`",
        f"- 检测时间：{detected_at}",
        f"- 版本：{from_version_id if from_version_id is not None else '（初始版本）'} → {to_version_id}",
        f"- 摘要：{headline}",
        "",
        "## 变更统计",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
        f"| 内容变化页 | {summary['pages_changed']} |",
        f"| 新增/删除页 | {summary['pages_added']} / {summary['pages_removed']} |",
        f"| 文本新增/删除行 | {summary['text_lines_added']} / {summary['text_lines_removed']} |",
        f"| 表格新增/删除 | {summary['tables_added']} / {summary['tables_removed']} |",
        f"| 表格行新增/删除 | {summary['rows_added']} / {summary['rows_removed']} |",
        f"| 单元格修改/新增/删除 | {summary['table_cells_modified']} / {summary['table_cells_added']} / {summary['table_cells_removed']} |",
        "",
    ]

    for page in page_changes:
        old_page = page.get("old_page")
        new_page = page.get("new_page")
        if page["status"] == "added":
            heading = f"## 新增第 {new_page} 页"
        elif page["status"] == "removed":
            heading = f"## 删除原第 {old_page} 页"
        else:
            heading = f"## 第 {new_page} 页"
        lines.append(heading)
        lines.append("")

        table_changes = page.get("table_changes", [])
        if table_changes:
            lines.append("### 表格变更")
            lines.append("")
            for change in table_changes:
                change_type = change["type"]
                if change_type == "cell_modified":
                    row_label = (
                        f"第 {change['old_row']} 行"
                        if change["old_row"] == change["new_row"]
                        else f"第 {change['old_row']}→{change['new_row']} 行"
                    )
                    lines.append(
                        f"- 表格 {change['table_index']}，{row_label}，"
                        f"第 {change['column']} 列：{_md_code(change['old'])} → {_md_code(change['new'])}"
                    )
                elif change_type == "cell_added":
                    lines.append(
                        f"- 表格 {change['table_index']}，第 {change['new_row']} 行，"
                        f"第 {change['column']} 列新增：{_md_code(change['new'])}"
                    )
                elif change_type == "cell_removed":
                    lines.append(
                        f"- 表格 {change['table_index']}，第 {change['old_row']} 行，"
                        f"第 {change['column']} 列删除：{_md_code(change['old'])}"
                    )
                elif change_type == "row_added":
                    lines.append(
                        f"- 表格 {change['table_index']} 新增行 {change['new_row']}：{_md_code(change['row_context'])}"
                    )
                elif change_type == "row_removed":
                    lines.append(
                        f"- 表格 {change['table_index']} 删除行 {change['old_row']}：{_md_code(change['row_context'])}"
                    )
                elif change_type == "table_added":
                    lines.append(
                        f"- 新增表格 {change['table_index']}（{change['row_count']} 行）"
                    )
                elif change_type == "table_removed":
                    lines.append(
                        f"- 删除表格 {change['table_index']}（{change['row_count']} 行）"
                    )
            lines.append("")

        unified = page.get("unified_diff", "").strip()
        if unified:
            lines.append("### 文本差异")
            lines.append("")
            lines.append("```diff")
            lines.append(unified)
            lines.append("```")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def diff_snapshots(
    old_snapshot: dict[str, Any] | None,
    new_snapshot: dict[str, Any],
    *,
    document_key: str,
    detected_at: str,
    from_version_id: int | None,
    to_version_id: int,
    max_diff_chars: int = 250_000,
) -> dict[str, Any]:
    old_pages = old_snapshot.get("pages", []) if old_snapshot else []
    new_pages = new_snapshot.get("pages", [])

    page_pairs, old_pages_only, new_pages_only = _sequence_align(
        [page_signature(page) for page in old_pages],
        [page_signature(page) for page in new_pages],
    )

    page_changes: list[dict[str, Any]] = []
    summary: dict[str, int] = {
        "pages_changed": 0,
        "pages_added": 0,
        "pages_removed": 0,
        "text_lines_added": 0,
        "text_lines_removed": 0,
        "tables_added": 0,
        "tables_removed": 0,
        "rows_added": 0,
        "rows_removed": 0,
        "table_cells_modified": 0,
        "table_cells_added": 0,
        "table_cells_removed": 0,
    }

    for old_index, new_index in page_pairs:
        if page_signature(old_pages[old_index]) == page_signature(new_pages[new_index]):
            continue
        change, added, removed, table_counts = _page_change(
            old_page=old_pages[old_index],
            new_page=new_pages[new_index],
            status="modified",
        )
        summary["pages_changed"] += 1
        summary["text_lines_added"] += added
        summary["text_lines_removed"] += removed
        _merge_counts(summary, table_counts)
        page_changes.append(change)

    for page_index in new_pages_only:
        change, added, removed, table_counts = _page_change(
            old_page=None,
            new_page=new_pages[page_index],
            status="added",
        )
        summary["pages_added"] += 1
        summary["text_lines_added"] += added
        _merge_counts(summary, table_counts)
        page_changes.append(change)

    for page_index in old_pages_only:
        change, added, removed, table_counts = _page_change(
            old_page=old_pages[page_index],
            new_page=None,
            status="removed",
        )
        summary["pages_removed"] += 1
        summary["text_lines_removed"] += removed
        _merge_counts(summary, table_counts)
        page_changes.append(change)

    page_changes.sort(
        key=lambda page: (
            page.get("new_page") is None,
            page.get("new_page") or page.get("old_page") or 0,
        )
    )

    headline = _headline(summary)
    unified_parts = [
        page["unified_diff"] for page in page_changes if page.get("unified_diff")
    ]
    unified_diff = "\n\n".join(unified_parts).strip()
    truncated = False
    if len(unified_diff) > max_diff_chars:
        unified_diff = (
            unified_diff[:max_diff_chars]
            + "\n\n... [unified diff truncated due to size limit]"
        )
        truncated = True

    markdown = _build_markdown(
        document_key=document_key,
        detected_at=detected_at,
        from_version_id=from_version_id,
        to_version_id=to_version_id,
        summary=summary,
        headline=headline,
        page_changes=page_changes,
    )
    if len(markdown) > max_diff_chars:
        markdown = (
            markdown[:max_diff_chars]
            + "\n\n... [markdown report truncated due to size limit]\n"
        )
        truncated = True

    return {
        "document_key": document_key,
        "detected_at": detected_at,
        "from_version_id": from_version_id,
        "to_version_id": to_version_id,
        "headline": headline,
        "summary": summary,
        "pages": page_changes,
        "unified_diff": unified_diff,
        "markdown": markdown,
        "truncated": truncated,
    }
