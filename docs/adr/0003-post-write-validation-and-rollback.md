# Post-Write Validation and Automatic Rollback

To ensure that agent-driven modifications (DAX formula updates, partition SQL line modifications, visual field binding updates) never leave Power BI PBIP projects in an unusable or unparseable state, all mutations operate with a pre-write backup snapshot. When executed (`dry_run=False`), the server immediately runs post-write structural and syntax validation. If validation fails, the server automatically restores files from the backup snapshot and reports the validation error in the tool response.
