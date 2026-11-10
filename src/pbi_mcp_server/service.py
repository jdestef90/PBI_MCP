from __future__ import annotations

from pathlib import Path
from typing import Any

from .analyst import (
    check_model_bpa,
    find_unused_objects,
    format_dax_string,
    generate_measure_docstring,
)
from .engine_a.parser import get_table_schema, parse_file, scan_model, tmdl_files
from .engine_a.writer import (
    atomic_write,
    delete_measure_declaration,
    replace_exact_references,
    replace_measure_declaration,
    replace_object_declaration,
    restore_backup,
    update_measure_description,
    update_measure_expression,
)
from .engine_b.parser import (
    json_files,
    list_pages,
    list_visuals,
    query_json,
    resolve_visual,
    update_visual_fields as update_vis_fields,
)
from .integrity.references import find_references as find_refs
from .integrity.validator import has_validation_errors, validate
from .models import ChangePlan, FileChange
from .tmdl_sql import (
    discover_tmdl_sql_sources,
    get_table_sql as extract_table_sql,
    update_table_sql as mutate_table_sql,
)
from .workspace import discover_workspace, relative


class PbipService:
    def __init__(
        self,
        workspace: Path,
        create_backups: bool = True,
    ) -> None:
        self.info = discover_workspace(workspace)
        self.create_backups = create_backups

    def _validate_or_rollback(
        self,
        modified_paths: list[Path],
        plan_dict: dict[str, Any],
    ) -> dict[str, Any]:
        if not modified_paths:
            return plan_dict
        errors = has_validation_errors(self.info)
        if errors:
            for path in modified_paths:
                restore_backup(path)
            plan_dict["rolled_back"] = True
            for err in errors:
                plan_dict.setdefault("warnings", []).append(
                    f"Post-write validation failed ({err.code}): {err.message} — changes rolled back."
                )
        return plan_dict

    def summary(self) -> dict[str, Any]:
        objects = scan_model(
            self.info.semantic_model_dir,
            self.info.root,
        )

        return {
            "root": str(self.info.root),
            "pbip_file": str(self.info.pbip_file) if self.info.pbip_file else None,
            "semantic_model_dir": (
                str(self.info.semantic_model_dir) if self.info.semantic_model_dir else None
            ),
            "report_dir": str(self.info.report_dir) if self.info.report_dir else None,
            "counts": {
                "tables": sum(x.kind == "table" for x in objects),
                "columns": sum(x.kind in {"column", "calculated_column"} for x in objects),
                "measures": sum(x.kind == "measure" for x in objects),
                "relationships": sum(x.kind == "relationship" for x in objects),
                "pages": len(list_pages(self.info.report_dir, self.info.root)),
                "visuals": len(list_visuals(self.info.report_dir, self.info.root)),
            },
        }

    def objects(self, kind: str | None = None) -> list[dict[str, Any]]:
        items = scan_model(self.info.semantic_model_dir, self.info.root)
        return [item.model_dump() for item in items if kind is None or item.kind == kind]

    def list_tables(self) -> list[dict[str, Any]]:
        objects = scan_model(self.info.semantic_model_dir, self.info.root)
        sql_tables = (
            {s.table for s in discover_tmdl_sql_sources(self.info.semantic_model_dir)}
            if self.info.semantic_model_dir
            else set()
        )
        tables = [x for x in objects if x.kind == "table"]
        result: list[dict[str, Any]] = []
        for tbl in tables:
            tname_cf = tbl.name.casefold()
            col_count = sum(
                x.kind in {"column", "calculated_column"} and (x.table or "").casefold() == tname_cf
                for x in objects
            )
            meas_count = sum(
                x.kind == "measure" and (x.table or "").casefold() == tname_cf for x in objects
            )
            result.append({
                "name": tbl.name,
                "file": tbl.file,
                "line": tbl.line,
                "column_count": col_count,
                "measure_count": meas_count,
                "has_native_sql": tbl.name in sql_tables,
            })
        return result

    def get_table(self, table_name: str) -> dict[str, Any] | None:
        schema = get_table_schema(self.info.semantic_model_dir, self.info.root, table_name)
        if not schema:
            return None
        if self.info.semantic_model_dir:
            try:
                sql_info = extract_table_sql(
                    self.info.semantic_model_dir, table=schema["table"]
                )
                schema["sql_source"] = sql_info
            except (FileNotFoundError, ValueError):
                schema["sql_source"] = None
        return schema

    def search_measures(
        self,
        query: str | None = None,
        table: str | None = None,
    ) -> list[dict[str, Any]]:
        measures = [
            x
            for x in scan_model(self.info.semantic_model_dir, self.info.root)
            if x.kind == "measure"
        ]
        if table:
            t_cf = table.strip().casefold()
            measures = [m for m in measures if (m.table or "").casefold() == t_cf]
        if query:
            q_cf = query.strip().casefold()
            tokens = [tok for tok in q_cf.split() if tok]
            filtered = []
            for m in measures:
                haystack = " ".join([
                    m.name,
                    m.table or "",
                    m.expression or "",
                    m.description or "",
                ]).casefold()
                if q_cf in haystack or (tokens and all(tok in haystack for tok in tokens)):
                    filtered.append(m)
            measures = filtered
        return [m.model_dump() for m in measures]

    def get_table_sql(
        self,
        table_name: str | None = None,
        partition: str | None = None,
    ) -> dict[str, Any]:
        if not self.info.semantic_model_dir:
            raise FileNotFoundError("Semantic model directory was not found.")
        return extract_table_sql(
            self.info.semantic_model_dir,
            table=table_name,
            partition=partition,
        )

    def update_table_sql(
        self,
        old_text: str,
        new_text: str,
        *,
        table_name: str | None = None,
        partition: str | None = None,
        expected_replacements: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not self.info.semantic_model_dir:
            raise FileNotFoundError("Semantic model directory was not found.")
        plan_dict = mutate_table_sql(
            self.info.semantic_model_dir,
            old_text,
            new_text,
            table=table_name,
            partition=partition,
            expected_replacements=expected_replacements,
            dry_run=dry_run,
            create_backups=self.create_backups,
        )
        if not dry_run and plan_dict.get("replacement_count", 0) > 0:
            modified = [
                (self.info.semantic_model_dir / ch["file"]).resolve()
                for ch in plan_dict.get("changes", [])
                if "file" in ch
            ]
            return self._validate_or_rollback(modified, plan_dict)
        return plan_dict

    def pages(self) -> list[dict[str, Any]]:
        pages = list_pages(self.info.report_dir, self.info.root)
        for page in pages:
            file = page.get("file")
            if file:
                page["absolute_file"] = str((self.info.root / file).resolve())
        return pages

    def visuals(
        self,
        page: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        visuals = [
            item.model_dump()
            for item in list_visuals(
                self.info.report_dir,
                self.info.root,
                page=page,
                query=query,
            )
        ]
        for visual in visuals:
            file = visual.get("file")
            if file:
                visual["absolute_file"] = str((self.info.root / file).resolve())
        return visuals

    def get_visual(
        self,
        visual_id_or_title: str,
        page: str | None = None,
    ) -> dict[str, Any] | None:
        vis = resolve_visual(
            self.info.report_dir,
            self.info.root,
            visual_id_or_title,
            page=page,
        )
        if not vis:
            return None
        res = vis.model_dump()
        if res.get("file"):
            res["absolute_file"] = str((self.info.root / res["file"]).resolve())
        return res

    def update_visual_fields(
        self,
        visual_id_or_title: str,
        *,
        add_fields: list[dict[str, str] | str] | None = None,
        remove_fields: list[str] | None = None,
        page: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        measures = [
            x
            for x in scan_model(self.info.semantic_model_dir, self.info.root)
            if x.kind == "measure"
        ]
        known_measures: set[str] = set()
        for m in measures:
            known_measures.add(m.name)
            if m.table:
                known_measures.add(f"{m.table}.{m.name}")

        plan_dict = update_vis_fields(
            self.info.report_dir,
            self.info.root,
            visual_id_or_title,
            add_fields=add_fields,
            remove_fields=remove_fields,
            known_measures=known_measures,
            page=page,
            dry_run=dry_run,
            create_backups=self.create_backups,
        )
        if not dry_run and plan_dict.get("changes"):
            modified = [
                (self.info.root / ch["file"]).resolve()
                for ch in plan_dict["changes"]
                if "file" in ch
            ]
            return self._validate_or_rollback(modified, plan_dict)
        return plan_dict

    def query_report(
        self,
        relative_file: str,
        expression: str,
    ) -> Any:
        supplied_path = Path(relative_file)
        if supplied_path.is_absolute():
            path = supplied_path.resolve()
        else:
            path = (self.info.root / supplied_path).resolve()

        workspace_root = self.info.root.resolve()
        try:
            path.relative_to(workspace_root)
        except ValueError as exc:
            raise ValueError(
                f"Report file must be inside the selected PBIP workspace.\n"
                f"Workspace: {workspace_root}\nRequested file: {path}"
            ) from exc

        if not path.exists():
            raise FileNotFoundError(
                f"Report file does not exist in the selected PBIP workspace: {path}"
            )
        if not path.is_file():
            raise ValueError(f"Report path is not a file: {path}")
        if path.suffix.lower() != ".json":
            raise ValueError(f"query_report only supports JSON files. Received: {path}")

        return query_json(path, expression)

    def references(self, term: str) -> list[dict[str, object]]:
        results = find_refs(
            self.info.root,
            self.info.semantic_model_dir,
            self.info.report_dir,
            term,
        )
        for result in results:
            file = result.get("file")
            if isinstance(file, str):
                result["absolute_file"] = str((self.info.root / file).resolve())
        return results

    def impact(self, term: str) -> dict[str, Any]:
        refs = self.references(term)
        visuals = []
        tmdl = []
        for ref in refs:
            file = str(ref.get("file", ""))
            if file.lower().endswith(".json"):
                visuals.append(ref)
            elif file.lower().endswith(".tmdl"):
                tmdl.append(ref)
        affected_visuals = [
            v.model_dump()
            for v in list_visuals(self.info.report_dir, self.info.root, query=term)
        ]
        return {
            "term": term,
            "references": refs,
            "tmdl_references": tmdl,
            "visual_references": visuals,
            "affected_visuals": affected_visuals,
            "counts": {
                "total": len(refs),
                "tmdl": len(tmdl),
                "visual": len(visuals),
                "affected_visual_objects": len(affected_visuals),
            },
            "affected_visual_files": sorted({str(x["file"]) for x in visuals}),
        }

    def rename_measure(
        self,
        old_name: str,
        new_name: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        plan = ChangePlan(
            operation=f"rename measure {old_name!r} to {new_name!r}",
            dry_run=dry_run,
        )
        declaration_count = 0
        modified_paths: list[Path] = []

        for path in tmdl_files(self.info.semantic_model_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, declarations = replace_measure_declaration(original, old_name, new_name)
            declaration_count += declarations
            if declarations:
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=declarations,
                        reason="measure declaration",
                    )
                )
            changed, refs = replace_exact_references(changed, old_name, new_name)
            if refs:
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=refs,
                        reason="TMDL references",
                    )
                )
            if changed != original and not dry_run:
                atomic_write(path, changed, self.create_backups)
                modified_paths.append(path)

        for path in json_files(self.info.report_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, count = replace_exact_references(original, old_name, new_name)
            if count:
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=count,
                        reason="report JSON references",
                    )
                )
                if not dry_run:
                    atomic_write(path, changed, self.create_backups)
                    modified_paths.append(path)

        if declaration_count != 1:
            plan.warnings.append(
                f"Expected exactly one measure declaration; found {declaration_count}. "
                "No guarantee of a safe rename."
            )

        res = plan.model_dump()
        return self._validate_or_rollback(modified_paths, res) if not dry_run else res

    def update_dax(
        self,
        measure_name: str,
        expression: str,
        table: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        plan = ChangePlan(
            operation=f"update DAX for {measure_name!r}",
            dry_run=dry_run,
        )
        count = 0
        modified_paths: list[Path] = []

        for path in tmdl_files(self.info.semantic_model_dir):
            if table:
                file_objs = parse_file(path, self.info.root)
                if not any(
                    o.kind == "table" and o.name.casefold() == table.casefold() for o in file_objs
                ):
                    continue
            original = path.read_text(encoding="utf-8-sig")
            changed, matches = update_measure_expression(original, measure_name, expression)
            if matches:
                count += matches
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=matches,
                        reason="measure expression",
                    )
                )
                if not dry_run:
                    atomic_write(path, changed, self.create_backups)
                    modified_paths.append(path)

        if count != 1:
            plan.warnings.append(f"Expected exactly one measure declaration; found {count}.")

        res = plan.model_dump()
        return self._validate_or_rollback(modified_paths, res) if not dry_run else res

    def format_dax(
        self,
        *,
        measure_name: str | None = None,
        dax_expression: str | None = None,
        table: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if dax_expression and not measure_name:
            formatted = format_dax_string(dax_expression)
            return {
                "original_dax": dax_expression,
                "formatted_dax": formatted,
                "changed": formatted != dax_expression.strip(),
            }
        if not measure_name:
            raise ValueError("Provide measure_name (to format in-place) or dax_expression.")

        matches = [
            m
            for m in self.search_measures(table=table)
            if m["name"].casefold() == measure_name.casefold()
        ]
        if not matches:
            raise FileNotFoundError(f"Measure {measure_name!r} was not found.")
        if len(matches) > 1:
            raise ValueError(f"Multiple measures named {measure_name!r}; specify table.")

        target = matches[0]
        raw_expr = dax_expression or target.get("expression") or ""
        formatted = format_dax_string(raw_expr)
        plan_dict = self.update_dax(
            target["name"],
            formatted,
            table=target.get("table"),
            dry_run=dry_run,
        )
        plan_dict["original_dax"] = raw_expr
        plan_dict["formatted_dax"] = formatted
        return plan_dict

    def generate_measure_docs(
        self,
        measure_name: str,
        *,
        description: str | None = None,
        table: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        matches = [
            m
            for m in self.search_measures(table=table)
            if m["name"].casefold() == measure_name.casefold()
        ]
        if not matches:
            raise FileNotFoundError(f"Measure {measure_name!r} was not found.")
        if len(matches) > 1:
            raise ValueError(f"Multiple measures named {measure_name!r}; specify table.")

        target = matches[0]
        docstring = description or generate_measure_docstring(
            target["name"],
            target.get("table"),
            target.get("expression") or "",
        )
        plan = ChangePlan(
            operation=f"document measure {target['name']!r}",
            dry_run=dry_run,
        )
        modified_paths: list[Path] = []

        for path in tmdl_files(self.info.semantic_model_dir):
            if target.get("table"):
                file_objs = parse_file(path, self.info.root)
                if not any(
                    o.kind == "table" and o.name.casefold() == str(target["table"]).casefold()
                    for o in file_objs
                ):
                    continue
            original = path.read_text(encoding="utf-8-sig")
            changed, n = update_measure_description(original, target["name"], docstring)
            if n:
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=n,
                        reason="measure /// description",
                    )
                )
                if not dry_run and changed != original:
                    atomic_write(path, changed, self.create_backups)
                    modified_paths.append(path)

        res = plan.model_dump()
        res["measure"] = target["name"]
        res["table"] = target.get("table")
        res["description"] = docstring
        return self._validate_or_rollback(modified_paths, res) if not dry_run else res

    def find_unused_objects(self) -> dict[str, Any]:
        return find_unused_objects(self.info)

    def check_model_bpa(self) -> dict[str, Any]:
        return check_model_bpa(self.info)

    def get_object(
        self,
        kind: str,
        name: str,
        table: str | None = None,
    ) -> dict[str, Any] | None:
        matches = [
            x
            for x in self.objects(kind)
            if x["name"] == name and (table is None or x.get("table") == table)
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"Multiple {kind} objects named {name!r}; specify table.")
        return matches[0]

    def rename_column(
        self,
        old_name: str,
        new_name: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return self._rename_object("column", old_name, new_name, dry_run)

    def _rename_object(
        self,
        kind: str,
        old_name: str,
        new_name: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        plan = ChangePlan(
            operation=f"rename {kind} {old_name!r} to {new_name!r}",
            dry_run=dry_run,
        )
        declarations = 0
        modified_paths: list[Path] = []
        for path in tmdl_files(self.info.semantic_model_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, count = replace_object_declaration(original, kind, old_name, new_name)
            declarations += count
            changed, refs = replace_exact_references(changed, old_name, new_name)
            if count:
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=count,
                        reason=f"{kind} declaration",
                    )
                )
            if refs:
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=refs,
                        reason="TMDL references",
                    )
                )
            if changed != original and not dry_run:
                atomic_write(path, changed, self.create_backups)
                modified_paths.append(path)
        for path in json_files(self.info.report_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, count = replace_exact_references(original, old_name, new_name)
            if count:
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=count,
                        reason="report JSON references",
                    )
                )
                if not dry_run:
                    atomic_write(path, changed, self.create_backups)
                    modified_paths.append(path)
        if declarations != 1:
            plan.warnings.append(f"Expected exactly one {kind} declaration; found {declarations}.")
        res = plan.model_dump()
        return self._validate_or_rollback(modified_paths, res) if not dry_run else res

    def delete_measure(self, name: str, dry_run: bool = True) -> dict[str, Any]:
        impact = self.impact(name)
        plan = ChangePlan(operation=f"delete measure {name!r}", dry_run=dry_run)
        if impact["counts"]["total"] > 1:
            plan.warnings.append(
                f"Measure has {impact['counts']['total']} textual references; deletion may leave stale dependencies."
            )
        count = 0
        modified_paths: list[Path] = []
        for path in tmdl_files(self.info.semantic_model_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, n = delete_measure_declaration(original, name)
            count += n
            if n:
                plan.changes.append(
                    FileChange(
                        file=relative(path, self.info.root),
                        replacements=n,
                        reason="delete measure declaration",
                    )
                )
                if not dry_run:
                    atomic_write(path, changed, self.create_backups)
                    modified_paths.append(path)
        if count != 1:
            plan.warnings.append(f"Expected exactly one measure declaration; found {count}.")
        res = plan.model_dump()
        return self._validate_or_rollback(modified_paths, res) if not dry_run else res

    def validation(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in validate(self.info)]
