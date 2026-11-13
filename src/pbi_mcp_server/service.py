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
    create_measure_declaration,
    delete_measure_declaration,
    replace_exact_references,
    replace_measure_declaration,
    replace_object_declaration,
    restore_backup,
    update_measure_description,
    update_measure_expression,
    upsert_relationship_declaration,
)
from .engine_b.parser import (
    json_files,
    list_pages,
    list_visuals,
    query_json,
    resolve_visual,
    update_visual_fields as update_vis_fields,
)
from .errors import (
    AmbiguousObjectError,
    InvalidArgumentError,
    ObjectNotFoundError,
)
from .integrity.references import find_references as find_refs
from .integrity.validator import has_validation_errors, validate
from .lineage import analyze_object_impact
from .mutation import (
    PendingFileEdit,
    build_mutation_diff,
    execute_mutation_pipeline,
    verify_expression_precondition,
)
from .tmdl_sql import (
    discover_partition_metadata,
    discover_tmdl_sql_sources,
    get_table_sql as extract_table_sql,
    update_table_sql as mutate_table_sql,
)
from .workflows import WorkflowOrchestrator, build_workflow_guidance
from .workspace import discover_workspace, relative


class PbipService:
    def __init__(
        self,
        workspace: Path,
        create_backups: bool = True,
    ) -> None:
        self.info = discover_workspace(workspace)
        self.create_backups = create_backups

    @property
    def workflows(self) -> WorkflowOrchestrator:
        return WorkflowOrchestrator(self)

    def _validate_or_rollback(
        self,
        modified_paths: list[Path],
        plan_dict: dict[str, Any],
    ) -> dict[str, Any]:
        if not modified_paths:
            return plan_dict
        errors = has_validation_errors(self.info)
        plan_dict["validation"] = [i.model_dump() for i in validate(self.info)]
        if errors:
            for path in modified_paths:
                restore_backup(path)
            plan_dict["rolled_back"] = True
            plan_dict["status"] = "rolled_back"
            plan_dict["error_code"] = "ROLLBACK_COMPLETED"
            for err in errors:
                plan_dict.setdefault("warnings", []).append(
                    f"Post-write validation failed ({err.code}): {err.message} — changes rolled back."
                )
        else:
            plan_dict["status"] = "applied"
        return plan_dict

    def summary(self) -> dict[str, Any]:
        objects = scan_model(
            self.info.semantic_model_dir,
            self.info.root,
            workspace_id=self.info.workspace_id,
        )
        bpa = check_model_bpa(self.info)

        return {
            "workspace_id": self.info.workspace_id,
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
                "partitions": sum(x.kind == "partition" for x in objects),
                "calculation_groups": sum(x.kind == "calculation_group" for x in objects),
                "calculation_items": sum(x.kind == "calculation_item" for x in objects),
                "roles": sum(x.kind == "role" for x in objects),
                "pages": len(list_pages(self.info.report_dir, self.info.root, workspace_id=self.info.workspace_id)),
                "visuals": len(list_visuals(self.info.report_dir, self.info.root, workspace_id=self.info.workspace_id)),
            },
            "health_summary": bpa.get("health_summary"),
        }

    def objects(self, kind: str | None = None) -> list[dict[str, Any]]:
        items = scan_model(
            self.info.semantic_model_dir,
            self.info.root,
            workspace_id=self.info.workspace_id,
        )
        return [item.model_dump() for item in items if kind is None or item.kind == kind]

    def list_tables(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        objects = scan_model(
            self.info.semantic_model_dir,
            self.info.root,
            workspace_id=self.info.workspace_id,
        )
        sql_tables = (
            {s.table for s in discover_tmdl_sql_sources(self.info.semantic_model_dir)}
            if self.info.semantic_model_dir
            else set()
        )
        partition_meta = (
            discover_partition_metadata(self.info.semantic_model_dir, self.info.root)
            if self.info.semantic_model_dir
            else []
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
            tbl_parts = [p for p in partition_meta if str(p.get("table", "")).casefold() == tname_cf]
            result.append({
                "name": tbl.name,
                "object_id": tbl.object_id,
                "qualified_name": tbl.qualified_name,
                "file": tbl.file,
                "line": tbl.line,
                "column_count": col_count,
                "measure_count": meas_count,
                "partition_count": len(tbl_parts),
                "has_native_sql": tbl.name in sql_tables,
            })
        if offset > 0:
            result = result[offset:]
        if limit is not None and limit >= 0:
            result = result[:limit]
        return result

    def get_table(self, table_name: str) -> dict[str, Any] | None:
        schema = get_table_schema(
            self.info.semantic_model_dir,
            self.info.root,
            table_name,
            workspace_id=self.info.workspace_id,
        )
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
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        measures = [
            x
            for x in scan_model(
                self.info.semantic_model_dir,
                self.info.root,
                workspace_id=self.info.workspace_id,
            )
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
                    m.qualified_name or "",
                    m.object_id or "",
                    m.expression or "",
                    m.description or "",
                ]).casefold()
                if q_cf in haystack or (tokens and all(tok in haystack for tok in tokens)):
                    filtered.append(m)
            measures = filtered
        if offset > 0:
            measures = measures[offset:]
        if limit is not None and limit >= 0:
            measures = measures[:limit]
        return [m.model_dump() for m in measures]

    def resolve_measure(self, measure_name: str, table: str | None = None) -> dict[str, Any]:
        """Resolve a measure unambiguously by name or qualified `Table[Measure]`, raising structured errors."""
        cleaned = measure_name.strip()
        explicit_table = table
        if not explicit_table and "[" in cleaned and cleaned.endswith("]"):
            tbl_part, rest = cleaned.split("[", 1)
            explicit_table = tbl_part.strip("'\" ")
            cleaned = rest[:-1].strip()

        candidates = [
            m
            for m in self.search_measures(table=explicit_table)
            if m["name"].casefold() == cleaned.casefold()
            or (m.get("object_id") or "").casefold() == measure_name.strip().casefold()
            or (m.get("qualified_name") or "").casefold() == measure_name.strip().casefold()
        ]
        if not candidates:
            raise ObjectNotFoundError(
                f"Measure {measure_name!r} was not found"
                + (f" in table {explicit_table!r}." if explicit_table else ".")
            )
        if len(candidates) > 1:
            raise AmbiguousObjectError(
                f"Multiple measures named {cleaned!r}; specify table: "
                + ", ".join(f"{c.get('table')}[{c['name']}]" for c in candidates),
                candidates=[
                    {"name": c["name"], "table": c.get("table"), "file": c.get("file"), "object_id": c.get("object_id")}
                    for c in candidates
                ],
            )
        return candidates[0]

    def get_table_sql(
        self,
        table_name: str | None = None,
        partition: str | None = None,
    ) -> dict[str, Any]:
        if not self.info.semantic_model_dir:
            raise ObjectNotFoundError("Semantic model directory was not found.")
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
            raise ObjectNotFoundError("Semantic model directory was not found.")

        # Pre-read file before mutation so we can produce a unified diff
        sql_before = extract_table_sql(
            self.info.semantic_model_dir,
            table=table_name,
            partition=partition,
        )
        target_path = (self.info.semantic_model_dir / sql_before["file"]).resolve()
        original_file_text = target_path.read_text(encoding="utf-8-sig")

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

        # Compute unified diff and impact summary
        rel_file = relative(target_path, self.info.root)
        impact_info = self.impact(sql_before["table"])
        plan_dict["target_object"] = f"{sql_before['table']}.{sql_before['partition']}"
        plan_dict["changed_files"] = [rel_file] if plan_dict.get("replacement_count", 0) > 0 else []
        plan_dict["impact_summary"] = impact_info.get("counts")

        if plan_dict.get("replacement_count", 0) > 0:
            if not dry_run:
                updated_file_text = target_path.read_text(encoding="utf-8-sig")
            else:
                updated_file_text = original_file_text.replace(
                    sql_before["sql"],
                    plan_dict.get("updated_sql", sql_before["sql"]),
                    1,
                )
            diff_obj = build_mutation_diff(rel_file, original_file_text, updated_file_text)
            plan_dict["diffs"] = [diff_obj.model_dump()]
            plan_dict["status"] = "dry_run" if dry_run else "applied"
        else:
            plan_dict["diffs"] = []
            plan_dict["status"] = "no_op"

        if not dry_run and plan_dict.get("replacement_count", 0) > 0:
            modified = [
                (self.info.semantic_model_dir / ch["file"]).resolve()
                for ch in plan_dict.get("changes", [])
                if "file" in ch
            ]
            plan_dict = self._validate_or_rollback(modified, plan_dict)

        plan_dict["workflow"] = build_workflow_guidance(
            "sql_inspection_and_replacement",
            current_stage="dry_run_preview" if dry_run else "apply_mutation",
            dry_run=dry_run,
            rolled_back=bool(plan_dict.get("rolled_back")),
            target_object=plan_dict.get("target_object"),
        )
        return plan_dict

    def pages(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        pages = list_pages(
            self.info.report_dir,
            self.info.root,
            limit=limit,
            offset=offset,
            workspace_id=self.info.workspace_id,
        )
        for page in pages:
            file = page.get("file")
            if file:
                page["absolute_file"] = str((self.info.root / file).resolve())
        return pages

    def visuals(
        self,
        page: str | None = None,
        query: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        visuals = [
            item.model_dump()
            for item in list_visuals(
                self.info.report_dir,
                self.info.root,
                page=page,
                query=query,
                limit=limit,
                offset=offset,
                workspace_id=self.info.workspace_id,
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
            workspace_id=self.info.workspace_id,
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
        expected_replacements: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        measures = [
            x
            for x in scan_model(
                self.info.semantic_model_dir,
                self.info.root,
                workspace_id=self.info.workspace_id,
            )
            if x.kind == "measure"
        ]
        known_measures: set[str] = set()
        for m in measures:
            known_measures.add(m.name)
            if m.table:
                known_measures.add(f"{m.table}.{m.name}")

        target_vis = resolve_visual(
            self.info.report_dir,
            self.info.root,
            visual_id_or_title,
            page=page,
            workspace_id=self.info.workspace_id,
        )
        if not target_vis:
            raise ObjectNotFoundError(f"Visual {visual_id_or_title!r} was not found in report.")

        target_path = (self.info.root / target_vis.file).resolve()
        original_text = target_path.read_text(encoding="utf-8-sig")

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

        actual_replacements = int(plan_dict.get("replacement_count", 0))
        if expected_replacements is not None and actual_replacements != expected_replacements:
            if not dry_run and plan_dict.get("changes"):
                restore_backup(target_path)
            from .mutation import verify_replacements_precondition
            verify_replacements_precondition(
                operation=plan_dict.get("operation", "update_visual_fields"),
                actual_replacements=actual_replacements,
                expected_replacements=expected_replacements,
            )

        if not dry_run and plan_dict.get("changes"):
            updated_text = target_path.read_text(encoding="utf-8-sig")
            diff_obj = build_mutation_diff(target_vis.file, original_text, updated_text)
            plan_dict["diffs"] = [diff_obj.model_dump()]
            modified = [
                (self.info.root / ch["file"]).resolve()
                for ch in plan_dict["changes"]
                if "file" in ch
            ]
            plan_dict = self._validate_or_rollback(modified, plan_dict)

        plan_dict["workflow"] = build_workflow_guidance(
            "visual_field_binding_update",
            current_stage="dry_run_preview" if dry_run else "apply_mutation",
            dry_run=dry_run,
            rolled_back=bool(plan_dict.get("rolled_back")),
            target_object=target_vis.object_id or target_vis.visual_id,
        )
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
            raise InvalidArgumentError(
                f"Report file must be inside the selected PBIP workspace.\n"
                f"Workspace: {workspace_root}\nRequested file: {path}"
            ) from exc

        if not path.exists():
            raise ObjectNotFoundError(
                f"Report file does not exist in the selected PBIP workspace: {path}"
            )
        if not path.is_file():
            raise InvalidArgumentError(f"Report path is not a file: {path}")
        if path.suffix.lower() != ".json":
            raise InvalidArgumentError(f"query_report only supports JSON files. Received: {path}")

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
        return analyze_object_impact(self.info, term)

    def rename_measure(
        self,
        old_name: str,
        new_name: str,
        *,
        table: str | None = None,
        expected_replacements: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        # Check for ambiguous measures if multiple tables define old_name and no table was supplied
        matches = [
            m
            for m in self.search_measures(table=table)
            if m["name"].casefold() == old_name.strip().casefold()
        ]
        if len(matches) > 1 and not table:
            raise AmbiguousObjectError(
                f"Multiple measures named {old_name!r}; specify table: "
                + ", ".join(f"{m.get('table')}[{m['name']}]" for m in matches),
                candidates=[
                    {"name": m["name"], "table": m.get("table"), "file": m.get("file"), "object_id": m.get("object_id")}
                    for m in matches
                ],
            )
        target_obj_id = matches[0].get("object_id") if len(matches) == 1 else old_name
        impact_info = self.impact(old_name)

        edits: list[PendingFileEdit] = []
        declaration_count = 0

        for path in tmdl_files(self.info.semantic_model_dir):
            original = path.read_text(encoding="utf-8-sig")
            in_target_table = True
            if table:
                file_objs = parse_file(path, self.info.root, workspace_id=self.info.workspace_id)
                in_target_table = any(
                    o.kind == "table" and o.name.casefold() == table.casefold() for o in file_objs
                )
            if in_target_table:
                changed, declarations = replace_measure_declaration(original, old_name, new_name)
                declaration_count += declarations
            else:
                changed, declarations = original, 0

            changed_final, refs = replace_exact_references(changed, old_name, new_name)
            if declarations:
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed_final,
                        replacements=declarations,
                        reason="measure declaration",
                    )
                )
            if refs:
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed_final,
                        replacements=refs,
                        reason="TMDL references",
                    )
                )

        for path in json_files(self.info.report_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, count = replace_exact_references(original, old_name, new_name)
            if count:
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed,
                        replacements=count,
                        reason="report JSON references",
                    )
                )

        warnings: list[str] = []
        if declaration_count != 1:
            warnings.append(
                f"Expected exactly one measure declaration; found {declaration_count}. "
                "No guarantee of a safe rename."
            )

        res = execute_mutation_pipeline(
            self.info,
            operation=f"rename measure {old_name!r} to {new_name!r}",
            target_object=target_obj_id,
            edits=edits,
            dry_run=dry_run,
            create_backups=self.create_backups,
            expected_replacements=expected_replacements,
            impact_summary=impact_info.get("counts"),
            warnings=warnings,
        )
        res["workflow"] = build_workflow_guidance(
            "cross_layer_measure_rename",
            current_stage="dry_run_preview" if dry_run else "apply_mutation",
            dry_run=dry_run,
            rolled_back=bool(res.get("rolled_back")),
            target_object=target_obj_id,
        )
        return res

    def update_dax(
        self,
        measure_name: str,
        expression: str,
        table: str | None = None,
        *,
        create_if_missing: bool = False,
        format_string: str | None = None,
        display_folder: str | None = None,
        description: str | None = None,
        expected_current_expression: str | None = None,
        expected_replacements: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        # Check for ambiguous measure across tables if no table filter is provided
        matches = [
            m
            for m in self.search_measures(table=table)
            if m["name"].casefold() == measure_name.strip().casefold()
        ]
        if len(matches) > 1:
            raise AmbiguousObjectError(
                f"Multiple measures named {measure_name!r}; specify table: "
                + ", ".join(f"{m.get('table')}[{m['name']}]" for m in matches),
                candidates=[
                    {"name": m["name"], "table": m.get("table"), "file": m.get("file"), "object_id": m.get("object_id")}
                    for m in matches
                ],
            )
        if len(matches) == 1 and expected_current_expression is not None:
            verify_expression_precondition(
                object_name=measure_name,
                actual_expression=matches[0].get("expression"),
                expected_expression=expected_current_expression,
            )
        elif not matches and expected_current_expression is not None:
            raise ObjectNotFoundError(f"Measure {measure_name!r} was not found.")

        target_obj_id = matches[0].get("object_id") if len(matches) == 1 else (
            f"{self.info.workspace_id}::measure::{table}[{measure_name}]" if table else measure_name
        )
        impact_info = self.impact(measure_name)

        edits: list[PendingFileEdit] = []
        count = 0

        for path in tmdl_files(self.info.semantic_model_dir):
            if table:
                file_objs = parse_file(path, self.info.root, workspace_id=self.info.workspace_id)
                if not any(
                    o.kind == "table" and o.name.casefold() == table.casefold() for o in file_objs
                ):
                    continue
            original = path.read_text(encoding="utf-8-sig")
            changed, m_count = update_measure_expression(
                original,
                measure_name,
                expression,
                format_string=format_string,
                display_folder=display_folder,
            )
            if m_count and description is not None:
                changed, _ = update_measure_description(changed, measure_name, description)
            if m_count:
                count += m_count
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed,
                        replacements=m_count,
                        reason="measure expression",
                    )
                )

        if count == 0 and create_if_missing:
            if not table:
                raise InvalidArgumentError(
                    f"table is required when creating new measure {measure_name!r} with create_if_missing=True."
                )
            for path in tmdl_files(self.info.semantic_model_dir):
                file_objs = parse_file(path, self.info.root, workspace_id=self.info.workspace_id)
                if any(o.kind == "table" and o.name.casefold() == table.casefold() for o in file_objs):
                    original = path.read_text(encoding="utf-8-sig")
                    changed, c_count = create_measure_declaration(
                        original,
                        measure_name,
                        expression,
                        format_string=format_string,
                        display_folder=display_folder,
                        description=description,
                    )
                    count += c_count
                    edits.append(
                        PendingFileEdit(
                            path=path,
                            original_text=original,
                            updated_text=changed,
                            replacements=c_count,
                            reason="create measure declaration",
                        )
                    )
                    break
            if count == 0:
                raise ObjectNotFoundError(f"Table {table!r} was not found to create measure {measure_name!r}.")

        warnings: list[str] = []
        if count != 1:
            warnings.append(f"Expected exactly one measure declaration; found {count}.")

        op_label = (
            f"create measure {measure_name!r} in {table!r}"
            if (not matches and create_if_missing)
            else f"update DAX for {measure_name!r}"
        )
        res = execute_mutation_pipeline(
            self.info,
            operation=op_label,
            target_object=target_obj_id,
            edits=edits,
            dry_run=dry_run,
            create_backups=self.create_backups,
            expected_replacements=expected_replacements,
            impact_summary=impact_info.get("counts"),
            warnings=warnings,
        )
        res["workflow"] = build_workflow_guidance(
            "measure_lookup_and_update",
            current_stage="dry_run_preview" if dry_run else "apply_mutation",
            dry_run=dry_run,
            rolled_back=bool(res.get("rolled_back")),
            target_object=target_obj_id,
        )
        return res

    def upsert_relationship(
        self,
        relationship_name: str,
        *,
        from_column: str | None = None,
        to_column: str | None = None,
        cross_filtering: str | None = None,
        is_active: bool | None = None,
        from_cardinality: str | None = None,
        to_cardinality: str | None = None,
        create_if_missing: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Update an existing relationship or create a new one in relationships.tmdl."""
        if not self.info.semantic_model_dir:
            raise ObjectNotFoundError("Semantic model directory was not found.")
        rel_path = self.info.semantic_model_dir / "definition" / "relationships.tmdl"
        if not rel_path.exists():
            if not create_if_missing:
                raise ObjectNotFoundError("relationships.tmdl does not exist in semantic model.")
            rel_path.parent.mkdir(parents=True, exist_ok=True)
            original = ""
        else:
            original = rel_path.read_text(encoding="utf-8-sig")

        changed, count = upsert_relationship_declaration(
            original,
            relationship_name,
            from_column=from_column,
            to_column=to_column,
            cross_filtering=cross_filtering,
            is_active=is_active,
            from_cardinality=from_cardinality,
            to_cardinality=to_cardinality,
            create_if_missing=create_if_missing,
        )
        if count == 0:
            raise ObjectNotFoundError(f"Relationship {relationship_name!r} was not found.")

        edits = [
            PendingFileEdit(
                path=rel_path,
                original_text=original,
                updated_text=changed,
                replacements=count,
                reason="upsert relationship declaration",
            )
        ]
        return execute_mutation_pipeline(
            self.info,
            operation=f"upsert relationship {relationship_name!r}",
            target_object=f"{self.info.workspace_id}::relationship::{relationship_name}",
            edits=edits,
            dry_run=dry_run,
            create_backups=self.create_backups,
        )

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
            raise InvalidArgumentError("Provide measure_name (to format in-place) or dax_expression.")

        target = self.resolve_measure(measure_name, table=table)
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
        target = self.resolve_measure(measure_name, table=table)
        impact_info = self.impact(target["name"])
        counts = impact_info.get("counts", {})
        docstring = description or generate_measure_docstring(
            target["name"],
            target.get("table"),
            target.get("expression") or "",
            dependent_visual_count=int(counts.get("affected_visual_objects", 0)),
            dependent_measure_count=int(counts.get("dependent_measures", 0)),
        )

        edits: list[PendingFileEdit] = []
        for path in tmdl_files(self.info.semantic_model_dir):
            if target.get("table"):
                file_objs = parse_file(path, self.info.root, workspace_id=self.info.workspace_id)
                if not any(
                    o.kind == "table" and o.name.casefold() == str(target["table"]).casefold()
                    for o in file_objs
                ):
                    continue
            original = path.read_text(encoding="utf-8-sig")
            changed, n = update_measure_description(original, target["name"], docstring)
            if n:
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed,
                        replacements=n,
                        reason="measure /// description",
                    )
                )

        res = execute_mutation_pipeline(
            self.info,
            operation=f"document measure {target['name']!r}",
            target_object=target.get("object_id") or target["name"],
            edits=edits,
            dry_run=dry_run,
            create_backups=self.create_backups,
            impact_summary=counts,
        )
        res["measure"] = target["name"]
        res["table"] = target.get("table")
        res["description"] = docstring
        return res

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
            raise AmbiguousObjectError(
                f"Multiple {kind} objects named {name!r}; specify table.",
                candidates=[
                    {"name": m["name"], "table": m.get("table"), "file": m.get("file"), "object_id": m.get("object_id")}
                    for m in matches
                ],
            )
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
        edits: list[PendingFileEdit] = []
        declarations = 0
        for path in tmdl_files(self.info.semantic_model_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, count = replace_object_declaration(original, kind, old_name, new_name)
            declarations += count
            changed_final, refs = replace_exact_references(changed, old_name, new_name)
            if count:
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed_final,
                        replacements=count,
                        reason=f"{kind} declaration",
                    )
                )
            if refs:
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed_final,
                        replacements=refs,
                        reason="TMDL references",
                    )
                )
        for path in json_files(self.info.report_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, count = replace_exact_references(original, old_name, new_name)
            if count:
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed,
                        replacements=count,
                        reason="report JSON references",
                    )
                )
        warnings: list[str] = []
        if declarations != 1:
            warnings.append(f"Expected exactly one {kind} declaration; found {declarations}.")
        return execute_mutation_pipeline(
            self.info,
            operation=f"rename {kind} {old_name!r} to {new_name!r}",
            target_object=old_name,
            edits=edits,
            dry_run=dry_run,
            create_backups=self.create_backups,
            warnings=warnings,
        )

    def delete_measure(self, name: str, dry_run: bool = True) -> dict[str, Any]:
        impact = self.impact(name)
        warnings: list[str] = []
        if impact["counts"]["total"] > 1:
            warnings.append(
                f"Measure has {impact['counts']['total']} textual references; deletion may leave stale dependencies."
            )
        count = 0
        edits: list[PendingFileEdit] = []
        for path in tmdl_files(self.info.semantic_model_dir):
            original = path.read_text(encoding="utf-8-sig")
            changed, n = delete_measure_declaration(original, name)
            count += n
            if n:
                edits.append(
                    PendingFileEdit(
                        path=path,
                        original_text=original,
                        updated_text=changed,
                        replacements=n,
                        reason="delete measure declaration",
                    )
                )
        if count != 1:
            warnings.append(f"Expected exactly one measure declaration; found {count}.")
        return execute_mutation_pipeline(
            self.info,
            operation=f"delete measure {name!r}",
            target_object=name,
            edits=edits,
            dry_run=dry_run,
            create_backups=self.create_backups,
            impact_summary=impact.get("counts"),
            warnings=warnings,
        )

    def validation(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in validate(self.info)]
