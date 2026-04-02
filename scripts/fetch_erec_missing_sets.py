#!/usr/bin/env python3
import html
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import requests


LOGIN_URL = "https://e-rec123.jp/e-REC/Login.aspx?Auth=on"
TREE_URL = "https://e-rec123.jp/e-REC/Module/SubModule_Laboratory/TreeSearch_Extra.aspx?Round={round}"
PREVIEW_URL = (
    "https://e-rec123.jp/e-REC/Module/SubModule_Laboratory/"
    "Preview3_Extra.aspx?id={id}&Round={round}&Question={question}"
)
TARGET_ROUNDS = [100, 99, 98, 97]
REPO_ROOT = Path(__file__).resolve().parents[1]


class HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"br", "p", "div", "tr", "table", "li"}:
            self.parts.append("\n")
        elif tag == "img":
            alt = ""
            for key, value in attrs:
                if key == "alt" and value:
                    alt = value
                    break
            self.parts.append(alt or "[image]")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "tr", "table", "li", "td"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\r\n?", "\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(fragment: str) -> str:
    parser = HtmlToText()
    parser.feed(fragment)
    return parser.get_text()


def normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９，．", "0123456789,."))


def login_guest(session: requests.Session) -> None:
    response = session.get(LOGIN_URL, timeout=30)
    response.raise_for_status()
    html_text = response.text
    fields = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]:
        match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html_text)
        if not match:
            raise RuntimeError(f"missing {name} on login page")
        fields[name] = html.unescape(match.group(1))
    fields.update({"TextBox1": "", "TextBox2": "", "Button3": "ゲスト閲覧"})
    post = session.post(LOGIN_URL, data=fields, timeout=30, allow_redirects=True)
    if post.status_code not in {200, 301, 302}:
        raise RuntimeError(f"guest login failed: {post.status_code}")


def fetch_round_pages(session: requests.Session, round_num: int) -> list[tuple[int, int]]:
    last_count = 0
    for attempt in range(1, 6):
        response = session.get(TREE_URL.format(round=round_num), timeout=30)
        response.raise_for_status()
        matches = re.findall(
            rf"Preview3_Extra\.aspx\?id=(\d+)&Round={round_num}&Question=(\d+)",
            response.text,
        )
        if len(matches) == 345:
            seen = set()
            pages: list[tuple[int, int]] = []
            for id_value, question in matches:
                id_num = int(id_value)
                if id_num in seen:
                    continue
                seen.add(id_num)
                pages.append((id_num, int(question)))
            return pages
        last_count = len(matches)
        time.sleep(1.0 * attempt)
    raise RuntimeError(f"round {round_num}: expected 345 ids, got {last_count}")


def existing_questions(round_num: int) -> set[int]:
    round_dir = REPO_ROOT / f"set-{round_num}"
    found = set()
    for path in round_dir.glob(f"{round_num}-*.md"):
        match = re.fullmatch(rf"{round_num}-(\d+)\.md", path.name)
        if match:
            found.add(int(match.group(1)))
    return found


def extract_td(html_text: str, td_id: str) -> str:
    match = re.search(
        rf"<td id=['\"]{td_id}['\"][^>]*>(.*?)</td>",
        html_text,
        re.S,
    )
    if not match:
        raise RuntimeError(f"missing {td_id}")
    return match.group(1)


def split_question_and_choices(question_text: str) -> tuple[str, list[str]]:
    lines = [normalize_digits(line).strip() for line in question_text.splitlines() if line.strip()]
    question_lines: list[str] = []
    choices: list[str] = []
    for line in lines:
        match = re.match(r"^([1-9][0-9]?)\s*[.．]?\s*(.+)$", line)
        if match and 1 <= int(match.group(1)) <= 9:
            choices.append(match.group(2).strip())
        elif choices:
            choices[-1] = f"{choices[-1]} {line}".strip()
        else:
            question_lines.append(line)
    return "\n".join(question_lines).strip(), choices


def extract_answer_line(answer_text: str) -> tuple[str, str]:
    lines = [normalize_digits(line).strip() for line in answer_text.splitlines() if line.strip()]
    if not lines:
        return "", ""
    first = lines[0]
    match = re.match(r"^(?:問\s*\d+\s*)?解答\s*([0-9,\-、，・ ]+)", first)
    if match:
        answer = (
            match.group(1)
            .replace("、", ",")
            .replace("，", ",")
            .replace("・", ",")
            .replace(" ", "")
        )
        explanation = "\n".join(lines[1:]).strip()
        return answer, explanation
    return "", "\n".join(lines).strip()


