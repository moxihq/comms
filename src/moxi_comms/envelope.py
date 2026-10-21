"""The treatment untrusted text gets before it is rendered into an agent's context.

Two surfaces render text comms did not write into a context comms does not own: a card
(`cards.py`, `moxi comms whois`) and a delivered message body (`adapters/claude_shim.py`,
the digest a session reads as a turn). Both put somebody else's prose next to comms'
own lines, and in both the reader is a model that has no structural way to tell the two
apart. So both get the same three things, and they live here because a second copy of
them is a second thing to forget:

  * **A two-space INDENT on every body line.** This is the whole forgery defense, and it
    is a positional one: comms' own lines start at column zero and body lines never do.
    A body may contain the exact text of a frame line, a footer or a notice -- it will
    render indented, and the reader's rule is that only column zero is comms speaking.
    Nothing about the text is inspected, rejected or rewritten to achieve that, which is
    why it holds for text nobody anticipated.
  * **A closed envelope.** Text that is opened and never closed has no end a reader can
    point at, so the line after it inherits whatever standing the opening line had. Each
    surface owns its own closing line (`cards.FOOTER`, `claude_shim.end_line`); what is
    shared is that there is one, that it sits at column zero, and that every comms-
    authored line renders inside the envelope rather than after it.
  * **A control- and format-character scrub.** Rendered text is not a terminal program
    and not a typographic trick. `Cc` takes the escape sequences and the C1 CSI; `Cf`
    takes the bidi overrides and isolates, the zero-widths and the BOM -- the characters
    that let text lie about its own shape without being visible in it, including making
    an indented line look as though it began at column zero.

The pushed surface -- a body pasted unsolicited into a session that may be running with
permissions bypassed -- needs this at least as much as the pull-only card surface does.
Sharing the code is what keeps the two from drifting apart.

This module imports nothing from the package (`unicodedata` and nothing else), which is
what lets both a leaf reader and an adapter on the delivery path use it: `cards.py`'s
import closure is a fence (`test_cards.py::test_only_the_cli_imports_the_card_reader`),
and a shared helper that pulled anything behind it would have broken it.
"""

from __future__ import annotations

import unicodedata

#: Body lines sit here; comms' own lines sit at column zero. See the module docstring --
#: this is the forgery defense, not decoration.
INDENT = "  "

# Every C0 control character except the newline text is split on, plus DEL and the C1
# range. C1 is here because U+009B is a single-character CSI on its own.
_STRIP = {c: None for c in range(0x20) if c != 0x0A}
_STRIP[0x7F] = None
_STRIP.update({c: None for c in range(0x80, 0xA0)})
# ...and beyond the control ranges, Unicode FORMAT characters (category Cf): the bidi
# overrides and isolates (U+202E, U+2066-69), the zero-widths (U+200B-0F), the BOM.
# Leading spaces are bidi-neutral, so under an RLO a terminal can render an indented line
# as though it began at column zero. The reading MODEL sees the codepoints either way, so
# this is the operator's terminal being defended -- but a stripping rule that stops at C0
# is not "this is text" in any useful sense.
_STRIPPED_CATEGORIES = ("Cc", "Cf")


def scrub(line: str) -> str:
    """One line with control and format characters removed. Nothing else is touched.

    Two passes because the second costs something: the control ranges go by translate
    table, and only a line carrying non-ASCII pays for a category lookup per character.
    """
    line = line.translate(_STRIP)
    if line.isascii():
        return line
    return "".join(c for c in line
                   if unicodedata.category(c) not in _STRIPPED_CATEGORIES)


def indent(line: str) -> str:
    """A body line, indented. A line with nothing on it renders empty rather than as two
    spaces: trailing whitespace is noise in a diff and in a reader's terminal alike."""
    return f"{INDENT}{line}" if line.strip() else ""


def body(text: str) -> list[str]:
    """A whole untrusted body as the lines to render: scrubbed, indented, in order.

    `\\r` goes with the split so a CRLF body does not carry a stray return into the
    render, and an empty body yields one empty line rather than nothing -- the envelope
    stays two lines around a gap, never a frame line with a footer welded to it.
    """
    return [indent(scrub(line.rstrip("\r"))) for line in text.split("\n")]
