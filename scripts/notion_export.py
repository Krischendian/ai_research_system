"""把sector报告导入Notion，生成带Toggle的精美页面。"""
import re
import sys
from pathlib import Path
from notion_client import Client

NOTION_TOKEN = "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"
PAGE_ID = "34890ac26a5b80679484fa52194dc845"
REPORT_FILE = "final_report_v6.txt"

client = Client(auth=NOTION_TOKEN)


def text(content: str, bold=False, color="default") -> dict:
    return {
        "type": "text",
        "text": {"content": content},
        "annotations": {"bold": bold, "color": color},
    }


def heading1(content: str) -> dict:
    return {
        "object": "block",
        "type": "heading_1",
        "heading_1": {"rich_text": [text(content)]},
    }


def heading2(content: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [text(content)]},
    }


def heading3(content: str) -> dict:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": [text(content)]},
    }


def paragraph(content: str) -> dict:
    # 截断超过2000字符的段落
    if len(content) > 1900:
        content = content[:1900] + "..."
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [text(content)] if content.strip() else []},
    }


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def callout(content: str, emoji: str = "ℹ️") -> dict:
    if len(content) > 1900:
        content = content[:1900] + "..."
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [text(content)],
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def toggle(title: str, children: list) -> dict:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [text(title, bold=True)],
            "children": children[:99],  # Notion 限制每次最多100个子块
        },
    }


def bullet(content: str) -> dict:
    if len(content) > 1900:
        content = content[:1900] + "..."
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [text(content)]},
    }


def parse_report(md: str) -> list:
    """把Markdown报告解析成Notion块列表。"""
    blocks = []
    lines = md.split("\n")
    i = 0
    MARKER = "<!--- COMPANY_DETAILS_START --->"

    while i < len(lines):
        line = lines[i]

        # 跳过空行
        if not line.strip():
            i += 1
            continue

        # H1
        if line.startswith("# ") and not line.startswith("## "):
            blocks.append(heading1(line[2:].strip()))
            i += 1
            continue

        # H2 — 主要章节
        if line.startswith("## "):
            title = line[3:].strip()
            # 收集这个章节的所有内容直到下一个H2
            section_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                section_lines.append(lines[i])
                i += 1

            section_text = "\n".join(section_lines)

            # 判断是否有公司详情标记
            if MARKER in section_text:
                summary_part, details_part = section_text.split(MARKER, 1)
                # 渲染章节标题
                blocks.append(heading2(title))
                # 渲染总结部分
                for sl in summary_part.split("\n"):
                    sl = sl.strip()
                    if not sl:
                        continue
                    if sl.startswith("> "):
                        blocks.append(callout(sl[2:], "⚠️"))
                    elif sl.startswith("- ") or sl.startswith("* "):
                        blocks.append(bullet(sl[2:]))
                    elif sl.startswith("**参考来源"):
                        blocks.append(paragraph(sl))
                    else:
                        blocks.append(paragraph(sl))

                # 渲染公司详情为Toggle
                company_pattern = re.compile(
                    r"^###\s+([\w/\-\.]+(?:\s+[\w/\-\.]+)?)\s+—\s+(.+)$",
                    re.MULTILINE
                )
                matches = list(company_pattern.finditer(details_part))

                if matches:
                    company_blocks = []
                    for j, m in enumerate(matches):
                        ticker = m.group(1).strip()
                        company_name = m.group(2).strip()
                        start = m.end()
                        end = matches[j+1].start() if j+1 < len(matches) else len(details_part)
                        company_body = details_part[start:end].strip()

                        # 把公司内容转成子块
                        children = []
                        for cl in company_body.split("\n"):
                            cl = cl.strip()
                            if not cl:
                                continue
                            if cl.startswith("> "):
                                children.append(callout(cl[2:], "📌"))
                            elif cl.startswith("- ") or cl.startswith("* "):
                                children.append(bullet(cl[2:]))
                            elif cl.startswith("### "):
                                children.append(heading3(cl[4:]))
                            else:
                                children.append(paragraph(cl))

                        if children:
                            company_blocks.append(
                                toggle(f"{'✅' if children else '⚠️'} {ticker} — {company_name}", children[:50])
                            )

                    if company_blocks:
                        blocks.append(
                            toggle(f"📂 各公司详情 — {title}", company_blocks[:50])
                        )
            else:
                # 没有公司详情，直接渲染
                blocks.append(heading2(title))
                for sl in section_lines:
                    sl_stripped = sl.strip()
                    if not sl_stripped:
                        continue
                    if sl_stripped.startswith("> "):
                        blocks.append(callout(sl_stripped[2:], "⚠️"))
                    elif sl_stripped.startswith("- ") or sl_stripped.startswith("* "):
                        blocks.append(bullet(sl_stripped[2:]))
                    else:
                        blocks.append(paragraph(sl_stripped))
            continue

        # 普通行
        line_stripped = line.strip()
        if line_stripped.startswith("> "):
            blocks.append(callout(line_stripped[2:], "⚠️"))
        elif line_stripped.startswith("- ") or line_stripped.startswith("* "):
            blocks.append(bullet(line_stripped[2:]))
        elif line_stripped.startswith("---"):
            blocks.append(divider())
        else:
            blocks.append(paragraph(line_stripped))
        i += 1

    return blocks


def upload_blocks(page_id: str, blocks: list) -> None:
    """分批上传块到Notion（每批50个）。"""
    BATCH = 50
    total = len(blocks)
    print(f"共 {total} 个块，分批上传...")

    for start in range(0, total, BATCH):
        batch = blocks[start:start + BATCH]
        client.blocks.children.append(block_id=page_id, children=batch)
        print(f"已上传 {min(start + BATCH, total)}/{total}")


def main():
    report_path = Path(REPORT_FILE)
    if not report_path.exists():
        print(f"找不到报告文件：{REPORT_FILE}")
        sys.exit(1)

    md = report_path.read_text(encoding="utf-8")
    print(f"报告长度：{len(md)} 字符")

    print("解析报告...")
    blocks = parse_report(md)
    print(f"解析完成，共 {len(blocks)} 个块")

    print("上传到Notion...")
    upload_blocks(PAGE_ID, blocks)
    print("完成！")
    print(f"页面链接：https://notion.so/{PAGE_ID}")


if __name__ == "__main__":
    main()