def split_question_blocks(question_text: str, lead_question: int) -> dict[int, str]:
    lines = [normalize_digits(line).rstrip() for line in question_text.splitlines()]
    marker_indexes = [
        index for index, line in enumerate(lines) if re.match(r"^問\s*\d+(?:[（(].*)?$", line.strip())
    ]
    if not marker_indexes:
        return {lead_question: "\n".join(line for line in lines if line.strip()).strip()}
    common = "\n".join(line for line in lines[: marker_indexes[0]] if line.strip()).strip()
    blocks: dict[int, str] = {}
    starts = marker_indexes + [len(lines)]
    for start, end in zip(starts, starts[1:]):
        chunk_lines = [line for line in lines[start:end] if line.strip()]
        if not chunk_lines:
            continue
        match = re.match(r"^問\s*(\d+)", chunk_lines[0].strip())
        if not match:
            continue
        question_num = int(match.group(1))
        chunk = "\n".join(chunk_lines).strip()
        if common:
            chunk = f"{common}\n\n{chunk}"
        blocks[question_num] = chunk
    return blocks


def split_answer_blocks(answer_text: str, lead_question: int) -> dict[int, str]:
    lines = [normalize_digits(line).rstrip() for line in answer_text.splitlines()]
    marker_indexes = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^問\s*\d+\s*解答", line.strip())
    ]
    if not marker_indexes:
        return {lead_question: "\n".join(line for line in lines if line.strip()).strip()}
    blocks: dict[int, str] = {}
    starts = marker_indexes + [len(lines)]
    for start, end in zip(starts, starts[1:]):
        chunk_lines = [line for line in lines[start:end] if line.strip()]
        if not chunk_lines:
            continue
        match = re.match(r"^問\s*(\d+)\s*解答", chunk_lines[0].strip())
        if not match:
            continue
        question_num = int(match.group(1))
        blocks[question_num] = "\n".join(chunk_lines).strip()
    return blocks


def clean_question_block(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and re.match(r"^問\s*\d+(?:[（(].*)?$", lines[0].strip()):
        lines = lines[1:]
    return "\n".join(lines).strip()


def build_markdown(round_num: int, question_num: int, question_text: str, answer_text: str) -> str:
    problem, choices = split_question_and_choices(clean_question_block(question_text))
    answer, explanation = extract_answer_line(answer_text)
    has_image = (
        "[image]" in question_text
        or bool(re.search(r"\.(?:png|jpg|jpeg|gif)\b", question_text, re.I))
    )

    parts = [f"# 第{round_num}回 問{question_num}", "", "**問題文**", problem or question_text]
    if has_image:
        parts.extend(
            [
                "",
                f"> ⚠️ 画像を含む問題のため、図・構造式等はe-REC {round_num}-{question_num} を参照",
            ]
        )
    if choices:
        parts.extend(["", "**選択肢**"])
        for idx, choice in enumerate(choices, 1):
            parts.append(f"{idx}. {choice}")
    if answer:
        parts.extend(["", f"**正解:** {answer}"])
    if explanation:
        parts.extend(["", "**解説**", explanation])
    parts.extend(["", "---", f"*e-RECより自動取得（{time.strftime('%Y-%m-%d')}）*"])
    return "\n".join(parts).rstrip() + "\n"


def fetch_preview(session: requests.Session, round_num: int, question_num: int, id_value: int) -> str:
    url = PREVIEW_URL.format(id=id_value, round=round_num, question=question_num)
    last_error = None
    for attempt in range(1, 6):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            if "Sentence_Q_Css" in response.text and "Sentence_A_Css" in response.text:
                return response.text
            last_error = RuntimeError("preview payload missing expected sections")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(1.0 * attempt)
    raise RuntimeError(
        f"failed to fetch preview for round={round_num} question={question_num} id={id_value}"
    ) from last_error


def build_markdowns_from_page(round_num: int, lead_question: int, html_text: str) -> dict[int, str]:
    question_html = extract_td(html_text, "Sentence_Q_Css")
    answer_html = extract_td(html_text, "Sentence_A_Css")
    question_blocks = split_question_blocks(html_to_text(question_html), lead_question)
    answer_blocks = split_answer_blocks(html_to_text(answer_html), lead_question)
    docs: dict[int, str] = {}
    for question_num, question_text in question_blocks.items():
        docs[question_num] = build_markdown(
            round_num,
            question_num,
            question_text,
            answer_blocks.get(question_num, ""),
        )
    return docs


def main() -> int:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    login_guest(session)

    total_written = 0
    for round_num in TARGET_ROUNDS:
        round_dir = REPO_ROOT / f"set-{round_num}"
        round_dir.mkdir(parents=True, exist_ok=True)
        pages = fetch_round_pages(session, round_num)
        existing = existing_questions(round_num)
        print(f"round {round_num}: pages {len(pages)}, missing {345 - len(existing)}")
        for index, (id_value, lead_question) in enumerate(pages, 1):
            html_text = fetch_preview(session, round_num, lead_question, id_value)
            docs = build_markdowns_from_page(round_num, lead_question, html_text)
            for question_num, md in docs.items():
                if question_num in existing:
                    continue
                output_path = round_dir / f"{round_num}-{question_num}.md"
                output_path.write_text(md, encoding="utf-8")
                existing.add(question_num)
                total_written += 1
            if index % 20 == 0 or index == len(pages):
                print(
                    f"  fetched pages {index}/{len(pages)} for round {round_num} "
                    f"(now {len(existing)}/345)",
                    flush=True,
                )
            time.sleep(0.15)
    print(f"done: wrote {total_written} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
