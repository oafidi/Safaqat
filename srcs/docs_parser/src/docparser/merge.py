"""Stitch tables that a page break split in two.

A price schedule or a bordereau routinely runs over three or four pages. The
layout model sees one table per page and says nothing about the relationship
between them, so without this pass the output holds four unrelated tables --
three of which begin mid-data, and one of which (the last) is a fragment with no
header at all. A chunker cannot recover from that: a retrieved row from page 12
has lost the column it belongs to.

Two fragments are joined only when every one of these holds:

1. they sit on consecutive pages;
2. the first is the *last* table on its page and the second is the *first* on
   its own -- a table that ends above other tables did not run off the page;
3. the first runs to the bottom of its page and the second starts at the top of
   its own: a table that stops halfway down, leaving the rest of the page empty,
   stopped because it had nothing more to say;
4. nothing but furniture lies in the gap between them (below one, above the
   other): a paragraph there means the first table ended;
5. they are the same width, within a tolerance, relative to the page;
6. they have the same number of columns.

Rules 5 and 6 are what separate a continuation from two unrelated tables that
happen to straddle a page break. Every rule is a veto, so the failure mode is
leaving a table split -- the status quo -- rather than fusing two tables that
have nothing to do with each other.

Merging then has to undo two things the page break did:

* **the repeated header** -- most continuations reprint it, and it must not
  become a data row in the middle of the merged table;
* **the severed rowspan** -- a cell merged across ten rows ("Lot n° 6") that is
  cut by the break leaves the rows on the next page with an empty cell where
  their lot should be. See :func:`_restitch_rowspans`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace

from .schema import Block, Cell, Page, Table
from .tables import cells_to_html, cells_to_markdown

log = logging.getLogger("docparser")

# Slack, in PDF points, when asking whether a block sits below or above a table.
# Layout boxes overlap by a point or two all the time; without this, a table's
# own caption band can look like content in the gap.
_EDGE_SLACK = 4.0

# Fraction of cells that must match for two rows to be "the same row", used to
# recognise a header reprinted on the next page. Not 1.0: the OCR of a repeated
# header is rarely character-identical across two pages.
_HEADER_MATCH = 0.7


def merge_split_tables(
    blocks: list[Block],
    pages: list[Page],
    width_tolerance: float = 0.15,
    edge_margin: float = 0.25,
) -> list[Block]:
    """Fuse each run of table fragments into the single table it really is.

    The first fragment's block absorbs the others: it keeps its own id, page and
    bbox (so a citation still points at where the table *starts*), and its
    ``table`` is replaced by the merged one, which records every page it covers
    in ``Table.pages``. The continuation blocks are dropped from the list.

    Returns the new block list. ``blocks`` must be in reading order.
    """
    sizes = {p.number: (p.width, p.height) for p in pages}
    by_page: dict[int, list[Block]] = defaultdict(list)
    for block in blocks:
        by_page[block.page].append(block)

    groups = _group_fragments(blocks, by_page, sizes, width_tolerance, edge_margin)
    if not groups:
        return blocks

    absorbed: set[str] = set()
    for group in groups:
        head = group[0]
        head.table = _merge_group(group)
        head.text = head.table.markdown
        absorbed.update(b.id for b in group[1:])
        log.info(
            "table %s: merged %d fragments across pages %s",
            head.id,
            len(group),
            ", ".join(str(b.page) for b in group),
        )

    return [b for b in blocks if b.id not in absorbed]


# -- deciding what continues what ----------------------------------------
def _group_fragments(
    blocks: list[Block],
    by_page: dict[int, list[Block]],
    sizes: dict[int, tuple[float, float]],
    width_tolerance: float,
    edge_margin: float,
) -> list[list[Block]]:
    """Split the document's tables into runs of fragments. Runs of 1 are dropped."""
    groups: list[list[Block]] = []

    for block in blocks:
        if block.type != "table" or block.table is None or not block.table.cells:
            continue
        if groups and _continues(
            groups[-1][-1], block, by_page, sizes, width_tolerance, edge_margin
        ):
            groups[-1].append(block)
        else:
            groups.append([block])

    return [g for g in groups if len(g) > 1]


