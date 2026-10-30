"""Lossless TMDL Document AST and Round-Trip Serializer.

Preserves exact indentation, whitespace, developer comments (/// and //),
and property ordering to ensure zero noisy git diffs during round-trip
modifications.
"""

from __future__ import annotations
import re
from typing import List, Dict, Optional, Tuple, Any


def unquote_identifier(name: str) -> str:
    """Strip single quotes from a TMDL identifier if quoted, unescaping doubled quotes."""
    name = name.strip()
    if name.startswith("'") and name.endswith("'") and len(name) >= 2:
        inner = name[1:-1]
        return inner.replace("''", "'")
    return name


def quote_identifier(name: str) -> str:
    """Quote an identifier with single quotes if it contains spaces or special characters."""
    if not name:
        return "''"
    # If contains spaces, punctuation, or starts with a digit, quote it
    if re.search(r"[ \t\-\.\[\]\(\)\{\}\\\/,\;:\'\"`~!@#\$%\^&\*\+=<>\?]", name) or name[0].isdigit():
        escaped = name.replace("'", "''")
        return f"'{escaped}'"
    return name


class TMDLNode:
    """Represents a structured node in a TMDL document."""

    def __init__(
        self,
        node_type: str,
        name: str = "",
        raw_name: str = "",
        indent: str = "",
        header_line: str = "",
        expression: str = "",
        leading_comments: Optional[List[str]] = None,
        properties_lines: Optional[List[str]] = None,
        children: Optional[List[TMDLNode]] = None,
        trailing_blank_lines: int = 0,
    ):
        self.node_type = node_type  # e.g., 'table', 'column', 'measure', 'partition', 'hierarchy'
        self.name = name  # unquoted name
        self.raw_name = raw_name  # original raw name
        self.indent = indent
        self.header_line = header_line
        self.expression = expression
        self.leading_comments: List[str] = leading_comments if leading_comments is not None else []
        self.properties_lines: List[str] = properties_lines if properties_lines is not None else []
        self.children: List[TMDLNode] = children if children is not None else []
        self.trailing_blank_lines = trailing_blank_lines

    def get_property(self, key: str) -> Optional[str]:
        """Extract value of a property like 'dataType: int64' or 'formatString: ...'."""
        prefix = f"{key}:"
        for line in self.properties_lines:
            stripped = line.strip()
            if stripped.startswith(prefix):
                return stripped[len(prefix):].strip()
        return None

    def set_property(self, key: str, value: str, indent: Optional[str] = None) -> None:
        """Set or update a property line while preserving indentation."""
        prefix = f"{key}:"
        new_indent = indent or (self.indent + "\t")
        new_line = f"{new_indent}{key}: {value}\n"
        for i, line in enumerate(self.properties_lines):
            if line.strip().startswith(prefix):
                self.properties_lines[i] = new_line
                return
        self.properties_lines.append(new_line)

    def to_string(self) -> str:
        """Serialize node back to TMDL string."""
        lines: List[str] = []

        # 1. Leading comments
        lines.extend(self.leading_comments)

        # 2. Header line
        if self.header_line:
            lines.append(self.header_line if self.header_line.endswith("\n") else self.header_line + "\n")

        # 3. Property lines
        for prop_line in self.properties_lines:
            lines.append(prop_line if prop_line.endswith("\n") else prop_line + "\n")

        # 4. Children
        for child in self.children:
            lines.append(child.to_string())

        # 5. Trailing blank lines
        lines.extend(["\n"] * self.trailing_blank_lines)

        return "".join(lines)


