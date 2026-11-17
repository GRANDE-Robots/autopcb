"""Read KiCad S-expressions while preserving the original source text.

The parser records spans instead of reformatting the document.  Callers can
therefore edit individual expressions without rewriting unrelated geometry,
UUIDs, formatting, or line endings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True, slots=True)
class Atom:
    """A quoted or unquoted token in the original text."""

    start: int
    end: int
    value: str


@dataclass(frozen=True, slots=True)
class Form:
    """One complete parenthesized expression and its direct contents."""

    start: int
    end: int
    items: tuple[Atom | Form, ...]

    @property
    def name(self) -> str:
        if not self.items or not isinstance(self.items[0], Atom):
            return ""
        return self.items[0].value

    def atoms(self) -> tuple[Atom, ...]:
        return tuple(item for item in self.items if isinstance(item, Atom))

    def children(self, name: str | None = None) -> tuple[Form, ...]:
        return tuple(
            item
            for item in self.items
            if isinstance(item, Form) and (name is None or item.name == name)
        )

    def descendants(self) -> Iterator[Form]:
        """Yield nested forms without depending on Python's recursion limit."""
        pending = list(reversed(self.children()))
        while pending:
            current = pending.pop()
            yield current
            pending.extend(reversed(current.children()))


def quote(value: str) -> str:
    """Encode a text property as a quoted KiCad string."""
    return json.dumps(value, ensure_ascii=False)


def _unquote(text: str) -> str:
    """Decode common KiCad escapes and preserve unfamiliar escape sequences."""
    value: list[str] = []
    escapes = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
    index = 1
    while index < len(text) - 1:
        character = text[index]
        if character == "\\" and index + 1 < len(text) - 1:
            following = text[index + 1]
            value.append(escapes.get(following, "\\" + following))
            index += 2
        else:
            value.append(character)
            index += 1
    return "".join(value)


_SCAN_TOKEN = re.compile(r'\s+|\(|\)|"(?:[^"\\]|\\[\s\S])*"|[^()\s"]+')


def select_direct_children(source: str, name: str) -> tuple[str, ...]:
    """Select root children while validating syntax without building the full tree."""
    if not isinstance(source, str) or not isinstance(name, str) or not name:
        raise ValueError("A source and nonempty child name are required")
    cursor = 1 if source.startswith("\ufeff") else 0
    pending: list[bool] = []
    starts: list[int] = []
    selected: list[bool] = []
    result: list[str] = []
    root_closed = False
    for token in _SCAN_TOKEN.finditer(source, cursor):
        if token.start() != cursor:
            raise ValueError(f"Unexpected character at {cursor}")
        cursor = token.end()
        first = source[token.start()]
        if first.isspace():
            continue
        if first == "(":
            if root_closed or (pending and pending[-1]):
                raise ValueError(f"Invalid expression at {token.start()}")
            starts.append(token.start())
            pending.append(True)
            selected.append(False)
        elif first == ")":
            if not pending or pending[-1]:
                raise ValueError(f"Invalid closing expression at {token.start()}")
            if len(pending) == 2 and selected[-1]:
                result.append(source[starts[-1]:token.end()])
            starts.pop()
            pending.pop()
            selected.pop()
            if not pending:
                root_closed = True
        else:
            if not pending:
                raise ValueError(f"Unexpected text outside S-expression at {token.start()}")
            if pending[-1] and len(pending) == 2:
                atom = token.group()
                selected[-1] = (_unquote(atom) if first == '"' else atom) == name
            pending[-1] = False
    if cursor != len(source) or pending or not root_closed:
        raise ValueError("Unclosed, malformed, or missing S-expression")
    return tuple(result)


def parse(source: str) -> Form:
    """Parse exactly one expression, rejecting extra or unbalanced text."""
    stack: list[tuple[int, list[Atom | Form]]] = []
    root: Form | None = None
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace() or (index == 0 and character == "\ufeff"):
            index += 1
            continue
        if character == "(":
            if root is not None and not stack:
                raise ValueError("Multiple top-level S-expressions")
            stack.append((index, []))
            index += 1
            continue
        if character == ")":
            if not stack:
                raise ValueError(f"Unexpected ')' at character {index}")
            start, items = stack.pop()
            if not items or not isinstance(items[0], Atom):
                raise ValueError(f"Expression has no name at character {start}")
            form = Form(start, index + 1, tuple(items))
            if stack:
                stack[-1][1].append(form)
            elif root is None:
                root = form
            else:
                raise ValueError("Multiple top-level S-expressions")
            index += 1
            continue
        if not stack:
            raise ValueError(
                f"Unexpected text outside S-expression at character {index}"
            )
        start = index
        if character == '"':
            index += 1
            escaped = False
            while index < len(source):
                current = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
            else:
                raise ValueError(f"Unterminated quoted string at character {start}")
            token = source[start:index]
            value = _unquote(token)
        else:
            while (
                index < len(source)
                and not source[index].isspace()
                and source[index] not in '()"'
            ):
                index += 1
            if index == start:
                raise ValueError(f"Unexpected character at {index}")
            value = source[start:index]
        stack[-1][1].append(Atom(start, index, value))
    if stack or root is None:
        raise ValueError("Unclosed or missing S-expression")
    return root