def _continues(
    head: Block,
    tail: Block,
    by_page: dict[int, list[Block]],
    sizes: dict[int, tuple[float, float]],
    width_tolerance: float,
    edge_margin: float,
) -> bool:
    """Is *tail* the continuation of *head* on the following page?"""
    if tail.page != head.page + 1:
        return False

    assert head.table is not None and tail.table is not None
    if head.table.n_cols != tail.table.n_cols:
        return False

    if head.page not in sizes or tail.page not in sizes:
        return False  # nothing to measure against; leave the table alone
    (head_w, head_h), (tail_w, tail_h) = sizes[head.page], sizes[tail.page]
    if not head_w or not tail_w or not head_h or not tail_h:
        return False

    if head.bbox[3] < head_h * (1 - edge_margin) or tail.bbox[1] > tail_h * edge_margin:
        return False  # one of them sits in the middle of its page: no break to bridge

    head_ratio = (head.bbox[2] - head.bbox[0]) / head_w
    tail_ratio = (tail.bbox[2] - tail.bbox[0]) / tail_w
    if abs(head_ratio - tail_ratio) > width_tolerance:
        return False

    if not _is_last_table_on_page(head, by_page) or not _is_first_table_on_page(tail, by_page):
        return False

    return _gap_is_clear(head, tail, by_page)


def _is_last_table_on_page(block: Block, by_page: dict[int, list[Block]]) -> bool:
    return not any(
        b.type == "table" and b is not block and b.bbox[1] >= block.bbox[3] - _EDGE_SLACK
        for b in by_page[block.page]
    )


def _is_first_table_on_page(block: Block, by_page: dict[int, list[Block]]) -> bool:
    return not any(
        b.type == "table" and b is not block and b.bbox[3] <= block.bbox[1] + _EDGE_SLACK
        for b in by_page[block.page]
    )


def _gap_is_clear(head: Block, tail: Block, by_page: dict[int, list[Block]]) -> bool:
    """True when only furniture lies between the bottom of *head* and the top of *tail*.

    The gap is a physical region, not a range of reading order: text printed
    *above* the table on the head's page, or *below* it on the tail's page, is
    part of neither and is ignored.
    """
    for block in by_page[head.page]:
        if block is not head and _is_content(block) and block.bbox[1] >= head.bbox[3] - _EDGE_SLACK:
            return False

    for block in by_page[tail.page]:
        if block is not tail and _is_content(block) and block.bbox[3] <= tail.bbox[1] + _EDGE_SLACK:
            return False

    return True


def _is_content(block: Block) -> bool:
    """Would this block, sitting in the gap, mean the first table had ended?

    Running headers, footers and page numbers would not: they are printed in the
    gap of *every* page break. Figures do not either -- a stamp or a logo beside
    a table says nothing about whether the table continues. Printed prose does.
    """
    if block.is_furniture or block.figure is not None:
        return False
    return bool(block.text.strip())


# -- fusing the grids -----------------------------------------------------
def _merge_group(group: list[Block]) -> Table:
    """Build one :class:`Table` out of a run of fragment blocks."""
    tables = [b.table for b in group if b.table is not None]
    head = tables[0]
    header = _dense_grid(head)[: _header_depth(head)]

    cells: list[Cell] = [replace(c) for c in head.cells]
    row_offset = head.n_rows
    seams: list[int] = []  # first merged-grid row of each continuation

    for table in tables[1:]:
        skip = _reprinted_header_depth(table, header)
        seams.append(row_offset)
        cells.extend(_shift(table.cells, skip, row_offset))
        row_offset += table.n_rows - skip

    n_cols = max(t.n_cols for t in tables)
    n_rows = max((c.row + c.row_span for c in cells), default=0)
    _restitch_rowspans(cells, seams, n_rows, n_cols)

    return Table(
        n_rows=n_rows,
        n_cols=n_cols,
        cells=cells,
        markdown=cells_to_markdown(cells, n_rows, n_cols),
        html=cells_to_html(cells, n_rows, n_cols),
        has_merged_cells=any(c.row_span > 1 or c.col_span > 1 for c in cells),
        n_fragments=len(group),
        pages=[b.page for b in group],
    )