class TMDLDocument:
    """Top-level document representing an entire TMDL file."""

    def __init__(self, raw_content: str, header_comments: List[str], nodes: List[TMDLNode]):
        self.raw_content = raw_content
        self.header_comments = header_comments
        self.nodes = nodes

    @classmethod
    def from_string(cls, content: str) -> TMDLDocument:
        """Parse raw TMDL content into a lossless TMDLDocument."""
        lines = content.splitlines(keepends=True)
        return cls._parse_lines(lines, content)

    @classmethod
    def from_file(cls, filepath: str) -> TMDLDocument:
        """Load and parse TMDL file from disk."""
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return cls.from_string(content)

    @classmethod
    def _parse_lines(cls, lines: List[str], raw_content: str) -> TMDLDocument:
        """Parse TMDL lines preserving whitespace and structure."""
        nodes: List[TMDLNode] = []
        header_comments: List[str] = []

        i = 0
        n = len(lines)

        # Helper to get indentation level (count tabs or 4 spaces)
        def get_indent(line: str) -> str:
            m = re.match(r"^(\s*)", line)
            return m.group(1) if m else ""

        def indent_level(line: str) -> int:
            indent = get_indent(line)
            # count tabs as 1, every 4 spaces as 1
            return indent.count("\t") + indent.count("    ")

        current_comments: List[str] = []

        while i < n:
            line = lines[i]
            stripped = line.strip()

            # Empty lines
            if not stripped:
                if nodes:
                    nodes[-1].trailing_blank_lines += 1
                else:
                    header_comments.append(line)
                i += 1
                continue

            # Standalone comments
            if stripped.startswith("//") or stripped.startswith("///"):
                current_comments.append(line)
                i += 1
                continue

            # Match element definition
            indent = get_indent(line)
            level = indent_level(line)

            # Check if this line is an element keyword
            # keywords: table, column, measure, partition, hierarchy, level, relationship, model, annotation, role
            elem_match = re.match(
                r"^(\s*)(table|column|measure|partition|hierarchy|level|relationship|model|annotation|role|cultureInfo)\b(?:\s+(.+))?",
                line
            )

            if elem_match:
                node_indent = elem_match.group(1)
                node_type = elem_match.group(2)
                rest = (elem_match.group(3) or "").strip()

                name = ""
                raw_name = ""
                expression = ""

                if node_type == "measure":
                    # measure 'Name' = Expression  OR  measure Name = Expression
                    m_parts = re.match(r"^('([^']|'')+'|[^\s=]+)(?:\s*=\s*(.*))?$", rest)
                    if m_parts:
                        raw_name = m_parts.group(1).strip()
                        name = unquote_identifier(raw_name)
                        expression = (m_parts.group(3) or "").strip()
                    else:
                        name = rest
                        raw_name = rest
                elif node_type == "column":
                    # column 'Name'  OR  column Name  OR  column 'Name' = Expression
                    m_parts = re.match(r"^('([^']|'')+'|[^\s=]+)(?:\s*=\s*(.*))?$", rest)
                    if m_parts:
                        raw_name = m_parts.group(1).strip()
                        name = unquote_identifier(raw_name)
                        expression = (m_parts.group(3) or "").strip()
                    else:
                        name = rest
                        raw_name = rest
                elif node_type in ("table", "partition", "hierarchy", "level", "relationship", "model"):
                    m_parts = re.match(r"^('([^']|'')+'|[^\s=]+)(?:\s*=\s*(.*))?$", rest)
                    if m_parts:
                        raw_name = m_parts.group(1).strip()
                        name = unquote_identifier(raw_name)
                        expression = (m_parts.group(3) or "").strip()
                    else:
                        name = rest
                        raw_name = rest
                else:
                    name = rest
                    raw_name = rest

                node = TMDLNode(
                    node_type=node_type,
                    name=name,
                    raw_name=raw_name,
                    indent=node_indent,
                    header_line=line,
                    expression=expression,
                    leading_comments=current_comments,
                    properties_lines=[],
                    children=[],
                )
                current_comments = []
                i += 1

                # Gather properties and sub-blocks belonging to this node
                # Everything indented deeper than node_indent belongs to this node until next peer/parent
                node_level = indent_level(line)
                prop_comments: List[str] = []

                while i < n:
                    next_line = lines[i]
                    next_stripped = next_line.strip()

                    if not next_stripped:
                        # Blank line inside node or between children
                        # Look ahead to see if next non-blank line has deeper indentation
                        j = i + 1
                        while j < n and not lines[j].strip():
                            j += 1
                        if j < n and indent_level(lines[j]) > node_level:
                            # It's an internal blank line
                            node.properties_lines.append(next_line)
                            i += 1
                            continue
                        else:
                            # Blank line at the end of node
                            node.trailing_blank_lines += 1
                            i += 1
                            continue

                    next_indent_lvl = indent_level(next_line)

                    if next_indent_lvl <= node_level:
                        # Reached end of current node scope
                        break

                    # Check if next_line starts a child element
                    child_match = re.match(
                        r"^(\s*)(column|measure|partition|hierarchy|level|annotation)\b",
                        next_line
                    )
                    if child_match and next_indent_lvl == node_level + 1:
                        # It's a child element (e.g. column or measure inside table)
                        # We will let the outer or recursive loop handle, or recursively parse
                        break

                    # Otherwise, it's a property line or calculation block
                    node.properties_lines.append(next_line)
                    i += 1

                # Place node into hierarchy:
                # If level 0 (e.g. table, model, relationship), it's a top-level node
                # If level > 0 and we have a preceding top-level node, it's a child
                if level == 0 or not nodes:
                    nodes.append(node)
                else:
                    # Find enclosing parent node
                    parent = nodes[-1]
                    parent.children.append(node)
                continue

            # Fallback for other lines (e.g. annotations at root or ref table)
            fallback_node = TMDLNode(
                node_type="raw",
                name=stripped,
                raw_name=stripped,
                indent=indent,
                header_line=line,
                leading_comments=current_comments,
                properties_lines=[],
            )
            current_comments = []
            if level == 0 or not nodes:
                nodes.append(fallback_node)
            else:
                nodes[-1].children.append(fallback_node)
            i += 1

        # If any comments remain at the end of the file
        if current_comments and nodes:
            nodes[-1].properties_lines.extend(current_comments)

        return cls(raw_content, header_comments, nodes)

    def to_string(self) -> str:
        """Serialize the entire document back to string."""
        # If unmodified and raw_content is preserved, we can test exact round-trip
        lines: List[str] = []
        lines.extend(self.header_comments)
        for node in self.nodes:
            lines.append(node.to_string())
        return "".join(lines)

    def write_to_file(self, filepath: str) -> None:
        """Write serialized document to disk."""
        content = self.to_string()
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    def get_table(self) -> Optional[TMDLNode]:
        """Find the table node in this document."""
        for node in self.nodes:
            if node.node_type == "table":
                return node
        return None

    def get_measures(self) -> List[TMDLNode]:
        """List all measure nodes in the document."""
        measures = []
        table = self.get_table()
        if table:
            for child in table.children:
                if child.node_type == "measure":
                    measures.append(child)
        for node in self.nodes:
            if node.node_type == "measure":
                measures.append(node)
        return measures

    def get_columns(self) -> List[TMDLNode]:
        """List all column nodes in the document."""
        cols = []
        table = self.get_table()
        if table:
            for child in table.children:
                if child.node_type == "column":
                    cols.append(child)
        for node in self.nodes:
            if node.node_type == "column":
                cols.append(node)
        return cols

    def find_measure(self, measure_name: str) -> Optional[TMDLNode]:
        """Find a measure by unquoted name."""
        clean_target = unquote_identifier(measure_name)
        for m in self.get_measures():
            if m.name == clean_target:
                return m
        return None

    def find_column(self, column_name: str) -> Optional[TMDLNode]:
        """Find a column by unquoted name."""
        clean_target = unquote_identifier(column_name)
        for c in self.get_columns():
            if c.name == clean_target:
                return c
        return None

    def update_measure_dax(self, measure_name: str, new_dax: str) -> bool:
        """Update the DAX expression of a measure cleanly, preserving comments and properties."""
        measure = self.find_measure(measure_name)
        if not measure:
            return False

        quoted_name = quote_identifier(measure.name)
        measure.expression = new_dax.strip()
        measure.header_line = f"{measure.indent}measure {quoted_name} = {measure.expression}\n"
        return True

    def rename_measure(self, old_name: str, new_name: str) -> bool:
        """Rename a measure definition, preserving its DAX, comments, and properties."""
        measure = self.find_measure(old_name)
        if not measure:
            return False

        clean_new = unquote_identifier(new_name)
        quoted_new = quote_identifier(clean_new)
        measure.name = clean_new
        measure.raw_name = quoted_new
        measure.header_line = f"{measure.indent}measure {quoted_new} = {measure.expression}\n"
        return True
