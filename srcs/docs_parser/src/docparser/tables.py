"""Table HTML -> cell grid + Markdown, preserving merged cells.

PaddleOCR-VL returns tables as HTML with ``colspan``/``rowspan``. That form is
faithful but awkward to chunk, and Markdown has no way to express a span at all.
So each table is kept in three forms (see :class:`docparser.schema.Table`):

* **html**     - verbatim, the lossless record.
* **cells**    - an explicit grid: every cell with its ``row``, ``col``,
                 ``row_span`` and ``col_span``. This is what a RAG chunker
                 should read, because it can tell that one value governs three
                 rows without having to parse HTML.
* **markdown** - GFM. A merged cell is *repeated* across every grid position it
                 covers rather than being left blank.

That last choice matters. Given a lot header spanning three article rows, the
naive rendering leaves two cells empty, and a chunk starting mid-table then
loses the lot it belongs to. Repeating the value keeps every row independently
meaningful, which is what retrieval needs -- at the cost of a table that looks
slightly redundant to a human. ``has_merged_cells`` records that the Markdown
is a lossy view, and the cell grid remains available for anyone who needs the
exact geometry.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

from .schema import Cell, Table


class _TableHTMLParser(HTMLParser):
    """Collect (row, is_header, text, colspan, rowspan) tuples from table HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._in_thead = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "thead":
            self._in_thead = True
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:  # a <td> outside any <tr>: tolerate it
                self._row = []
            self._cell = {
                "text": [],
                "colspan": _to_int(a.get("colspan"), 1),
                "rowspan": _to_int(a.get("rowspan"), 1),
                "is_header": tag == "th" or self._in_thead,
            }

    def handle_endtag(self, tag: str) -> None:
        if tag == "thead":
            self._in_thead = False
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)

    def close(self) -> None:  # flush an unterminated trailing row
        super().close()
        if self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        if self._row:
            self.rows.append(self._row)
            self._row = None


def _to_int(value: str | None, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _clean(text: str) -> str:
    """Collapse whitespace and unwrap the LaTeX math the VL model emits."""
    text = text.replace("\n", " ")
    # PaddleOCR-VL writes inline math as "$ \times $"; for a table cell the
    # symbol itself is what matters, not the markup.
    text = re.sub(r"\$\s*\\times\s*\$", "x", text)
    text = re.sub(r"\$\s*([^$]*?)\s*\$", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_table_html(html: str) -> Table:
    """Turn a table's HTML into a :class:`Table` with an explicit cell grid."""
    parser = _TableHTMLParser()
    parser.feed(html or "")
    parser.close()

    # Lay the cells onto a grid, honouring spans. `occupied` tracks positions
    # already claimed by a cell spanning down or across from earlier in the grid.
    cells: list[Cell] = []
    occupied: set[tuple[int, int]] = set()
    n_cols = 0
    has_merged = False

    for r, raw_row in enumerate(parser.rows):
        c = 0
        for raw in raw_row:
            while (r, c) in occupied:  # skip positions covered by a span
                c += 1

            colspan, rowspan = raw["colspan"], raw["rowspan"]
            if colspan > 1 or rowspan > 1:
                has_merged = True

            cells.append(
                Cell(
                    row=r,
                    col=c,
                    row_span=rowspan,
                    col_span=colspan,
                    text=_clean("".join(raw["text"])),
                    is_header=raw["is_header"],
                )
            )
            for dr in range(rowspan):
                for dc in range(colspan):
                    occupied.add((r + dr, c + dc))

            c += colspan
            n_cols = max(n_cols, c)

    n_rows = max((c.row + c.row_span for c in cells), default=0)
    return Table(
        n_rows=n_rows,
        n_cols=n_cols,
        cells=cells,
        html=html or "",
        markdown=cells_to_markdown(cells, n_rows, n_cols),
        has_merged_cells=has_merged,
    )


def cells_to_html(cells: list[Cell], n_rows: int, n_cols: int) -> str:
    """Rebuild table HTML from a cell grid, spans included.

    Used for a table stitched back together from fragments (see
    :mod:`docparser.merge`): the fragments' HTML no longer describes anything
    that exists, so the merged grid becomes the record instead. Round-trips
    through :func:`parse_table_html`.
    """
    if not cells or n_rows == 0 or n_cols == 0:
        return ""

    by_row: dict[int, list[Cell]] = {}
    for cell in cells:
        by_row.setdefault(cell.row, []).append(cell)

    out = ["<table>"]
    for r in range(n_rows):
        out.append("<tr>")
        for cell in sorted(by_row.get(r, []), key=lambda c: c.col):
            tag = "th" if cell.is_header else "td"
            attrs = ""
            if cell.col_span > 1:
                attrs += f' colspan="{cell.col_span}"'
            if cell.row_span > 1:
                attrs += f' rowspan="{cell.row_span}"'
            out.append(f"<{tag}{attrs}>{escape(cell.text)}</{tag}>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def cells_to_markdown(cells: list[Cell], n_rows: int, n_cols: int) -> str:
    """Render the cell grid as a GFM table, repeating merged values."""
    if not cells or n_rows == 0 or n_cols == 0:
        return ""

    grid: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for cell in cells:
        text = cell.text.replace("|", "\\|")  # a literal pipe would break the row
        for dr in range(cell.row_span):
            for dc in range(cell.col_span):
                r, c = cell.row + dr, cell.col + dc
                if r < n_rows and c < n_cols:
                    grid[r][c] = text

    # GFM requires a header row. Use the table's own header cells when it has
    # them; otherwise promote the first row, which is the usual convention.
    header_rows = {c.row for c in cells if c.is_header}
    header_idx = min(header_rows) if header_rows else 0

    lines = [
        "| " + " | ".join(grid[header_idx]) + " |",
        "| " + " | ".join("---" for _ in range(n_cols)) + " |",
    ]
    lines.extend(
        "| " + " | ".join(row) + " |"
        for i, row in enumerate(grid)
        if i != header_idx
    )
    return "\n".join(lines)