def _shift(cells: list[Cell], skip: int, offset: int) -> list[Cell]:
    """Drop the first *skip* rows of a fragment and move the rest down to *offset*.

    A cell that starts inside the dropped rows but spans past them (a header cell
    merged into the body) is kept, clipped to what survives.
    """
    out: list[Cell] = []
    for cell in cells:
        end = cell.row + cell.row_span  # exclusive
        if end <= skip:
            continue  # entirely inside the reprinted header
        row = max(cell.row - skip, 0)
        out.append(
            replace(
                cell,
                row=row + offset,
                row_span=min(cell.row_span, end - skip),
                is_header=False,  # only the head fragment's header heads the table
            )
        )
    return out


def _header_depth(table: Table) -> int:
    """How many rows at the top of a table are its header.

    Counted from row 0 and stopping at the first row without a header cell: a
    stray ``<th>`` further down (the VL model emits them) marks a row, not a
    header block reaching that far.
    """
    header_rows = {c.row for c in table.cells if c.is_header}
    if 0 not in header_rows:
        return 1 if table.n_rows else 0  # no <th>: the first row is the header, by convention

    depth = 0
    while depth in header_rows:
        depth += 1
    return depth


def _reprinted_header_depth(table: Table, header: list[list[str]]) -> int:
    """How many of *table*'s leading rows are a reprint of *header*."""
    rows = _dense_grid(table)
    depth = 0
    while depth < len(header) and depth < len(rows) and _same_row(rows[depth], header[depth]):
        depth += 1
    return depth


def _same_row(a: list[str], b: list[str]) -> bool:
    if len(a) != len(b) or not a:
        return False
    matches = sum(1 for x, y in zip(a, b) if x.strip().lower() == y.strip().lower())
    return matches >= max(1, len(a) * _HEADER_MATCH)


def _restitch_rowspans(cells: list[Cell], seams: list[int], n_rows: int, n_cols: int) -> None:
    """Repair the vertical merges the page break cut in half.

    When a cell spanning many rows ("Lot n° 6", an arrondissement, a chapter)
    reaches the bottom of the page, the fragment on the next page carries only
    the rows -- the cell is gone, and those rows come back with a hole where
    their category should be. Every row of the merged table would then be
    self-describing except the ones that happen to follow a page break, which is
    exactly the property this parser sells.

    So at each seam, and only there, a hole is filled from the value directly
    above it, downwards until the column speaks for itself again. Two guards keep
    this from inventing data:

    * the column must carry a rowspan somewhere in the merged table -- evidence
      that it is a column where values *do* govern several rows, rather than one
      that is simply blank in places;
    * only the run of holes starting at the seam is filled. A gap further down
      the page was printed blank, and stays blank.

    Modifies *cells* in place.
    """
    if not seams:
        return

    grid = _lay_out(cells, n_rows, n_cols)
    owner = _owners(cells, n_rows, n_cols)
    spanning = {c.col for c in cells if c.row_span > 1 and c.col_span == 1}

    for seam in seams:
        if seam == 0 or seam >= n_rows:
            continue
        for col in spanning:
            above = grid[seam - 1][col].strip()
            if not above or grid[seam][col].strip():
                continue  # nothing to carry down, or the hole is not there

            row = seam
            while row < n_rows and not grid[row][col].strip():
                cell = owner.get((row, col))
                if cell is None:
                    cells.append(Cell(row=row, col=col, row_span=1, col_span=1, text=above))
                elif cell.row_span == 1 and cell.col_span == 1:
                    cell.text = above
                else:
                    break  # a merged empty cell: filling it would speak for other columns too
                grid[row][col] = above
                row += 1


def _dense_grid(table: Table) -> list[list[str]]:
    return _lay_out(table.cells, table.n_rows, table.n_cols)


def _lay_out(cells: list[Cell], n_rows: int, n_cols: int) -> list[list[str]]:
    """The grid as text, with every merged value repeated across the cells it covers."""
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for cell in cells:
        for r in range(cell.row, min(cell.row + cell.row_span, n_rows)):
            for c in range(cell.col, min(cell.col + cell.col_span, n_cols)):
                grid[r][c] = cell.text
    return grid


def _owners(cells: list[Cell], n_rows: int, n_cols: int) -> dict[tuple[int, int], Cell]:
    """Map each grid position to the cell that covers it."""
    owner: dict[tuple[int, int], Cell] = {}
    for cell in cells:
        for r in range(cell.row, min(cell.row + cell.row_span, n_rows)):
            for c in range(cell.col, min(cell.col + cell.col_span, n_cols)):
                owner[(r, c)] = cell
    return owner
