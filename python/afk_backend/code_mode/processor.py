"""Convert Parakeet transcripts into paste-ready source code.

The ASR model should stay focused on hearing words correctly. Code mode runs
after transcription and translates spoken programming tokens ("open paren",
"comment", "new line", "indent") into syntax. The deterministic pass handles
common commands locally; a later model pass can be layered on top without
changing the dictation flow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass
class CodeProcessor:
    language: str = "auto"

    def process(self, transcript: str, language: str | None = None) -> dict:
        source = _normalize(transcript)
        lang = (language or self.language or "auto").lower()
        code = _spoken_to_code(source, lang)
        code = _cleanup_code(code, lang)
        return {
            "text": code,
            "raw_text": transcript or "",
            "language": lang,
            "processor": "deterministic",
        }


_PHRASE_TOKENS: tuple[tuple[str, str], ...] = (
    ("open parentheses", "("),
    ("open parenthesis", "("),
    ("open paren", "("),
    ("left paren", "("),
    ("close parentheses", ")"),
    ("close parenthesis", ")"),
    ("close paren", ")"),
    ("right paren", ")"),
    ("open curly bracket", "{"),
    ("open curly brace", "{"),
    ("open curly", "{"),
    ("left curly", "{"),
    ("open brace", "{"),
    ("close curly bracket", "}"),
    ("close curly brace", "}"),
    ("close curly", "}"),
    ("right curly", "}"),
    ("close brace", "}"),
    ("open square bracket", "["),
    ("open bracket", "["),
    ("left bracket", "["),
    ("close square bracket", "]"),
    ("close bracket", "]"),
    ("right bracket", "]"),
    ("double quote", '"'),
    ("single quote", "'"),
    ("back tick", "`"),
    ("backtick", "`"),
    ("equals equals", "=="),
    ("double equals", "=="),
    ("triple equals", "==="),
    ("not equals", "!="),
    ("not equal", "!="),
    ("less than or equal to", "<="),
    ("greater than or equal to", ">="),
    ("less than", "<"),
    ("greater than", ">"),
    ("arrow", "->"),
    ("fat arrow", "=>"),
    ("plus equals", "+="),
    ("minus equals", "-="),
    ("times equals", "*="),
    ("divide equals", "/="),
    ("plus plus", "++"),
    ("minus minus", "--"),
    ("double slash", "//"),
    ("slash slash", "//"),
    ("forward slash", "/"),
    ("back slash", "\\"),
    ("asterisk", "*"),
    ("star", "*"),
    ("underscore", "_"),
    ("comma", ","),
    ("period", "."),
    ("dot", "."),
    ("colon", ":"),
    ("semicolon", ";"),
    ("semi colon", ";"),
    ("question mark", "?"),
    ("exclamation mark", "!"),
    ("exclamation point", "!"),
    ("hash", "#"),
    ("pound", "#"),
    ("at sign", "@"),
    ("dollar sign", "$"),
    ("percent", "%"),
    ("ampersand", "&"),
    ("pipe", "|"),
)

_LINE_BREAKS = (
    "new paragraph",
    "new line",
    "newline",
    "next line",
)


def _normalize(text: str) -> str:
    out = re.sub(r"\s+", " ", text or "").strip()
    out = out.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return out


def _spoken_to_code(text: str, language: str) -> str:
    if not text:
        return ""
    text = _replace_line_breaks(text)
    lines = []
    indent = 0
    block_indent_pending = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        lowered = line.lower()
        if lowered in {"dedent", "outdent"}:
            indent = max(0, indent - 1)
            continue
        while lowered.startswith(("dedent ", "outdent ")):
            indent = max(0, indent - 1)
            line = line.split(" ", 1)[1].strip()
            lowered = line.lower()
        while lowered.startswith("indent "):
            if block_indent_pending:
                block_indent_pending = False
            else:
                indent += 1
            line = line.split(" ", 1)[1].strip()
            lowered = line.lower()

        converted = _convert_line(line, language)
        if converted in {"}", "};"}:
            indent = max(0, indent - 1)
        lines.append(("    " * indent) + converted)
        if _opens_block(converted, language):
            indent += 1
            block_indent_pending = True
        elif converted:
            block_indent_pending = False
    return "\n".join(lines)


def _replace_line_breaks(text: str) -> str:
    out = text
    for phrase in _LINE_BREAKS:
        out = re.sub(rf"\s*\b{re.escape(phrase)}\b\s*", "\n", out, flags=re.I)
    return out


def _convert_line(line: str, language: str) -> str:
    comment_prefix = _comment_prefix(language)
    lower = line.lower()
    if lower.startswith("comment "):
        body = line.split(" ", 1)[1].strip()
        return f"{comment_prefix} {_sentence(body)}".rstrip()
    if lower in {"comment", "add comment"}:
        return f"{comment_prefix} "

    tokens = _replace_phrases(line)
    tokens = _join_identifier_commands(tokens)
    tokens = _space_code(tokens)
    tokens = _keyword_fixes(tokens, language)
    return tokens


def _replace_phrases(line: str) -> str:
    out = f" {line.lower()} "
    for phrase, token in sorted(_PHRASE_TOKENS, key=lambda item: len(item[0]), reverse=True):
        out = re.sub(rf"\b{re.escape(phrase)}\b", f" {token} ", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip()


def _join_identifier_commands(text: str) -> str:
    text = re.sub(r"\b([a-z_][a-z0-9_]*)\s+dot\s+([a-z_][a-z0-9_]*)\b", r"\1.\2", text)
    text = re.sub(r"\b([a-z_][a-z0-9_]*)\s+underscore\s+([a-z0-9_]+)\b", r"\1_\2", text)
    text = re.sub(r"\b([a-z_][a-z0-9_]*)\s+dash\s+([a-z0-9_]+)\b", r"\1-\2", text)
    return text


def _space_code(text: str) -> str:
    out = text
    out = re.sub(r"\s*([()\[\]{}.,:;?])\s*", r"\1", out)
    out = re.sub(r"\s*([=!<>+\-*/%&|]{1,3})\s*", r" \1 ", out)
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\(\s+", "(", out)
    out = re.sub(r"\s+\)", ")", out)
    out = re.sub(r"\[\s+", "[", out)
    out = re.sub(r"\s+\]", "]", out)
    out = re.sub(r"\{\s*", "{", out)
    out = re.sub(r"\s*\}", "}", out)
    out = re.sub(r"(?<=[\w\)])\{", " {", out)
    out = re.sub(r"\)\{", ") {", out)
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    out = re.sub(r"([,;:])(?=\S)", r"\1 ", out)
    out = out.replace(" ,", ",").replace(" .", ".")
    return out


def _keyword_fixes(text: str, language: str) -> str:
    out = text
    out = re.sub(r"\.is ([a-z_][a-z0-9_]*)\b", r".is_\1", out)
    out = re.sub(r"\bdef ([a-z_][a-z0-9_]*)\(", r"def \1(", out)
    out = re.sub(r"\bfunction ([a-z_][a-z0-9_]*)\(", r"function \1(", out)
    if language in {"python", "py"}:
        out = re.sub(r"\{\s*$", ":", out)
        out = out.replace(" true", " True").replace(" false", " False").replace(" none", " None")
    return out


def _cleanup_code(code: str, language: str) -> str:
    lines = [line.rstrip() for line in (code or "").splitlines()]
    code = "\n".join(lines).strip()
    code = re.sub(r"\n{3,}", "\n\n", code)
    return code


def _opens_block(line: str, language: str) -> bool:
    stripped = line.rstrip()
    if language in {"python", "py"}:
        return stripped.endswith(":")
    return stripped.endswith("{")


def _comment_prefix(language: str) -> str:
    if language in {"python", "py", "ruby", "rb", "shell", "bash", "sh"}:
        return "#"
    if language in {"html", "xml"}:
        return "<!--"
    return "//"


def _sentence(text: str) -> str:
    text = _replace_phrases(text)
    text = _space_code(text)
    return text[:1].upper() + text[1:] if text else ""
