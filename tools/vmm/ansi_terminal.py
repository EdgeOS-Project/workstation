#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) EdgeOS Contributors.
"""ANSI terminal renderer with xterm 256-color support for the VMM console."""

from __future__ import annotations

from dataclasses import dataclass

try:
    from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
    from PyQt6.QtWidgets import QTextEdit
except ImportError:
    from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
    from PySide6.QtWidgets import QTextEdit


DEFAULT_FOREGROUND = QColor("#d9e2e8")
DEFAULT_BACKGROUND = QColor("#151b20")

ANSI_COLORS = (
    "#000000",
    "#cd3131",
    "#0dbc79",
    "#e5e510",
    "#2472c8",
    "#bc3fbc",
    "#11a8cd",
    "#e5e5e5",
    "#666666",
    "#f14c4c",
    "#23d18b",
    "#f5f543",
    "#3b8eea",
    "#d670d6",
    "#29b8db",
    "#ffffff",
)


def xterm_color(index: int) -> QColor:
    """Return the RGB color defined by the xterm 256-color palette."""
    index = max(0, min(255, index))
    if index < 16:
        return QColor(ANSI_COLORS[index])
    if index < 232:
        cube = index - 16
        levels = (0, 95, 135, 175, 215, 255)
        red = levels[cube // 36]
        green = levels[(cube // 6) % 6]
        blue = levels[cube % 6]
        return QColor(red, green, blue)
    level = 8 + (index - 232) * 10
    return QColor(level, level, level)


@dataclass
class TerminalStyle:
    foreground: QColor | None = None
    background: QColor | None = None
    bold: bool = False
    faint: bool = False
    italic: bool = False
    underline: bool = False
    inverse: bool = False
    conceal: bool = False
    strikeout: bool = False


class AnsiTerminal(QTextEdit):
    """Render incremental ANSI/ECMA-48 terminal output in a Qt text document."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setAcceptRichText(False)
        self.document().setMaximumBlockCount(12000)
        self._cursor = QTextCursor(self.document())
        self._style = TerminalStyle()
        self._escape = ""
        self._escape_kind = ""
        self._osc_escaped = False

    def write(self, text: str) -> None:
        """Consume text and ANSI control sequences without losing split escapes."""
        if not text:
            return
        scroll = self.verticalScrollBar()
        follow_output = scroll.value() >= scroll.maximum() - 2
        index = 0
        length = len(text)
        while index < length:
            if self._escape_kind:
                self._consume(text[index])
                index += 1
                continue
            run_start = index
            while index < length and text[index] >= " " and text[index] != "\x7f":
                index += 1
            if index > run_start:
                self._insert_text(text[run_start:index])
            if index < length:
                self._consume(text[index])
                index += 1
        if follow_output:
            self.setTextCursor(self._cursor)
            self.ensureCursorVisible()

    def append_plain_line(self, text: str) -> None:
        if self._cursor.positionInBlock() != 0 or self._cursor.block().text():
            self.write("\n")
        self.write(text)
        self.write("\n")

    def reset_terminal(self) -> None:
        self.clear()
        self._cursor = QTextCursor(self.document())
        self._style = TerminalStyle()
        self._escape = ""
        self._escape_kind = ""
        self._osc_escaped = False

    def _consume(self, character: str) -> None:
        if self._escape_kind == "osc":
            if character == "\a" or (self._osc_escaped and character == "\\"):
                self._escape = ""
                self._escape_kind = ""
                self._osc_escaped = False
                return
            self._osc_escaped = character == "\x1b"
            if len(self._escape) < 8192:
                self._escape += character
            else:
                self._escape = ""
                self._escape_kind = ""
            return

        if self._escape_kind == "csi":
            self._escape += character
            if "@" <= character <= "~":
                self._handle_csi(self._escape[2:-1], character)
                self._escape = ""
                self._escape_kind = ""
            elif len(self._escape) > 128:
                self._escape = ""
                self._escape_kind = ""
            return

        if self._escape_kind == "esc":
            self._escape += character
            if character == "[":
                self._escape_kind = "csi"
            elif character == "]":
                self._escape_kind = "osc"
            else:
                self._handle_escape(character)
                self._escape = ""
                self._escape_kind = ""
            return

        if character == "\x1b":
            self._escape = character
            self._escape_kind = "esc"
        elif character == "\r":
            self._cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        elif character == "\n":
            self._cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            self._cursor.insertBlock()
        elif character == "\b":
            if self._cursor.positionInBlock() > 0:
                self._cursor.deletePreviousChar()
        elif character == "\t":
            spaces = 8 - (self._cursor.positionInBlock() % 8)
            self._insert_text(" " * spaces)
        elif character >= " " and character != "\x7f":
            self._insert_text(character)

    def _insert_text(self, text: str) -> None:
        if not text:
            return
        remaining = len(self._cursor.block().text()) - self._cursor.positionInBlock()
        if remaining > 0:
            self._cursor.movePosition(
                QTextCursor.MoveOperation.Right,
                QTextCursor.MoveMode.KeepAnchor,
                min(remaining, len(text)),
            )
            self._cursor.removeSelectedText()
        self._cursor.insertText(text, self._character_format())

    def _handle_escape(self, final: str) -> None:
        if final == "c":
            self.reset_terminal()
        elif final == "D":
            self._cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            self._cursor.insertBlock()
        elif final == "E":
            self._cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            self._cursor.insertBlock()
        elif final == "M" and self._cursor.blockNumber() > 0:
            self._cursor.movePosition(QTextCursor.MoveOperation.PreviousBlock)

    def _handle_csi(self, raw_parameters: str, final: str) -> None:
        private = raw_parameters.startswith(("?", ">", "!"))
        if private:
            raw_parameters = raw_parameters[1:]
        parameters = self._parameters(raw_parameters)
        if final == "m":
            self._handle_sgr(parameters)
        elif final == "K":
            self._erase_line(parameters[0] if parameters else 0)
        elif final == "J":
            self._erase_display(parameters[0] if parameters else 0)
        elif final in ("G", "`"):
            self._move_to_column(max(1, parameters[0] if parameters else 1))
        elif final == "C":
            self._move_horizontal(parameters[0] if parameters else 1)
        elif final == "D":
            self._move_horizontal(-(parameters[0] if parameters else 1))
        elif final == "A":
            self._move_vertical(-(parameters[0] if parameters else 1))
        elif final == "B":
            self._move_vertical(parameters[0] if parameters else 1)
        elif final in ("H", "f") and not private:
            row = parameters[0] if parameters else 1
            column = parameters[1] if len(parameters) > 1 else 1
            self._move_to_position(row, column)

    @staticmethod
    def _parameters(raw: str) -> list[int]:
        if raw == "":
            return [0]
        values: list[int] = []
        for item in raw.replace(":", ";").split(";"):
            try:
                values.append(int(item) if item else 0)
            except ValueError:
                values.append(0)
        return values

    def _handle_sgr(self, parameters: list[int]) -> None:
        if not parameters:
            parameters = [0]
        index = 0
        while index < len(parameters):
            code = parameters[index]
            if code == 0:
                self._style = TerminalStyle()
            elif code == 1:
                self._style.bold = True
            elif code == 2:
                self._style.faint = True
            elif code == 3:
                self._style.italic = True
            elif code == 4:
                self._style.underline = True
            elif code == 7:
                self._style.inverse = True
            elif code == 8:
                self._style.conceal = True
            elif code == 9:
                self._style.strikeout = True
            elif code == 22:
                self._style.bold = False
                self._style.faint = False
            elif code == 23:
                self._style.italic = False
            elif code == 24:
                self._style.underline = False
            elif code == 27:
                self._style.inverse = False
            elif code == 28:
                self._style.conceal = False
            elif code == 29:
                self._style.strikeout = False
            elif 30 <= code <= 37:
                self._style.foreground = xterm_color(code - 30)
            elif code == 39:
                self._style.foreground = None
            elif 40 <= code <= 47:
                self._style.background = xterm_color(code - 40)
            elif code == 49:
                self._style.background = None
            elif 90 <= code <= 97:
                self._style.foreground = xterm_color(code - 90 + 8)
            elif 100 <= code <= 107:
                self._style.background = xterm_color(code - 100 + 8)
            elif code in (38, 48):
                consumed, color = self._extended_color(parameters, index + 1)
                if color is not None:
                    if code == 38:
                        self._style.foreground = color
                    else:
                        self._style.background = color
                index += consumed
            index += 1

    @staticmethod
    def _extended_color(parameters: list[int], start: int) -> tuple[int, QColor | None]:
        if start >= len(parameters):
            return 0, None
        mode = parameters[start]
        if mode == 5 and start + 1 < len(parameters):
            return 2, xterm_color(parameters[start + 1])
        if mode == 2 and start + 3 < len(parameters):
            red, green, blue = parameters[start + 1:start + 4]
            return 4, QColor(
                max(0, min(255, red)),
                max(0, min(255, green)),
                max(0, min(255, blue)),
            )
        return 1, None

    def _character_format(self) -> QTextCharFormat:
        foreground = self._style.foreground or DEFAULT_FOREGROUND
        background = self._style.background or DEFAULT_BACKGROUND
        if self._style.inverse:
            foreground, background = background, foreground
        if self._style.conceal:
            foreground = background
        char_format = QTextCharFormat()
        char_format.setForeground(foreground)
        char_format.setBackground(background)
        char_format.setFontWeight(QFont.Weight.Bold if self._style.bold else QFont.Weight.Normal)
        char_format.setFontItalic(self._style.italic)
        char_format.setFontUnderline(self._style.underline)
        char_format.setFontStrikeOut(self._style.strikeout)
        if self._style.faint:
            char_format.setForeground(QColor(
                (foreground.red() + background.red()) // 2,
                (foreground.green() + background.green()) // 2,
                (foreground.blue() + background.blue()) // 2,
            ))
        return char_format

    def _erase_line(self, mode: int) -> None:
        cursor = QTextCursor(self._cursor)
        if mode == 0:
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        elif mode == 1:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            self._cursor = QTextCursor(cursor)
        cursor.removeSelectedText()

    def _erase_display(self, mode: int) -> None:
        if mode in (2, 3):
            self.reset_terminal()
            return
        cursor = QTextCursor(self._cursor)
        target = QTextCursor.MoveOperation.End if mode == 0 else QTextCursor.MoveOperation.Start
        cursor.movePosition(target, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()

    def _move_to_column(self, column: int) -> None:
        self._cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        self._cursor.movePosition(
            QTextCursor.MoveOperation.Right,
            QTextCursor.MoveMode.MoveAnchor,
            min(column - 1, len(self._cursor.block().text())),
        )

    def _move_horizontal(self, amount: int) -> None:
        operation = QTextCursor.MoveOperation.Right if amount > 0 else QTextCursor.MoveOperation.Left
        self._cursor.movePosition(operation, QTextCursor.MoveMode.MoveAnchor, abs(amount))

    def _move_vertical(self, amount: int) -> None:
        column = self._cursor.positionInBlock()
        operation = QTextCursor.MoveOperation.NextBlock if amount > 0 else QTextCursor.MoveOperation.PreviousBlock
        self._cursor.movePosition(operation, QTextCursor.MoveMode.MoveAnchor, abs(amount))
        self._move_to_column(column + 1)

    def _move_to_position(self, row: int, column: int) -> None:
        self._cursor.movePosition(QTextCursor.MoveOperation.Start)
        self._cursor.movePosition(
            QTextCursor.MoveOperation.NextBlock,
            QTextCursor.MoveMode.MoveAnchor,
            max(0, row - 1),
        )
        self._move_to_column(column)
